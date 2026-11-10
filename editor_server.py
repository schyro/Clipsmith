"""Local clip editor server (`run.cmd --review-gallery`).

Serves the editor from `editor/`, media from `Output` and `Review`, and a JSON
API that stages review decisions in `review-decisions.json`, saves caption
edits and runs one background job at a time (Apply, preview render, prepare:
proxy and missed-speech pass, re-transcribing a range).
The clip list is the source's plan: rendered clips come with their files, the
other candidates are triaged from the proxy before anything renders.
It listens on 127.0.0.1 only.
"""
from __future__ import annotations
import http.server
import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

import editor_ops
from gallery import (DECISIONS_FILE, REJECT_REASONS, _LOCK, _resolve_inside, apply_decision, apply_edit_decision,
                     check_overrides_against_shots,
                     clip_key, empty_decisions, load_decisions, parse_range, save_decisions, write_gallery)
TYPES = {'.mp4': 'video/mp4', '.jpg': 'image/jpeg', '.png': 'image/png', '.html': 'text/html; charset=utf-8',
         '.json': 'application/json; charset=utf-8', '.srt': 'text/plain; charset=utf-8',
         '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
         '.css': 'text/css; charset=utf-8', '.ttf': 'font/ttf', '.svg': 'image/svg+xml', '.txt': 'text/plain; charset=utf-8'}


def url_for(root: Path, path: Path, version=True):
    rel = Path(path).resolve().relative_to(Path(root).resolve())
    url = '/'+'/'.join(quote(p) for p in rel.parts)
    if version and Path(path).exists():
        url += f'?v={int(Path(path).stat().st_mtime)}'
    return url


class Job:
    """One background command at a time; its output lines are kept for the page."""

    def __init__(self):
        self.lock = threading.Lock()
        self.info = {'kind': None, 'state': 'idle', 'log': [], 'progress': None}
        self.seq = 0

    def busy(self, kind=None):
        with self.lock:
            return self.info['state'] == 'running' and (kind is None or self.info['kind'] == kind)

    def snapshot(self):
        with self.lock:
            return {**self.info, 'log': self.info['log'][-200:], 'seq': self.seq}

    def start(self, kind, command, clip=None, before=None, after=None):
        with self.lock:
            if self.info['state'] == 'running':
                raise RuntimeError(f'A {self.info["kind"]} job is already running.')
            self.seq += 1
            self.info = {'kind': kind, 'clip': clip, 'state': 'running', 'log': [], 'progress': None,
                         'started': time.time(), 'finished': None, 'error': None}
        threading.Thread(target=self._run, args=(command, before, after), daemon=True).start()

    def _line(self, line):
        with self.lock:
            if line.startswith('progress='):
                try:
                    self.info['progress'] = float(line.split('=', 1)[1])
                except ValueError:
                    pass
            elif not line.startswith('duration='):
                self.info['log'].append(line)

    def _run(self, command, before, after):
        error = None
        try:
            if before:
                before()
            env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       encoding='utf-8', errors='replace', env=env, cwd=str(Path(__file__).parent))
            with process.stdout:
                for line in process.stdout:
                    self._line(line.rstrip())
            if process.wait():
                error = f'The job failed (exit code {process.returncode}).'
        except Exception as exc:  # reported to the page
            error = str(exc)
        try:
            if after:
                after()
        except Exception as exc:
            error = error or f'Reloading after the job failed: {exc}'
        with self.lock:
            self.info.update(state='failed' if error else 'done', error=error, finished=time.time())
            if error:
                self.info['log'].append('ERROR: '+error)


def folder_source_id(output: Path):
    """The source id an output folder belongs to, from its plan or its decisions."""
    for name in ('plan.json', DECISIONS_FILE):
        path = Path(output)/name
        if path.exists():
            found = json.loads(path.read_text(encoding='utf-8-sig')).get('source_id')
            if found:
                return found
    raise ValueError(f'{output} is not an output folder of a plan (no plan.json or {DECISIONS_FILE}).')


def last_folder(root: Path, plan, config):
    """Where the editor opens a plan: the folder its last render went to, else the folder
    the next render will use (it may not exist yet when nothing is rendered)."""
    from shorts import output_folder
    last = Path(root)/'work'/plan['source_id']/'last-render.json'
    try:
        folder = Path(json.loads(last.read_text(encoding='utf-8'))['output'])
        if (folder/'clips.json').exists():
            return folder.resolve()
    except (OSError, ValueError, KeyError):
        pass
    return output_folder(plan, config).resolve()


def editor_target(root: Path, target='', source=None, config=None):
    """(output folder, source id) for `--review-gallery`: the folder given, the plan of
    `--input`, or else the plan worked on last (by its plan, render or decisions)."""
    from shorts import find_source, settings, source_id
    config = config or settings()
    if target:
        output = Path(target).resolve()
        return output, folder_source_id(output)
    plans = Path(root)/'Plans'
    if source is not None:
        wanted = source_id(Path(source).resolve())
        if not (plans/f'{wanted}.json').exists():
            raise ValueError(f'No plan for {Path(source).name} yet (Plans/{wanted}.json). Write the plan first.')
        paths = [plans/f'{wanted}.json']
    else:
        paths = sorted(plans.glob('*.json'))
    best = None
    for path in paths:
        try:
            plan = json.loads(path.read_text(encoding='utf-8-sig'))
            if source is None:
                find_source(plan['source_id'])  # only sources that are still in Raw
        except (OSError, ValueError, KeyError, TypeError):
            continue
        folder = last_folder(root, plan, config)
        marks = [path, folder/'clips.json', folder/DECISIONS_FILE]
        when = max(m.stat().st_mtime for m in marks if m.exists())
        if best is None or when > best[0]:
            best = (when, folder, plan['source_id'])
    if best is None:
        raise ValueError('No plan of a source in Raw was found. Write a plan under Plans/ first.')
    return best[1], best[2]


class State:
    """The plan and output folder being edited; reloaded after Apply changes files or the profile.
    Clips that the folder has rendered carry their files; the plan's other clips are
    triage candidates (`rendered: False`), shown from the proxy only."""

    def __init__(self, root: Path, output: Path, source_id=None, model=None):
        from shorts import find_source, probe, settings
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.config = settings()
        self.source_id = source_id or folder_source_id(output)
        self.cache = self.root/'work'/self.source_id
        self.model = editor_ops.source_model(self.cache, model)
        self.load(Path(output).resolve())
        self.source = find_source(self.source_id)
        self.duration = float(probe(self.source)['format']['duration'])
        self._words = self._transcript = None
        self._focus = {}

    def grid(self):
        from frame_grid import load_grid
        return load_grid(self.source, self.cache)

    def load(self, output: Path):
        from shorts import ensure_clip_ids
        with self.lock:
            self.output = output
            index = output/'clips.json'
            rendered = {c.get('id'): c for c in json.loads(index.read_text(encoding='utf-8-sig'))} if index.exists() else {}
            plan_path = self.root/'Plans'/f'{self.source_id}.json'
            self.plan = json.loads((plan_path if plan_path.exists() else output/'plan.json').read_text(encoding='utf-8-sig'))
            ensure_clip_ids(self.plan)  # in memory only; the render writes them to the plan
            self.clips = []
            for clip in self.plan['clips']:
                done = rendered.get(clip['id'])
                if done and (output/done['file']).exists():
                    self.clips.append({**done, 'rendered': True})
                else:
                    self.clips.append({**clip, 'file': '', 'rendered': False})
            self._focus = {}

    def reload(self):
        """Pick up the plan and the folder Apply rendered into (it can change with the profile)."""
        from shorts import settings
        self.config = settings()
        target = self.output
        last = self.cache/'last-render.json'
        if last.exists():
            candidate = Path(json.loads(last.read_text(encoding='utf-8'))['output'])
            if (candidate/'clips.json').exists():
                target = candidate.resolve()
        self.load(target)
        self.forget_transcript()

    def forget_transcript(self):
        with self.lock:
            self._words = self._transcript = None
            self._focus = {}

    def clip(self, key):
        return next((c for c in self.clips if clip_key(c) == key), None)

    def transcript(self):
        from shorts import english_transcript
        with self.lock:
            if self._transcript is None:
                # The missed-speech pass runs as the background "prepare" job, not in a request.
                self._transcript = english_transcript(self.source, self.cache, self.model, find_missed=False)
            return self._transcript

    def words(self):
        with self.lock:
            if self._words is None:
                self._words = editor_ops.indexed_words(self.source, self.cache, self.model)
            return self._words

    def focus(self, key):
        # Computed outside the lock: a speaker-focus pass takes a second or two, and the
        # captions of the clip the user just opened must not wait for it.
        with self.lock:
            if key in self._focus:
                return self._focus[key]
            clip = {k: v for k, v in self.clip(key).items() if k not in ('file', 'rendered')}
            cache, output = self._focus, self.output
        value = editor_ops.auto_focus(self.source, clip, self.cache, self.config, self.transcript(), output)
        with self.lock:
            if cache is self._focus:  # not reloaded meanwhile
                self._focus[key] = value
        return value

    def decisions(self):
        return load_decisions(self.output) or empty_decisions(self.plan, self.output.name)

    def payload(self, job):
        from missed_speech import gaps_path
        from review_apply import history_file
        edits = editor_ops.edits_path(self.cache, self.model)
        gaps = gaps_path(self.cache, self.model, editor_ops.task_of(self.source, self.cache, self.model))
        index_file = self.output/'clips.json'
        clips_mtime = index_file.stat().st_mtime if index_file.exists() else None
        previews = editor_ops.preview_dir(self.root, self.source_id)
        proxy = editor_ops.proxy_path(self.root, self.source_id)
        edits_dir = self.output/'edits'
        index = edits_dir/'edits.json'
        edit_infos = {e['id']: e for e in json.loads(index.read_text(encoding='utf-8'))} if index.exists() else {}
        plan_edits = self.plan.get('edits') or {}
        items = []
        for clip in self.clips:
            key = clip_key(clip)
            if not clip['rendered']:
                items.append({**clip, 'key': key, 'video_url': None, 'sheet_url': None, 'history_url': None,
                              'preview_url': None, 'preview': None, 'locked': False, 'edit': None,
                              'poster_url': f'/api/clips/{quote(key)}/poster?at={round(clip["start"]*1000)}'})
                continue
            stem = Path(clip['file']).stem
            preview = previews/f'{key}.mp4'
            meta = previews/f'{key}.json'
            history = history_file(self.output, key)
            items.append({**clip, 'key': key, 'video_url': url_for(self.root, self.output/clip['file']),
                          'poster_url': url_for(self.root, self.output/(stem+'.jpg')),
                          'sheet_url': url_for(self.root, self.root/'Review'/self.source_id/'focus'/(stem+'.jpg')),
                          'history_url': url_for(self.root, history) if history else None,
                          'preview_url': url_for(self.root, preview) if preview.exists() else None,
                          'preview': json.loads(meta.read_text(encoding='utf-8')) if meta.exists() else None,
                          'locked': bool(clip.get('approved')),
                          'edit': edit_item(self.root, edit_infos.get(key), plan_edits.get(key), edits_dir)})
        gallery = self.output/'index.html'
        return {'source': {'id': self.source_id, 'name': self.plan.get('source', ''), 'duration': self.duration,
                           'proxy_url': url_for(self.root, proxy) if editor_ops.proxy_ready(self.root, self.source_id) else None,
                           'missed_speech': gaps.exists(), 'model': self.model},
                'profile': self.output.name, 'output': str(self.output),
                'gallery_url': url_for(self.root, gallery, False) if gallery.exists() else None,
                'reject_reasons': [[k, v] for k, v in REJECT_REASONS.items()],
                'settings': {k: self.config[k] for k in ('width', 'height', 'font', 'font_size', 'subtitle_y', 'highlight',
                                                        'speaker_switch_min_seconds')},
                'limits': {'context': editor_ops.CONTEXT, 'fps': editor_ops.FPS},
                'captions_dirty': clips_mtime is not None and any(p.exists() and p.stat().st_mtime > clips_mtime
                                                                  for p in (edits, gaps)),
                'caption_edits': caption_edit_count(edits),
                'clips': items, 'decisions': self.decisions(), 'job': job.snapshot()}


def edit_item(root, info, planned, folder):
    """What the editor shows for a clip's music edit: its two files, the music and the checks."""
    if not info:
        return None
    return {**{k: info.get(k) for k in ('track', 'mood', 'bpm', 'grade', 'transition', 'effects', 'duration', 'payoff_text',
                                         'music_hint', 'checks', 'track_drop')},
            'drop_at': info['timeline']['drop_at'], 'interval': info['timeline']['interval'],
            'shots': len(info['timeline']['shot_frames']),
            'reason': (planned or {}).get('reason', ''), 'locked': bool((planned or {}).get('approved')),
            'video_url': url_for(root, folder/info['file']),
            'nomusic_url': url_for(root, folder/info['nomusic_file']),
            'poster_url': url_for(root, folder/info['cover'])}


def caption_edit_count(path: Path):
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    return len(data.get('edits', []))+len(data.get('added', []))


def needs_prepare(state: State):
    from missed_speech import load_gaps
    return (not editor_ops.proxy_ready(state.root, state.source_id)
            or load_gaps(state.cache, state.model, editor_ops.task_of(state.source, state.cache, state.model)) is None)


def make_handler(state: State, job: Job):
    root = state.root
    editor_dir = (root/'editor').resolve()
    fonts_dir = (root/'assets'/'fonts').resolve()

    def start_prepare():
        def after():
            state.forget_transcript()
        job.start('prepare', [sys.executable, str(root/'editor_ops.py'), 'prepare', state.source_id, state.model], after=after)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype='application/json; charset=utf-8'):
            if not isinstance(body, (bytes, str)):
                body = json.dumps(body, ensure_ascii=False)
            data = body if isinstance(body, bytes) else body.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def _error(self, code, text):
            self._send(code, {'error': text})

        # ----- GET -----
        def do_GET(self):
            parts = urlsplit(self.path)
            path, query = parts.path, parse_qs(parts.query)
            try:
                if path in ('/', ''):
                    self.send_response(302)
                    self.send_header('Location', '/editor/')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if path.startswith('/api/'):
                    return self._api_get(path[5:], query)
                if path.startswith('/editor/'):
                    rel = unquote(path[len('/editor/'):]) or 'index.html'
                    return self._static(editor_dir, rel)
                if path.startswith('/fonts/'):
                    return self._static(fonts_dir, unquote(path[len('/fonts/'):]))
                if path.endswith('/'):
                    path += 'index.html'
                target = _resolve_inside(root, path)
                if target is None or not target.is_file():
                    return self._error(404, 'Not found')
                self._file(target)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _static(self, base, rel):
            target = (base/rel).resolve()
            if base not in target.parents or not target.is_file():
                return self._error(404, 'Not found')
            self._file(target)

        def _api_get(self, name, query):
            if name == 'state':
                with state.lock:
                    return self._send(200, state.payload(job))
            if name == 'job':
                return self._send(200, job.snapshot())
            if name == 'frames':
                return self._send(200, state.grid().client())
            if name == 'retranscribe':
                path = editor_ops.retranscribe_path(root, state.source_id)
                if not path.exists():
                    return self._error(404, 'Nothing has been re-transcribed yet.')
                return self._send(200, json.loads(path.read_text(encoding='utf-8')))
            if name == 'find':
                text = query.get('q', [''])[0]
                if len(text) > 200:
                    return self._error(400, 'The search text is too long.')
                return self._send(200, {'query': text, 'matches': editor_ops.find_words(state.words(), text)})
            segments = name.split('/')
            if len(segments) == 3 and segments[0] == 'clips':
                clip = state.clip(unquote(segments[1]))
                if clip is None:
                    return self._error(404, 'Unknown clip')
                try:
                    if segments[2] == 'focus':
                        return self._send(200, state.focus(clip_key(clip)))
                    if segments[2] == 'captions':
                        return self._captions(clip, query)
                    if segments[2] == 'snap':
                        return self._snap(clip, query)
                    if segments[2] == 'poster':
                        poster = editor_ops.make_poster(root, state.source_id, clip)
                        if poster is None:
                            return self._error(404, 'The poster needs the preview proxy.')
                        return self._file(poster)
                except ValueError as exc:
                    return self._error(400, str(exc))
            return self._error(404, 'Unknown API')

        def _number(self, query, name, default):
            try:
                value = float(query.get(name, [default])[0])
            except (TypeError, ValueError):
                raise ValueError(f'{name} must be a number.')
            if value != value or abs(value) > 1e7:
                raise ValueError(f'{name} is out of range.')
            return value

        def _captions(self, clip, query):
            start = max(0.0, self._number(query, 'start', clip['start']))
            end = min(state.duration, self._number(query, 'end', clip['end']))
            if not end > start:
                raise ValueError('The caption range is empty.')
            ctx = min(600.0, max(editor_ops.CONTEXT, self._number(query, 'context', editor_ops.CONTEXT)))
            low, high = max(0, min(start, clip['start'])-ctx), max(end, clip['end'])+ctx
            words = [w for w in state.words() if w['end'] >= low and w['start'] <= high]
            return self._send(200, {'start': start, 'end': end, 'words': words,
                                    'groups': editor_ops.caption_events(state.transcript(), start, end, state.config)})

        def _snap(self, clip, query):
            from review_apply import snap_edges
            words = [w for s in state.transcript()['segments'] for w in s.get('words', [])]
            start, end = snap_edges(words, clip['start'], clip['end'], self._number(query, 'start_shift', 0),
                                    self._number(query, 'end_shift', 0), state.duration)
            return self._send(200, {'start': start, 'end': end})

        def _file(self, target: Path):
            size = target.stat().st_size
            ctype = TYPES.get(target.suffix.lower(), 'application/octet-stream')
            header = self.headers.get('Range')
            span = parse_range(header, size)
            if header and span is None:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            start, end = span or (0, size-1)
            self.send_response(206 if span else 200)
            self.send_header('Content-Type', ctype)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end-start+1))
            self.send_header('Cache-Control', 'no-cache')
            if span:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            with target.open('rb') as handle:
                handle.seek(start)
                left = end-start+1
                while left > 0:
                    chunk = handle.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        # ----- POST -----
        def _body(self):
            length = int(self.headers.get('Content-Length', 0))
            if length > 256*1024:
                raise ValueError('Request is too large.')
            raw = self.rfile.read(length).decode('utf-8') if length else '{}'
            return json.loads(raw or '{}')

        def _same_origin(self):
            """Refuse requests from other web pages: JSON only, and the Origin must be this server."""
            if not (self.headers.get('Content-Type') or '').startswith('application/json'):
                return False
            port = self.server.server_address[1]
            host = self.headers.get('Host', '')
            if host not in (f'127.0.0.1:{port}', f'localhost:{port}'):  # refuses DNS-rebinding hosts
                return False
            origin = self.headers.get('Origin')
            return origin is None or origin == f'http://{host}'

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                if not self._same_origin():
                    return self._error(403, 'Cross-origin or non-JSON requests are refused.')
                body = self._body()
                if not path.startswith('/api/'):
                    return self._error(404, 'Unknown API')
                return self._api_post(path[5:], body)
            except (ValueError, json.JSONDecodeError) as exc:
                return self._error(400, str(exc))
            except RuntimeError as exc:
                return self._error(409, str(exc))
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _api_post(self, name, body):
            segments = name.split('/')
            if name == 'apply':
                return self._apply()
            if name in ('proxy', 'prepare'):
                if needs_prepare(state):
                    start_prepare()
                return self._send(200, job.snapshot())
            if name == 'export':
                folder, files = editor_ops.export_ready(state.output, state.decisions())
                return self._send(200, {'folder': str(folder), 'files': files})
            if job.busy('apply'):
                raise RuntimeError('Apply is running; edits are paused until it finishes.')
            if name == 'captions':
                from transcript_edits import FIELDS
                changes = {k: body[k] for k in FIELDS if k in body}
                with _LOCK:
                    editor_ops.save_caption_edit(state.source, state.cache, state.model, body.get('index'), changes)
                state.forget_transcript()
                return self._send(200, {'ok': True})
            if name == 'captions/batch':
                with _LOCK:
                    editor_ops.save_caption_edits(state.source, state.cache, state.model, body.get('changes'))
                state.forget_transcript()
                return self._send(200, {'ok': True})
            if name == 'captions/add':
                with _LOCK:
                    ids = editor_ops.add_caption_words(state.cache, state.model, body.get('words'))
                state.forget_transcript()
                return self._send(200, {'ids': ids})
            if name == 'retranscribe':
                start, end = body.get('start'), body.get('end')
                if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (start, end)):
                    raise ValueError('start and end must be numbers.')
                start, end = max(0.0, float(start)), min(state.duration, float(end))
                if not 0 < end-start <= 30:
                    raise ValueError('Re-transcribe between 0 and 30 seconds at a time.')
                editor_ops.retranscribe_path(root, state.source_id).unlink(missing_ok=True)
                job.start('transcribe', [sys.executable, str(root/'editor_ops.py'), 'retranscribe', state.source_id,
                                         f'{start:.3f}', f'{end:.3f}', state.model])
                return self._send(200, job.snapshot())
            if len(segments) == 3 and segments[0] == 'clips':
                clip = state.clip(unquote(segments[1]))
                if clip is None:
                    return self._error(404, 'Unknown clip')
                if segments[2] == 'decision':
                    return self._decision(clip, body)
                if segments[2] == 'edit-decision':
                    return self._edit_decision(clip, body)
                if segments[2] == 'preview':
                    key = clip_key(clip)
                    if not clip['rendered']:
                        raise ValueError('The exact preview is for rendered clips; keep this candidate and Apply to render it.')
                    job.start('preview', [sys.executable, str(root/'editor_ops.py'), 'preview', str(state.output), key,
                                          state.model], clip=key)
                    return self._send(200, job.snapshot())
            return self._error(404, 'Unknown API')

        def _decision(self, clip, body):
            status = body.get('status')
            if status == 'kept' and clip['rendered']:
                raise ValueError('Keep is the triage of a candidate that was never rendered; this clip is rendered.')
            if status == 'approved' and not clip['rendered']:
                raise ValueError('Approve a clip after watching its render; keep this candidate to render it.')
            overrides = body.get('focus_overrides')
            if overrides is not None:
                focus = state.focus(clip_key(clip))
                if focus.get('centered'):
                    raise ValueError('Framing can be changed once the clip has a speaker analysis (after it renders).')
                from gallery import validate_overrides
                check_overrides_against_shots(validate_overrides(overrides), focus['shots'], focus['frame_count'])
            base = body.pop('base', None)
            for edge, name in (('start', 'start_shift'), ('end', 'end_shift')):
                value = body.get(name, 0)
                if isinstance(value, (int, float)) and not -1e-3 <= clip[edge]+value <= state.duration+.05:
                    raise ValueError(f'The {edge} would move outside the source (0-{state.duration:.2f} s).')
            with _LOCK:
                decisions = state.decisions()
                current = decisions['clips'].get(clip_key(clip), {}).get('updated')
                # Each save names the version it was edited from, so a stale window cannot overwrite newer edits.
                if base != current:
                    raise RuntimeError('This clip was changed elsewhere; the editor reloads it.')
                entry = apply_decision(decisions, clip, body)
                save_decisions(state.output, decisions)
            return self._send(200, entry)

        def _edit_decision(self, clip, body):
            base = body.pop('base', None)
            with _LOCK:
                decisions = state.decisions()
                current = decisions.get('edits', {}).get(clip_key(clip), {}).get('updated')
                # Like clip decisions: a stale window cannot overwrite a newer decision.
                if base != current:
                    raise RuntimeError('This edit was changed elsewhere; the editor reloads it.')
                entry = apply_edit_decision(decisions, clip, body)
                save_decisions(state.output, decisions)
            return self._send(200, entry)

        def _apply(self):
            def before():
                with _LOCK:
                    state.output.mkdir(parents=True, exist_ok=True)
                    if not (state.output/DECISIONS_FILE).exists():
                        save_decisions(state.output, empty_decisions(state.plan, state.output.name))

            def after():
                with state.lock:
                    state.reload()
            job.start('apply', [sys.executable, str(root/'shorts.py'), '--apply-review', str(state.output),
                                '--model', state.model], before=before, after=after)
            return self._send(200, job.snapshot())

    return Handler, start_prepare


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A browser dropping a connection (a closed window, a cancelled video range) is normal.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def app_browser():
    """A Chromium browser that can open a page as its own window (Edge or Chrome), or None."""
    import shutil
    names = ['msedge', 'chrome', 'google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'microsoft-edge']
    found = next((shutil.which(n) for n in names if shutil.which(n)), None)
    if found:
        return found
    candidates = []
    if sys.platform == 'win32':
        for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles'), os.environ.get('LocalAppData')):
            if base:
                candidates += [Path(base)/'Microsoft/Edge/Application/msedge.exe', Path(base)/'Google/Chrome/Application/chrome.exe']
    elif sys.platform == 'darwin':
        candidates += [Path('/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'),
                       Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')]
    return next((str(p) for p in candidates if p.is_file()), None)


def open_app_window(url):
    """Open the editor as its own window without tabs or address bar (Edge/Chrome app
    mode); without such a browser, open it in the default browser."""
    browser = app_browser()
    if browser:
        try:
            subprocess.Popen([browser, f'--app={url}', '--window-size=1600,1000'], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    import webbrowser
    webbrowser.open(url)


def serve(root: Path, output: Path, port=8765, open_browser=True, model=None, source_id=None):
    """Serve the editor on 127.0.0.1 until interrupted; creates the preview proxy if missing.
    `output` may not exist yet: a plan that has not rendered opens for triage."""
    if (Path(output)/'clips.json').exists():
        write_gallery(output, root)
    state, job = State(root, output, source_id, model), Job()
    handler, start_prepare = make_handler(state, job)
    server = None
    for candidate in range(port, port+20):
        try:
            server = _Server(('127.0.0.1', candidate), handler)
            break
        except OSError:
            continue
    if server is None:
        raise OSError(f'No free port between {port} and {port+19}.')
    url = f'http://127.0.0.1:{server.server_address[1]}/editor/'
    print(f'Clip editor: {url}', flush=True)
    print(f'Decisions are saved to: {Path(output)/DECISIONS_FILE}', flush=True)
    waiting = sum(not c['rendered'] for c in state.clips)
    if waiting:
        print(f'{waiting} candidate(s) are not rendered yet: keep or reject them, then Apply renders the kept ones.', flush=True)
    print('Press Ctrl+C to stop.', flush=True)
    if needs_prepare(state):
        print('Preparing the editor in the background (preview proxy, missed-speech pass).', flush=True)
        start_prepare()
    if open_browser:
        open_app_window(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
