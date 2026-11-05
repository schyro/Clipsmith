"""Frame-accurate, fixed-aspect crops driven by visible active-speaker tracks.

Two stages:
1. `analyze` (slow, cached): exact camera cuts, face tracks and Light-ASD
   speaking scores for every output frame.
2. `focus_plan` (fast): chooses the followed face per shot from those tracks,
   the transcript's speech timing and reviewed overrides.
"""
import hashlib
import json
import math
import subprocess
import tempfile

ANALYSIS_VERSION = 3
FPS = 30


def geometry(stream, config):
    scale = max(config['width']/stream['width'], config['height']/stream['height'])
    return {'scaled_width': math.ceil(stream['width']*scale/2)*2,
            'scaled_height': math.ceil(stream['height']*scale/2)*2}


def is_cut(previous, thumb, hist, previous_hist):
    import cv2
    import numpy as np
    if previous is None:
        return False
    difference = float(np.mean(np.abs(thumb-previous)))/255
    brightness = (float(np.mean(thumb))+float(np.mean(previous)))/510
    relative = difference/max(.10, brightness)
    hist_change = cv2.compareHist(hist, previous_hist, cv2.HISTCMP_BHATTACHARYYA)
    return difference > .19 or (relative > .28 and hist_change > .30)


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
    """Cache key inside the source's own work folder; independent of file name and time."""
    from shorts import ROOT
    weights = hashlib.sha256((ROOT/'assets/models/light_asd_talkset.model').read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps({
        'start': clip['start'], 'end': clip['end'], 'width': config['width'], 'height': config['height'],
        'version': ANALYSIS_VERSION, 'weights': weights}, sort_keys=True).encode()).hexdigest()[:16], weights


def analyze(source, clip, cache, config, stream):
    """Cut frames, face tracks and speaking scores for one clip (cached)."""
    from shorts import ROOT, run, write_json
    key, weights = analysis_key(clip, config)
    path = cache/f'speaker-tracks-{key}.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    import cv2
    import numpy as np
    from python_speech_features import mfcc
    from scipy.io import wavfile
    from active_speaker import analyze_shot
    duration = clip['end']-clip['start']
    print(f'Analyzing active speakers and exact cut frames: {clip["title"]}', flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        wav = f'{tmp}/speech.wav'
        run(['ffmpeg', '-v', 'error', '-y', '-ss', clip['start'], '-i', source, '-t', duration,
             '-vn', '-ar', '16000', '-ac', '1', wav])
        rate, audio = wavfile.read(wav)
    audio_features = mfcc(audio, rate, numcep=13, winlen=.025, winstep=.010)
    width = 640
    height = round(width*stream['height']/stream['width']/2)*2
    detector = cv2.FaceDetectorYN.create(str(ROOT/'assets/models/face_detection_yunet_2023mar.onnx'),
                                         '', (width, height), .65, .3, 5000)
    cmd = ['ffmpeg', '-v', 'error', '-ss', str(clip['start']), '-i', str(source), '-t', str(duration),
           '-vf', f'setpts=PTS-STARTPTS,fps={FPS},scale={width}:{height}',
           '-an', '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-']
    frames, shots = [], []
    first_frame = index = 0
    previous = previous_hist = None

    def finish_shot():
        if frames:
            tracks = analyze_shot(frames, first_frame, detector, audio_features, width)
            shots.append({'start_frame': first_frame, 'end_frame': first_frame+len(frames), 'tracks': tracks})
            print(f'  Shot {len(shots)}: frames {first_frame}..{first_frame+len(frames)-1}, '
                  f'{len(tracks)} tracks', flush=True)

    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors)
        try:
            while True:
                raw = process.stdout.read(width*height*3)
                if not raw:
                    break
                if len(raw) != width*height*3:
                    raise RuntimeError('Speaker analysis received an incomplete video frame.')
                frame = np.frombuffer(raw, np.uint8).reshape(height, width, 3).copy()
                thumb = cv2.resize(frame, (64, 36)).astype(np.float32)
                hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
                hist = cv2.calcHist([hsv], [0, 1], None, [24, 24], [0, 180, 0, 256])
                cv2.normalize(hist, hist)
                # Every output frame is compared, so a cut is found on its exact frame.
                if is_cut(previous, thumb, hist, previous_hist):
                    finish_shot()
                    frames = []
                    first_frame = index
                frames.append(frame)
                previous, previous_hist = thumb, hist
                index += 1
            finish_shot()
        except Exception:
            process.kill(); process.wait()
            raise
        finally:
            process.stdout.close()
        if process.wait():
            errors.seek(0)
            raise RuntimeError(errors.read().decode('utf-8', errors='replace')[-2000:])
    result = {'version': ANALYSIS_VERSION, 'fps': FPS, 'frame_count': index,
              'weights_sha256': weights, 'shots': shots}
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
