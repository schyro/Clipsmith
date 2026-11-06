"""Keep clip edges off the last or first frames of a neighbouring camera shot.

A clip that opens with a few frames of the previous shot, or closes with a few
frames of the next one, flashes that shot for a moment. When such a fragment is
shorter than MIN_EDGE_FRAMES and moving the edge onto the camera cut cuts no
spoken word, the edge moves onto the cut. A fragment that holds speech stays
and is reported.
"""
from __future__ import annotations

MIN_EDGE_FRAMES = 12   # 0.4 s at 30 fps
START_LEAD = .001      # the seek lands just before the cut frame, so that frame is the first one kept
MIN_LENGTH = 3.0
FPS = 30


def _spoken(words):
    return [w for w in words if w['end'] > w['start'] and w['word'].strip()]


def cut_edges(clip, shots, frame_count, words, grid):
    """New (start, end) and a list of notes. `shots` and `frame_count` come from the
    clip's speaker analysis, `grid` is the source's frame_grid.FrameGrid."""
    words = _spoken(words)
    start, end = clip['start'], clip['end']
    origin = grid.origin(start)
    notes = []
    new_start, new_end = start, end
    if len(shots) > 1 and shots[0]['end_frame'] < MIN_EDGE_FRAMES:
        frames = shots[0]['end_frame']
        cut = grid.frame_seconds(frames, origin)
        blocking = next((w for w in words if w['start'] < cut and w['end'] > start), None)
        if blocking:
            notes.append(f'opens with {frames} frame(s) of the previous shot; moving the start onto the cut at '
                         f'{cut:.2f} s would cut "{blocking["word"].strip()}".')
        elif end-cut >= MIN_LENGTH:
            new_start = round(cut-START_LEAD, 4)
            notes.append(f'start moved {start:.2f} -> {new_start:.2f} s onto the camera cut '
                         f'({frames} frame(s) of the previous shot dropped).')
    if len(shots) > 1 and frame_count-shots[-1]['start_frame'] < MIN_EDGE_FRAMES:
        frames = frame_count-shots[-1]['start_frame']
        cut_index = grid.shown(shots[-1]['start_frame'], origin)
        cut = grid.seconds(cut_index)
        blocking = next((w for w in words if w['end'] > cut and w['start'] < end), None)
        if blocking:
            notes.append(f'closes with {frames} frame(s) of the next shot; moving the end onto the cut at '
                         f'{cut:.2f} s would cut "{blocking["word"].strip()}".')
        else:
            # Keep every grid frame before the cut frame, ending half a frame early so
            # rounding in the frame count can never bring the cut frame back.
            kept = grid.slot(cut_index, grid.origin(new_start))
            candidate = round(new_start+(kept-.5)/FPS, 4)
            if candidate-new_start >= MIN_LENGTH and candidate < end:
                new_end = candidate
                notes.append(f'end moved {end:.2f} -> {new_end:.2f} s onto the camera cut '
                             f'({frames} frame(s) of the next shot dropped).')
    return new_start, new_end, notes


def snap_clip_to_cuts(source, clip, cache, config, words, grid):
    """The clip with its edges on camera cuts where that is safe, plus notes.
    Runs (or reuses) the clip's speaker analysis, which holds its exact cut frames."""
    from shorts import probe
    from speaker_focus import analyze
    from review_apply import shift_clip_timing
    stream = next(s for s in probe(source)['streams'] if s['codec_type'] == 'video')
    analysis = analyze(source, clip, cache, config, stream)
    start, end, notes = cut_edges(clip, analysis['shots'], analysis['frame_count'], words, grid)
    if (start, end) == (clip['start'], clip['end']):
        return clip, notes
    return shift_clip_timing(clip, start, end, grid), notes
