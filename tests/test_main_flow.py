import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import shorts

PLAN = {'source': 'a.mp4', 'source_id': 'sid', 'selection': 'agent-reviewed',
        # Kept in the editor's triage: a candidate nobody kept is not rendered.
        'clips': [{'id': '01', 'start': 10, 'end': 30, 'title': 't', 'reason': 'r', 'kept': {'at': '2026-01-01'}}],
        'edits': {'01': {'track': 'missing.mp3', 'payoff_word': 1}}}


class MainFlow(unittest.TestCase):
    def run_main(self, argv, edit_problems, render_error=None, check_error=None):
        calls = []

        def render_edits(*args):
            calls.append('edits')
            if render_error:
                raise ValueError(render_error)

        def check(plan, duration, transcript, include_edits=True):
            calls.append(f'check edits={include_edits}')
            if check_error:
                raise ValueError(check_error)

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'a.mp4'
            source.write_bytes(b'')
            (Path(tmp)/'Plans').mkdir()
            (Path(tmp)/'Plans'/'sid.json').write_text('{}')
            patches = [
                mock.patch.object(shorts, 'ROOT', Path(tmp)),
                mock.patch.object(shorts, 'migrate_all'), mock.patch.object(shorts, 'migrate_source_id'),
                mock.patch.object(shorts, 'settings', return_value={}),
                mock.patch.object(shorts, 'source_id', return_value='sid'),
                mock.patch.object(shorts, 'probe', return_value={'streams': [{'codec_type': 'audio'}], 'format': {'duration': '600'}}),
                mock.patch.object(shorts, 'load_plan', side_effect=lambda *a: dict(PLAN)),
                mock.patch.object(shorts, 'english_transcript', return_value={'segments': []}),
                mock.patch.object(shorts, 'render_plan', side_effect=lambda *a: calls.append('clips') or Path(tmp)),
                mock.patch('selection.check_reviewed_plan', side_effect=check),
                mock.patch('selection.edit_plan_problems', return_value=edit_problems),
                mock.patch('verify_outputs.verify_folder', side_effect=lambda *a: calls.append('verify')),
                mock.patch('edits.render_edits', side_effect=render_edits),
                mock.patch('edits.preview_edits', return_value=([], [])),
                mock.patch.object(sys, 'argv', ['shorts', '--input', str(source), *argv]),
            ]
            err = io.StringIO()
            with contextlib.ExitStack() as stack:
                for p in patches:
                    stack.enter_context(p)
                stack.enter_context(contextlib.redirect_stderr(err))
                code = shorts.main()
        return code, calls, err.getvalue()

    def test_edit_problems_render_clips_and_other_edits_but_fail_the_run(self):
        code, calls, err = self.run_main(['--require-reviewed'], ['Edit 01: the track is missing.'])
        self.assertEqual(['check edits=False', 'clips', 'verify', 'edits'], calls)
        self.assertEqual(1, code)
        self.assertIn('Edit 01: the track is missing.', err)
        self.assertIn('music edit problem', err)

    def test_analyze_only_checks_the_edits_and_renders_nothing(self):
        code, calls, err = self.run_main(['--require-reviewed', '--analyze-only'], [],
                                         check_error='Edit 01: the track is missing.')
        self.assertEqual(['check edits=True'], calls)
        self.assertEqual(1, code)
        self.assertIn('Edit 01', err)

    def test_clean_plan_succeeds(self):
        code, calls, _ = self.run_main(['--require-reviewed'], [])
        self.assertEqual(0, code)
        self.assertEqual(['check edits=False', 'clips', 'verify', 'edits'], calls)

    def test_debug_prints_the_traceback_and_default_does_not(self):
        quiet = self.run_main(['--require-reviewed'], [], render_error='boom')
        loud = self.run_main(['--require-reviewed', '--debug'], [], render_error='boom')
        self.assertEqual(1, quiet[0])
        self.assertIn('boom', quiet[2])
        self.assertNotIn('Traceback', quiet[2])
        self.assertIn('Traceback (most recent call last)', loud[2])
        self.assertIn('boom', loud[2])


if __name__ == '__main__':
    unittest.main()
