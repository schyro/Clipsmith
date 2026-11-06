"""Apply gallery review decisions to the plan, then re-render what changed.

Mechanical decisions are applied directly:
- approved: the clip is locked (not re-rendered until --force or un-approval);
- rejected: the clip moves to the plan's `rejected_clips` and its files to rejected/;
- needs changes with framing staged in the editor: the reviewed focus_overrides
  replace the clip's overrides (before any re-cut, since they use its frames);
- needs changes with moved edges: the new start/end are kept off spoken words.
An entry is closed (`addressed`) when every requested option was handled and it
has no comment. Comments and unhandled requests stay open for the editorial agent.
"""
from __future__ import annotations
import json
from pathlib import Path

FPS = 30
START_PAD = .15   # seconds kept before the first word
END_PAD = .25     # seconds kept after the last word
MIN_LENGTH = 3.0
MAX_LENGTH = 180.0


def frame_aligned(words, start, value):
    """Move `value` to a whole number of frames from `start`, preferring a
    frame that is not inside any word (the earlier one if both are)."""
    import math
    def inside(t):
        return any(w['start']+1e-6 < t < w['end']-1e-6 for w in words)
    early = start+math.floor((value-start)*FPS+1e-9)/FPS
    late = start+math.ceil((value-start)*FPS-1e-9)/FPS
    choice = late if inside(early) and not inside(late) else early
    return max(0, choice)


def snap_edges(words, start, end, start_shift, end_shift, duration):
    """New (start, end) after moving the edges, without cutting a word.

    An edge that lands in silence stays where the reviewer put it (keeping a
    short margin from the neighbouring words). An edge that lands inside a
    word leaves that word in the direction it was moved: the start goes just
    before the word's onset (or the next word's), the end just after the
    word's end (or the previous word's). An edge never moves against its
    requested direction. The start change is a whole number of 30 fps frames,
    so reviewed frame indices can be shifted exactly.
    """
    words = sorted((w for w in words if w['end'] > w['start']), key=lambda w: w['start'])
    new_start, new_end = start, end
    if start_shift:
        target = max(0, start+start_shift)
        inside = next((k for k, w in enumerate(words) if w['start'] < target < w['end']), None)
        if inside is not None:
            k = inside if start_shift < 0 else inside+1
            new_start = words[k]['start']-START_PAD if k < len(words) else target
            floor = words[k-1]['end'] if 0 < k <= len(words) else 0
            new_start = max(new_start, floor)
        else:
            after = next((w for w in words if w['start'] >= target), None)
            before = next((w for w in reversed(words) if w['end'] <= target), None)
            new_start = target
            if after:
                new_start = min(new_start, after['start']-min(START_PAD, after['start']-(before['end'] if before else 0)))
        new_start = frame_aligned(words, start, new_start)
        if (start_shift > 0) != (new_start > start) and new_start != start:
            new_start = start
    if end_shift:
        target = min(duration, end+end_shift)
        inside = next((k for k, w in enumerate(words) if w['start'] < target < w['end']), None)
        if inside is not None:
            k = inside if end_shift > 0 else inside-1
            new_end = words[k]['end']+END_PAD if k >= 0 else target
            ceiling = words[k+1]['start'] if -1 <= k < len(words)-1 else duration
            new_end = min(new_end, ceiling, duration)
        else:
            before = next((w for w in reversed(words) if w['end'] <= target), None)
            after = next((w for w in words if w['start'] >= target), None)
            new_end = target
            if before:
                new_end = max(new_end, before['end']+min(END_PAD, (after['start'] if after else duration)-before['end']))
        if (end_shift > 0) != (new_end > end) and new_end != end:
            new_end = end
    new_start, new_end = round(new_start, 4), round(min(new_end, duration), 3)
    if new_end-new_start < MIN_LENGTH:
        raise ValueError(f'The new cut {new_start:.2f}-{new_end:.2f} s is shorter than {MIN_LENGTH:g} s.')
    if new_end-new_start > MAX_LENGTH:
        raise ValueError(f'The new cut is longer than {MAX_LENGTH:g} s.')
    return new_start, new_end


def shift_clip_timing(clip, new_start, new_end, grid=None):
    """Move start/end and keep reviewed overrides and the cover on the same source moment.
    With the source's frame grid (frame_grid.FrameGrid) each override edge keeps showing
    the same source frame; a new start can change which frames repeat in the 30 fps grid,
    so a plain frame offset could move an override edge across a camera cut."""
    if grid is not None:
        old_origin, new_origin = grid.origin(clip['start']), grid.origin(new_start)
        def moved_frame(n):
            return grid.remap(n, old_origin, new_origin)
    else:
        frames = round((new_start-clip['start'])*FPS)
        def moved_frame(n):
            return n-frames
    length = int((new_end-new_start)*FPS+.999)
    overrides = []
    for o in clip.get('focus_overrides', []):
        a, b = moved_frame(o['start_frame']), moved_frame(o['end_frame'])
        lo, hi = max(0, a), min(length, b)
        if hi <= lo:
            continue
        moved = dict(o, start_frame=lo, end_frame=hi)
        if 'center_x_end' in o:
            def at(frame):
                return o['center_x']+(o['center_x_end']-o['center_x'])*(frame-a)/(b-a)
            moved['center_x'], moved['center_x_end'] = round(at(lo), 4), round(at(hi), 4)
        overrides.append(moved)
    updated = dict(clip, start=new_start, end=new_end)
    if 'focus_overrides' in clip:
        updated['focus_overrides'] = overrides
    if 'cover_time' in clip:
        updated['cover_time'] = round(min(max(0, clip['cover_time']-(new_start-clip['start'])), new_end-new_start-.1), 2)
    return updated


def apply_to_plan(plan, decisions, words, duration, timestamp, handled=None, grid=None):
    """Change the plan in place. Returns (notes per clip id, ids applied mechanically).
    `handled`, when given, is filled with the set of options handled per clip id;
    `grid` (frame_grid.FrameGrid) keeps re-cut overrides on their source frames."""
    from gallery import clip_key
    notes, done = {}, set()
    handled = {} if handled is None else handled
    entries = decisions.get('clips', {})
    kept = []
    for clip in plan['clips']:
        key = clip_key(clip)
        entry = entries.get(key)
        status = (entry or {}).get('status', 'pending')
        if status == 'approved':
            if not clip.get('approved'):
                clip['approved'] = {'at': entry.get('updated', timestamp)}
                notes[key] = 'Approved; locked against re-rendering.'
        elif clip.pop('approved', None):
            notes[key] = 'Approval withdrawn; the clip renders normally again.'
        if status == 'rejected' and not entry.get('addressed'):
            plan.setdefault('rejected_clips', []).append(
                {**clip, 'rejected_at': timestamp, 'review_comment': entry.get('comment', '')})
            notes[key] = 'Removed from the plan; files moved to rejected/.'
            done.add(key)
            continue
        open_change = status == 'needs_changes' and not entry.get('addressed')
        said = []
        if open_change and entry.get('focus_overrides') is not None:
            clip = {k: v for k, v in clip.items() if k != 'focus_overrides'}
            if entry['focus_overrides']:
                clip['focus_overrides'] = [dict(o) for o in entry['focus_overrides']]
            said.append(f'Applied reviewed framing ({len(entry["focus_overrides"])} override(s)).')
            handled.setdefault(key, set()).add('fix_framing')
            done.add(key)
        if (open_change and 'recut' in entry.get('options', [])
                and (entry.get('start_shift') or entry.get('end_shift'))):
            start, end = snap_edges(words, clip['start'], clip['end'], entry.get('start_shift', 0),
                                    entry.get('end_shift', 0), duration)
            said.append(f'Re-cut {clip["start"]:.2f}-{clip["end"]:.2f} s to {start:.2f}-{end:.2f} s without cutting words.')
            clip = shift_clip_timing(clip, start, end, grid)
            handled.setdefault(key, set()).add('recut')
            done.add(key)
        if said:
            notes[key] = ' '.join(said)
        kept.append(clip)
    plan['clips'] = kept
    return notes, done


def apply_review(output: Path, root: Path, config, force=False, model='medium'):
    from gallery import DECISIONS_FILE, actionable, load_decisions, now_iso, save_decisions, write_gallery
    from shorts import (clip_files, clip_stem, english_transcript, find_source, load_plan, probe,
                        render_plan, validate_plan, write_json)
    output = Path(output)
    decisions = load_decisions(output)
    if decisions is None:
        raise ValueError(f'No {DECISIONS_FILE} in {output}. Review the clips with --review-gallery first.')
    source_id = decisions.get('source_id') or json.loads((output/'plan.json').read_text(encoding='utf-8-sig'))['source_id']
    source = find_source(source_id)
    plan_path = Path(root)/'Plans'/f'{source_id}.json'
    duration = float(probe(source)['format']['duration'])
    plan = load_plan(plan_path, source_id, duration)
    cache = Path(root)/'work'/source_id
    transcript = english_transcript(source, cache, model)
    words = [w for s in transcript['segments'] for w in s.get('words', [])]
    from frame_grid import load_grid
    grid = load_grid(source, cache)
    timestamp = now_iso()
    before = {c['id']: c for c in plan['clips']}
    handled = {}
    notes, done = apply_to_plan(plan, decisions, words, duration, timestamp, handled, grid)
    validate_plan(plan, source_id, duration)
    write_json(plan_path, plan)
    # Archive rejected clips explicitly, before render_plan tidies the folder.
    for key in done:
        if key not in {c['id'] for c in plan['clips']} and key in before:
            archive = output/'rejected'
            archive.mkdir(exist_ok=True)
            for f in clip_files(output, clip_stem(before[key])):
                f.replace(archive/f.name)
    if config['profile'] != output.name:
        print(f'Note: rendering into the current profile {config["profile"]}, not {output.name}.', flush=True)
    pending = snapshot_history(output)
    target = render_plan(source, plan, cache, config, force, model, plan_path)
    finalize_history(output if target == output else None, pending)
    current = {c['id']: c for c in plan['clips']}
    for key, note in notes.items():
        entry = decisions['clips'][key]
        entry['agent_note'] = note
        if key in done:
            entry.update({'start_shift': 0, 'end_shift': 0})
            entry.pop('focus_overrides', None)
            if key in current:
                entry.update({'start': current[key]['start'], 'end': current[key]['end'],
                              'file': clip_stem(current[key])+'.mp4'})
            # Closed when every requested option was handled and no comment needs the agent.
            done_options = handled.get(key, set())
            if entry['status'] == 'rejected' or (done_options and set(entry.get('options') or []) <= done_options
                                                 and not entry.get('comment')):
                entry.update({'addressed': True, 'addressed_at': timestamp})
    decisions['profile'] = target.name
    save_decisions(target, decisions)
    if target != output:
        save_decisions(output, decisions)
    write_gallery(target, root)
    remaining = actionable(decisions)
    print(f'Applied {len(notes)} decision(s).', flush=True)
    for key, note in sorted(notes.items()):
        print(f'  [{key}] {note}', flush=True)
    if remaining:
        print(f'Open for the editor ({len(remaining)}):', flush=True)
        for key, entry in sorted(remaining.items()):
            what = ', '.join(entry.get('options') or []) or entry['status']
            print(f'  [{key}] {entry.get("title", "")}: {what}; comment: {entry.get("comment") or "-"}', flush=True)
    return target, remaining


HISTORY = 'history'


def snapshot_history(output: Path):
    """Hard-link every current clip file before a render, so the editor can compare
    before and after. A link costs no space: a re-render replaces the file and the
    link keeps the old one. Returns {clip id: pending link}."""
    import os
    import shutil
    output = Path(output)
    pending = {}
    if not (output/'clips.json').exists():
        return pending
    for clip in json.loads((output/'clips.json').read_text(encoding='utf-8-sig')):
        current = output/clip['file']
        if 'id' not in clip or not current.exists():
            continue
        (output/HISTORY).mkdir(exist_ok=True)
        link = output/HISTORY/f'{clip["id"]}.pending.mp4'
        link.unlink(missing_ok=True)
        try:
            os.link(current, link)
        except OSError:
            shutil.copy2(current, link)
        pending[clip['id']] = link
    return pending


def finalize_history(output, pending):
    """Keep `history/<id>.mp4` only for clips whose file actually changed."""
    import os
    clips = json.loads((Path(output)/'clips.json').read_text(encoding='utf-8-sig')) if output else []
    files = {c.get('id'): Path(output)/c['file'] for c in clips}
    for key, link in pending.items():
        now = files.get(key)
        if now is not None and now.exists() and not os.path.samefile(now, link):
            os.replace(link, link.with_name(f'{key}.mp4'))
        else:
            link.unlink(missing_ok=True)


def history_file(output: Path, clip_id):
    path = Path(output)/HISTORY/f'{clip_id}.mp4'
    return path if path.exists() else None
