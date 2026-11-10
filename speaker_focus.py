"""Frame-accurate, fixed-aspect crops driven by visible active-speaker tracks.

Two stages:
1. `analyze` (slow parts cached per source, see source_analysis.py): exact camera
   cuts, face tracks and Light-ASD speaking scores for every output frame of a clip.
2. `focus_plan` (fast): chooses the followed face per shot from those tracks,
   the transcript's speech timing and reviewed overrides.
"""
import hashlib
import json
import math

from source_analysis import ANALYSIS_VERSION, FPS, is_cut  # noqa: F401 (is_cut is part of this module's API)


def geometry(stream, config):
    scale = max(config['width']/stream['width'], config['height']/stream['height'])
    return {'scaled_width': math.ceil(stream['width']*scale/2)*2,
            'scaled_height': math.ceil(stream['height']*scale/2)*2}


def write_commands(path, frames, scaled_width, output_width):
    """Schedule frame N halfway after N-1, avoiding decimal-time rounding drift."""
    commands = []
    previous = None
    for item in frames:
        index = item['frame']
        x = max(0, min(scaled_width-output_width, round((item['center_x']*scaled_width-output_width/2)/2)*2))
        if x != previous:
            time = 0 if index == 0 else (index-.5)/FPS
            commands.append(f'{time:.9f} crop@focus x {x};')
            previous = x
    path.write_text('\n'.join(commands)+'\n', encoding='utf-8')


def speech_frames(transcript, clip, frame_count, pad=3):
    """True for frames inside a recognized word (plus a small margin)."""
    from captions import clip_words
    mask = [False]*frame_count
    for word in clip_words(transcript, clip):
        first = max(0, int(word['start']*FPS)-pad)
        last = min(frame_count, int(math.ceil(word['end']*FPS))+pad)
        for i in range(first, last):
            mask[i] = True
    return mask


def analysis_key(clip, config):
    """Names a clip's own files in the source's work folder (crop commands, the stored
    slice of the analysis); independent of file name and time."""
    from source_analysis import asd_weights
    weights = asd_weights()
    return hashlib.sha256(json.dumps({
        'start': clip['start'], 'end': clip['end'], 'width': config['width'], 'height': config['height'],
        'version': ANALYSIS_VERSION, 'weights': weights}, sort_keys=True).encode()).hexdigest()[:16], weights


def analyze(source, clip, cache, config, stream):
    """Cut frames, face tracks and speaking scores for one clip on its 30 fps grid.

    The work is done per source and per camera shot (source_analysis.py); this slices
    it, so a clip whose shots are known costs no decoding. The slice is also stored
    as `speaker-tracks-<key>.json`, which marks the clip as analysed for the editor.
    """
    from shorts import write_json
    from source_analysis import clip_analysis
    result = clip_analysis(source, clip, cache, config, stream)
    key, _ = analysis_key(clip, config)
    path = cache/f'speaker-tracks-{key}.json'
    if not path.exists():
        write_json(path, result)
    return result


def track_summary(track, shot_start):
    return {'track': track['track'], 'first_frame': shot_start+track['start'], 'frames': len(track['score']),
            'mean_score': round(sum(track['score'])/len(track['score']), 3),
            'max_score': round(max(track['score']), 3)}


def focus_plan(source, clip, cache, config, transcript, commands_path=None):
    """Per-frame focus for a clip. The crop commands go to the clip's cache file unless
    `commands_path` names another file, or is False to skip writing them (the editor
    uses this so its previews never touch the file a real render reads)."""
    from shorts import probe
    from active_speaker import focus_decisions, shot_review
    stream = next(s for s in probe(source)['streams'] if s['codec_type'] == 'video')
    size = geometry(stream, config)
    overrides = clip.get('focus_overrides', [])
    if commands_path is False:
        from source_analysis import clip_analysis
        analysis = clip_analysis(source, clip, cache, config, stream)  # previews and montage shots leave no files
    else:
        analysis = analyze(source, clip, cache, config, stream)
    for override in overrides:
        if any(override['start_frame'] < shot['start_frame'] < override['end_frame'] for shot in analysis['shots']):
            raise ValueError('Split focus overrides at camera cuts; an override cannot target the next shot early.')
    count = analysis['frame_count']
    speech = speech_frames(transcript, clip, count)
    min_dwell = max(1, round(config.get('speaker_switch_min_seconds', 1.5)*FPS))
    fixed = not config.get('auto_reframe', True) or clip.get('auto_reframe') is False
    fixed_center = (config['width']/2+(size['scaled_width']-config['width'])*clip.get('crop_x', .5))/size['scaled_width']
    frames, shots = [], []
    for shot in analysis['shots']:
        a, b = shot['start_frame'], shot['end_frame']
        local = shot['tracks']  # track 'start' is relative to the shot
        decisions = focus_decisions(local, b-a, speech[a:b], min_dwell, a, fixed_center)
        shots.append({'start_frame': a, 'end_frame': b,
                      'tracks': [track_summary(t, a) for t in local],
                      'review': shot_review(local, decisions, speech[a:b])})
        frames.extend(decisions)
    if len(frames) != count:
        raise RuntimeError('Speaker analysis did not produce one focus decision per output frame.')
    if fixed:
        frames = [{**f, 'center_x': fixed_center, 'track': None, 'mode': 'fixed'} for f in frames]
    for item in frames:
        for override in overrides:
            if override['start_frame'] <= item['frame'] < override['end_frame']:
                center = override['center_x']
                if 'center_x_end' in override:
                    progress = (item['frame']-override['start_frame'])/(override['end_frame']-override['start_frame'])
                    center += (override['center_x_end']-center)*progress
                item.update(center_x=round(center, 6), track=None, mode='reviewed-speaker-focus')
    if commands_path is None:
        key, _ = analysis_key(clip, config)
        commands_path = cache/f'speaker-focus-{key}.cmd'
    if commands_path is not False:
        write_commands(commands_path, frames, size['scaled_width'], config['width'])
    return {**size, 'version': analysis['version'], 'fps': FPS, 'frame_count': count,
            'weights_sha256': analysis['weights_sha256'], 'mode': 'fixed-4x5-active-speaker',
            'min_switch_frames': min_dwell, 'shots': shots, 'frames': frames,
            'commands_path': str(commands_path.resolve()) if commands_path is not False else None}
