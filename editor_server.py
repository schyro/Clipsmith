"""Local clip editor server (`run.cmd --review-gallery`).

Serves the editor from `editor/`, media from `Output` and `Review`, and a JSON
API that stages review decisions in `review-decisions.json`, saves caption word
fixes and runs one background job at a time (Apply, preview render, proxy).
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
from gallery import (DECISIONS_FILE, _LOCK, _resolve_inside, apply_decision, check_overrides_against_shots,
                     clip_key, empty_decisions, load_decisions, parse_range, save_decisions, write_gallery)

MODEL = 'medium'
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


class State:
    """The output folder being edited; reloaded after Apply changes files or the profile."""

    def __init__(self, root: Path, output: Path):
        from shorts import find_source, probe, settings
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.config = settings()
        self.load(Path(output).resolve())
        self.source = find_source(self.source_id)
        self.duration = float(probe(self.source)['format']['duration'])
        self.cache = self.root/'work'/self.source_id
        self._words = self._transcript = None
        self._focus = {}

    def load(self, output: Path):
        with self.lock:
            self.output = output
            self.clips = json.loads((output/'clips.json').read_text(encoding='utf-8-sig'))
            self.plan = json.loads((output/'plan.json').read_text(encoding='utf-8-sig'))
            self.source_id = self.plan['source_id']
            self._focus = {}

    def reload(self):
        """Pick up the folder Apply rendered into (it can change with the profile)."""
        from shorts import settings
        self.config = settings()
        target = self.output
        last = self.root/'work'/self.source_id/'last-render.json'
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
                self._transcript = english_transcript(self.source, self.cache, MODEL)
            return self._transcript

    def words(self):
        with self.lock:
            if self._words is None:
                self._words = editor_ops.indexed_words(self.source, self.cache, MODEL)
            return self._words

    def focus(self, key):
        with self.lock:
            if key not in self._focus:
                clip = {k: v for k, v in self.clip(key).items() if k != 'file'}
                self._focus[key] = editor_ops.auto_focus(self.source, clip, self.cache, self.config,
                                                         self.transcript(), self.output)
            return self._focus[key]

    def decisions(self):
        return load_decisions(self.output) or empty_decisions(self.plan, self.output.name)

    def payload(self, job):
        from review_apply import history_file
        from shorts import clip_stem
        edits = editor_ops.edits_path(self.cache, MODEL)
        clips_mtime = (self.output/'clips.json').stat().st_mtime
        previews = editor_ops.preview_dir(self.root, self.source_id)
        proxy = editor_ops.proxy_path(self.root, self.source_id)
        items = []
        for clip in self.clips:
            key = clip_key(clip)
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
                          'locked': bool(clip.get('approved'))})
        return {'source': {'id': self.source_id, 'name': self.plan.get('source', ''), 'duration': self.duration,
                           'proxy_url': url_for(self.root, proxy) if proxy.exists() else None},
                'profile': self.output.name, 'output': str(self.output),
                'gallery_url': url_for(self.root, self.output/'index.html', False),
                'settings': {k: self.config[k] for k in ('width', 'height', 'font', 'font_size', 'subtitle_y', 'highlight',
                                                        'speaker_switch_min_seconds')},
                'limits': {'max_shift': 30.0, 'context': editor_ops.CONTEXT, 'fps': editor_ops.FPS},
                'captions_dirty': edits.exists() and edits.stat().st_mtime > clips_mtime,
                'caption_edits': len(json.loads(edits.read_text(encoding='utf-8-sig'))['edits']) if edits.exists() else 0,
                'clips': items, 'decisions': self.decisions(), 'job': job.snapshot()}


def make_handler(state: State, job: Job):
    root = state.root
    editor_dir = (root/'editor').resolve()
    fonts_dir = (root/'assets'/'fonts').resolve()

    def start_proxy():
        job.start('proxy', [sys.executable, str(root/'editor_ops.py'), 'proxy', state.source_id])

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
            ctx = editor_ops.CONTEXT
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
            if name == 'proxy':
                if not editor_ops.proxy_path(root, state.source_id).exists():
                    start_proxy()
                return self._send(200, job.snapshot())
            if name == 'export':
                folder, files = editor_ops.export_ready(state.output, state.decisions())
                return self._send(200, {'folder': str(folder), 'files': files})
            if job.busy('apply'):
                raise RuntimeError('Apply is running; edits are paused until it finishes.')
            if name == 'captions':
                index, to = body.get('index'), body.get('to')
                with _LOCK:
                    editor_ops.save_caption_edit(state.source, state.cache, MODEL, index, to)
                state.forget_transcript()
                return self._send(200, {'word': next(w for w in state.words() if w['index'] == index)})
            if len(segments) == 3 and segments[0] == 'clips':
                clip = state.clip(unquote(segments[1]))
                if clip is None:
                    return self._error(404, 'Unknown clip')
                if segments[2] == 'decision':
                    return self._decision(clip, body)
                if segments[2] == 'preview':
                    key = clip_key(clip)
                    job.start('preview', [sys.executable, str(root/'editor_ops.py'), 'preview', str(state.output), key], clip=key)
                    return self._send(200, job.snapshot())
            return self._error(404, 'Unknown API')

        def _decision(self, clip, body):
            overrides = body.get('focus_overrides')
            if overrides is not None:
                focus = state.focus(clip_key(clip))
                from gallery import validate_overrides
                check_overrides_against_shots(validate_overrides(overrides), focus['shots'], focus['frame_count'])
            base = body.pop('base', None)
            with _LOCK:
                decisions = state.decisions()
                current = decisions['clips'].get(clip_key(clip), {}).get('updated')
                # Each save names the version it was edited from, so a stale window cannot overwrite newer edits.
                if base != current:
                    raise RuntimeError('This clip was changed elsewhere; the editor reloads it.')
                entry = apply_decision(decisions, clip, body)
                save_decisions(state.output, decisions)
            return self._send(200, entry)

        def _apply(self):
            def before():
                with _LOCK:
                    if not (state.output/DECISIONS_FILE).exists():
                        save_decisions(state.output, empty_decisions(state.plan, state.output.name))

            def after():
                with state.lock:
                    state.reload()
            job.start('apply', [sys.executable, str(root/'shorts.py'), '--apply-review', str(state.output)],
                      before=before, after=after)
            return self._send(200, job.snapshot())

    return Handler, start_proxy


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def serve(root: Path, output: Path, port=8765, open_browser=True):
    """Serve the editor on 127.0.0.1 until interrupted; creates the preview proxy if missing."""
    write_gallery(output, root)
    state, job = State(root, output), Job()
    handler, start_proxy = make_handler(state, job)
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
    print('Press Ctrl+C to stop.', flush=True)
    if not editor_ops.proxy_path(state.root, state.source_id).exists():
        print('Creating the preview proxy in the background.', flush=True)
        start_proxy()
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
