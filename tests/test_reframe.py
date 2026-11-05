import unittest
import numpy as np
from active_speaker import build_tracks, choose_path, focus_decisions, possible_missed_reply
from speaker_focus import geometry

FPS = 30
DWELL = 45  # 1.5 seconds


def track(index, x, area, scores, start=0):
    return {'track': index, 'start': start, 'center_x': [x]*len(scores),
            'area': [area]*len(scores), 'sharpness': [50]*len(scores), 'score': list(scores)}


def speaking(length, *ranges):
    scores = np.full(length, -1.)
    for a, b in ranges:
        scores[a:b] = 3
    return scores


class TrackingTests(unittest.TestCase):
    def test_a_moving_face_with_missed_detections_stays_one_track(self):
        detections = []
        for i in range(60):
            # Moves 4 px per frame and is missed for 6 frames in the middle.
            detections.append([] if 25 <= i < 31 else [np.array([100+4*i, 80, 50, 50], float)])
        self.assertEqual(1, len(build_tracks(detections)))

    def test_two_people_side_by_side_stay_separate(self):
        detections = [[np.array([100, 80, 50, 50], float), np.array([400, 80, 50, 50], float)] for _ in range(30)]
        self.assertEqual(2, len(build_tracks(detections)))

    def test_short_spurious_detections_are_not_tracks(self):
        detections = [[np.array([100, 80, 50, 50], float)] if i < 5 else [] for i in range(30)]
        self.assertEqual([], build_tracks(detections))


class FocusPathTests(unittest.TestCase):
    def test_short_interjection_does_not_move_the_crop(self):
        n = 150
        a = track(0, .3, .02, speaking(n, (0, 80), (100, 150)))
        b = track(1, .7, .02, speaking(n, (80, 100)))  # 0.67 s reply
        path = choose_path([a, b], n, [True]*n, DWELL)
        self.assertEqual({0}, set(path))

    def test_long_reply_moves_the_crop_once(self):
        n = 150
        a = track(0, .3, .02, speaking(n, (0, 50)))
        b = track(1, .7, .02, speaking(n, (50, 150)))  # 3.3 s reply
        path = choose_path([a, b], n, [True]*n, DWELL)
        self.assertEqual(0, path[0])
        self.assertEqual(1, path[-1])
        self.assertEqual(1, sum(1 for p, q in zip(path, path[1:]) if p != q))
        self.assertLessEqual(abs(path.index(1)-50), 3)

    def test_silence_never_moves_the_crop_to_a_bigger_face(self):
        n = 300
        small = track(0, .3, .01, speaking(n, (0, 60)))
        big = track(1, .7, .06, np.full(n, -1.))
        speech = [i < 60 for i in range(n)]
        path = choose_path([small, big], n, speech, DWELL)
        self.assertEqual({0}, set(path))

    def test_smaller_speaking_face_wins_over_large_silent_listener(self):
        n = 90
        speaker = track(0, .3, .01, speaking(n, (0, 90)))
        listener = track(1, .7, .06, np.full(n, -2.))
        self.assertEqual({0}, set(choose_path([speaker, listener], n, [True]*n, DWELL)))

    def test_weak_crowd_scores_do_not_beat_the_main_face(self):
        n = 120
        main = track(0, .5, .05, np.full(n, .2))
        extra = track(1, .2, .02, np.full(n, .9))
        self.assertEqual({0}, set(choose_path([main, extra], n, [True]*n, DWELL)))

    def test_face_leaving_the_shot_forces_an_immediate_change(self):
        n = 120
        leaving = track(0, .3, .03, speaking(70, (0, 70)))
        staying = track(1, .7, .03, np.full(n, -1.))
        path = choose_path([leaving, staying], n, [True]*n, DWELL)
        self.assertEqual(0, path[69])
        self.assertEqual(1, path[70])

    def test_change_never_starts_too_close_to_the_shot_end(self):
        n = 100
        a = track(0, .3, .02, speaking(n, (0, 80)))
        b = track(1, .7, .02, speaking(n, (80, 100)))  # would last 0.67 s before the cut
        self.assertEqual({0}, set(choose_path([a, b], n, [True]*n, DWELL)))

    def test_no_change_to_a_face_that_leaves_before_the_minimum(self):
        n = 150
        a = track(0, .3, .02, speaking(n, (0, 60)))
        b = track(1, .7, .02, speaking(30, (0, 30)), start=60)  # visible 1 s only
        c = track(2, .5, .02, speaking(n, (90, 150)), start=0)
        path = choose_path([a, b, c], n, [True]*n, DWELL)
        self.assertNotIn(1, path)

    def test_weak_speech_does_not_justify_a_change(self):
        n = 150
        a = track(0, .3, .02, np.r_[np.full(50, 3.), np.full(100, .8)])
        b = track(1, .7, .02, np.r_[np.full(50, -1.), np.full(100, 1.3)])  # weak reply
        self.assertEqual({0}, set(choose_path([a, b], n, [True]*n, DWELL)))

    def test_a_clear_unfollowed_speaker_is_flagged_for_review(self):
        n = 90
        listener = track(0, .3, .05, np.full(n, -2.))
        speaker = track(1, .7, .01, np.full(n, 3.))
        self.assertTrue(possible_missed_reply([listener, speaker], [0]*n, [True]*n))
        self.assertFalse(possible_missed_reply([listener, speaker], [1]*n, [True]*n))

    def test_no_face_holds_the_previous_centre(self):
        n = 60
        a = track(0, .25, .02, speaking(30, (0, 30)))
        decisions = focus_decisions([a], n, [True]*n, DWELL, 0, .5)
        self.assertEqual('hold', decisions[-1]['mode'])
        self.assertAlmostEqual(decisions[29]['center_x'], decisions[-1]['center_x'])

    def test_source_always_covers_the_output_without_widening(self):
        for w, h in [(1920, 1080), (1080, 1920), (1000, 1000)]:
            output = geometry({'width': w, 'height': h}, {'width': 1080, 'height': 1350})
            self.assertGreaterEqual(output['scaled_width'], 1080)
            self.assertGreaterEqual(output['scaled_height'], 1350)


if __name__ == '__main__':
    unittest.main()
