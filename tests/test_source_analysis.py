import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

import shot_index
import source_analysis as sa
from active_speaker import build_tracks
from frame_grid import FrameGrid


def grid_24():
    """A 23.976 fps source: 1001 ticks per frame at 1/24000."""
    return FrameGrid({'time_base': [1, 24000], 'start_time': 0.0, 'pts': [1001*i for i in range(2400)]})


def record(start, end, tracks):
    return {'version': sa.SHOT_VERSION, 'start': start, 'end': end, 'weights_sha256': 'w',
            'tracks': [{'track': k, 'start': first, 'center_x': [x]*count, 'area': [.02]*count,
                        'sharpness': [50.0]*count, 'score': [float(first+i) for i in range(count)]}
                       for k, (first, count, x) in enumerate(tracks)]}


class ClipSliceTests(unittest.TestCase):
    def test_grid_frames_take_the_values_of_the_source_frames_they_show(self):
        grid = grid_24()
        clip = {'start': 10.0, 'end': 12.0}
        shown = sa.clip_source_frames(grid, clip)
        self.assertEqual(60, len(shown))
        self.assertEqual(grid.origin(10.0), shown[0])
        # 24 -> 30 fps repeats every fourth frame and never skips one.
        self.assertTrue(all(0 <= b-a <= 1 for a, b in zip(shown, shown[1:])))
        shot = sa.slice_shot(record(shown[0]-30, shown[0]+500, [(0, 530, .4)]), shown, 0)
        track = shot['tracks'][0]
        self.assertEqual(0, track['start'])
        self.assertEqual([float(f-(shown[0]-30)) for f in shown], track['score'])

    def test_a_track_that_starts_inside_the_clip_keeps_its_place_on_the_grid(self):
        grid = grid_24()
        shown = sa.clip_source_frames(grid, {'start': 10.0, 'end': 12.0})
        first = shown[0]
        # Visible only on source frames first+20 .. first+29.
        shot = sa.slice_shot(record(first, first+500, [(20, 10, .7)]), shown, 0)
        track = shot['tracks'][0]
        visible = [n for n, frame in enumerate(shown) if first+20 <= frame < first+30]
        self.assertEqual(visible[0], track['start'])
        self.assertEqual(len(visible), len(track['score']))

    def test_tracks_outside_the_clip_are_left_out_and_the_rest_renumbered(self):
        grid = grid_24()
        shown = sa.clip_source_frames(grid, {'start': 10.0, 'end': 11.0})
        first = shown[0]
        shot = sa.slice_shot(record(first-100, first+500, [(0, 50, .2), (90, 300, .6)]), shown, 0)
        self.assertEqual([0], [t['track'] for t in shot['tracks']])
        self.assertEqual([1], [t['source_track'] for t in shot['tracks']])

    def test_moving_an_edge_keeps_every_source_frame_on_the_same_values(self):
        grid = grid_24()
        data = record(0, 2400, [(0, 2400, .5)])
        a = sa.clip_source_frames(grid, {'start': 10.0, 'end': 14.0})
        b = sa.clip_source_frames(grid, {'start': 10.5, 'end': 14.0})
        score_a = dict(zip(a, sa.slice_shot(data, a, 0)['tracks'][0]['score']))
        score_b = dict(zip(b, sa.slice_shot(data, b, 0)['tracks'][0]['score']))
        shared = set(score_a) & set(score_b)
        self.assertGreater(len(shared), 80)
        self.assertTrue(all(score_a[f] == score_b[f] for f in shared))

    def test_a_seek_position_isolates_one_source_frame(self):
        grid = grid_24()
        for index in (0, 1, 2, 777, 2399):
            self.assertEqual(index, grid.origin(sa.seek_time(grid, index)))


class TrackingRateTests(unittest.TestCase):
    def test_limits_mean_the_same_time_at_the_source_frame_rate(self):
        box = np.array([100, 80, 50, 50], float)
        # 8 frames at 23.976 fps last as long as 10 frames at 30 fps.
        self.assertEqual(1, len(build_tracks([[box]]*8, rate=.8)))
        self.assertEqual([], build_tracks([[box]]*8))
        # A face missed for 9 frames at 23.976 fps is gone longer than ten 30 fps frames.
        gap = [[box]]*10+[[]]*9+[[box]]*10
        self.assertEqual(1, len(build_tracks(gap)))
        self.assertEqual(1, len(build_tracks(gap, rate=.8)))  # stitched: the gap is under 20 frames of 30 fps
        long_gap = [[box]]*10+[[]]*18+[[box]]*10
        self.assertEqual(1, len(build_tracks(long_gap)))
        self.assertEqual(2, len(build_tracks(long_gap, rate=.8)))


    def test_stitching_copes_with_two_faces_seen_on_the_same_frames(self):
        left, right = np.array([100, 80, 50, 50], float), np.array([400, 80, 50, 50], float)
        # Both faces vanish and return together, so two tracks share their frame numbers.
        frames = [[left, right]]*12+[[]]*14+[[left, right]]*12
        self.assertEqual(2, len(build_tracks(frames)))


class AudioTests(unittest.TestCase):
    def test_features_computed_in_pieces_equal_one_pass(self):
        from python_speech_features import mfcc
        rng = np.random.default_rng(3)
        audio = (rng.standard_normal(16000*5)*3000).astype(np.int16)
        whole = mfcc(audio, 16000, numcep=13, winlen=.025, winstep=.010)
        pieces = sa.mfcc_chunks(audio, 16000, chunk=120)
        self.assertEqual(whole.shape, pieces.shape)
        np.testing.assert_allclose(whole, pieces, rtol=1e-4, atol=1e-4)


class StreamTests(unittest.TestCase):
    def test_frames_are_read_from_the_requested_source_frame(self):
        from frame_grid import probe_frames
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp)/'counter.mp4'
            # Brightness encodes the frame number, so each decoded frame identifies itself.
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=black:s=64x36:r=24000/1001:d=3',
                            '-vf', "geq=lum='40+mod(N*2,180)':cb=128:cr=128", '-c:v', 'libx264', '-g', '24', '-pix_fmt',
                            'yuv420p', str(video)], check=True)
            grid = FrameGrid(probe_frames(video))
            for first in (0, 1, 23, 40):
                frames = list(sa.read_frames(video, grid, first, 5, (64, 36)))
                self.assertEqual(5, len(frames))
                values = [float(f.mean()) for f in frames]
                reference = list(sa.read_frames(video, grid, 0, first+5, (64, 36)))[first:]
                self.assertEqual([float(f.mean()) for f in reference], values)
                self.assertGreater(values[1], values[0])


def scan_with(faces, starts, ends, embedded=True):
    """A scan of 24 fps frames; `faces`: (frame, x, y, w, h, sharpness, embedding)."""
    frames = sorted({f[0] for f in faces} | {s+3 for s in starts})
    rows = np.zeros((len(faces), 15), np.float32)
    for n, face in enumerate(faces):
        rows[n, :4] = face[1:5]
    return {'frame_count': ends[-1], 'width': 640, 'height': 360, 'sample_step': 6, 'embedded': embedded,
            'starts': starts, 'ends': ends, 'sample_frame': np.asarray(frames, np.int32),
            'face_frame': np.asarray([f[0] for f in faces], np.int32), 'face_row': rows,
            'face_sharp': np.asarray([f[5] for f in faces], np.float32),
            'face_embed': np.asarray([f[6] for f in faces], np.float32).reshape(-1, 128)}


def identity(seed):
    return np.random.default_rng(seed).standard_normal(128)


class ShotIndexTests(unittest.TestCase):
    def index(self):
        jane, lisbon = identity(1), identity(2)
        faces = []
        starts = list(range(0, 480, 48))
        ends = starts[1:]+[480]
        for number, start in enumerate(starts):
            who, big = (jane, number % 4 == 0) if number % 2 == 0 else (lisbon, False)
            for frame in range(start+3, start+48, 6):
                noise = np.random.default_rng(frame).standard_normal(128)*.1
                size = 150 if big else 60
                faces.append((frame, 200, 60, size, size, 90.0 if number < 6 else 20.0, who+noise))
        return shot_index.build(scan_with(faces, starts, ends), FrameGrid(
            {'time_base': [1, 24], 'start_time': 0.0, 'pts': list(range(480))}))

    def test_faces_of_one_person_share_a_character_across_shots(self):
        index = self.index()
        ids = [shot['faces'][0]['character'] for shot in index['shots']]
        self.assertEqual({1, 2}, set(ids))
        self.assertEqual(1, len(set(ids[0::2])))
        self.assertEqual(1, len(set(ids[1::2])))
        self.assertEqual([5, 5], [c['shots'] for c in index['characters']])

    def test_shots_carry_times_and_one_face_per_person(self):
        shot = self.index()['shots'][1]
        self.assertEqual((2.0, 4.0, 2.0), (shot['start'], shot['end'], shot['seconds']))
        self.assertEqual(1, len(shot['faces']))
        self.assertEqual('medium', shot['faces'][0]['framing'])
        self.assertEqual(1.0, shot['faces'][0]['visible'])

    def test_query_filters_by_character_framing_sharpness_and_length(self):
        index = self.index()
        jane = index['shots'][0]['faces'][0]['character']
        close = shot_index.find(index, character=jane, framing='close-up')
        self.assertEqual([0, 4, 8], [shot['shot'] for shot, _ in close])
        sharp = shot_index.find(index, character=jane, framing='close-up', sharp=True)
        self.assertEqual([0, 4], [shot['shot'] for shot, _ in sharp])
        self.assertEqual([], shot_index.find(index, character=jane, min_seconds=2.5))
        self.assertEqual([2, 3], [shot['shot'] for shot, _ in shot_index.find(index, start=4.5, end=7.5)])

    def test_without_identities_faces_stay_unidentified(self):
        faces = [(frame, 200, 60, 80, 80, 50.0, np.zeros(128)) for frame in range(3, 48, 6)]
        scan = scan_with(faces, [0], [48], embedded=False)
        scan['face_embed'] = np.zeros((0, 128), np.float32)
        index = shot_index.build(scan, FrameGrid({'time_base': [1, 24], 'start_time': 0.0, 'pts': list(range(48))}))
        self.assertEqual([], index['characters'])
        self.assertIsNone(index['shots'][0]['faces'][0]['character'])
        self.assertEqual(1, len(shot_index.find(index, framing='medium')))

    def test_two_people_in_one_shot_are_two_faces(self):
        a, b = identity(5), identity(6)
        faces = []
        for frame in range(3, 48, 6):
            faces += [(frame, 100, 60, 70, 70, 50.0, a), (frame, 420, 60, 70, 70, 50.0, b)]
        index = shot_index.build(scan_with(faces, [0], [48]), FrameGrid(
            {'time_base': [1, 24], 'start_time': 0.0, 'pts': list(range(48))}))
        self.assertEqual(2, len(index['shots'][0]['faces']))
        self.assertEqual([], shot_index.find(index, solo=True))
        json.dumps(index)  # the index is plain JSON


if __name__ == '__main__':
    unittest.main()
