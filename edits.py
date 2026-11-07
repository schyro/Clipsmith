"""Music edits: a reviewed clip's dialogue up to its payoff, a drop, and a montage cut on the beat.

A plan's top-level `edits` block, keyed by clip id, describes each edit:

    "edits": {"03": {"track": "song.mp3", "payoff_word": 18234, "grade": "bw",
                     "transition": "white_flash", "montage": [{"start": 812.4}, ...],
                     "reason": "..."}}

Timeline of an edit (30 fps frames):
- dialogue: the rendered clip MP4 (its framing and captions as reviewed) from
  `dialogue_start` (default: the clip start) up to the drop frame D, which lies
  `drop_delay` after the payoff word ends;
- montage: one source shot per slot of `cut_beats` beats; cut k sits on frame
  round(D + k*interval*30), computed from absolute times so rounding never adds up;
- tail: `tail_seconds` of black while the music fades out.

The music's drop lands on frame D. Before it the track plays as a bed set
`bed_below_dialogue_db` under the dialogue's measured loudness (measured on the part
of the track that actually plays there) and dips a little under speech; from D on it
plays at full level and the montage shots' own sound is muted. Only a limiter runs on the final mix, so the drop
keeps its jump. A second file has the same picture with the dialogue only, for
platforms where the music is added in the app.

Files go to `<profile>/edits/`: `<stem>.edit.mp4`, `<stem>.edit-nomusic.mp4`,
`<stem>.edit.json` (timeline, music offset and the acceptance checks) and
`<stem>.edit.jpg` (cover).
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path

import copper
from frame_grid import FPS

EDIT_VERSION = 6
COPPER_VERSION = 7
CUT_BEATS = (.5, 1, 2, 4)
EDIT_KEYS = {'track', 'payoff_word', 'drop_delay', 'grade', 'transition', 'cut_beats', 'montage', 'reason',
             'approved', 'music_lufs', 'dialogue_start', 'effects'}
MIN_DIALOGUE = 3.0  # seconds of dialogue before the drop
SHOT_KEYS = {'start', 'center_x', 'slots', 'reason', 'flash'}
NON_RENDER_KEYS = {'reason', 'approved'}


def edit_settings(root: Path | None = None):
    if root is None:
        from shorts import ROOT
        root = ROOT
    return json.loads((Path(root)/'edit_settings.json').read_text(encoding='utf-8'))


def indexed_words(transcript):
    """Words in time order with the `index` that Review/<id>/words.json shows."""
    words = [w for s in transcript['segments'] for w in s.get('words', [])]
    words.sort(key=lambda w: w['start'])
    return [{**w, 'index': w.get('index', i)} for i, w in enumerate(words)]


def find_word(words, index):
    return next((w for w in words if w['index'] == index), None)


def choose_cut_beats(period, low, high):
    """Fewest beats per cut whose interval reaches `low`; the one closest to the range otherwise."""
    for beats in CUT_BEATS:
        if low <= beats*period <= high:
            return beats
    return min(CUT_BEATS, key=lambda b: min(abs(b*period-low), abs(b*period-high)))


def timeline(clip, edit, word, track, settings):
    """Frame-exact plan of one edit. `word` is the payoff word, `track` the sidecar values.
    Raises ValueError with a message the agent can act on."""
    duration = clip['end']-clip['start']
    clip_frames = round(duration*FPS)
    word_end = word['end']-clip['start']
    low, high = settings['drop_delay_range']
    delay = float(edit.get('drop_delay', settings['drop_delay_seconds']))
    if not low <= delay <= high:
        raise ValueError(f'drop_delay {delay} s is outside {low}-{high} s.')
    drop_frame = round((word_end+delay)*FPS)
    # Rounding to the frame grid must not push the drop outside the allowed delay.
    while drop_frame/FPS-word_end > high+1e-9:
        drop_frame -= 1
    while drop_frame/FPS-word_end < low-1e-9:
        drop_frame += 1
    if word_end <= 0:
        raise ValueError(f'the payoff word "{word["word"].strip()}" ends before the clip starts.')
    if drop_frame > clip_frames-1:
        raise ValueError(f'the drop at {clip["start"]+drop_frame/FPS:.2f} s (payoff word "{word["word"].strip()}" '
                         f'ends {word["end"]:.2f} s, +{delay:.2f} s) is after the clip end {clip["end"]:.2f} s; '
                         f'extend the clip or pick an earlier payoff word.')
    # The edit may open later than the clip, on a later line, to keep it short.
    start = float(edit.get('dialogue_start', clip['start']))
    head = round((start-clip['start'])*FPS)
    if head < 0:
        raise ValueError(f'dialogue_start {start:.2f} s is before the clip start {clip["start"]:.2f} s.')
    if (drop_frame-head)/FPS < MIN_DIALOGUE:
        raise ValueError(f'dialogue_start {start:.2f} s leaves {(drop_frame-head)/FPS:.2f} s of dialogue before the '
                         f'drop; start at {clip["start"]+(drop_frame-MIN_DIALOGUE*FPS)/FPS:.2f} s or earlier.')
    clip_drop_frame = drop_frame
    drop_frame -= head  # from here on, frames count from the edit's first frame
    period = 60/float(track['bpm'])
    cut_low, cut_high = settings['cut_interval_range']
    beats = edit.get('cut_beats') or choose_cut_beats(period, cut_low, cut_high)
    if beats not in CUT_BEATS:
        raise ValueError(f'cut_beats must be one of {", ".join(map(str, CUT_BEATS))}.')
    interval = beats*period
    shots = edit['montage']
    slots = [int(s.get('slots', 1)) for s in shots]
    total = sum(slots)
    montage = total*interval
    m_low, m_high = settings['montage_seconds_range']
    if not m_low-1e-6 <= montage <= m_high+1e-6:
        fewest, most = math.ceil(m_low/interval-1e-6), math.floor(m_high/interval+1e-6)
        raise ValueError(f'the montage lasts {montage:.2f} s ({total} slots of {interval:.3f} s); it must last '
                         f'{m_low:g}-{m_high:g} s, so use {fewest}-{most} slots.')
    bounds = [0]
    for s in slots:
        bounds.append(bounds[-1]+s)
    cuts = [drop_frame+round(b*interval*FPS) for b in bounds]
    frames = [b-a for a, b in zip(cuts[:-1], cuts[1:])]
    if min(frames) < 6:
        raise ValueError(f'a montage shot would last {min(frames)} frames; use cut_beats {beats*2:g} or more.')
    fx = effects(edit, settings)
    tail = round(fx.get('tail_seconds', settings['tail_seconds'])*FPS)
    total_frames = cuts[-1]+tail
    drop_at = drop_frame/FPS
    word_end -= head/FPS
    track_start = float(track['drop'])-drop_at          # track time at edit time 0 (negative: music starts later)
    needed = float(track['drop'])+(total_frames-drop_frame)/FPS
    if needed > float(track['duration'])+1e-6:
        raise ValueError(f'the track ends {needed-float(track["duration"]):.2f} s too early after its drop; '
                         f'shorten the montage or pick another track.')
    return {'clip_frames': clip_frames, 'head_frame': head, 'clip_drop_frame': clip_drop_frame,
            'drop_frame': drop_frame, 'drop_at': round(drop_at, 6),
            'payoff_end': round(word_end, 6), 'drop_delay': round(drop_at-word_end, 6),
            'beat': round(period, 6), 'cut_beats': beats, 'interval': round(interval, 6),
            'cut_frames': cuts, 'shot_frames': frames, 'tail_frames': tail, 'total_frames': total_frames,
            'montage_seconds': round((cuts[-1]-drop_frame)/FPS, 6), 'track_start': round(track_start, 6),
            'music_delay': round(max(0.0, -track_start), 6), 'track_from': round(max(0.0, track_start), 6)}


def structure_problems(plan, duration, settings):
    """Shape of the edits block (no transcript or media needed)."""
    edits = plan.get('edits')
    if edits is None:
        return []
    if not isinstance(edits, dict):
        return ['"edits" must be an object keyed by clip id.']
    ids = {c['id'] for c in plan['clips']}
    problems = []
    for key, edit in edits.items():
        label = f'Edit {key}'
        if key not in ids:
            problems.append(f'{label}: there is no clip {key} in the plan.')
            continue
        if not isinstance(edit, dict):
            problems.append(f'{label}: must be an object.')
            continue
        unknown = set(edit)-EDIT_KEYS
        if unknown:
            problems.append(f'{label}: unknown field(s) {", ".join(sorted(unknown))}.')
        if not isinstance(edit.get('track'), str) or not edit['track']:
            problems.append(f'{label}: "track" must name a file in Music/.')
        if not isinstance(edit.get('payoff_word'), (int, str)) or isinstance(edit.get('payoff_word'), bool):
            problems.append(f'{label}: "payoff_word" must be the word\'s index from words.json.')
        if edit.get('grade', 'none') not in settings['grades']:
            problems.append(f'{label}: grade must be one of {", ".join(settings["grades"])}.')
        if 'dialogue_start' in edit and not isinstance(edit['dialogue_start'], (int, float)):
            problems.append(f'{label}: "dialogue_start" must be a number of source seconds.')
        if edit.get('effects', 'soft') not in settings['effects']:
            problems.append(f'{label}: effects must be one of {", ".join(settings["effects"])}.')
        if edit.get('transition', 'cut') not in settings['transitions']:
            problems.append(f'{label}: transition must be one of {", ".join(settings["transitions"])}.')
        shots = edit.get('montage')
        if not isinstance(shots, list) or not shots:
            problems.append(f'{label}: "montage" must list at least one shot.')
            continue
        is_copper = edit.get('effects') == 'copper'
        keys = SHOT_KEYS | copper.SHOT_KEYS if is_copper else SHOT_KEYS
        for n, shot in enumerate(shots, 1):
            if not isinstance(shot, dict) or not isinstance(shot.get('start'), (int, float)):
                problems.append(f'{label}, shot {n}: needs a numeric "start" in source seconds.')
                continue
            if set(shot)-keys:
                problems.append(f'{label}, shot {n}: unknown field(s) {", ".join(sorted(set(shot)-keys))}.')
            if not 0 <= shot['start'] < duration:
                problems.append(f'{label}, shot {n}: start {shot["start"]} s is outside the source.')
            if 'center_x' in shot and not (isinstance(shot['center_x'], (int, float)) and 0 <= shot['center_x'] <= 1):
                problems.append(f'{label}, shot {n}: center_x must be between 0 and 1.')
            if shot.get('flash', 'none') not in ('none', 'white', 'black'):
                problems.append(f'{label}, shot {n}: flash must be white, black or none.')
            if not (isinstance(shot.get('slots', 1), int) and shot.get('slots', 1) >= 1):
                problems.append(f'{label}, shot {n}: slots must be a whole number of 1 or more.')
            if is_copper:
                problems.extend(f'{label}, shot {n}: {p}' for p in copper.shot_problems(shot, n-1))
    return problems


def edit_problems(plan, duration, transcript, music_folder=None, settings=None):
    """Every problem with the plan's edits that can be found without rendering."""
    from music import load_track
    settings = settings or edit_settings()
    problems = structure_problems(plan, duration, settings)
    if problems or not plan.get('edits'):
        return problems
    words = indexed_words(transcript)
    clips = {c['id']: c for c in plan['clips']}
    for key, edit in plan['edits'].items():
        clip = clips[key]
        label = f'Edit {key}'
        try:
            _, track = load_track(edit['track'], music_folder)
        except ValueError as exc:
            problems.append(f'{label}: {exc}')
            continue
        word = find_word(words, edit['payoff_word'])
        if word is None:
            problems.append(f'{label}: no word has index {edit["payoff_word"]} in words.json.')
            continue
        if not clip['start'] < word['end'] <= clip['end']:
            problems.append(f'{label}: the payoff word "{word["word"].strip()}" ({word["start"]:.2f}-{word["end"]:.2f} s) '
                            f'is not inside clip {key} ({clip["start"]:.2f}-{clip["end"]:.2f} s).')
            continue
        if 'dialogue_start' in edit:
            start = float(edit['dialogue_start'])
            if not clip['start'] <= start < word['start']:
                problems.append(f'{label}: dialogue_start {start:.2f} s must lie inside clip {key}, before the payoff word.')
                continue
            cut = next((w for w in words if w['end'] > w['start'] and w['start']+.02 < start < w['end']-.02), None)
            if cut:
                problems.append(f'{label}: dialogue_start {start:.2f} s cuts "{cut["word"].strip()}" '
                                f'({cut["start"]:.2f}-{cut["end"]:.2f} s); move it to {cut["start"]:.2f} s or earlier, '
                                f'or {cut["end"]:.2f} s or later.')
                continue
        try:
            plan_t = timeline(clip, edit, word, track, settings)
        except ValueError as exc:
            problems.append(f'{label}: {exc}')
            continue
        for n, (shot, frames) in enumerate(zip(edit['montage'], plan_t['shot_frames'])):
            seconds = shot_source_seconds(edit, shot, n, frames)
            if shot['start']+seconds > duration+1e-6:
                problems.append(f'{label}, shot {n+1}: {shot["start"]:.2f} s + {seconds:.2f} s runs past the source end.')
    return problems


def shot_source_seconds(edit, shot, index, frames):
    if edit.get('effects') == 'copper':
        return copper.source_seconds(shot, index, frames)
    return frames/FPS


def shot_clip(shot, seconds):
    """A pseudo clip covering one montage shot, for the speaker analysis."""
    start = float(shot['start'])
    return {'start': start, 'end': round(start+seconds, 6), 'id': 'montage', 'title': f'montage shot at {start:.2f} s'}


def montage_framing(source, edit, plan_t, cache, config, transcript):
    """Crop centre per montage shot and the problems found (a shot crossing a camera cut).
    The centre follows the visible (speaking) face like a clip does; `center_x` overrides it."""
    from speaker_focus import focus_plan
    centers, problems = [], []
    for n, (shot, frames) in enumerate(zip(edit['montage'], plan_t['shot_frames'])):
        seconds = shot_source_seconds(edit, shot, n, frames)
        pseudo = shot_clip(shot, seconds)
        framing = focus_plan(source, pseudo, cache, config, transcript, commands_path=False)
        if len(framing['shots']) > 1:
            cut = shot['start']+framing['shots'][1]['start_frame']/FPS
            problems.append(f'shot {n+1} ({shot["start"]:.2f} s, {frames} frames) crosses a camera cut at about '
                            f'{cut:.2f} s; start it at {cut+.04:.2f} s or later, or at {cut-seconds-.04:.2f} s '
                            f'or earlier.')
        if 'center_x' in shot:
            centers.append(float(shot['center_x']))
        else:
            values = sorted(f['center_x'] for f in framing['frames'])
            centers.append(round(values[len(values)//2], 4))
    return centers, problems


def crop_x(center, scaled_width, width):
    return max(0, min(scaled_width-width, round((center*scaled_width-width/2)/2)*2))


def zoom_filter(frames, zoom=0.0, punch=0.0, punch_frames=3.0, shake_px=0.0):
    """Push-in over `frames` frames (`zoom` = final scale - 1), a punch that starts `punch`
    larger and settles over about `punch_frames` frames, and a shake that dies out with it.
    `perspective` maps an inner rectangle onto the whole frame with subpixel (cubic)
    interpolation, so the motion is smooth, the output keeps its size and, since the
    rectangle always lies inside the picture, the frame stays filled. Empty when nothing moves."""
    if not (zoom or punch or shake_px):
        return ''
    n = max(1, frames-1)
    # perspective counts output frames from 1 (`on` is 1 on the first frame); f is the frame index.
    f = '(on-1)'
    z = f'((1+{zoom:.5f}*{f}/{n})*(1+{punch:.5f}*exp(-{f}/{punch_frames:.3f})))'
    mx, my = f'(W*(1-1/{z})/2)', f'(H*(1-1/{z})/2)'
    if shake_px:
        decay = f'{shake_px:.3f}*exp(-{f}/{punch_frames:.3f})'
        dx = f'clip({decay}*sin({f}*2.1+1),-{mx},{mx})'
        dy = f'clip({decay}*cos({f}*2.7+1),-{my},{my})'
    else:
        dx = dy = '0'
    left, top = f'{mx}+{dx}', f'{my}+{dy}'
    right, bottom = f'W-{mx}+{dx}', f'H-{my}+{dy}'
    return (f"perspective=x0='{left}':y0='{top}':x1='{right}':y1='{top}':x2='{left}':y2='{bottom}':"
            f"x3='{right}':y3='{bottom}':interpolation=cubic:eval=frame")


def measure_levels(path, times, pre, settings):
    """(dark, bright) luma percentiles (0..1) of a few frames, after the `pre` filters.
    Levels are measured once per piece and applied as fixed numbers: a per-frame
    normalisation would pump at every camera cut inside the dialogue."""
    import numpy as np
    import subprocess
    look = settings['look']
    values = []
    for t in times:
        vf = (pre+',' if pre else '')+'scale=270:-2,format=gray'
        result = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{max(0.0, t):.3f}', '-i', str(path), '-frames:v', '1',
                                 '-vf', vf, '-f', 'rawvideo', '-'], capture_output=True)
        if not result.returncode and result.stdout:
            values.append(np.frombuffer(result.stdout, np.uint8))
    if not values:
        return 0.0, 1.0
    pixels = np.concatenate(values)
    return (round(float(np.percentile(pixels, look['low_percentile']))/255, 4),
            round(float(np.percentile(pixels, look['high_percentile']))/255, 4))


def look_chain(label, levels, settings, grade=None, glow=True):
    """The edit's HD look: levels stretched towards the measured dark and bright points
    (linked channels, so colours keep their balance), a contrast curve that also lifts the
    mid-tones of dark footage, the colour grade, highlight glow and luma detail.
    Detail runs after the glow so the bloom does not wash out fine edges."""
    look = settings['look']
    strength = look['levels_strength']
    low, high = levels
    lo, hi = round(strength*low, 4), round(1-strength*(1-high), 4)
    if hi-lo < .2:  # a nearly flat picture (a fade, a black frame): leave its levels alone
        lo, hi = 0.0, 1.0
    chain = (f',colorlevels=rimin={lo}:gimin={lo}:bimin={lo}:rimax={hi}:gimax={hi}:bimax={hi}'
             f",curves=all='{look['curve']}'")
    if grade and grade != 'null':
        chain += ','+grade
    if glow and look.get('glow'):
        chain += (f',format=yuv420p,split[{label}g0][{label}g1];'
                  f'[{label}g1]lutyuv=y=\'clip((val-{look["glow_from"]})*2.5,0,255)\':u=128:v=128,gblur=sigma=22[{label}g2];'
                  f'[{label}g0][{label}g2]blend=c0_mode=screen:c0_opacity={look["glow"]:.3f}')
    return chain+detail_chain(settings)


def detail_chain(settings):
    """Restore local contrast and fine edges at output size without sharpening colour noise.

    A wider, gentle luma mask adds definition; a small mask restores resampling-softened
    edges. Keep CAS moderate: high strengths amplify grain in faces and dark footage.
    Explicit planar YUV makes plane 1 the luma plane regardless of the grade's format.
    """
    look = settings['look']
    clarity, detail, sharpen = (look.get(k, 0) for k in ('clarity', 'detail', 'sharpen'))
    if not any((clarity, detail, sharpen)):
        return ''
    chain = ',format=yuv444p'
    if clarity:
        chain += f',unsharp=13:13:{clarity}:3:3:0'
    if detail:
        chain += f',unsharp=5:5:{detail}:3:3:0'
    if sharpen:
        chain += f',cas=strength={sharpen}:planes=1'
    return chain


def crop_filter(size, x, config):
    return (f'scale={size["scaled_width"]}:{size["scaled_height"]}:flags=lanczos,'
            f'crop={config["width"]}:{config["height"]}:{x}:(ih-oh)/2')


def edit_levels(ctx, clip_mp4, config, settings):
    """Levels of the dialogue (nine frames across it, captions included) and of every
    montage shot (its middle frame, cropped as rendered)."""
    plan_t = ctx.plan_t
    head, end = plan_t['head_frame']/FPS, plan_t['clip_drop_frame']/FPS
    times = [head+.2+(end-head-.4)*i/8 for i in range(9)]
    dialogue = (0, 1) if ctx.edit.get('effects') == 'copper' else measure_levels(clip_mp4, times, '', settings)
    shots = []
    for index, (shot, center, frames) in enumerate(zip(ctx.edit['montage'], ctx.centers, plan_t['shot_frames'])):
        pre = crop_filter(ctx.size, crop_x(center, ctx.size['scaled_width'], config['width']), config)
        seconds = shot_source_seconds(ctx.edit, shot, index, frames)
        shots.append(measure_levels(ctx.source, [shot['start']+seconds/2], pre, settings))
    return {'dialogue': dialogue, 'shots': shots}


def shot_flash(k, shot, edit, settings, fx):
    """(colour, frames) of the flash a montage shot opens with, or None. Shot 0 opens on the
    drop with the edit's transition; later shots follow the effect profile's pattern
    (a flash every `flash_every` cuts, a one-frame black strobe every `strobe_every`),
    unless the shot names its own `flash`."""
    drop_colour = settings['transitions'][edit.get('transition', 'cut')]
    if k == 0:
        return (drop_colour, settings['flash_frames']) if drop_colour else None
    named = shot.get('flash')
    if named == 'none':
        return None
    if named in ('white', 'black'):
        return named, fx.get('flash_frames', 2) if named == 'white' else 1
    if fx.get('flash_every') and k % fx['flash_every'] == 0:
        return drop_colour or 'white', fx.get('flash_frames', 2)
    if fx.get('strobe_every') and k % fx['strobe_every'] == 0:
        return 'black', 1
    return None


def shot_filter(index, label, size, x, config, grade, frames, flash, fx, levels, settings,
                *, staged=False, cool_background=False, hold=0):
    chain = f'[{index}:v]setpts=PTS-STARTPTS'
    if not staged:
        chain += f',fps={FPS},{crop_filter(size, x, config)}'
    chain += f',trim=end_frame={frames},setpts=PTS-STARTPTS'
    zoom = zoom_filter(frames, fx.get('zoom', 0), fx.get('punch', 0), fx.get('punch_frames', 3), fx.get('shake_px', 0))
    if zoom:
        chain += ','+zoom
    chain += look_chain(label, levels, settings, grade)
    if 'flash_opacity' in fx:
        chain += ',format=yuv420p'
    if cool_background:
        blue, red = 'cb(X,Y)', 'cr(X,Y)'
        weight = f'clip(({blue}-140)/35,0,1)*clip(({blue}-{red}-8)/25,0,1)'
        chain += (f",geq=lum='lum(X,Y)':cb='{blue}+(128-{blue})*.16*{weight}':"
                  f"cr='{red}-16*{weight}':interpolation=nearest")
    if flash:
        colour, count = flash
        if 'flash_opacity' in fx:
            target = 235 if colour == 'white' else 16
            for n, opacity in enumerate(fx['flash_opacity']):
                chain += (f",lutyuv=y='val+({target}-val)*{opacity}':u='val+(128-val)*{opacity}':"
                          f"v='val+(128-val)*{opacity}':enable='eq(n,{n})'")
        else:
            chain += f',fade=t=in:start_frame=0:nb_frames={count}:color={colour}'
    if hold:
        chain += (f",lutyuv=y='val+6*clip((val-16)/32,0,1)*clip((122-val)/72,0,1)',"
                  f'trim=end_frame={frames-hold},tpad=stop_mode=clone:stop={hold}')
    return chain+f',format=yuv420p,setsar=1[{label}]'


def effects(edit, settings):
    name = edit.get('effects', 'soft')
    return settings['effects'][name]


def montage_filter(index, label, k, plan_t, center, size, config, edit, settings, level, staged):
    shot = edit['montage'][k]
    fx = effects(edit, settings)
    grade = settings['grades'][edit.get('grade', 'none')]
    if edit.get('effects') == 'copper':
        grade = grade if 'grade' in edit else fx['grade']
        settings = {**settings, 'look': {**settings['look'], **fx['look']}}
    frames = plan_t['shot_frames'][k]
    hold = min(frames-2, max(0, fx.get('final_hold_frames', 1)-1)) if k == len(edit['montage'])-1 else 0
    x = crop_x(center, size['scaled_width'], config['width'])
    return shot_filter(index, label, size, x, config, grade, frames, shot_flash(k, shot, edit, settings, fx),
                       fx, level, settings, staged=staged, cool_background=shot.get('cool_background', False), hold=hold)


def shot_input(source, shot, index, frames, center, size, config, edit, settings, folder):
    if edit.get('effects') == 'copper':
        stage = Path(folder)/f'shot-{index}.nut'
        x = crop_x(center, size['scaled_width'], config['width'])
        if copper.stage_shot(source, shot, index, frames, size, x, config, effects(edit, settings), stage):
            return ['-i', stage], True
    return ['-ss', f'{shot["start"]:.6f}', '-t', f'{frames/FPS+.5:.6f}', '-i', source], False


def video_graph(plan_t, centers, size, config, edit, settings, levels, first_shot_input=1, staged=()):
    """`levels` comes from edit_levels. The dialogue gets the HD look without the colour
    grade or glow (its own colours stay); the montage gets all of it."""
    fx = effects(edit, settings)
    dialogue_frames = plan_t['drop_frame']
    dialogue_zoom = zoom_filter(dialogue_frames, fx.get('dialogue_zoom', 0))
    dialogue_look = ''
    if edit.get('effects') != 'copper' and settings['look'].get('dialogue'):
        dialogue_look = look_chain('d', levels['dialogue'], settings, glow=False)
    parts = [f'[0:v]trim=start_frame={plan_t["head_frame"]}:end_frame={plan_t["clip_drop_frame"]},'
             f'setpts=PTS-STARTPTS{"," + dialogue_zoom if dialogue_zoom else ""}{dialogue_look},format=yuv420p,setsar=1[d]']
    labels = ['[d]']
    for k, center in enumerate(centers):
        parts.append(montage_filter(first_shot_input+k, f's{k}', k, plan_t, center, size, config, edit, settings,
                                    levels['shots'][k], k in staged))
        labels.append(f'[s{k}]')
    parts.append(f'color=c=black:s={config["width"]}x{config["height"]}:r={FPS},trim=end_frame={plan_t["tail_frames"]},'
                 f'setpts=PTS-STARTPTS,format=yuv420p,setsar=1[t]')
    labels.append('[t]')
    parts.append(''.join(labels)+f'concat=n={len(labels)}:v=1:a=0,settb=1/{FPS},setpts=N,format=yuv420p[v]')
    return ';'.join(parts)


def dialogue_chain(plan_t, settings, label_in='[0:a]', out='[dlg]', continuous=False):
    head = plan_t['head_frame']/FPS
    drop = plan_t['drop_at']
    fade = min(settings['dialogue_fade_ms']/1000, drop/2)
    total = plan_t['total_frames']/FPS
    fade_in = f'afade=t=in:st=0:d={fade:.6f},' if head else ''
    # AAC inputs may contain real timestamp gaps. Fill them before the trim, so the
    # music bed and its drop stay on the picture clock even if dialogue has a gap.
    clock = 'aresample=async=1:min_hard_comp=0.001:first_pts=0,asetpts=N/SR/TB,' if continuous else ''
    return (f'{label_in}aformat=sample_rates=48000:channel_layouts=stereo,{clock}'
            f'atrim=start={head:.6f}:end={head+drop:.6f},asetpts=PTS-STARTPTS,{fade_in}'
            f'afade=t=out:st={drop-fade:.6f}:d={fade:.6f},apad=whole_dur={total:.6f},atrim=duration={total:.6f}{out}')


def music_gains(edit, settings, track_lufs, bed_lufs, dialogue_lufs):
    """(bed gain, full gain) in dB. The full level brings the part after the drop to
    `music_lufs`; the bed brings the part of the track that plays under the dialogue to
    `bed_below_dialogue_db` under the dialogue's own loudness. Without a measurable bed
    the bed keeps the full gain minus the same amount."""
    full_db = float(edit.get('music_lufs', settings['music_lufs']))-track_lufs
    below = settings['bed_below_dialogue_db']
    if bed_lufs is None or dialogue_lufs is None:
        return full_db-below, full_db
    return dialogue_lufs-below-bed_lufs, full_db


def music_graph(plan_t, edit, settings, gains, music_input):
    total = plan_t['total_frames']/FPS
    drop = plan_t['drop_at']
    delay = plan_t['music_delay']
    bed_db, full_db = gains
    full = 10**(full_db/20)
    bed = 10**(bed_db/20)
    ramp = .03
    fade_in = min(settings['music_fade_in_seconds'], max(.01, drop-delay))
    fade_out = settings['music_fade_out_seconds']
    duck = settings['duck']
    length = total-delay
    music = (f'[{music_input}:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=duration={length:.6f},'
             f'asetpts=PTS-STARTPTS')
    if delay > 0:
        ms = round(delay*1000)
        music += f',adelay={ms}|{ms}'
    is_copper = edit.get('effects') == 'copper'
    fx = effects(edit, settings)
    block = 64 if is_copper else 256
    music += (f',asetnsamples=n={block},volume=volume=\'{bed:.6f}+({full:.6f}-{bed:.6f})*clip((t-{drop-ramp:.6f})/{ramp},0,1)\''
              f':eval=frame,afade=t=in:st={delay:.6f}:d={fade_in:.6f},'
              f'afade=t=out:st={total-fade_out:.6f}:d={fade_out:.6f},apad=whole_dur={total:.6f},'
             f'atrim=duration={total:.6f}')
    dip = min(fx.get('bed_dip_seconds', 0), plan_t['drop_delay']-.01)
    if dip > 0 and delay < drop:
        depth = 1-10**(-fx['bed_dip_db']/20)
        music += (f",volume='1-{depth:.8f}*clip((t-{drop-dip:.6f})/.024,0,1)"
                  f"*clip(({drop:.6f}-t)/.018,0,1)':eval=frame")
    music += '[mus]'
    return ';'.join([
        dialogue_chain(plan_t, settings, out='[dlgall]', continuous=is_copper),
        '[dlgall]asplit=2[dlg][key]',
        music,
        f'[mus][key]sidechaincompress=threshold={duck["threshold"]}:ratio={duck["ratio"]}:attack={duck["attack"]}:'
        f'release={duck["release"]}[duck]',
        f'[dlg][duck]amix=inputs=2:normalize=0:duration=first,'
        f'alimiter=limit={settings["limiter"]}:level=0:latency=1,atrim=duration={total:.6f}[a]',
    ])


def nomusic_graph(plan_t, settings, edit=None):
    total = plan_t['total_frames']/FPS
    return dialogue_chain(plan_t, settings, out='[dlg]', continuous=(edit or {}).get('effects') == 'copper')+(
        f';[dlg]alimiter=limit={settings["limiter"]}:level=0:latency=1,atrim=duration={total:.6f}[a]')


def edit_signature(edit, clip_signature, track_path, track, centers, config, settings, plan_t):
    from music import file_hash
    is_copper = edit.get('effects') == 'copper'
    if not is_copper:
        settings = {**settings, 'effects': {k: v for k, v in settings['effects'].items() if k != 'copper'}}
    return hashlib.sha256(json.dumps({
        'edit': {k: v for k, v in edit.items() if k not in NON_RENDER_KEYS}, 'clip': clip_signature,
        'timeline': plan_t,
        'track': file_hash(track_path), 'timing': {k: track[k] for k in ('bpm', 'drop', 'duration')},
        'centers': centers, 'size': [config['width'], config['height']], 'settings': settings,
        'version': COPPER_VERSION if is_copper else EDIT_VERSION}, sort_keys=True).encode()).hexdigest()


def edit_paths(folder: Path, stem):
    return {'music': folder/f'{stem}.edit.mp4', 'nomusic': folder/f'{stem}.edit-nomusic.mp4',
            'info': folder/f'{stem}.edit.json', 'done': folder/f'{stem}.edit.done.json',
            'cover': folder/f'{stem}.edit.jpg'}


EDIT_SUFFIXES = ('.edit.mp4', '.edit-nomusic.mp4', '.edit.json', '.edit.done.json', '.edit.jpg')


def encode_args():
    from shorts import nvenc_available
    if nvenc_available():
        return ['-c:v', 'h264_nvenc', '-preset', 'p6', '-tune', 'hq', '-rc', 'vbr', '-cq', '16', '-b:v', '0']
    return ['-c:v', 'libx264', '-preset', 'slow', '-crf', '17']


# ----- acceptance checks -----

def decode_gray(path, width=96, height=120):
    import numpy as np
    import subprocess
    result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', f'scale={width}:{height}',
                             '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace')[-2000:])
    return np.frombuffer(result.stdout, np.uint8).reshape(-1, height, width, 3)


def frame_changes(frames):
    """Mean absolute grey change from the previous frame (0..1); frame 0 gets 0."""
    import numpy as np
    gray = frames.astype(np.float32).mean(axis=3)
    changes = np.zeros(len(frames))
    changes[1:] = np.abs(np.diff(gray, axis=0)).mean(axis=(1, 2))/255
    return changes


def cut_check(frames, plan_t, settings):
    """Every montage cut (and the cut to the black tail) must change the picture on its own
    grid frame: around an expected cut, the frame-to-frame change starts on that frame.
    A cut between two very similar shots shows no clear peak and is listed as unclear.
    Works on colour and black-and-white pictures alike."""
    import numpy as np
    changes = frame_changes(frames)
    expected = plan_t['cut_frames'][1:]   # cut 0 is the drop, under the flash
    inside = changes[plan_t['drop_frame']+settings['flash_frames']+1:expected[-1]]
    floor = max(.03, 3*float(np.median(inside))) if len(inside) else .03
    offsets, unclear = {}, []
    for e in expected:
        lo, hi = max(1, e-2), min(len(changes), e+3)
        window = changes[lo:hi]
        if not len(window) or window.max() < floor:
            unclear.append(e)
            continue
        # The cut is where the change starts: the first frame reaching half of the window's
        # largest change. A flash or strobe changes the picture again right after the cut,
        # sometimes by more than the cut itself, so the largest change is not the cut frame.
        onset = lo+int(np.argmax(window >= .5*window.max()))
        offsets[str(e)] = onset-e
    off = [int(e) for e, o in offsets.items() if abs(o) > 1]
    return {'expected': plan_t['cut_frames'], 'offsets': offsets, 'seen': len(offsets),
            'exact': sum(1 for o in offsets.values() if o == 0), 'of': len(expected),
            'unclear': unclear, 'off_grid': off, 'passed': not off}


def rms_db(samples):
    import numpy as np
    return float(20*np.log10(np.sqrt(np.mean(samples.astype(np.float64)**2))+1e-9))


def decode_audio(path, rate=48000, channels=1):
    """Samples as float32; with channels=2 they come interleaved. FFmpeg's mono downmix
    adds the channels at 0.707 each, so measure peaks on the stereo samples."""
    import numpy as np
    import subprocess
    result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-ac', str(channels), '-ar', str(rate),
                             '-f', 'f32le', '-'], capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace')[-2000:])
    return np.frombuffer(result.stdout, np.float32)


def bed_check(music_file, dry_file, plan_t, settings, rate=48000):
    """Level of the music under the dialogue, relative to the dialogue (RMS dB): the
    music file minus the no-music file, over the dialogue part away from the fades."""
    low, high = settings['bed_check_range_db']
    a = int((plan_t['music_delay']+settings['music_fade_in_seconds']+.1)*rate)
    b = int((plan_t['drop_at']-.2)*rate)
    if b-a < rate:
        return {'level': None, 'range': [low, high], 'passed': True, 'note': 'less than a second of bed'}
    wet, dry = decode_audio(music_file, rate)[a:b], decode_audio(dry_file, rate)[a:b]
    level = round(rms_db(wet-dry)-rms_db(dry), 2)
    return {'level': level, 'range': [low, high], 'passed': low <= level <= high}


def drop_gain(path, plan_t, rate=48000):
    audio = decode_audio(path, rate)
    d = int(plan_t['drop_at']*rate)
    before = rms_db(audio[max(0, d-rate):d])
    after = rms_db(audio[d+int(.05*rate):d+int(1.05*rate)])
    return round(before, 2), round(after, 2), round(after-before, 2)


# ----- previews and rendering -----

def montage_sheet(source, edit, plan_t, centers, size, config, settings, dest: Path, title=''):
    """One graded, cropped frame from the middle of each montage shot, with the look as rendered."""
    if edit.get('effects') == 'copper':
        return copper_montage_sheet(source, edit, plan_t, centers, size, config, settings, dest, title)
    from PIL import Image, ImageDraw, ImageFont
    from shorts import ROOT, run
    import tempfile
    grade = settings['grades'][edit.get('grade', 'none')]
    tile_w, tile_h = 216, 270
    count = len(centers)
    cols = min(6, count)
    rows = math.ceil(count/cols)
    sheet = Image.new('RGB', (cols*tile_w, rows*(tile_h+28)+32), '#10151d')
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(str(ROOT/'assets/fonts/Dosis-Bold.ttf'), 18)
    draw.text((8, 6), title, font=font, fill='white')
    with tempfile.TemporaryDirectory() as tmp:
        for k, (shot, center, frames) in enumerate(zip(edit['montage'], centers, plan_t['shot_frames'])):
            x = crop_x(center, size['scaled_width'], config['width'])
            out = Path(tmp)/f'{k}.jpg'
            at = shot['start']+frames/FPS/2
            pre = crop_filter(size, x, config)
            look = look_chain('m', measure_levels(source, [at], pre, settings), settings, grade)
            run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{at:.3f}', '-i', source, '-frames:v', '1',
                 '-filter_complex', f'[0:v]{pre}{look},scale={tile_w}:{tile_h}[o]', '-map', '[o]', '-update', '1', out])
            col, row = k % cols, k//cols
            sheet.paste(Image.open(out), (col*tile_w, 32+row*(tile_h+28)))
            draw.text((col*tile_w+6, 32+row*(tile_h+28)+tile_h+2), f'{k+1}: {shot["start"]:.2f}s {frames}f',
                      font=font, fill='white')
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest, quality=90)
    return dest


def copper_montage_sheet(source, edit, plan_t, centers, size, config, settings, dest, title):
    """The first, effect and last frames of each shot through the same graph as export."""
    import subprocess
    import tempfile
    import numpy as np
    images = []
    with tempfile.TemporaryDirectory(prefix='clipsmith-preview-') as folder:
        for k, (shot, center, count) in enumerate(zip(edit['montage'], centers, plan_t['shot_frames'])):
            inputs, staged = shot_input(source, shot, k, count, center, size, config, edit, settings, folder)
            x = crop_x(center, size['scaled_width'], config['width'])
            pre = crop_filter(size, x, config)
            seconds = shot_source_seconds(edit, shot, k, count)
            levels = measure_levels(source, [shot['start']+seconds/2], pre, settings)
            graph = montage_filter(0, 'shot', k, plan_t, center, size, config, edit, settings, levels, staged)
            active = min(count-2, 5) if copper.motion(shot, k) == 'push' else count//2
            picks = (0, active, count-1)
            select = '+'.join(f'eq(n,{n})' for n in picks)
            graph += f";[shot]select='{select}',scale=216:270[out]"
            raw = subprocess.check_output([str(arg) for arg in ('ffmpeg', '-v', 'error', *inputs,
                                          '-filter_complex', graph, '-map', '[out]', '-fps_mode', 'passthrough',
                                          '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-')])
            frames = np.frombuffer(raw, np.uint8).reshape(-1, 270, 216, 3)
            if len(frames) != len(picks):
                raise ValueError(f'Copper shot {k+1} could not produce all preview frames.')
            images.extend((frame, f'Shot {k+1} | {name} | {shot["start"]:.2f}s')
                          for frame, name in zip(frames, ('entry', 'motion', 'exit')))
    return frame_sheet(images, dest, title, 3)


def qa_sheet(path, plan_t, dest: Path, label, detailed=False):
    """Last dialogue frame, the drop, frames from every montage shot and the tail."""
    frames = decode_gray(path, 216, 270)
    picks = [(0, 'first'), (plan_t['drop_frame']-1, 'last line'), (plan_t['drop_frame'], 'drop')]
    if detailed:
        picks.append((plan_t['drop_frame']+2, 'drop +2'))
    for k, (a, b) in enumerate(zip(plan_t['cut_frames'][:-1], plan_t['cut_frames'][1:]), 1):
        if detailed:
            active = min(a+5, b-1) if k == 1 else (a+b)//2
            picks.extend(((a, f'shot {k} entry'), (active, f'shot {k} motion'), (b-1, f'shot {k} exit')))
        else:
            picks.append(((a+b)//2, f'shot {k}'))
    picks.append((plan_t['cut_frames'][-1], 'tail'))
    return frame_sheet([(frames[min(index, len(frames)-1)], f'{name} f{index}') for index, name in picks], dest, label)


def frame_sheet(images, dest, label, columns=6):
    from PIL import Image, ImageDraw, ImageFont
    from shorts import ROOT
    cols = min(columns, len(images))
    rows = math.ceil(len(images)/cols)
    sheet = Image.new('RGB', (cols*216, rows*298+32), '#10151d')
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(str(ROOT/'assets/fonts/Dosis-Bold.ttf'), 18)
    draw.text((8, 6), label, font=font, fill='white')
    for n, (frame, name) in enumerate(images):
        col, row = n % cols, n//cols
        sheet.paste(Image.fromarray(frame), (col*216, 32+row*298))
        draw.text((col*216+6, 32+row*298+272), name, font=font, fill='white')
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest, quality=90)
    return dest


class EditContext:
    """Everything one edit needs, resolved once for preview and render."""

    def __init__(self, source, plan, clip, edit, cache, config, transcript, settings, music_folder=None):
        from music import load_track
        from shorts import probe
        from speaker_focus import geometry
        self.source, self.clip, self.edit = source, clip, edit
        self.track_path, self.track = load_track(edit['track'], music_folder)
        word = find_word(indexed_words(transcript), edit['payoff_word'])
        if word is None:
            raise ValueError(f'Edit {clip["id"]}: no word has index {edit["payoff_word"]}.')
        self.word = word
        self.plan_t = timeline(clip, edit, word, self.track, settings)
        stream = next(s for s in probe(source)['streams'] if s['codec_type'] == 'video')
        self.size = geometry(stream, config)
        self.centers, self.problems = montage_framing(source, edit, self.plan_t, cache, config, transcript)


def render_edit(ctx: EditContext, clip_mp4: Path, folder: Path, stem, config, settings, force=False, out=print):
    """Render both files of one edit and run its acceptance checks; returns the info record."""
    from shorts import run, verify, write_json
    paths = edit_paths(folder, stem)
    marker = clip_mp4.with_suffix('.done.json')
    if not marker.exists():
        raise ValueError(f'Edit {ctx.clip["id"]}: its clip has not been rendered ({clip_mp4.name}).')
    clip_signature = json.loads(marker.read_text(encoding='utf-8'))['signature']
    digest = edit_signature(ctx.edit, clip_signature, ctx.track_path, ctx.track, ctx.centers, config, settings,
                            ctx.plan_t)
    total = ctx.plan_t['total_frames']/FPS
    if paths['info'].exists() and paths['music'].exists() and paths['nomusic'].exists() and paths['done'].exists():
        done = json.loads(paths['done'].read_text(encoding='utf-8'))
        if ctx.edit.get('approved') and not force:
            out(f'Approved and locked: {paths["music"].name}')
            return json.loads(paths['info'].read_text(encoding='utf-8'))
        if done.get('signature') == digest and not force:
            for p in (paths['music'], paths['nomusic']):
                verify(p, total, config)
            out(f'Already complete: {paths["music"].name}')
            return json.loads(paths['info'].read_text(encoding='utf-8'))
    if ctx.problems:
        raise ValueError(f'Edit {ctx.clip["id"]}: '+' '.join(ctx.problems))
    from music import loudness
    plan_t = ctx.plan_t
    after = float(ctx.track['duration'])-float(ctx.track['drop'])
    track_lufs = loudness(ctx.track_path, float(ctx.track['drop']), min(10.0, after))
    # Loudness of what plays together before the drop: the dialogue and the part of the track under it.
    bed_lufs = dialogue_lufs = None
    bed_length = float(ctx.track['drop'])-plan_t['track_from']
    if bed_length >= 1.0:
        try:
            bed_lufs = loudness(ctx.track_path, plan_t['track_from'], bed_length)
            dialogue_lufs = loudness(clip_mp4, plan_t['head_frame']/FPS, plan_t['drop_at'])
        except RuntimeError:
            bed_lufs = dialogue_lufs = None
    gains = music_gains(ctx.edit, settings, track_lufs, bed_lufs, dialogue_lufs)
    music_input = 1+len(plan_t['shot_frames'])
    levels = edit_levels(ctx, clip_mp4, config, settings)
    folder.mkdir(parents=True, exist_ok=True)
    temp = paths['music'].with_name(stem+'.edit.partial.mp4')
    common = ['-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-pix_fmt', 'yuv420p', '-r', str(FPS),
              '-movflags', '+faststart', '-map_metadata', '-1']
    # A strong montage can exceed Windows' command-line limit. Keep the graph in
    # a separate UTF-8 file for the duration of FFmpeg's synchronous render.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='clipsmith-edit-') as tmp:
        inputs, staged = ['-i', clip_mp4], set()
        for k, (shot, frames, center) in enumerate(zip(ctx.edit['montage'], plan_t['shot_frames'], ctx.centers)):
            args, is_staged = shot_input(ctx.source, shot, k, frames, center, ctx.size, config, ctx.edit, settings, tmp)
            inputs += args
            if is_staged:
                staged.add(k)
        inputs += ['-ss', f'{plan_t["track_from"]:.6f}', '-i', ctx.track_path]
        graph = video_graph(plan_t, ctx.centers, ctx.size, config, ctx.edit, settings, levels, staged=staged)
        graph += ';'+music_graph(plan_t, ctx.edit, settings, gains, music_input)
        graph_path = Path(tmp)/'video.ffgraph'
        graph_path.write_text(graph, encoding='utf-8')
        run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', *inputs,
             '-filter_complex_script', graph_path, '-map', '[v]', '-map', '[a]',
             *common, *encode_args(), temp])
    run(['ffmpeg', '-v', 'error', '-xerror', '-i', temp, '-f', 'null', '-'])
    temp_dry = paths['nomusic'].with_name(stem+'.edit-nomusic.partial.mp4')
    run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', clip_mp4, '-i', temp,
         '-filter_complex', nomusic_graph(plan_t, settings, ctx.edit), '-map', '1:v', '-map', '[a]', '-c:v', 'copy',
         '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-movflags', '+faststart', '-map_metadata', '-1', temp_dry])
    for p in (temp, temp_dry):
        verify(p, total, config)
    frames = decode_gray(temp)
    checks = {'frames': {'expected': plan_t['total_frames'], 'found': len(frames),
                         'passed': abs(len(frames)-plan_t['total_frames']) <= 1}}
    checks['cuts'] = cut_check(frames, plan_t, settings)
    before, after_db, gain = drop_gain(temp, plan_t)
    checks['drop_gain_db'] = {'before': before, 'after': after_db, 'gain': gain,
                              'min': settings['min_drop_gain_db'], 'passed': gain >= settings['min_drop_gain_db']}
    checks['bed_db'] = bed_check(temp, temp_dry, plan_t, settings)
    low, high = settings['drop_delay_range']
    checks['drop_delay'] = {'seconds': plan_t['drop_delay'], 'range': [low, high],
                            'passed': low-1e-6 <= plan_t['drop_delay'] <= high+1e-6}
    failed = [k for k, v in checks.items() if not v['passed']]
    info = {'id': ctx.clip['id'], 'title': ctx.clip['title'], 'file': paths['music'].name,
            'nomusic_file': paths['nomusic'].name, 'cover': paths['cover'].name,
            'duration': round(total, 3), 'track': ctx.track_path.name, 'mood': ctx.track.get('mood', ''),
            'bpm': ctx.track['bpm'], 'track_drop': ctx.track['drop'], 'track_lufs': round(track_lufs, 2),
            'bed_lufs': None if bed_lufs is None else round(bed_lufs, 2),
            'dialogue_lufs': None if dialogue_lufs is None else round(dialogue_lufs, 2),
            'gains_db': [round(g, 2) for g in gains],
            'grade': ctx.edit.get('grade', 'copper' if ctx.edit.get('effects') == 'copper' else 'none'),
            'transition': ctx.edit.get('transition', 'cut'),
            'effects': ctx.edit.get('effects', 'soft'), 'levels': levels,
            'payoff_word': ctx.edit['payoff_word'], 'payoff_text': ctx.word['word'].strip(),
            'centers': ctx.centers, 'timeline': plan_t, 'checks': checks,
            'music_hint': (f'In the app, start {ctx.track_path.name} at {max(0.0, plan_t["track_start"]):.2f} s'
                           + (f' and {plan_t["music_delay"]:.2f} s into the video' if plan_t['music_delay'] else '')
                           + f' so its drop lands on the cut at {plan_t["drop_at"]:.2f} s.')}
    if failed:
        write_json(paths['info'].with_name(stem+'.edit.failed.json'), info)
        details = []
        if 'cuts' in failed:
            details.append(f'cuts more than one frame off the beat grid at frames {checks["cuts"]["off_grid"]}')
        if 'drop_gain_db' in failed:
            details.append(f'the drop is only {gain:+.1f} dB louder than the second before it '
                           f'(minimum {settings["min_drop_gain_db"]:+.1f} dB); raise "music_lufs" for this edit')
        if 'frames' in failed:
            details.append(f'{len(frames)} frames, expected {plan_t["total_frames"]}')
        if 'bed_db' in failed:
            details.append(f'the music under the dialogue sits {-checks["bed_db"]["level"]:.1f} dB under it '
                           f'(allowed {-checks["bed_db"]["range"][1]:g}-{-checks["bed_db"]["range"][0]:g} dB)')
        if 'drop_delay' in failed:
            details.append(f'drop {plan_t["drop_delay"]:.3f} s after the payoff word')
        raise ValueError(f'Edit {ctx.clip["id"]} failed its checks: '+'; '.join(details)+'.')
    temp.replace(paths['music'])
    temp_dry.replace(paths['nomusic'])
    cover_frame = min(plan_t['total_frames']-1, (plan_t['cut_frames'][0]+plan_t['cut_frames'][1])//2)
    run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{cover_frame/FPS:.4f}', '-i', paths['music'], '-frames:v', '1',
         '-update', '1', paths['cover']])
    write_json(paths['info'], info)
    write_json(paths['done'], {'signature': digest, 'duration': total})
    stale = paths['info'].with_name(stem+'.edit.failed.json')
    if stale.exists():
        stale.unlink()
    cuts = checks['cuts']
    out(f'Edit rendered: {paths["music"].name} ({total:.1f} s; cuts on their grid frame {cuts["exact"]} of '
        f'{cuts["seen"]} seen ({cuts["of"]} cuts), drop {gain:+.1f} dB)')
    return info


def edits_folder(output: Path):
    return Path(output)/'edits'


def tidy_edits(folder: Path, keep_stems):
    """Move files of edits that are no longer in the plan into edits/rejected/."""
    if not folder.exists():
        return
    for path in sorted(folder.glob('*.edit.mp4')):
        stem = path.name[:-len('.edit.mp4')]
        if stem in keep_stems or stem.endswith('.partial'):
            continue
        archive = folder/'rejected'
        archive.mkdir(exist_ok=True)
        for suffix in EDIT_SUFFIXES:
            f = folder/(stem+suffix)
            if f.exists():
                f.replace(archive/f.name)
        print(f'Moved to edits/rejected/: {stem} (no longer in the plan)', flush=True)


def contexts(source, plan, cache, config, transcript, settings, music_folder=None):
    """(clip id, EditContext or the ValueError that stopped it) per edit, so one bad edit
    never stops the others."""
    clips = {c['id']: c for c in plan['clips']}
    for key, edit in (plan.get('edits') or {}).items():
        try:
            if key not in clips:
                raise ValueError(f'there is no clip {key} in the plan.')
            yield key, EditContext(source, plan, clips[key], edit, cache, config, transcript, settings, music_folder)
        except ValueError as exc:
            yield key, exc


def preview_edits(source, plan, cache, config, transcript, settings=None, music_folder=None):
    """Montage sheets for review before rendering; returns (sheets, problems)."""
    from shorts import ROOT, clip_stem
    settings = settings or edit_settings()
    sheets, problems = [], []
    for key, ctx in contexts(source, plan, cache, config, transcript, settings, music_folder):
        if isinstance(ctx, ValueError):
            problems.append(f'Edit {key}: {ctx}')
            continue
        problems += [f'Edit {key}: {p}' for p in ctx.problems]
        if ctx.problems and ctx.edit.get('effects') == 'copper':
            continue
        dest = ROOT/'Review'/plan['source_id']/'edits'/f'{clip_stem(ctx.clip)}-montage.jpg'
        title = (f'Edit {key} | {ctx.track_path.name} {ctx.track["bpm"]:.1f} BPM | cut every {ctx.plan_t["interval"]:.3f} s '
                 f'| {ctx.edit.get("grade", "none")} | {ctx.edit.get("transition", "cut")}')
        sheets.append(montage_sheet(source, ctx.edit, ctx.plan_t, ctx.centers, ctx.size, config, settings, dest, title))
    return sheets, problems


def render_edits(source, plan, cache, config, output: Path, transcript, force=False, music_folder=None):
    """Render every edit of the plan after its clips; returns the edit index."""
    from shorts import clip_stem, write_json
    settings = edit_settings()
    folder = edits_folder(output)
    edits = plan.get('edits') or {}
    if not edits:
        tidy_edits(folder, set())
        if folder.exists():
            write_json(folder/'edits.json', [])  # the editor must not offer edits that are gone
        return []
    index, failures, sheets = [], [], []
    # Their own folder: the clip validation clears qa-current/*.jpg when it runs.
    qa = cache/'qa-current'/'edits'
    qa.mkdir(parents=True, exist_ok=True)
    for old in qa.glob('*.jpg'):
        old.unlink()
    for key, ctx in contexts(source, plan, cache, config, transcript, settings, music_folder):
        if isinstance(ctx, ValueError):
            failures.append(f'Edit {key}: {ctx}')
            continue
        stem = clip_stem(ctx.clip)
        print(f'Edit [{key}]: {ctx.clip["title"]} with {ctx.track_path.name}', flush=True)
        try:
            info = render_edit(ctx, output/(stem+'.mp4'), folder, stem, config, settings, force,
                               out=lambda text: print(text, flush=True))
        except ValueError as exc:
            failures.append(str(exc))
            continue
        sheets.append(qa_sheet(folder/info['file'], info['timeline'], qa/f'edit-{key}.jpg',
                               f'Edit {key} | {info["track"]} | drop {info["timeline"]["drop_at"]:.2f} s',
                               detailed=info.get('effects') == 'copper'))
        index.append(info)
    tidy_edits(folder, {clip_stem(c) for c in plan['clips'] if c['id'] in edits})
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder/'edits.json', index)
    if sheets:
        print('Look at every edit sheet (last line, drop, each montage shot, tail):', flush=True)
        for sheet in sheets:
            print(f'    {sheet}', flush=True)
    if failures:
        raise ValueError('Music edits failed:'+''.join(f'\n  - {f}' for f in failures))
    return index
