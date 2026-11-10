import json
from pathlib import Path
import tempfile
import unittest
from gallery import (DECISIONS_FILE, actionable, apply_decision, check_overrides_against_shots, empty_decisions,
                     load_decisions, parse_range, render_page, save_decisions, validate_update, write_gallery,
                     _resolve_inside)

CLIP = {'id': '01', 'file': '01_A.mp4', 'title': 'A', 'start': 10.0, 'end': 40.0, 'reason': 'Setup and payoff.'}


class DecisionTests(unittest.TestCase):
    def test_first_rejection_saves_before_the_output_profile_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'source'/'profile'
            decisions = empty_decisions({'source_id': 's'}, output.name)
            candidate = {**CLIP, 'file': '', 'rendered': False}
            apply_decision(decisions, candidate, {'status': 'rejected', 'reasons': ['too_long']})
            save_decisions(output, decisions)
            self.assertEqual(decisions, load_decisions(output))

    def test_validation_rejects_unknown_status_and_options(self):
        for bad in [{'status': 'maybe'}, {'options': ['fix_captions']}, {'options': 'recut'}, {'comment': 5}, [],
                    {'start_shift': float('inf')}, {'end_shift': '1'}, {'start_shift': True}, {'focus_overrides': {}},
                    {'focus_overrides': [{'start_frame': 5, 'end_frame': 5, 'center_x': .5}]},
                    {'focus_overrides': [{'start_frame': 0, 'end_frame': 9, 'center_x': 1.5}]},
                    {'focus_overrides': [{'start_frame': 0, 'end_frame': 9}]},
                    {'focus_overrides': [{'start_frame': 0.0, 'end_frame': 9, 'center_x': .5}]},
                    {'focus_overrides': [{'start_frame': 0, 'end_frame': 9, 'center_x': .5, 'zoom': 2}]},
                    {'focus_overrides': [{'start_frame': 0, 'end_frame': 9, 'center_x': .5},
                                         {'start_frame': 8, 'end_frame': 20, 'center_x': .5}]}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_update(bad)
        self.assertEqual({'status': 'needs_changes', 'options': ['recut', 'fix_framing'], 'comment': 'x',
                          'start_shift': 0.0, 'end_shift': 0.0, 'focus_overrides': None, 'notes': [], 'reasons': []},
                         validate_update({'status': 'needs_changes', 'options': ['fix_framing', 'recut'], 'comment': ' x '}))
        # Edges are not limited to a fixed distance; the editor server keeps them inside the source.
        self.assertEqual(95.5, validate_update({'start_shift': 95.5})['start_shift'])

    def test_staged_overrides_are_sorted_and_checked_against_cuts(self):
        clean = validate_update({'status': 'needs_changes', 'focus_overrides': [
            {'start_frame': 30, 'end_frame': 60, 'center_x': .7, 'center_x_end': .2},
            {'start_frame': 0, 'end_frame': 30, 'center_x': .123456}]})
        self.assertEqual([0, 30], [o['start_frame'] for o in clean['focus_overrides']])
        self.assertEqual(.1235, clean['focus_overrides'][0]['center_x'])
        shots = [{'start_frame': 0, 'end_frame': 30}, {'start_frame': 30, 'end_frame': 90}]
        check_overrides_against_shots(clean['focus_overrides'], shots, 90)
        with self.assertRaises(ValueError):
            check_overrides_against_shots([{'start_frame': 20, 'end_frame': 40, 'center_x': .5}], shots, 90)
        with self.assertRaises(ValueError):
            check_overrides_against_shots([{'start_frame': 40, 'end_frame': 91, 'center_x': .5}], shots, 90)
        decisions = empty_decisions({}, 'p')
        entry = apply_decision(decisions, CLIP, {'status': 'needs_changes', 'focus_overrides': clean['focus_overrides']})
        self.assertEqual(2, len(entry['focus_overrides']))
        entry = apply_decision(decisions, CLIP, {'status': 'needs_changes'})
        self.assertNotIn('focus_overrides', entry)  # null drops staged framing

    def test_user_edit_reopens_an_addressed_decision(self):
        decisions = empty_decisions({'source_id': 's'}, 'p')
        apply_decision(decisions, CLIP, {'status': 'needs_changes', 'options': ['recut'], 'comment': 'Start later'}, 't1')
        decisions['clips']['01'].update({'addressed': True, 'agent_note': 'Moved start'})
        same = apply_decision(decisions, CLIP, {'status': 'needs_changes', 'options': ['recut'], 'comment': 'Start later'}, 't2')
        self.assertTrue(same['addressed'])
        self.assertEqual('t1', same['updated'])
        changed = apply_decision(decisions, CLIP, {'status': 'rejected'}, 't3')
        self.assertFalse(changed['addressed'])
        self.assertNotIn('agent_note', changed)
        self.assertEqual({'title': 'A', 'start': 10.0, 'end': 40.0}, {k: changed[k] for k in ('title', 'start', 'end')})

    def test_notes_are_sorted_trimmed_and_empty_ones_dropped(self):
        for bad in [{'notes': {}}, {'notes': [{'t': -1, 'text': 'x'}]}, {'notes': [{'t': '3', 'text': 'x'}]},
                    {'notes': [{'t': 3, 'text': 'x'*501}]}, {'notes': [{'t': 3, 'text': 'x', 'by': 'me'}]},
                    {'notes': [{'t': 1, 'text': 'x'}]*51}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_update(bad)
        notes = validate_update({'notes': [{'t': 20.1234, 'text': ' Keep  the pause '}, {'t': 12, 'text': 'Start here'},
                                           {'t': 15, 'text': '  '}]})['notes']
        self.assertEqual([{'t': 12.0, 'text': 'Start here'}, {'t': 20.123, 'text': 'Keep the pause'}], notes)

    def test_notes_are_stored_reopen_the_entry_and_show_in_the_gallery(self):
        decisions = empty_decisions({'source_id': 's'}, 'p')
        entry = apply_decision(decisions, CLIP, {'status': 'needs_changes', 'notes': [{'t': 12.5, 'text': 'Cut here'}]}, 't1')
        self.assertEqual([{'t': 12.5, 'text': 'Cut here'}], entry['notes'])
        entry.update({'addressed': True})
        same = apply_decision(decisions, CLIP, {'status': 'needs_changes', 'notes': [{'t': 12.5, 'text': 'Cut here'}]}, 't2')
        self.assertTrue(same['addressed'])
        cleared = apply_decision(decisions, CLIP, {'status': 'needs_changes'}, 't3')
        self.assertNotIn('notes', cleared)
        self.assertFalse(cleared['addressed'])
        plain = apply_decision(empty_decisions({}, 'p'), CLIP, {'status': 'pending'})
        self.assertNotIn('notes', plain)
        decisions['clips']['01']['notes'] = [{'t': 12.5, 'text': '<b>Cut</b> here'}]
        page = render_page([CLIP], {'source': 'a.mp4'}, 'p', decisions)
        self.assertIn('0:02.50', page)
        self.assertIn('&lt;b&gt;Cut&lt;/b&gt; here', page)

    def test_actionable_lists_only_open_change_requests(self):
        clips = {'a': {'status': 'approved'}, 'b': {'status': 'needs_changes'}, 'c': {'status': 'rejected', 'addressed': True},
                 'd': {'status': 'rejected'}, 'e': {'status': 'pending'}}
        self.assertEqual(['b', 'd'], sorted(actionable({'clips': clips})))


class RangeTests(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual((0, 99), parse_range('bytes=0-', 100))
        self.assertEqual((10, 19), parse_range('bytes=10-19', 100))
        self.assertEqual((90, 99), parse_range('bytes=-10', 100))
        self.assertEqual((50, 99), parse_range('bytes=50-500', 100))
        for bad in [None, '', 'bytes=-', 'bytes=100-', 'bytes=20-10', 'items=0-1', 'bytes=0-1,5-6', 'bytes=-0']:
            with self.subTest(bad=bad):
                self.assertIsNone(parse_range(bad, 100))


class PageTests(unittest.TestCase):
    def test_page_escapes_titles_and_embedded_json(self):
        clip = {**CLIP, 'title': '</script><b>x</b>', 'file': '01 A&B.mp4'}
        decisions = empty_decisions({}, 'p')
        apply_decision(decisions, clip, {'status': 'approved', 'comment': '</script>alert(1)'})
        page = render_page([clip], {}, 'p', decisions, 'focus%20dir')
        self.assertNotIn('<script', page)  # the static overview is read-only and has no script
        self.assertNotIn('<b>x</b>', page)
        self.assertIn('&lt;/script&gt;alert(1)', page)
        self.assertIn('src="01%20A%26B.mp4"', page)
        self.assertIn('href="focus%20dir/01%20A%26B.jpg"', page)

    def test_write_gallery_keeps_decisions_and_links_focus_sheets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root/'Output'/'src 1'/'profile'
            out.mkdir(parents=True)
            (root/'Review'/'abc'/'focus').mkdir(parents=True)
            (out/'clips.json').write_text(json.dumps([CLIP]), encoding='utf-8')
            (out/'plan.json').write_text(json.dumps({'source_id': 'abc', 'source': 'src 1.mp4'}), encoding='utf-8')
            decisions = empty_decisions({}, 'profile')
            apply_decision(decisions, CLIP, {'status': 'approved'})
            save_decisions(out, decisions)
            before = (out/DECISIONS_FILE).read_bytes()
            page = write_gallery(out, root).read_text(encoding='utf-8')
            self.assertEqual(before, (out/DECISIONS_FILE).read_bytes())
            self.assertIn('href="../../../Review/abc/focus/01_A.jpg"', page)

    def test_server_paths_stay_inside_output_and_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNotNone(_resolve_inside(root, '/Output/src%201/p/01.mp4'))
            self.assertIsNotNone(_resolve_inside(root, '/Review/abc/focus/01.jpg'))
            for bad in ['/shorts.py', '/Output/../shorts.py', '/Output/%2e%2e/Plans/x.json', '/Raw/a.mp4']:
                with self.subTest(bad=bad):
                    self.assertIsNone(_resolve_inside(root, bad))


if __name__ == '__main__':
    unittest.main()
