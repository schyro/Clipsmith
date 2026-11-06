import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from frame_grid import FrameGrid, probe_frames
from review_apply import shift_clip_timing
from shot_edges import cut_edges

TB = 90000
STEP = 3753.75  # 24000/1001 fps in 1/90000 ticks


def ntsc_grid(count=400, first=7470):
    return FrameGrid({'time_base': [1, TB], 'start_time': 0.0, 'pts': [first+round(i*STEP) for i in range(count)]})


class GridRuleTests(unittest.TestCase):
    def test_origin_is_the_first_frame_at_or_after_the_start(self):
        grid = ntsc_grid()
        for start in (0, .083, .0831, 1.0, 2.5172):
            o = grid.origin(start)
            self.assertGreaterEqual(grid.seconds(o), start-1e-9)
            if o:
                self.assertLess(grid.seconds(o-1), start)

    def test_shown_is_the_inverse_of_slot(self):
        grid = ntsc_grid()
        o = grid.origin(1.234)
        for n in range(200):
            i = grid.shown(n, o)
            self.assertLessEqual(grid.slot(i, o), n)
            self.assertGreater(grid.slot(i+1, o), n)
        # Upsampling 23.976 -> 30 repeats about every fourth frame but never skips one.
        self.assertEqual(set(range(o, grid.shown(199, o)+1)), {grid.shown(n, o) for n in range(200)})

    def test_remap_keeps_an_override_edge_on_the_same_source_frame(self):
        grid = ntsc_grid()
        old, new = grid.origin(1.0), grid.origin(1.55)
        for n in range(30, 120):
            self.assertEqual(grid.shown(n, old), grid.shown(grid.remap(n, old, new), new))

    def test_recut_moves_overrides_to_the_same_source_frames(self):
        grid = ntsc_grid()
        clip = {'start': 1.0, 'end': 8.0, 'focus_overrides': [{'start_frame': 61, 'end_frame': 97, 'center_x': .3}]}
        moved = shift_clip_timing(clip, 1.55, 8.0, grid)['focus_overrides'][0]
        old, new = grid.origin(1.0), grid.origin(1.55)
        self.assertEqual(grid.shown(61, old), grid.shown(moved['start_frame'], new))
        self.assertEqual(grid.shown(97, old), grid.shown(moved['end_frame'], new))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
class FFmpegGridTests(unittest.TestCase):
    """The rule must match what FFmpeg does in the analysis and render command."""

    def test_grid_predicts_every_frame_ffmpeg_shows(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'frames.mp4'
            # Every frame gets its own brightness, losslessly, at 24000/1001 fps on a 1/90000 time base.
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=64x64:r=24000/1001:d=2.5',
                            '-vf', "geq=lum='40+3*N':cb=128:cr=128", '-c:v', 'libx264', '-qp', '0', '-pix_fmt', 'yuv420p',
                            '-video_track_timescale', str(TB), str(source)], check=True)
            grid = FrameGrid(probe_frames(source))

            def brightness(*args):
                raw = subprocess.run(['ffmpeg', '-v', 'error', *args, '-f', 'rawvideo', '-'], check=True,
                                     capture_output=True).stdout
                return [float(f.mean()) for f in np.frombuffer(raw, np.uint8).reshape(-1, 64, 64)]
            native = brightness('-i', str(source), '-vf', 'format=gray', '-fps_mode', 'passthrough')
            self.assertEqual(len(grid.pts), len(native))
            self.assertTrue(all(b-a > 2 for a, b in zip(native, native[1:])))
            for start in (0.0, 0.517, 0.9003):
                with self.subTest(start=start):
                    output = brightness('-ss', str(start), '-i', str(source), '-t', '1.2',
                                        '-vf', 'setpts=PTS-STARTPTS,fps=30,format=gray')
                    shown = [min(range(len(native)), key=lambda i: abs(native[i]-value)) for value in output]
                    origin = grid.origin(start)
                    self.assertEqual([grid.shown(n, origin) for n in range(len(output))], shown)


class EdgeTests(unittest.TestCase):
    def setUp(self):
        self.grid = ntsc_grid(800)

    def test_start_moves_onto_a_cut_three_frames_in(self):
        clip = {'start': 2.0, 'end': 12.0}
        shots = [{'start_frame': 0, 'end_frame': 3}, {'start_frame': 3, 'end_frame': 300}]
        words = [{'start': 2.4, 'end': 2.8, 'word': ' Hello'}]
        start, end, notes = cut_edges(clip, shots, 300, words, self.grid)
        cut = self.grid.frame_seconds(3, self.grid.origin(2.0))
        self.assertAlmostEqual(cut-.001, start, places=4)
        self.assertEqual(self.grid.shown(3, self.grid.origin(2.0)), self.grid.origin(start))
        self.assertEqual(12.0, end)
        self.assertIn('start moved', notes[0])

    def test_a_word_in_the_fragment_keeps_the_edge(self):
        clip = {'start': 2.0, 'end': 12.0}
        shots = [{'start_frame': 0, 'end_frame': 5}, {'start_frame': 5, 'end_frame': 300}]
        words = [{'start': 2.05, 'end': 2.5, 'word': ' Hey'}]
        start, _, notes = cut_edges(clip, shots, 300, words, self.grid)
        self.assertEqual(2.0, start)
        self.assertIn('would cut "Hey"', notes[0])

    def test_end_drops_the_next_shot_without_bringing_its_frame_back(self):
        clip = {'start': 2.0, 'end': 12.0}
        count = 300
        shots = [{'start_frame': 0, 'end_frame': 292}, {'start_frame': 292, 'end_frame': count}]
        _, end, notes = cut_edges(clip, shots, count, [], self.grid)
        self.assertLess(end, 12.0)
        import math
        kept = math.ceil((end-2.0)*30-1e-9)
        origin = self.grid.origin(2.0)
        # Every kept frame belongs to the first shot; the last one sits just before the cut frame.
        self.assertEqual(292, kept)
        self.assertLess(self.grid.shown(kept-1, origin), self.grid.shown(292, origin))
        self.assertIn('end moved', notes[0])

    def test_long_edge_shots_stay(self):
        clip = {'start': 2.0, 'end': 12.0}
        shots = [{'start_frame': 0, 'end_frame': 40}, {'start_frame': 40, 'end_frame': 300}]
        self.assertEqual((2.0, 12.0, []), cut_edges(clip, shots, 300, [], self.grid))


if __name__ == '__main__':
    unittest.main()
