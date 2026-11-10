"""Source-frame motion and practical light for the Copper montage profile."""
from __future__ import annotations

import subprocess

from frame_grid import FPS

SHOT_KEYS = {'motion', 'zoom', 'anchor', 'practical_light', 'cool_background'}
PUSH = (0, .02, .07, .11, .13, .14)
RAMP = (0, .147, .373, .625, .748, 1.02)


def motion(shot, index):
    return shot.get('motion', 'push' if index == 0 else 'none')


def shot_problems(shot, index):
    mode = motion(shot, index)
    if mode not in ('none', 'push', 'ramp'):
        yield 'motion must be none, push or ramp.'
    for field in ('anchor', 'practical_light'):
        if field in shot:
            point = shot[field]
            if not (isinstance(point, list) and len(point) == 2 and all(
                    type(v) in (int, float) and 0 <= v <= 1 for v in point)):
                yield f'{field} must be [x, y] in the cropped frame, each between 0 and 1.'
    if 'zoom' in shot and not (type(shot['zoom']) in (int, float) and .01 <= shot['zoom'] <= .20):
        yield 'zoom must be between 0.01 and 0.20.'
    if mode != 'push' and ('zoom' in shot or 'anchor' in shot):
        yield 'zoom and anchor require push motion.'
    if 'cool_background' in shot and not isinstance(shot['cool_background'], bool):
        yield 'cool_background must be true or false.'


def source_seconds(shot, index, frames):
    # Keep the last native frame inside the reviewed camera shot, including a grid frame of margin.
    return (frames*RAMP[-1]+1)/FPS if motion(shot, index) == 'ramp' else frames/FPS


def ramp_times(frames):
    import numpy as np
    from scipy.interpolate import PchipInterpolator
    knots = (0, 3/17, 6/17, 9/17, 12/17, 1)
    curve = PchipInterpolator(knots, RAMP)
    return curve(np.linspace(0, 1, frames))*frames/FPS


def sample_source(source, start, seconds, times):
    """Nearest native frames on a monotonic time map, bounded to a reviewed camera shot."""
    import av
    with av.open(str(source)) as media:
        stream = media.streams.video[0]
        origin = (media.start_time or 0)/av.time_base
        start += origin
        media.seek(int(start/stream.time_base), stream=stream, backward=True)
        frames = (frame for frame in media.decode(stream) if frame.time is not None)
        first = next((frame for frame in frames if frame.time >= start-1e-6), None)
        if first is None or first.time >= start+seconds:
            raise ValueError(f'No video frame at montage shot start {start-origin:.3f} s.')
        previous, upcoming = first, next(frames, None)
        # Use the same first decoded frame as an accurate input seek. No look-ahead frame
        # outside the source window may be sampled, even if it is nearer to the target.
        cached_frame = None
        for offset in times:
            target = first.time+float(offset)
            while upcoming is not None and upcoming.time < start+seconds and upcoming.time <= target:
                previous, upcoming = upcoming, next(frames, None)
            choice = previous
            if upcoming is not None and upcoming.time < start+seconds and upcoming.time-target < target-previous.time:
                choice = upcoming
            if choice is not cached_frame:
                image = choice.to_ndarray(format='bgr24')
                cached_frame = choice
            yield image


def eye_anchor(image, matrix, width, height):
    import cv2
    import numpy as np
    from shorts import ROOT
    small = cv2.resize(image, (640, round(image.shape[0]*640/image.shape[1])))
    detector = cv2.FaceDetectorYN.create(str(ROOT/'assets/models/face_detection_yunet_2023mar.onnx'),
                                         '', small.shape[1::-1], .7, .3, 500)
    _, faces = detector.detect(small)
    if faces is not None:
        eyes = (faces[:, 4:6]+faces[:, 6:8])/2
        eyes *= np.array(image.shape[1::-1])/np.array(small.shape[1::-1])
        eyes = (eyes @ matrix[:, :2].T + matrix[:, 2])/np.array([width, height])
        visible = [p for p in eyes if .08 <= p[0] <= .92 and .08 <= p[1] <= .80]
        if visible:
            return min(visible, key=lambda p: abs(p[0]-.5))
    return (.5, .38)


def practical_glow(frame, point, n):
    """Bloom from the actual bright area around a reviewed light, feathered before blur."""
    import cv2
    import numpy as np
    height, width = frame.shape[:2]
    small = cv2.resize(frame, (max(1, width//4), max(1, height//4)), interpolation=cv2.INTER_AREA)
    luma = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)/255
    y, x = np.ogrid[:luma.shape[0], :luma.shape[1]]
    region = np.exp(-((x/luma.shape[1]-point[0])/.13)**2-((y/luma.shape[0]-point[1])/.24)**2)
    bright = np.clip((luma-.42)/.40, 0, 1)*region
    near = cv2.GaussianBlur(bright, (0, 0), max(1, width*.0037))
    far = cv2.GaussianBlur(bright, (0, 0), max(1, width*.013))
    hit = np.interp(n, (0, 2, 5, 9, 16), (.67, 1, .54, .20, .16))
    haze = cv2.resize((.75*near+.64*far)*hit, (width, height))
    light = haze[:, :, None]*np.array([.20, .46, .83])
    return np.clip(np.rint(frame+(255-frame)*light), 0, 255).astype(np.uint8)


def stage_shot(source, shot, index, frames, size, left, config, fx, dest):
    """Write a lossless 30 fps input only for Copper shots needing source-frame processing."""
    mode = motion(shot, index)
    light = shot.get('practical_light')
    if mode == 'none' and light is None:
        return False
    import cv2
    import numpy as np
    from scipy.interpolate import PchipInterpolator
    width, height = config['width'], config['height']
    times = ramp_times(frames) if mode == 'ramp' else np.arange(frames)/FPS
    images = sample_source(source, shot['start'], source_seconds(shot, index, frames), times)
    push = PchipInterpolator(range(len(PUSH)), PUSH) if mode == 'push' else None
    zoom = shot.get('zoom', fx['push_zoom'])/PUSH[-1]
    shutter = fx['shutter_angle']/720  # half the exposure, measured in frames
    anchor = shot.get('anchor')
    command = ['ffmpeg','-v','error','-f','rawvideo','-pixel_format','bgr24','-video_size',f'{width}x{height}',
               '-framerate',str(FPS),'-i','-','-vf','scale=out_color_matrix=bt709:out_range=tv,format=yuv420p',
               '-an','-c:v','ffv1','-color_range','tv','-colorspace','bt709','-color_primaries','bt709',
               '-color_trc','bt709',str(dest)]
    with subprocess.Popen(command, stdin=subprocess.PIPE) as process:
        try:
            for n, image in enumerate(images):
                matrix = np.array([[size['scaled_width']/image.shape[1], 0, -left],
                                   [0, size['scaled_height']/image.shape[0], (height-size['scaled_height'])/2]])
                if push is not None and anchor is None:
                    anchor = eye_anchor(image, matrix, width, height)
                scales = [1.0]
                if push is not None:
                    at = np.clip(n+np.linspace(-shutter, shutter, 5), 0, len(PUSH)-1)
                    scales = 1+zoom*push(at if at[0] != at[-1] else at[2:3])
                pixels = np.zeros((height, width, 3), np.float32) if len(scales)>1 else None
                for scale in scales:
                    transform = matrix*scale
                    if anchor is not None:
                        transform[:, 2] += (1-scale)*np.asarray(anchor)*[width, height]
                    frame = cv2.warpAffine(image, transform, (width, height), flags=cv2.INTER_CUBIC,
                                           borderMode=cv2.BORDER_REPLICATE)
                    if pixels is not None:
                        pixels += frame
                if pixels is not None:
                    frame = np.rint(pixels/len(scales)).astype(np.uint8)
                if light is not None:
                    frame = practical_glow(frame, light, n)
                process.stdin.write(frame.tobytes())
        finally:
            images.close()
            process.stdin.close()
        if process.wait():
            raise RuntimeError(f'FFmpeg failed staging Copper shot {index+1} at {shot["start"]:.3f} s.')
    return True
