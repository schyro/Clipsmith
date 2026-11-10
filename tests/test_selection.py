import unittest

from selection import block_label, check_reviewed_plan, dialogue_blocks, expected_clip_count

REASON = 'Only exposition that depends on the previous scene.'


def transcript(duration, every=2.0):
    """One spoken word every `every` seconds, each 0.5 s long."""
    words, t = [], 1.0
    while t+.5 < duration:
        words.append({'start': t, 'end': t+.5, 'word': ' word.'})
        t += every
    return {'segments': [{'start': 0, 'end': duration, 'text': '', 'words': words}]}


def clip(number, start, length=20):
    # Words start at odd seconds; an even start sits in the gap before one.
    return {'id': f'{number:02}', 'start': start, 'end': start+length, 'title': 't', 'reason': 'r'}


def plan(clips, coverage, **extra):
    return {'source_id': 's', 'selection': 'agent-reviewed', 'clips': clips, 'coverage': coverage, **extra}


class SelectionChecks(unittest.TestCase):
    def assertProblems(self, data, duration, *fragments):
        with self.assertRaises(ValueError) as caught:
            check_reviewed_plan(data, duration, transcript(duration))
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))
        return str(caught.exception)

    def test_missing_coverage_names_every_block_start(self):
        text = self.assertProblems(plan([clip(1, 10)], None), 600, 'no "coverage" list')
        self.assertIn('0 (00:00–05:00)', text)
        self.assertIn('300 (05:00–10:00)', text)

    def test_complete_coverage_passes(self):
        clips = [clip(n, s) for n, s in enumerate([10, 60, 120, 320, 400], 1)]
        check_reviewed_plan(plan(clips, [{'start': 0, 'note': 'a'}, {'start': 300, 'note': 'b'}]), 600, transcript(600))

    def test_every_problem_is_listed_at_once(self):
        clips = [clip(n, s) for n, s in enumerate([10, 60, 120, 180, 240], 1)]
        text = self.assertProblems(plan(clips, [{'start': 0, 'note': ''}]), 600,
                                   '00:00–05:00: "note" is empty', 'Block 05:00–10:00 (start 300) has no coverage entry',
                                   'Block 05:00–10:00 has no clip')
        self.assertEqual(text.count('\n  - '), 3)

    def test_missing_list_still_reports_empty_and_sparse_blocks(self):
        self.assertProblems(plan([clip(1, 10)], None), 700, 'no "coverage" list',
                            'Block 05:00–10:00 has no clip', 'Block 10:00–11:40 has no clip',
                            'Re-read the blocks with one clip (00:00–05:00)')

    def test_block_without_clip_needs_a_reason(self):
        clips = [clip(n, s) for n, s in enumerate([10, 60, 120, 180, 240], 1)]
        coverage = [{'start': 0, 'note': 'a'}, {'start': 300, 'note': 'b'}]
        self.assertProblems(plan(clips, coverage), 600, 'Block 05:00–10:00 has no clip')
        coverage[1]['why_not_more'] = REASON
        check_reviewed_plan(plan(clips, coverage), 600, transcript(600))

    def test_short_tail_joins_the_previous_block(self):
        self.assertEqual(dialogue_blocks(transcript(609), 609), [0, 300])
        self.assertEqual(block_label(300, 609), '05:00–10:09')
        self.assertEqual(dialogue_blocks(transcript(700), 700), [0, 300, 600])

    def test_sparse_plan_asks_for_reasons_on_single_clip_blocks(self):
        self.assertEqual(expected_clip_count(600), 5)
        clips = [clip(1, 10), clip(2, 60), clip(3, 320)]
        coverage = [{'start': 0, 'note': 'a'}, {'start': 300, 'note': 'b'}]
        self.assertProblems(plan(clips, coverage), 600, 'Re-read the blocks with one clip (05:00–10:00)')
        coverage[1]['why_not_more'] = REASON
        check_reviewed_plan(plan(clips, coverage), 600, transcript(600))

    def test_rejected_clips_count_towards_the_expected_number(self):
        clips = [clip(1, 10), clip(2, 60), clip(3, 320)]
        rejected = [clip(4, 120), clip(5, 400)]
        coverage = [{'start': 0, 'note': 'a'}, {'start': 300, 'note': 'b'}]
        check_reviewed_plan(plan(clips, coverage, rejected_clips=rejected), 600, transcript(600))

    def test_count_note_from_the_user_lifts_the_count_check_only(self):
        clips = [clip(1, 10)]
        coverage = [{'start': 0, 'note': 'a'}, {'start': 300, 'note': 'b'}]
        self.assertProblems(plan(clips, coverage, count_note='The user asked for one clip.'), 600,
                            'Block 05:00–10:00 has no clip')
        coverage[1]['why_not_more'] = REASON
        check_reviewed_plan(plan(clips, coverage, count_note='The user asked for one clip.'), 600, transcript(600))

    def test_unknown_or_duplicate_blocks_are_reported(self):
        clips = [clip(n, s) for n, s in enumerate([10, 60, 120, 320, 400], 1)]
        coverage = [{'start': 0, 'note': 'a'}, {'start': 0, 'note': 'a'}, {'start': 300, 'note': 'b'}, {'start': 450, 'note': 'c'}]
        self.assertProblems(plan(clips, coverage), 600, 'more than one coverage entry', 'Unknown coverage entry')

    def test_edge_inside_a_word_is_rejected_and_a_gap_passes(self):
        clips = [clip(n, s) for n, s in enumerate([10, 60, 120, 320, 400], 1)]
        coverage = [{'start': 0, 'note': 'a'}, {'start': 300, 'note': 'b'}]
        clips[0]['end'] = 31.2  # inside the word at 31.0-31.5
        self.assertProblems(plan(clips, coverage), 600,
                            'Clip 01 end 31.20 cuts "word." (31.00–31.50); move it to 31.00 or earlier, or 31.50 or later')
        clips[0]['end'] = 31.6
        check_reviewed_plan(plan(clips, coverage), 600, transcript(600))

    def test_editor_reviewed_plans_skip_coverage_but_keep_the_edge_check(self):
        data = {'source_id': 's', 'selection': 'editor-reviewed', 'clips': [clip(1, 10)]}
        check_reviewed_plan(data, 600, transcript(600))
        data['clips'][0]['start'] = 11.2
        self.assertProblems(data, 600, 'Clip 01 start 11.20 cuts')


class EditProblemsDoNotBlockClips(unittest.TestCase):
    def broken_edit_plan(self):
        clips = [clip(n, s) for n, s in enumerate([10, 60, 120, 180, 240], 1)]
        data = plan(clips, [{'start': 0, 'note': 'a'}, {'start': 300, 'note': 'b', 'why_not_more': REASON}])
        data['edits'] = {'01': {'track': 'missing-track.mp3', 'payoff_word': 3}}
        return data

    def test_missing_track_is_listed_by_default_but_not_when_edits_are_excluded(self):
        data = self.broken_edit_plan()
        with self.assertRaises(ValueError) as caught:
            check_reviewed_plan(data, 600, transcript(600))
        self.assertIn('Edit 01', str(caught.exception))
        check_reviewed_plan(data, 600, transcript(600), include_edits=False)

    def test_clip_problems_still_raise_without_edits(self):
        data = self.broken_edit_plan()
        data['clips'][0]['start'] = 11.2
        with self.assertRaises(ValueError) as caught:
            check_reviewed_plan(data, 600, transcript(600), include_edits=False)
        self.assertIn('cuts', str(caught.exception))
        self.assertNotIn('Edit 01', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
