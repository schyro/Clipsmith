"""Checks that an agent's clip selection followed AGENTS.md before anything renders.

The checks cover structure, not taste: every dialogue block of review.md has a
coverage note, sparse blocks explain why nothing else qualifies, the clip count
is plausible and no clip edge cuts a spoken word. Every problem is reported at
once, so the plan can be fixed in one pass.
"""
import math

BLOCK_SECONDS = 300
MIN_TAIL_SECONDS = 60      # a shorter last block joins the one before it
SECONDS_PER_CLIP = 120     # AGENTS.md: usually at least one candidate per two minutes
MIN_REASON_CHARS = 30
EDGE_TOLERANCE = .01


def block_start(time, duration):
    """Start of the five-minute review block that holds `time`."""
    start = int(time//BLOCK_SECONDS)*BLOCK_SECONDS
    if start > 0 and duration-start < MIN_TAIL_SECONDS:
        start -= BLOCK_SECONDS
    return start


def block_end(start, duration):
    end = start+BLOCK_SECONDS
    return duration if duration-end < MIN_TAIL_SECONDS else min(end, duration)


def clock(seconds):
    seconds = int(math.floor(seconds))
    return f'{seconds//60:02}:{seconds%60:02}'


def block_label(start, duration):
    return f'{clock(start)}–{clock(block_end(start, duration))}'


def dialogue_blocks(transcript, duration):
    """Starts of the blocks that contain dialogue; review.md shows exactly these."""
    from shorts import sentences
    return sorted({block_start(unit['start'], duration) for unit in sentences(transcript)})


def expected_clip_count(duration):
    return int(duration//SECONDS_PER_CLIP)


def coverage_problems(plan, duration, transcript):
    problems = []
    blocks = dialogue_blocks(transcript, duration)
    labels = {b: block_label(b, duration) for b in blocks}
    counted = plan.get('clips', [])+plan.get('rejected_clips', [])
    per_block = {b: 0 for b in blocks}
    for clip in counted:
        start = block_start(float(clip['start']), duration)
        if start in per_block:
            per_block[start] += 1
    coverage = plan.get('coverage')
    no_list = not isinstance(coverage, list)
    if no_list:
        listing = ', '.join(f'{b} ({labels[b]})' for b in blocks)
        problems.append(f'The plan has no "coverage" list. Add one entry per dialogue block of review.md: '
                        f'{{"start": <block start>, "note": "..."}} for the starts {listing}. See AGENTS.md, step 3.')
        coverage = []
    entries = {}
    for entry in coverage:
        start = entry.get('start') if isinstance(entry, dict) else None
        if not isinstance(start, (int, float)) or start not in per_block:
            problems.append(f'Unknown coverage entry {entry!r}; block starts are {", ".join(map(str, blocks))}.')
        elif start in entries:
            problems.append(f'Block {labels[start]} has more than one coverage entry.')
        else:
            entries[start] = entry
    total, expected = len(counted), expected_clip_count(duration)
    # "count_note" is only for a user who asked for fewer clips.
    sparse = total < expected and not str(plan.get('count_note', '')).strip()
    sparse_blocks = []
    for start in blocks:
        entry = entries.get(start)
        if entry is None:
            if not no_list:
                problems.append(f'Block {labels[start]} (start {start}) has no coverage entry.')
            entry = {}
        elif not str(entry.get('note', '')).strip():
            problems.append(f'Block {labels[start]}: "note" is empty; say what the block holds and which moments you took.')
        if len(str(entry.get('why_not_more', '')).strip()) >= MIN_REASON_CHARS:
            continue
        if per_block[start] == 0:
            problems.append(f'Block {labels[start]} has no clip: re-read it and add its moments, or explain in '
                            f'"why_not_more" why nothing in it works on its own.')
        elif sparse and per_block[start] < 2:
            sparse_blocks.append(labels[start])
    if sparse_blocks:
        problems.append(f'The plan has {total} clip(s), rejected ones included, where dialogue-driven material usually '
                        f'gives at least {expected} for {clock(duration)}. Re-read the blocks with one clip '
                        f'({", ".join(sparse_blocks)}) and add the moments you missed; a block that stays at one '
                        f'clip needs a "why_not_more".')
    return problems


def edge_problems(plan, transcript):
    words = sorted((w for s in transcript['segments'] for w in s.get('words', []) if w['end'] > w['start']),
                   key=lambda w: w['start'])
    problems = []
    for clip in plan.get('clips', []):
        for name in ('start', 'end'):
            edge = float(clip[name])
            for word in words:
                if word['start']+EDGE_TOLERANCE < edge < word['end']-EDGE_TOLERANCE:
                    problems.append(f'Clip {clip.get("id", "?")} {name} {edge:.2f} cuts "{word["word"].strip()}" '
                                    f'({word["start"]:.2f}–{word["end"]:.2f}); move it to {word["start"]:.2f} or '
                                    f'earlier, or {word["end"]:.2f} or later.')
                    break
    return problems


def check_reviewed_plan(plan, duration, transcript):
    """Raise ValueError listing every problem of a reviewed plan before rendering."""
    problems = edge_problems(plan, transcript)
    if plan.get('selection') == 'agent-reviewed':
        problems = coverage_problems(plan, duration, transcript)+problems
    if problems:
        raise ValueError('The plan is not ready to render:\n'+'\n'.join(f'  - {p}' for p in problems))
