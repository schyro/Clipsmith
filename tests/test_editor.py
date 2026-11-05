import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import editor_ops
from editor_server import Job
from review_apply import apply_to_plan, finalize_history, history_file, snapshot_history

WORDS = [{'start': s, 'end': s+.4, 'word': 'w'} for s in [10.0, 10.5, 11.0, 13.0, 13.5, 20.0, 20.6, 21.2]]
CONFIG = {'max_caption_words': 5}


class StagedFramingTests(unittest.TestCase):
    def plan(self):
        return {'clips': [{'id': '01', 'title': 'A', 'start': 10.0, 'end': 22.0,
                           'focus_overrides': [{'start_frame': 0, 'end_frame': 30, 'center_x': .1}]}]}

    def test_framing_is_applied_before_the_recut_shifts_it(self):
        plan = self.plan()
        staged = [{'start_frame': 60, 'end_frame': 120, 'center_x': .7}]
        decisions = {'clips': {'01': {'status': 'needs_changes', 'options': ['recut', 'fix_framing'],
                                      'start_shift': 1.0, 'end_shift': 0, 'focus_overrides': staged}}}
        handled = {}
        notes, done = apply_to_plan(plan, decisions, WORDS, 100, 'now', handled)
        clip = plan['clips'][0]
        shift = round((clip['start']-10.0)*30)
        # The staged override replaced the old one and moved with the new start.
        self.assertEqual([(60-shift, 120-shift)], [(o['start_frame'], o['end_frame']) for o in clip['focus_overrides']])
        self.assertEqual({'recut', 'fix_framing'}, handled['01'])
        self.assertIn('framing', notes['01'])
        self.assertEqual({'01'}, done)

    def test_empty_staged_list_clears_overrides_and_null_keeps_them(self):
        plan = self.plan()
        apply_to_plan(plan, {'clips': {'01': {'status': 'needs_changes', 'options': ['fix_framing'], 'focus_overrides': []}}},
                      WORDS, 100, 'now')
        self.assertNotIn('focus_overrides', plan['clips'][0])
        plan = self.plan()
        apply_to_plan(plan, {'clips': {'01': {'status': 'needs_changes', 'options': ['fix_framing']}}}, WORDS, 100, 'now')
        self.assertEqual(1, len(plan['clips'][0]['focus_overrides']))

    def test_staged_edits_are_ignored_for_approved_or_addressed_entries(self):
        for entry in [{'status': 'approved', 'focus_overrides': []},
                      {'status': 'needs_changes', 'addressed': True, 'focus_overrides': []}]:
            plan = self.plan()
            apply_to_plan(plan, {'clips': {'01': entry}}, WORDS, 100, 'now')
            self.assertEqual(1, len(plan['clips'][0]['focus_overrides']))

    def test_draft_clip_matches_apply(self):
        entry = {'status': 'needs_changes', 'options': ['recut'], 'start_shift': 1.0, 'end_shift': 0,
                 'focus_overrides': [{'start_frame': 60, 'end_frame': 120, 'center_x': .7}]}
        draft = editor_ops.draft_clip(self.plan()['clips'][0], entry, WORDS, 100)
        plan = self.plan()
        apply_to_plan(plan, {'clips': {'01': dict(entry)}}, WORDS, 100, 'now')
        self.assertEqual(plan['clips'][0], draft)
        self.assertEqual(self.plan()['clips'][0], editor_ops.draft_clip(self.plan()['clips'][0], {'status': 'approved'}, WORDS, 100))


class CaptionEditTests(unittest.TestCase):
    def test_edit_keeps_the_original_word_and_reverting_removes_it(self):
        edits = {'model': 'medium', 'edits': [{'index': 3, 'from': 'and', 'to': 'an'}]}
        editor_ops.set_caption_edit(edits, 3, 'and', 'on')
        editor_ops.set_caption_edit(edits, 1, 'calculate', ' calculated. ')
        self.assertEqual([{'index': 1, 'from': 'calculate', 'to': 'calculated.'}, {'index': 3, 'from': 'and', 'to': 'on'}],
                         edits['edits'])
        editor_ops.set_caption_edit(edits, 3, 'and', 'and')
        self.assertEqual([1], [e['index'] for e in edits['edits']])
        for bad in ['two\nlines', 'x'*65, 5]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                editor_ops.set_caption_edit(edits, 2, 'w', bad)

    def test_caption_events_follow_the_renderer_schedule(self):
        transcript = {'segments': [{'start': 10, 'end': 22, 'words': [dict(w, word=' w') for w in WORDS]}]}
        groups = editor_ops.caption_events(transcript, 10.0, 22.0, CONFIG)
        flat = [w for g in groups for w in g]
        self.assertEqual(len(WORDS), len(flat))
        self.assertEqual(0.0, flat[0]['start'])
        # A word stays until shortly after its end but never past the next onset.
        self.assertAlmostEqual(.5, flat[0]['until'])
        self.assertTrue(all(a['until'] <= b['start']+1e-9 for a, b in zip(flat, flat[1:])))


class FileTests(unittest.TestCase):
    def test_history_keeps_only_changed_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            clips = [{'id': '01', 'file': '01_A.mp4'}, {'id': '02', 'file': '02_B.mp4'}]
            (out/'clips.json').write_text(json.dumps(clips))
            (out/'01_A.mp4').write_text('old one')
            (out/'02_B.mp4').write_text('same')
            pending = snapshot_history(out)
            temp = out/'01_A.partial.mp4'
            temp.write_text('new one')
            temp.replace(out/'01_A.mp4')  # how render() replaces a file
            finalize_history(out, pending)
            self.assertEqual('old one', history_file(out, '01').read_text())
            self.assertIsNone(history_file(out, '02'))
            self.assertEqual(['01.mp4'], sorted(os.listdir(out/'history')))

    def test_export_copies_only_approved_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            clips = [{'id': '01', 'file': '01_A.mp4'}, {'id': '02', 'file': '02_B.mp4'}]
            (out/'clips.json').write_text(json.dumps(clips))
            for stem in ('01_A', '02_B'):
                (out/(stem+'.mp4')).write_text(stem)
                (out/(stem+'.en.srt')).write_text(stem)
            (out/'ready').mkdir()
            (out/'ready'/'02_B.mp4').write_text('stale')
            folder, files = editor_ops.export_ready(out, {'clips': {'01': {'status': 'approved'}, '02': {'status': 'rejected'}}})
            self.assertEqual(['01_A.en.srt', '01_A.mp4'], files)
            self.assertEqual(['01_A.en.srt', '01_A.mp4'], sorted(os.listdir(folder)))


class JobTests(unittest.TestCase):
    def test_one_job_at_a_time_and_output_is_kept(self):
        job = Job()
        import threading
        gate = threading.Event()
        job.start('preview', [sys.executable, '-c', 'print("hello"); print("progress=0.5")'], before=gate.wait)
        with self.assertRaises(RuntimeError):
            job.start('apply', [sys.executable, '-c', 'pass'])
        self.assertTrue(job.busy('preview'))
        gate.set()
        for _ in range(200):
            if not job.busy():
                break
            threading.Event().wait(.05)
        info = job.snapshot()
        self.assertEqual('done', info['state'])
        self.assertEqual(['hello'], info['log'])
        self.assertEqual(.5, info['progress'])


if __name__ == '__main__':
    unittest.main()
