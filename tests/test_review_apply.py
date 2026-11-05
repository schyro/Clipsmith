import json
from pathlib import Path
import tempfile
import unittest
from gallery import load_decisions, save_decisions
from review_apply import apply_to_plan, shift_clip_timing, snap_edges
import shorts

WORDS = [{'start': s, 'end': s+.4, 'word': 'w'} for s in [10.0, 10.5, 11.0, 13.0, 13.5, 20.0, 20.6, 21.2]]


class SnapTests(unittest.TestCase):
    def test_edges_land_just_outside_the_nearest_words(self):
        start, end = snap_edges(WORDS, 10.85, 20.0, 2.0, 1.5, 100)
        # Nearest word start to 12.85 is 13.0; keep 0.15 s lead and a whole-frame shift.
        self.assertAlmostEqual(12.85, start, places=2)
        self.assertAlmostEqual(round((start-10.85)*30), (start-10.85)*30, places=3)
        # Nearest word end to 21.5 is 21.6, padded but not past the clip's last word.
        self.assertAlmostEqual(21.85, end, places=2)

    def test_start_never_cuts_into_the_previous_word(self):
        start, _ = snap_edges(WORDS, 10.0, 25.0, 3.0, 0, 100)
        self.assertGreaterEqual(start, 11.4)

    def test_moving_an_edge_into_silence_never_reverses_it(self):
        # The clip ends after the last word; moving the end later must lengthen it.
        _, end = snap_edges(WORDS, 10.0, 22.0, 0, .5, 100)
        self.assertAlmostEqual(22.5, end)
        start, _ = snap_edges(WORDS, 12.0, 22.0, -.5, 0, 100)
        self.assertAlmostEqual(11.5, start, places=3)

    def test_edge_inside_a_word_leaves_it_in_the_moved_direction(self):
        _, later = snap_edges(WORDS, 10.0, 20.5, 0, .2, 100)   # lands in 20.6-21.0
        self.assertAlmostEqual(21.2, later)                     # word end + pad, capped by the next onset
        _, earlier = snap_edges(WORDS, 10.0, 21.4, 0, -.6, 100)  # lands in 20.6-21.0
        self.assertAlmostEqual(20.6, earlier)                    # previous word 20.0-20.4, +0.25 capped at 20.6

    def test_frame_rounding_does_not_skip_the_word_it_starts_at(self):
        words = [{'start': 513.74, 'end': 514.06}, {'start': 514.06, 'end': 514.46}, {'start': 514.64, 'end': 514.78}]
        start, _ = snap_edges(words, 513.5, 532.8, .5, 0, 1000)  # inside the first word -> before the second
        self.assertLessEqual(start, 514.06)
        self.assertGreaterEqual(start, 514.06-1/30)

    def test_unmoved_edges_stay_exact(self):
        self.assertEqual((10.85, 20.0), snap_edges(WORDS, 10.85, 20.0, 0, 0, 100))

    def test_too_short_cut_is_refused(self):
        with self.assertRaises(ValueError):
            snap_edges(WORDS, 10.0, 14.0, 3.0, -3.0, 100)


class TimingTests(unittest.TestCase):
    def test_overrides_and_cover_follow_the_new_start(self):
        clip = {'start': 10.0, 'end': 20.0, 'cover_time': 5,
                'focus_overrides': [{'start_frame': 0, 'end_frame': 60, 'center_x': .2, 'center_x_end': .8},
                                    {'start_frame': 120, 'end_frame': 150, 'center_x': .5}]}
        moved = shift_clip_timing(clip, 11.0, 20.0)
        first, second = moved['focus_overrides']
        self.assertEqual((0, 30), (first['start_frame'], first['end_frame']))
        self.assertAlmostEqual(.5, first['center_x'])  # value at old frame 30
        self.assertAlmostEqual(.8, first['center_x_end'])
        self.assertEqual((90, 120), (second['start_frame'], second['end_frame']))
        self.assertEqual(4, moved['cover_time'])

    def test_overrides_outside_the_new_clip_are_dropped(self):
        clip = {'start': 10.0, 'end': 20.0, 'focus_overrides': [{'start_frame': 0, 'end_frame': 20, 'center_x': .2}]}
        self.assertEqual([], shift_clip_timing(clip, 11.0, 20.0)['focus_overrides'])


class ApplyTests(unittest.TestCase):
    def plan(self):
        return {'source': 'a.mp4', 'source_id': 's', 'clips': [
            {'id': '01', 'title': 'A', 'start': 10.0, 'end': 22.0},
            {'id': '02', 'title': 'B', 'start': 30.0, 'end': 50.0},
            {'id': '03', 'title': 'C', 'start': 60.0, 'end': 80.0, 'approved': {'at': 't0'}}]}

    def test_decisions_lock_reject_and_recut(self):
        plan = self.plan()
        decisions = {'clips': {
            '01': {'status': 'needs_changes', 'options': ['recut'], 'start_shift': 1.0, 'end_shift': 0},
            '02': {'status': 'rejected', 'comment': 'Weak'},
            '03': {'status': 'pending'}}}
        notes, done = apply_to_plan(plan, decisions, WORDS, 100, 'now')
        self.assertEqual(['01', '03'], [c['id'] for c in plan['clips']])
        self.assertEqual('02', plan['rejected_clips'][0]['id'])
        self.assertEqual('Weak', plan['rejected_clips'][0]['review_comment'])
        # The word before ends at 10.9, so the new start cannot reach back to 10.85.
        self.assertAlmostEqual(10.9, plan['clips'][0]['start'], places=3)
        self.assertNotIn('approved', plan['clips'][1])  # approval withdrawn
        self.assertEqual({'01', '02'}, done)
        self.assertIn('03', notes)

    def test_approval_locks_and_addressed_entries_are_not_reapplied(self):
        plan = self.plan()
        decisions = {'clips': {'01': {'status': 'approved', 'updated': 't1'},
                               '02': {'status': 'rejected', 'addressed': True}}}
        apply_to_plan(plan, decisions, WORDS, 100, 'now')
        self.assertEqual({'at': 't1'}, plan['clips'][0]['approved'])
        self.assertEqual(['01', '02', '03'], [c['id'] for c in plan['clips']])


class ClipIdTests(unittest.TestCase):
    def test_existing_clips_keep_their_position_and_new_ones_append(self):
        plan = {'clips': [{'title': 'a'}, {'title': 'b'}]}
        self.assertTrue(shorts.ensure_clip_ids(plan))
        self.assertEqual(['01', '02'], [c['id'] for c in plan['clips']])
        plan['clips'] = [plan['clips'][1], {'title': 'new'}]
        plan['rejected_clips'] = [{'id': '03', 'title': 'gone'}]
        shorts.ensure_clip_ids(plan)
        self.assertEqual(['02', '04'], [c['id'] for c in plan['clips']])

    def test_duplicate_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            shorts.validate_plan({'source_id': 's', 'clips': [{'id': '01', 'start': 0, 'end': 5}, {'id': '01', 'start': 6, 'end': 9}]}, 's', 20)

    def test_only_render_fields_change_the_signature(self):
        clip = {'id': '01', 'title': 'A', 'start': 1, 'end': 5, 'reason': 'x'}
        base = shorts.render_signature(clip, {}, {'commands_path': 'a'}, 'caps')
        renamed = shorts.render_signature({**clip, 'title': 'B', 'reason': 'y', 'approved': {'at': 't'}}, {}, {'commands_path': 'b'}, 'caps')
        moved = shorts.render_signature({**clip, 'start': 2}, {}, {'commands_path': 'a'}, 'caps')
        self.assertEqual(base, renamed)
        self.assertNotEqual(base, moved)

    def test_retitled_clip_files_are_replaced_and_removed_clips_archived(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            for stem in ['01_Old', '01_New', '02_Gone']:
                for suffix in ('.mp4', '.jpg', '.done.json'):
                    (out/(stem+suffix)).write_text('x')
            shorts.tidy_outputs(out, {'clips': [{'id': '01', 'title': 'New'}]})
            self.assertFalse((out/'01_Old.mp4').exists())
            self.assertTrue((out/'01_New.mp4').exists())
            self.assertTrue((out/'rejected'/'02_Gone.mp4').exists())
            self.assertTrue((out/'rejected'/'02_Gone.done.json').exists())

    def test_approved_clip_is_locked_until_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)/'01_A.mp4'
            dest.write_text('x'); dest.with_suffix('.done.json').write_text('{}')
            self.assertTrue(shorts.is_locked({'approved': {'at': 't'}}, dest, False))
            self.assertFalse(shorts.is_locked({'approved': {'at': 't'}}, dest, True))
            self.assertFalse(shorts.is_locked({}, dest, False))


class SourceIdTests(unittest.TestCase):
    def test_id_ignores_name_and_time_and_migration_moves_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'Raw').mkdir()
            source = root/'Raw'/'clip.mp4'
            source.write_bytes(b'video'*1000)
            copy = root/'renamed.mp4'
            copy.write_bytes(source.read_bytes())
            new = shorts.source_id(source)
            self.assertEqual(new, shorts.source_id(copy))
            old = shorts.legacy_source_id(source)
            (root/'work'/old).mkdir(parents=True)
            (root/'Review'/old).mkdir(parents=True)
            (root/'Plans').mkdir()
            (root/'Plans'/f'{old}.json').write_text(json.dumps({'source_id': old, 'clips': []}))
            profile = root/'Output'/f'clip_{old[:6]}'/'p'
            profile.mkdir(parents=True)
            save_decisions(profile, {'source_id': old, 'clips': {'01_A.mp4': {'status': 'approved'}}})
            self.assertTrue(shorts.migrate_source_id(source, new, root))
            self.assertTrue((root/'work'/new).is_dir())
            self.assertTrue((root/'Review'/new).is_dir())
            self.assertEqual(new, json.loads((root/'Plans'/f'{new}.json').read_text())['source_id'])
            moved = root/'Output'/f'clip_{new[:6]}'/'p'
            decisions = load_decisions(moved)
            self.assertEqual(new, decisions['source_id'])
            self.assertEqual({'01'}, set(decisions['clips']))  # file-name keys become ids
            self.assertFalse(shorts.migrate_source_id(source, new, root))


if __name__ == '__main__':
    unittest.main()
