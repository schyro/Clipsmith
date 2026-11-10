"""Triage of candidates before rendering, reject reasons, the preference record and
the editor's choice of Whisper model."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import editor_ops
import editor_server
from gallery import apply_decision, latest_output, validate_update
from review_apply import (apply_to_plan, awaiting_triage, preferences_markdown, rendered_ids, triage_view,
                          write_preferences)

WORDS = [{'start': s, 'end': s+.4, 'word': 'w'} for s in [10.0, 10.5, 11.0, 13.0, 13.5, 20.0, 20.6, 21.2]]


def write(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def plan_of(*ids, **extra):
    return {'source': 'Show.mp4', 'source_id': 'abcdef123456',
            'clips': [{'id': i, 'title': f'Clip {i}', 'start': 10.0*n, 'end': 10.0*n+8, 'reason': f'Reason {i}',
                       **extra.get(i, {})} for n, i in enumerate(ids, 1)]}


class DecisionTests(unittest.TestCase):
    def test_kept_is_a_status_and_reasons_belong_to_a_rejection(self):
        self.assertEqual('kept', validate_update({'status': 'kept'})['status'])
        clean = validate_update({'status': 'rejected', 'reasons': ['too_long', 'needs_context']})
        self.assertEqual(['needs_context', 'too_long'], clean['reasons'])  # in the list's order
        self.assertEqual([], validate_update({'status': 'kept', 'reasons': ['too_long']})['reasons'])
        with self.assertRaises(ValueError):
            validate_update({'status': 'rejected', 'reasons': ['boring']})

    def test_reasons_are_saved_and_cleared_with_the_rejection(self):
        decisions, clip = {'clips': {}}, {'id': '01', 'title': 'A', 'start': 1, 'end': 9}
        entry = apply_decision(decisions, clip, {'status': 'rejected', 'reasons': ['duplicate']}, 't1')
        self.assertEqual(['duplicate'], entry['reasons'])
        entry = apply_decision(decisions, clip, {'status': 'pending'}, 't2')
        self.assertNotIn('reasons', entry)
        # An unchanged save keeps the entry (and its version) as it is.
        apply_decision(decisions, clip, {'status': 'rejected', 'reasons': ['duplicate']}, 't3')
        self.assertEqual('t3', apply_decision(decisions, clip, {'status': 'rejected', 'reasons': ['duplicate']}, 't4')['updated'])


class ApplyTriageTests(unittest.TestCase):
    def test_kept_candidates_are_marked_and_rejected_ones_keep_their_reasons(self):
        plan = plan_of('01', '02', '03', '04', '05')
        decisions = {'clips': {'01': {'status': 'kept', 'updated': 'k1'},
                               '02': {'status': 'rejected', 'reasons': ['not_interesting'], 'comment': 'meh'},
                               '04': {'status': 'rejected'},
                               '05': {'status': 'needs_changes', 'comment': 'start earlier', 'updated': 'k5'}}}
        notes, done = apply_to_plan(plan, decisions, WORDS, 100, 'now', unrendered={'01', '02', '03', '05'})
        clips = {c['id']: c for c in plan['clips']}
        self.assertEqual({'at': 'k1'}, clips['01']['kept'])
        self.assertEqual({'at': 'k5'}, clips['05']['kept'])  # a note for the agent still renders it
        self.assertNotIn('kept', clips['03'])  # undecided: stays in the plan, not rendered
        rejected = {c['id']: c for c in plan['rejected_clips']}
        self.assertEqual((['not_interesting'], 'triage', 'meh'),
                         (rejected['02']['reject_reasons'], rejected['02']['rejected_stage'], rejected['02']['review_comment']))
        self.assertEqual(([], 'review'), (rejected['04']['reject_reasons'], rejected['04']['rejected_stage']))
        self.assertIn('Kept in triage', notes['01'])
        self.assertIn('Rejected in triage', notes['02'])
        self.assertEqual({'02', '04'}, done)

    def test_awaiting_triage_skips_rendered_kept_and_approved_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = plan_of('01', '02', '03', '04', **{'02': {'kept': {'at': 'x'}}, '03': {'approved': {'at': 'y'}}})
            # Rendered in any profile counts: a new profile must not send old clips back to triage.
            write(root/'Output'/'Show_abcdef'/'old-profile'/'clips.json', [{'id': '04', 'file': '04_Clip.mp4'}])
            self.assertEqual({'04'}, rendered_ids(root, plan))
            self.assertEqual(['01'], awaiting_triage(plan, root))

    def test_triage_view_leaves_out_waiting_clips_and_their_edits(self):
        plan = {**plan_of('01', '02'), 'edits': {'01': {'track': 'a'}, '02': {'track': 'b'}}}
        view = triage_view(plan, ['02'])
        self.assertEqual(['01'], [c['id'] for c in view['clips']])
        self.assertEqual(['01'], list(view['edits']))
        self.assertEqual(2, len(plan['clips']))  # the plan itself is untouched
        self.assertIs(plan, triage_view(plan, []))


class PreferenceTests(unittest.TestCase):
    def test_rejected_clips_are_grouped_by_reason_next_to_the_approved_ones(self):
        plan = plan_of('01', **{'01': {'approved': {'at': 'x'}}})
        plan['rejected_clips'] = [
            {'id': '02', 'title': 'Two', 'start': 0, 'end': 30, 'reason': 'R2', 'reject_reasons': ['too_long', 'needs_context'],
             'rejected_stage': 'triage', 'review_comment': 'way  too slow'},
            {'id': '03', 'title': 'Three', 'start': 0, 'end': 20, 'reason': 'R3', 'review_comment': ''}]
        text = preferences_markdown([plan], 'now')
        self.assertIn('| Show | 3 | 1 | 2 | 0 |', text)
        self.assertIn('| Rejected: Too long | 1 | 30.0 s |', text)
        self.assertIn('| Rejected: No reason given | 1 | 20.0 s |', text)
        self.assertIn('| Approved | 1 | 8.0 s |', text)
        self.assertIn('- Show #02 "Two", 30.0 s, rejected in triage (not rendered). Plan reason: R2 Comment: "way too slow"', text)
        self.assertEqual(2, text.count('#02 "Two"'))  # listed under both of its reasons
        self.assertIn('## Approved (1)', text)

    def test_the_record_is_rebuilt_from_every_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root/'Plans'/'a.json', plan_of('01'))
            (root/'Plans'/'broken.json').write_text('{', encoding='utf-8')
            path = write_preferences(root)
            self.assertEqual(root/'Plans'/'review-preferences.md', path)
            self.assertIn('| All | 1 | 0 | 0 | 1 |', path.read_text(encoding='utf-8'))


class ModelTests(unittest.TestCase):
    def test_the_editor_uses_the_model_the_source_was_transcribed_with(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            self.assertEqual('medium', editor_ops.source_model(cache))
            write(cache/'transcript-large-v3.json', {})
            write(cache/'transcript-large-v3-english.json', {})
            self.assertEqual('large-v3', editor_ops.source_model(cache))
            write(cache/'transcript-small.json', {})
            with self.assertRaises(ValueError):
                editor_ops.source_model(cache)
            write(cache/'last-render.json', {'output': 'x', 'model': 'small'})
            self.assertEqual('small', editor_ops.source_model(cache))
            self.assertEqual('base', editor_ops.source_model(cache, 'base'))


class EditorStateTests(unittest.TestCase):
    def state(self, root, output):
        state = editor_server.State.__new__(editor_server.State)
        state.root, state.source_id, state.lock = root, 'abcdef123456', threading.RLock()
        state.load(output)
        return state

    def test_the_clip_list_is_the_plan_with_files_for_rendered_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root/'Output'/'Show_abcdef'/'profile'
            write(root/'Plans'/'abcdef123456.json', plan_of('01', '02', '03'))
            # Nothing rendered yet: every clip is a candidate, and the folder need not exist.
            state = self.state(root, output)
            self.assertEqual([False]*3, [c['rendered'] for c in state.clips])
            write(output/'clips.json', [{**plan_of('01')['clips'][0], 'file': '01_Clip_01.mp4'},
                                        {'id': '02', 'start': 20.0, 'end': 28.0, 'title': 'Clip 02', 'file': '02_Clip_02.mp4'}])
            (output/'01_Clip_01.mp4').write_bytes(b'')
            state = self.state(root, output)
            self.assertEqual([('01', True, '01_Clip_01.mp4'), ('02', False, ''), ('03', False, '')],
                             [(c['id'], c['rendered'], c['file']) for c in state.clips])

    def test_the_editor_opens_an_output_folder_or_a_plan_that_has_not_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root/'Output'/'Show_abcdef'/'profile'
            write(output/'review-decisions.json', {'source_id': 'abcdef123456', 'clips': {}})
            self.assertEqual((output.resolve(), 'abcdef123456'), editor_server.editor_target(root, str(output), config={}))
            self.assertEqual(output, latest_output(root))  # decisions alone mark a triaged folder
            source = root/'Raw'/'Show.mp4'
            source.parent.mkdir()
            source.write_bytes(b'video')
            import shorts
            sid = shorts.source_id(source)
            write(root/'Plans'/f'{sid}.json', {**plan_of('01'), 'source_id': sid})
            with mock.patch('shorts.output_folder', lambda plan, config: root/'Output'/'Show_x'/config['profile']):
                folder, found = editor_server.editor_target(root, '', source, {'profile': 'p'})
            self.assertEqual((root/'Output'/'Show_x'/'p', sid), (folder, found))

    def test_a_candidate_without_analysis_gets_a_fixed_crop(self):
        stream = {'codec_type': 'video', 'width': 1920, 'height': 1080}
        with mock.patch('shorts.probe', return_value={'streams': [stream]}):
            focus = editor_ops.centered_focus('x.mp4', {'start': 10.0, 'end': 12.0, 'crop_x': .5},
                                              {'width': 1080, 'height': 1350})
        self.assertEqual((60, 1), (focus['frame_count'], len(focus['shots'])))
        self.assertEqual({.5}, {round(f['center_x'], 6) for f in focus['frames']})


if __name__ == '__main__':
    unittest.main()
