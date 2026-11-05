"""Review decisions model and the read-only static gallery.

`review-decisions.json` in an output profile folder holds the reviewer's
decision per clip id: status, change options, comment, moved edges and staged
framing (`focus_overrides`). The interactive editor (`editor_server.py`,
started by `run --review-gallery`) writes it; `--apply-review` applies it.
`index.html` is a read-only overview that also works when opened from disk.
"""
from __future__ import annotations
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path
import re
import threading
from urllib.parse import quote, unquote

DECISIONS_FILE = 'review-decisions.json'
STATUSES = ('pending', 'approved', 'needs_changes', 'rejected')
STATUS_LABELS = {'pending': 'Pending', 'approved': 'Approved', 'needs_changes': 'Needs changes', 'rejected': 'Rejected'}
OPTIONS = ('recut', 'fix_framing')
OPTION_LABELS = {'recut': 'Re-cut (start/end)', 'fix_framing': 'Fix framing'}
MAX_COMMENT = 4000
MAX_SHIFT = 30.0  # seconds a reviewer can move a clip edge from the editor
MAX_OVERRIDES = 200
_LOCK = threading.Lock()  # serializes decisions-file reads and writes across server threads


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def empty_decisions(plan=None, profile=''):
    plan = plan or {}
    return {'version': 1, 'source': plan.get('source', ''), 'source_id': plan.get('source_id', ''),
            'profile': profile, 'clips': {}}


def clip_key(clip):
    """Decisions are keyed by the clip's permanent id, not its file name."""
    if clip.get('id'):
        return clip['id']
    match = re.match(r'(\d{2,})_', clip.get('file', ''))
    return match.group(1) if match else clip.get('file', '')


def load_decisions(folder: Path):
    path = Path(folder)/DECISIONS_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    # Older files were keyed by file name ("01_Title.mp4"); move them to ids.
    data['clips'] = {clip_key({'file': k}) if k.endswith('.mp4') else k: v for k, v in data.get('clips', {}).items()}
    return data


def save_decisions(folder: Path, data):
    path = Path(folder)/DECISIONS_FILE
    tmp = path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_overrides(overrides):
    """Staged framing for a clip: None (no change) or a sorted, non-overlapping
    list of clip-relative half-open frame ranges with a source-normalized center."""
    if overrides is None:
        return None
    if not isinstance(overrides, list) or len(overrides) > MAX_OVERRIDES:
        raise ValueError('focus_overrides must be a list.')
    clean = []
    for o in overrides:
        if not isinstance(o, dict) or set(o) - {'start_frame', 'end_frame', 'center_x', 'center_x_end', 'reason'}:
            raise ValueError('Each focus override needs start_frame, end_frame, center_x and optional center_x_end and reason.')
        a, b = o.get('start_frame'), o.get('end_frame')
        if type(a) is not int or type(b) is not int or not 0 <= a < b:
            raise ValueError('Focus override frames must be integers with 0 <= start_frame < end_frame.')
        item = {'start_frame': a, 'end_frame': b}
        if 'reason' in o:
            if not isinstance(o['reason'], str) or len(o['reason']) > 500:
                raise ValueError('A focus override reason must be text of at most 500 characters.')
            item['reason'] = o['reason']
        for key in ('center_x', 'center_x_end'):
            if key in o:
                if not _number(o[key]) or not 0 <= o[key] <= 1:
                    raise ValueError(f'{key} must be between 0 and 1.')
                item[key] = round(float(o[key]), 4)
        if 'center_x' not in item:
            raise ValueError('A focus override needs center_x.')
        clean.append(item)
    clean.sort(key=lambda o: o['start_frame'])
    if any(x['end_frame'] > y['start_frame'] for x, y in zip(clean, clean[1:])):
        raise ValueError('Focus overrides cannot overlap.')
    return clean


def check_overrides_against_shots(overrides, shots, frame_count):
    """Refuse overrides outside the clip or across a camera cut (`shots` from .focus.json)."""
    cuts = [s['start_frame'] for s in shots if s['start_frame'] > 0]
    for o in overrides or []:
        if o['end_frame'] > frame_count:
            raise ValueError(f'Focus override {o["start_frame"]}-{o["end_frame"]} ends after the clip ({frame_count} frames).')
        if any(o['start_frame'] < cut < o['end_frame'] for cut in cuts):
            raise ValueError(f'Focus override {o["start_frame"]}-{o["end_frame"]} crosses a camera cut.')


def validate_update(update):
    """Return a clean decision dict or raise ValueError."""
    if not isinstance(update, dict):
        raise ValueError('Decision must be an object.')
    status = update.get('status', 'pending')
    if status not in STATUSES:
        raise ValueError(f'Unknown status: {status!r}')
    options = update.get('options', [])
    if not isinstance(options, list) or any(o not in OPTIONS for o in options):
        raise ValueError(f'Options must be a subset of {list(OPTIONS)}.')
    comment = update.get('comment', '')
    if not isinstance(comment, str) or len(comment) > MAX_COMMENT:
        raise ValueError(f'Comment must be text of at most {MAX_COMMENT} characters.')
    shifts = {}
    for name in ('start_shift', 'end_shift'):
        value = update.get(name, 0)
        if not _number(value) or abs(value) > MAX_SHIFT:
            raise ValueError(f'{name} must be a number of seconds within ±{MAX_SHIFT:g}.')
        shifts[name] = round(float(value), 3)
    return {'status': status, 'options': [o for o in OPTIONS if o in options], 'comment': comment.strip(), **shifts,
            'focus_overrides': validate_overrides(update.get('focus_overrides'))}


def apply_decision(decisions, clip, update, timestamp=None):
    """Merge a user edit for one clip. Any user edit reopens the decision for the agent."""
    clean = validate_update(update)
    key = clip_key(clip)
    entry = dict(decisions['clips'].get(key, {}))
    if not entry or any(entry.get(k, 0 if k.endswith('_shift') else None) != clean[k] for k in clean):
        entry.update(clean)
        entry.update({'file': clip.get('file', ''), 'title': clip.get('title', ''), 'start': clip.get('start'),
                      'end': clip.get('end'), 'updated': timestamp or now_iso(), 'addressed': False})
        entry.pop('addressed_at', None)
        entry.pop('agent_note', None)
    if entry.get('focus_overrides') is None:
        entry.pop('focus_overrides', None)
    decisions['clips'][key] = entry
    return entry


def actionable(decisions):
    """Decisions the agent still has to act on: needs_changes or rejected, not yet addressed."""
    return {name: d for name, d in (decisions or {}).get('clips', {}).items()
            if d.get('status') in ('needs_changes', 'rejected') and not d.get('addressed')}


def parse_range(header, size):
    """Parse a single `bytes=` Range header into an inclusive (start, end), or None when absent/invalid."""
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', (header or '').strip())
    if not match or size <= 0 or match.groups() == ('', ''):
        return None
    first, last = match.groups()
    if first == '':
        length = int(last)
        if length == 0:
            return None
        return max(0, size-length), size-1
    start = int(first)
    end = min(int(last), size-1) if last else size-1
    if start >= size or end < start:
        return None
    return start, end


PAGE_CSS = """
:root{--bg:#0f141b;--panel:#18202b;--line:#2a3544;--text:#e8edf3;--muted:#9aa8ba;--ok:#3fb27f;--warn:#e0a63a;--bad:#e2575b;--accent:#5b9cf2}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,Segoe UI,sans-serif}
header{border-bottom:1px solid var(--line);padding:14px 24px}h1{font-size:18px;margin:0}.meta{color:var(--muted);font-size:13px}
.note{margin:16px 24px 0;padding:10px 14px;border-radius:8px;font-size:14px;background:#3a2c12;border:1px solid #6b5320}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:20px;padding:20px 24px 60px}
article{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;border-top:4px solid var(--line)}
article[data-status=approved]{border-top-color:var(--ok)}article[data-status=needs_changes]{border-top-color:var(--warn)}article[data-status=rejected]{border-top-color:var(--bad)}
video{width:100%;aspect-ratio:4/5;background:#000;display:block}.body{padding:12px 14px 14px;display:flex;flex-direction:column;gap:6px}
h2{font-size:16px;margin:0}.num{color:var(--muted);font-weight:400;margin-right:6px}.reason,.comment{color:var(--muted);font-size:13px;margin:0}
.facts{color:var(--muted);display:flex;gap:10px;flex-wrap:wrap;font-size:12px}.facts a{color:var(--accent);text-decoration:none}
.status{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
"""


def render_page(clips, plan=None, profile='', decisions=None, focus_dir_href=None):
    """Read-only overview of an output folder; editing happens in the served editor."""
    plan = plan or {}
    entries = (decisions or {}).get('clips', {})
    cards = []
    for clip in clips:
        file = clip['file']
        key = clip_key(clip)
        stem = Path(file).stem
        entry = entries.get(key, {})
        status = entry.get('status', 'pending')
        links = [f'<a href="{quote(stem)}.en.srt">Captions</a>', f'<a href="{quote(stem)}.focus.json">Focus data</a>']
        if focus_dir_href:
            links.insert(0, f'<a href="{focus_dir_href}/{quote(stem)}.jpg" target="_blank">Framing sheet</a>')
        locked = ' · locked (approved)' if clip.get('approved') else ''
        comment = f'<p class="comment">“{html.escape(entry["comment"])}”</p>' if entry.get('comment') else ''
        cards.append(
            f'<article data-clip="{html.escape(key)}" data-status="{html.escape(status)}">'
            f'<video controls preload="metadata" poster="{quote(stem)}.jpg" src="{quote(file)}"></video>'
            f'<div class="body"><h2><span class="num">{html.escape(key)}</span>{html.escape(clip.get("title", ""))}</h2>'
            f'<div class="status">{html.escape(STATUS_LABELS.get(status, status))}</div>'
            f'<p class="reason">{html.escape(clip.get("reason", ""))}</p>{comment}'
            f'<div class="facts"><span>{clip["end"]-clip["start"]:.1f} s</span>'
            f'<span>Source {clip["start"]:.2f}–{clip["end"]:.2f} s{locked}</span>{"".join(links)}</div>'
            '</div></article>')
    title = plan.get('source') or 'Short clips'
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Clip review</title><style>{PAGE_CSS}</style></head><body>'
            f'<header><h1>Clip review</h1><div class="meta">{html.escape(title)} · {html.escape(profile)} · {len(clips)} clips</div></header>'
            '<div class="note">Read-only overview. Run <code>run.cmd --review-gallery</code> (Windows) or <code>./run.sh --review-gallery</code> to open the editor, where clips '
            'can be reviewed, re-cut, reframed, previewed and applied, and captions corrected.</div>'
            f'<main>{"".join(cards)}</main></body></html>')


def _focus_dir_href(output: Path, root: Path, plan):
    source_id = (plan or {}).get('source_id')
    if not source_id:
        return None
    folder = root/'Review'/source_id/'focus'
    if not folder.is_dir():
        return None
    rel = os.path.relpath(folder, output).replace(os.sep, '/')
    return '/'.join(quote(part) for part in rel.split('/'))


def write_gallery(output: Path, root: Path, clips=None, plan=None):
    """(Re)write index.html for an output profile folder. Never touches review-decisions.json."""
    output = Path(output)
    if clips is None:
        clips = json.loads((output/'clips.json').read_text(encoding='utf-8-sig'))
    if plan is None and (output/'plan.json').exists():
        plan = json.loads((output/'plan.json').read_text(encoding='utf-8-sig'))
    page = render_page(clips, plan, output.name, load_decisions(output), _focus_dir_href(output, Path(root), plan))
    (output/'index.html').write_text(page, encoding='utf-8')
    return output/'index.html'


def latest_output(root: Path):
    folders = [p.parent for p in (Path(root)/'Output').glob('*/*/clips.json')]
    if not folders:
        raise ValueError('No rendered output folder with clips.json was found.')
    return max(folders, key=lambda p: (p/'clips.json').stat().st_mtime)


def _resolve_inside(root: Path, url_path: str, allowed=('Output', 'Review')):
    """Map a URL path to a file under root/<allowed>, or None when it escapes those folders."""
    rel = unquote(url_path).lstrip('/')
    target = (Path(root)/rel).resolve()
    for name in allowed:
        base = (Path(root)/name).resolve()
        if target == base or base in target.parents:
            return target
    return None
