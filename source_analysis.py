"""Speaker analysis of a whole source, on the source's own frames.

Three caches per source, all inside its work folder:

1. `source-scan.npz` (`scan`, once per source): every camera cut as a source frame
   index, plus the faces found on a few frames per second, for the shot index.
2. `audio-mfcc.npy` (`audio_features`, once per source): speech features for Light-ASD.
3. `shots/<first frame>.json` (`ensure_shots`, on demand): the face tracks and
   Light-ASD scores of one camera shot, one value per source frame.

A clip's analysis (`clip_analysis`) is a slice of these: `frame_grid.FrameGrid` says
which source frame each frame of the clip's 30 fps grid shows. Moving a clip edge or
adding a montage shot therefore analyses at most the camera shots it newly touches.

No stage holds the frames of a long shot: frames are read as a stream and only the
112x112 face patches Light-ASD needs are kept (shots of at most KEEP_FRAMES frames
keep their frames between detection and patch extraction; longer ones are read twice).
"""
from __future__ import annotations
import bisect
from collections import deque
from fractions import Fraction
import functools
import hashlib
import json
import os
import subprocess
import tempfile
import threading
from pathlib import Path

ANALYSIS_VERSION = 4  # what a clip's analysis reports; raise it with either version below
SCAN_VERSION = 1
SHOT_VERSION = 1
FPS = 30
WIDTH = 640            # analysis picture width; the height follows the source
MIN_FACE = 24          # smallest face side (px at WIDTH) Light-ASD's mouth crop can use
SEGMENT_FRAMES = 1500  # the scan decodes this many frames per FFmpeg process
SCAN_WORKERS = 3
# Each detection thread has its own YuNet instance and OpenCV runs single-threaded
# inside it; measured faster than fewer threads with OpenCV's own parallelism.
DETECT_WORKERS = max(1, min(8, (os.cpu_count() or 4)//2))
SAMPLE_SECONDS = .25   # the scan looks for faces this often inside a shot
KEEP_FRAMES = 128      # about 88 MB at 640x360; longer shots are decoded twice instead
SFACE_MODEL = 'assets/models/face_recognition_sface_2021dec.onnx'
YUNET_MODEL = 'assets/models/face_detection_yunet_2023mar.onnx'
ASD_MODEL = 'assets/models/light_asd_talkset.model'


def sample_size(stream):
    return WIDTH, round(WIDTH*stream['height']/stream['width']/2)*2


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


def cut_features(frame):
    import cv2
    import numpy as np
    thumb = cv2.resize(frame, (64, 36)).astype(np.float32)
    hist = cv2.calcHist([cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)], [0, 1], None, [24, 24], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return thumb, hist


@functools.lru_cache(maxsize=4)
def file_sha256(path: str, mtime: float):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def asd_weights():
    from shorts import ROOT
    path = ROOT/ASD_MODEL
    return file_sha256(str(path), path.stat().st_mtime)


def frame_rate(grid):
    """Average frames per second of the source."""
    if len(grid.pts) < 2:
        return float(FPS)
    return (len(grid.pts)-1)/(grid.seconds(len(grid.pts)-1)-grid.seconds(0))


def frame_time(grid, index):
    """Seconds from the start of the file, the clock `-ss` and the audio features use."""
    return grid.seconds(index)-float(grid.start_time)


def seek_time(grid, index):
    """A seek position that makes source frame `index` the first frame FFmpeg keeps."""
    if index <= 0:
        return 0.0
    middle = Fraction(grid.pts[index-1]+grid.pts[index], 2)*grid.num/grid.den-grid.start_time
    seconds = max(0.0, round(float(middle), 6))
    if grid.origin(seconds) != index:
        raise RuntimeError(f'No seek position isolates source frame {index}.')
    return seconds


def read_frames(source, grid, first, count, size):
    """Yield `count` source frames from frame `first` as BGR arrays of `size`, one at a time."""
    import numpy as np
    width, height = size
    command = ['ffmpeg', '-v', 'error', '-ss', f'{seek_time(grid, first):.6f}', '-i', str(source),
               '-frames:v', str(count), '-vf', f'scale={width}:{height}', '-an', '-fps_mode', 'passthrough',
               '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-']
    length = width*height*3
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, bufsize=length)
        try:
            while True:
                raw = process.stdout.read(length)
                if not raw:
                    break
                if len(raw) != length:
                    raise RuntimeError('Speaker analysis received an incomplete video frame.')
                yield np.frombuffer(raw, np.uint8).reshape(height, width, 3)
            if process.wait():
                errors.seek(0)
                raise RuntimeError(errors.read().decode('utf-8', errors='replace')[-2000:])
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()
            process.wait()


class single_threaded_opencv:
    """OpenCV's own thread pool off while several detection threads run."""
    def __enter__(self):
        import cv2
        self.threads = cv2.getNumThreads()
        cv2.setNumThreads(1)

    def __exit__(self, *exc):
        import cv2
        cv2.setNumThreads(self.threads)


class Throttle:
    """Submit to an executor, but never let more than `limit` tasks (and the frames
    they hold) wait at once."""
    def __init__(self, pool, limit):
        self.pool = pool
        self.gate = threading.Semaphore(limit)

    def submit(self, fn, *args):
        self.gate.acquire()
        try:
            future = self.pool.submit(fn, *args)
        except BaseException:
            self.gate.release()
            raise
        future.add_done_callback(lambda _: self.gate.release())
        return future


def face_detector(size, local=threading.local()):
    """This thread's YuNet instance for pictures of `size`."""
    import cv2
    from shorts import ROOT
    if getattr(local, 'size', None) != size:
        local.detector = cv2.FaceDetectorYN.create(str(ROOT/YUNET_MODEL), '', size, .65, .3, 5000)
        local.size = size
    return local.detector


def face_recognizer(local=threading.local()):
    """This thread's SFace instance, or None when the model file is not installed."""
    import cv2
    from shorts import ROOT
    if not hasattr(local, 'recognizer'):
        path = ROOT/SFACE_MODEL
        local.recognizer = cv2.FaceRecognizerSF.create(str(path), '') if path.exists() else None
    return local.recognizer


def detect_faces(frame):
    from active_speaker import face_boxes
    return face_boxes(face_detector((frame.shape[1], frame.shape[0])), frame)


def describe_faces(frame, embed):
    """Faces of one frame for the shot index: YuNet rows (box, five landmarks, confidence),
    sharpness of each face and, with the SFace model, an identity embedding."""
    import cv2
    import numpy as np
    from active_speaker import face_patch
    _, found = face_detector((frame.shape[1], frame.shape[0])).detect(frame)
    rows = [f for f in (found if found is not None else []) if min(f[2], f[3]) >= MIN_FACE]
    recognizer = face_recognizer() if embed else None
    sharp, vectors = [], []
    for row in rows:
        sharp.append(float(cv2.Laplacian(face_patch(frame, row[:4]), cv2.CV_32F).var()))
        if recognizer is not None:
            vectors.append(recognizer.feature(recognizer.alignCrop(frame, row)).reshape(-1))
    return (np.asarray(rows, np.float32).reshape(-1, 15), np.asarray(sharp, np.float32),
            np.asarray(vectors, np.float32).reshape(-1, 128))


# ---------- whole-source scan ----------

def scan_path(cache):
    return Path(cache)/'source-scan.npz'


def scan_segment(source, grid, size, first, count, faces, step, embed):
    """Cuts and sampled faces of source frames first..first+count-1. The frame before
    `first` is read too, so a cut on the segment's first frame is found."""
    begin = max(0, first-1)
    cuts, sampled = [], []
    previous = previous_hist = None
    since = seen = 0
    for offset, frame in enumerate(read_frames(source, grid, begin, first+count-begin, size)):
        index = begin+offset
        thumb, hist = cut_features(frame)
        if index >= first:
            seen += 1
            if is_cut(previous, thumb, hist, previous_hist):
                cuts.append(index)
                since = 0
            if since % step == step//2:
                sampled.append((index, faces.submit(describe_faces, frame, embed)))
            since += 1
        previous, previous_hist = thumb, hist
    return cuts, [(index, future.result()) for index, future in sampled], seen


def cached_scan(cache, grid):
    """The stored scan of a source, or None when there is none for these frames."""
    path = scan_path(cache)
    if not path.exists():
        return None
    data = _load_scan(str(path), path.stat().st_mtime)
    return data if data['version'] == SCAN_VERSION and data['frame_count'] == len(grid.pts) else None


def scan(source, cache, stream=None, force=False, identities=False):
    """Camera cuts and sampled faces of the whole source (cached). Returns a dict with
    `frame_count`, `cuts` (first frame of every shot but the first), `starts`/`ends`
    of all shots, and the face samples `face_frame`, `face_row`, `face_sharp`, `face_embed`.
    With `identities`, a scan made before the SFace model was installed is redone once
    to add the embeddings."""
    import numpy as np
    from frame_grid import load_grid
    from shorts import ROOT, probe
    cache = Path(cache)
    grid = load_grid(source, cache)
    embed = (ROOT/SFACE_MODEL).exists()
    path = scan_path(cache)
    data = None if force else cached_scan(cache, grid)
    if data and (data['embedded'] or not (identities and embed)):
        return data
    from concurrent.futures import ThreadPoolExecutor
    stream = stream or next(s for s in probe(source)['streams'] if s['codec_type'] == 'video')
    size = sample_size(stream)
    total = len(grid.pts)
    step = max(1, round(SAMPLE_SECONDS*frame_rate(grid)))
    print(f'Scanning {Path(source).name}: camera cuts and faces of {total} frames (once per source)', flush=True)
    segments = [(first, min(SEGMENT_FRAMES, total-first)) for first in range(0, total, SEGMENT_FRAMES)]
    cuts, samples, done = [], [], 0
    with single_threaded_opencv(), ThreadPoolExecutor(DETECT_WORKERS) as detectors, \
            ThreadPoolExecutor(SCAN_WORKERS) as readers:
        faces = Throttle(detectors, DETECT_WORKERS*4)
        jobs = [readers.submit(scan_segment, source, grid, size, first, count, faces, step, embed)
                for first, count in segments]
        try:
            for number, ((first, count), job) in enumerate(zip(segments, jobs)):
                found, sampled, seen = job.result()
                # A file's last packets may not decode into frames; those belong to the last shot.
                if seen != count and number != len(segments)-1:
                    raise RuntimeError(f'The scan read {seen} of {count} frames from frame {first}.')
                cuts += found
                samples += sampled
                done += count
                if number % 5 == 4 or number == len(segments)-1:
                    print(f'  {100*done//total}% scanned', flush=True)
        except BaseException:
            for job in jobs:
                job.cancel()
            detectors.shutdown(wait=False, cancel_futures=True)
            raise
    frames = [index for index, (rows, _, _) in samples for _ in range(len(rows))]
    arrays = {
        'meta': np.array(json.dumps({'version': SCAN_VERSION, 'frame_count': total, 'width': size[0],
                                     'height': size[1], 'sample_step': step, 'embedded': embed})),
        'cuts': np.asarray(cuts, np.int32),
        'sample_frame': np.asarray([index for index, _ in samples], np.int32),
        'face_frame': np.asarray(frames, np.int32),
        'face_row': np.concatenate([s[1][0] for s in samples] or [np.zeros((0, 15), np.float32)]),
        'face_sharp': np.concatenate([s[1][1] for s in samples] or [np.zeros(0, np.float32)]),
        'face_embed': np.concatenate([s[1][2] for s in samples] or [np.zeros((0, 128), np.float32)]),
    }
    cache.mkdir(parents=True, exist_ok=True)
    temporary = cache/'source-scan.tmp.npz'
    np.savez_compressed(temporary, **arrays)
    temporary.replace(path)
    print(f'  {len(cuts)+1} camera shots, {len(frames)} face samples', flush=True)
    return _load_scan(str(path), path.stat().st_mtime)


@functools.lru_cache(maxsize=4)
def _load_scan(path: str, mtime: float):
    import numpy as np
    with np.load(path) as stored:
        data = {name: stored[name] for name in stored.files if name != 'meta'}
        data.update(json.loads(str(stored['meta'])))
    data['starts'] = [0]+[int(c) for c in data['cuts']]
    data['ends'] = data['starts'][1:]+[data['frame_count']]
    return data


def shot_of(scan_data, frame):
    """Number of the camera shot (from 0) that holds a source frame."""
    return bisect.bisect_right(scan_data['starts'], frame)-1


# ---------- audio ----------

def mfcc_chunks(audio, rate, chunk=6000):
    """MFCC features of a long signal, computed in pieces that give the same values
    as one call but never build the whole frame matrix (about 3 kB per 10 ms)."""
    import numpy as np
    from python_speech_features import mfcc
    window, hop = round(.025*rate), round(.010*rate)
    total = 1 if len(audio) <= window else 1+-(-(len(audio)-window)//hop)
    parts = []
    for first in range(0, total, chunk):
        last = min(total, first+chunk)
        # One extra leading frame gives pre-emphasis its previous sample; it is dropped.
        lead = 1 if first else 0
        signal = audio[(first-lead)*hop:(last-1)*hop+window]
        parts.append(mfcc(signal, rate, numcep=13, winlen=.025, winstep=.010)[lead:lead+last-first])
    return np.concatenate(parts).astype(np.float32)


def audio_features(source, cache):
    """Light-ASD's speech features for the whole source, 100 per second from the start
    of the file (cached, memory-mapped)."""
    import numpy as np
    from scipy.io import wavfile
    from shorts import run
    path = Path(cache)/'audio-mfcc.npy'
    if not path.exists():
        with tempfile.TemporaryDirectory() as tmp:
            wav = f'{tmp}/speech.wav'
            # first_pts=0 pads or trims so that sample 0 is the start of the file, like `-ss 0`.
            run(['ffmpeg', '-v', 'error', '-y', '-i', source, '-vn', '-ac', '1',
                 '-af', 'aresample=16000:first_pts=0', wav])
            rate, audio = wavfile.read(wav)
        temporary = Path(cache)/'audio-mfcc.tmp.npy'
        np.save(temporary, mfcc_chunks(audio, rate))
        temporary.replace(path)
    return np.load(path, mmap_mode='r')


# ---------- face tracks of one camera shot ----------

def shot_path(cache, start):
    return Path(cache)/'shots'/f'{start:07d}.json'


def load_shot(cache, start, end, weights):
    path = shot_path(cache, start)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    same = data.get('version') == SHOT_VERSION and data.get('end') == end and data.get('weights_sha256') == weights
    return data if same else None


def track_patches(tracks, frames):
    """Light-ASD's face patches for every track, taken from a stream of the shot's frames."""
    import numpy as np
    from active_speaker import face_patch, fill_gaps
    spans = [fill_gaps(track) for track in tracks]
    wanted = {}
    for k, (positions, boxes) in enumerate(spans):
        for j, (position, box) in enumerate(zip(positions, boxes)):
            wanted.setdefault(int(position), []).append((k, j, box))
    patches = [np.zeros((len(positions), 112, 112), np.uint8) for positions, _ in spans]
    last = max(wanted)
    for position, frame in enumerate(frames):
        for k, j, box in wanted.get(position, ()):
            patches[k][j] = face_patch(frame, box)
        if position >= last:
            break
    return spans, patches


def score_shot(start, end, spans, patches, times, features, rate, width, weights, path):
    """Light-ASD scores for the tracks of one shot; writes and returns the shot's record."""
    import cv2
    from active_speaker import score_track
    from shorts import write_json
    tracks = []
    for (positions, boxes), faces in zip(spans, patches):
        scores = score_track(faces, features, times[int(positions[0]):int(positions[-1])+1], rate)
        tracks.append({'track': len(tracks), 'start': int(positions[0]),
                       'center_x': [round(float((b[0]+b[2]/2)/width), 5) for b in boxes],
                       'area': [round(float(b[2]*b[3])/width**2, 6) for b in boxes],
                       'sharpness': [round(float(cv2.Laplacian(p, cv2.CV_32F).var()), 2) for p in faces],
                       'score': [round(float(s), 3) for s in scores]})
    record = {'version': SHOT_VERSION, 'start': start, 'end': end, 'weights_sha256': weights, 'tracks': tracks}
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, record)
    return record


def analyze_run(source, cache, grid, size, bounds, features, weights):
    """Analyse consecutive camera shots (`bounds`: their (start, end) source frames)
    from one stream of frames. One shot is scored in the background while the next
    is read and detected."""
    from concurrent.futures import ThreadPoolExecutor
    from active_speaker import build_tracks
    rate = frame_rate(grid)/FPS
    records = []
    with single_threaded_opencv(), ThreadPoolExecutor(DETECT_WORKERS) as detectors, ThreadPoolExecutor(1) as scorer:
        faces = Throttle(detectors, DETECT_WORKERS*2)
        stream = read_frames(source, grid, bounds[0][0], bounds[-1][1]-bounds[0][0], size)
        try:
            for start, end in bounds:
                keep = end-start <= KEEP_FRAMES
                kept, found = [], []
                for _ in range(start, end):
                    frame = next(stream, None)
                    if frame is None:  # the file ended before its last listed frames
                        break
                    found.append(faces.submit(detect_faces, frame))
                    if keep:
                        kept.append(frame)
                    del frame
                detections = [f.result() for f in found]
                tracks = build_tracks(detections, rate)
                spans = patches = []
                if tracks:
                    frames = kept if keep else read_frames(source, grid, start, len(detections), size)
                    spans, patches = track_patches(tracks, frames)
                    if not keep:
                        frames.close()
                    del frames
                del kept
                times = [frame_time(grid, i) for i in range(start, start+len(detections))]
                records.append(scorer.submit(score_shot, start, end, spans, patches, times, features, rate,
                                             size[0], weights, shot_path(cache, start)))
                # At most one finished shot's patches wait behind the one being scored.
                if len(records) > 1:
                    records[-2].result()
            return [r.result() for r in records]
        except BaseException:
            detectors.shutdown(wait=False, cancel_futures=True)
            scorer.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            stream.close()


def ensure_shots(source, cache, numbers, scan_data=None, stream=None):
    """Face tracks and speaking scores of the given camera shots (by number), analysing
    the ones not cached yet. Returns {shot number: record}."""
    from frame_grid import load_grid
    from shorts import probe
    cache = Path(cache)
    scan_data = scan_data or scan(source, cache, stream)
    weights = asd_weights()
    bounds = {n: (scan_data['starts'][n], scan_data['ends'][n]) for n in sorted(set(numbers))}
    records = {n: load_shot(cache, start, end, weights) for n, (start, end) in bounds.items()}
    missing = [n for n in bounds if records[n] is None]
    if missing:
        grid = load_grid(source, cache)
        stream = stream or next(s for s in probe(source)['streams'] if s['codec_type'] == 'video')
        size = sample_size(stream)
        features = audio_features(source, cache)
        frames = sum(bounds[n][1]-bounds[n][0] for n in missing)
        print(f'Analyzing active speakers: {len(missing)} camera shot(s), {frames} frames '
              f'from {frame_time(grid, bounds[missing[0]][0]):.2f} s', flush=True)
        runs = [[missing[0]]]
        for n in missing[1:]:
            if n == runs[-1][-1]+1:
                runs[-1].append(n)
            else:
                runs.append([n])
        for group in runs:
            for n, record in zip(group, analyze_run(source, cache, grid, size, [bounds[n] for n in group],
                                                    features, weights)):
                records[n] = record
    return records


# ---------- a clip's slice ----------

def clip_frame_count(clip):
    """Frames of the clip on the 30 fps grid; matches what FFmpeg writes for `-t`."""
    return max(1, round((clip['end']-clip['start'])*FPS))


def clip_source_frames(grid, clip):
    """The source frame shown at every frame of the clip's 30 fps grid."""
    origin = grid.origin(clip['start'])
    return [grid.shown(n, origin) for n in range(clip_frame_count(clip))]


def slice_shot(record, shown, first):
    """One shot of a clip: `shown` holds the source frames of its grid frames, `first`
    is the grid frame it starts at. Tracks get one value per grid frame."""
    relative = [frame-record['start'] for frame in shown]
    tracks = []
    for track in record['tracks']:
        begin = bisect.bisect_left(relative, track['start'])
        stop = bisect.bisect_left(relative, track['start']+len(track['score']))
        if stop <= begin:
            continue
        picks = [relative[i]-track['start'] for i in range(begin, stop)]
        tracks.append({'track': len(tracks), 'source_track': track['track'], 'start': begin,
                       **{name: [track[name][p] for p in picks] for name in ('center_x', 'area', 'sharpness', 'score')}})
    return {'start_frame': first, 'end_frame': first+len(shown), 'source_start': record['start'],
            'source_end': record['end'], 'tracks': tracks}


def clip_analysis(source, clip, cache, config=None, stream=None):
    """Cut frames, face tracks and speaking scores of a clip on its 30 fps grid, sliced
    from the source's caches. Same shape as the per-clip analysis it replaces."""
    from frame_grid import load_grid
    cache = Path(cache)
    grid = load_grid(source, cache)
    scan_data = scan(source, cache, stream)
    shown = clip_source_frames(grid, clip)
    numbers = [shot_of(scan_data, frame) for frame in shown]
    records = ensure_shots(source, cache, numbers, scan_data, stream)
    shots, first = [], 0
    for n in range(1, len(shown)+1):
        if n == len(shown) or numbers[n] != numbers[first]:
            shots.append(slice_shot(records[numbers[first]], shown[first:n], first))
            first = n
    return {'version': ANALYSIS_VERSION, 'fps': FPS, 'frame_count': len(shown),
            'weights_sha256': asd_weights(), 'shots': shots}


def analysis_ready(source, clip, cache):
    """True when a clip's analysis needs no decoding: the scan and all its shots are cached."""
    from frame_grid import load_grid
    cache = Path(cache)
    grid = load_grid(source, cache)
    scan_data = cached_scan(cache, grid)
    if scan_data is None:
        return False
    weights = asd_weights()
    numbers = {shot_of(scan_data, frame) for frame in clip_source_frames(grid, clip)}
    return all(load_shot(cache, scan_data['starts'][n], scan_data['ends'][n], weights) is not None for n in numbers)
