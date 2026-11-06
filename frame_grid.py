"""Exact link between the source's own video frames and the 30 fps grid that
speaker analysis, rendering and the clip editor share.

An analysis or render of a clip seeks to `start` (FFmpeg drops every frame
before the seek point), resets the first kept frame to time zero and resamples
to 30 fps, rounding each frame's time to the nearest grid slot. So, with
`origin` the first source frame at or after `start`:

- source frame i first appears at grid frame round((pts[i] - pts[origin]) * 30);
- grid frame n shows the last source frame whose slot is at most n.

The editor's proxy keeps the source frames and their timestamps, so the live
preview can show exactly what a render shows at each grid frame, and moving a
clip's start can carry frame-indexed overrides to the same source frames.
"""
from __future__ import annotations
import bisect
from fractions import Fraction
import functools
import json
from pathlib import Path

FPS = 30
VERSION = 1


def frames_path(cache: Path):
    return Path(cache)/'video-frames.json'


def probe_frames(source):
    """Timestamps of every frame of the first video stream, in its own time base."""
    from shorts import run
    info = json.loads(run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                           'stream=time_base:format=start_time', '-of', 'json', source]))
    num, den = (int(x) for x in info['streams'][0]['time_base'].split('/'))
    listing = run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'packet=pts',
                   '-of', 'csv=p=0', source])
    pts = sorted({int(x) for x in listing.split() if x.lstrip('-').isdigit()})
    if not pts:
        raise ValueError(f'No video frame timestamps were found in {source}.')
    start = info.get('format', {}).get('start_time')
    return {'version': VERSION, 'time_base': [num, den], 'start_time': float(start) if start else 0.0, 'pts': pts}


def load_frames(source, cache: Path):
    """Cached frame timestamps of the source (about a second to create)."""
    from shorts import write_json
    path = frames_path(cache)
    if path.exists():
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('version') == VERSION:
            return data
    data = probe_frames(source)
    Path(cache).mkdir(parents=True, exist_ok=True)
    write_json(path, data)
    return data


@functools.lru_cache(maxsize=4)
def _grid(path: str, mtime: float):
    return FrameGrid(json.loads(Path(path).read_text(encoding='utf-8')))


def load_grid(source, cache: Path):
    load_frames(source, cache)
    path = frames_path(cache)
    return _grid(str(path), path.stat().st_mtime)


class FrameGrid:
    def __init__(self, data):
        self.num, self.den = data['time_base']
        self.pts = data['pts']
        self.start_time = Fraction(str(data.get('start_time', 0.0)))

    def seconds(self, i):
        return self.pts[i]*self.num/self.den

    def origin(self, start):
        """Index of the first source frame an FFmpeg seek to `start` keeps
        (`-ss` counts from the file's start time)."""
        ticks = (Fraction(str(float(start)))+self.start_time)*self.den/self.num
        return min(bisect.bisect_left(self.pts, -(-ticks.numerator//ticks.denominator)), len(self.pts)-1)

    def slot(self, i, origin):
        """Grid frame at which source frame i first appears (round half up)."""
        return (2*(self.pts[i]-self.pts[origin])*self.num*FPS+self.den)//(2*self.den)

    def shown(self, n, origin):
        """Source frame shown at grid frame n: slot(i) <= n holds while
        pts[i] - pts[origin] < den*(2n+1)/(60*num)."""
        bound = Fraction(self.den*(2*n+1), 2*FPS*self.num)
        limit = self.pts[origin]+(-(-bound.numerator//bound.denominator))-1
        return max(0, bisect.bisect_right(self.pts, limit)-1)

    def frame_seconds(self, n, origin):
        """Source time of the frame shown at grid frame n."""
        return self.seconds(self.shown(n, origin))

    def remap(self, n, old_origin, new_origin):
        """Grid frame in the new grid that shows the same source frame as frame n of the old one."""
        return self.slot(self.shown(n, old_origin), new_origin)

    def client(self):
        """Compact form for the editor, which repeats these rules in JavaScript."""
        return {'time_base': [self.num, self.den], 'start_time': float(self.start_time), 'fps': FPS, 'pts': self.pts}
