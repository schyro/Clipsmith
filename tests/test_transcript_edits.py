import unittest
from types import SimpleNamespace

from captions import caption_groups, clip_words
from missed_speech import find_gaps, keep_words, windows_for
from transcript_edits import add_words, merged_transcript, update_added, update_edit

BASE = {'language': 'en', 'segments': [
    {'start': 10.0, 'end': 11.0, 'text': 'Hello there.', 'words': [
        {'start': 10.0, 'end': 10.4, 'word': ' Hello'}, {'start': 10.5, 'end': 11.0, 'word': ' there.'}]},
    {'start': 20.0, 'end': 20.5, 'text': 'Amazed.', 'words': [{'start': 20.0, 'end': 20.5, 'word': ' Amazed.'}]}]}
GAPS = {'segments': [{'start': 15.0, 'end': 15.6, 'text': 'Oh, God.', 'words': [
    {'id': 'g0', 'start': 15.0, 'end': 15.2, 'word': ' Oh,'}, {'id': 'g1', 'start': 15.25, 'end': 15.6, 'word': ' God.'}]}]}


def flat(transcript):
    return [w for s in transcript['segments'] for w in s.get('words', [])]


class MergeTests(unittest.TestCase):
    def test_missed_words_are_merged_in_time_order_without_renumbering(self):
        words = flat(merged_transcript(BASE, GAPS, None, 'medium'))
        self.assertEqual([0, 1, 'g0', 'g1', 2], [w['index'] for w in words])
        self.assertEqual(['Hello', 'there.', 'Oh,', 'God.', 'Amazed.'], [w['word'].strip() for w in words])
        self.assertNotIn('index', BASE['segments'][0]['words'][0])  # the transcript itself is untouched

    def test_edits_fix_text_timing_and_lines_and_added_words_join(self):
        edits = {'model': 'medium', 'edits': [{'index': 1, 'from': 'there.', 'to': 'there!', 'start': 10.45},
                                              {'index': 'g1', 'from': 'God.', 'line': 'break'}],
                 'added': [{'id': 'u1', 'start': 19.0, 'end': 19.4, 'word': "She's"}]}
        words = {w['index']: w for w in flat(merged_transcript(BASE, GAPS, edits, 'medium'))}
        self.assertEqual((' there!', 10.45, 11.0), (words[1]['word'], words[1]['start'], words[1]['end']))
        self.assertEqual('break', words['g1']['line'])
        self.assertEqual(" She's", words['u1']['word'])

    def test_a_stale_edit_is_refused(self):
        with self.assertRaises(ValueError):
            merged_transcript(BASE, GAPS, {'model': 'medium', 'edits': [{'index': 'g0', 'from': 'Ah,', 'to': 'Oh,'}]}, 'medium')

    def test_update_edit_drops_changes_that_restore_the_original(self):
        edits = {'model': 'medium', 'edits': [], 'added': []}
        original = {'word': ' there.', 'start': 10.5, 'end': 11.0}
        update_edit(edits, 1, original, {'start': 10.6, 'line': 'join'})
        self.assertEqual([{'index': 1, 'from': 'there.', 'start': 10.6, 'line': 'join'}], edits['edits'])
        update_edit(edits, 1, original, {'start': None, 'line': None})
        self.assertEqual([], edits['edits'])
        for bad in ({'start': 11.5}, {'line': 'wrap'}, {'to': 'a\nb'}, {'zoom': 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                update_edit(edits, 1, original, bad)

    def test_added_words_get_new_ids_and_an_empty_word_removes_one(self):
        edits = {'model': 'medium', 'edits': [], 'added': []}
        self.assertEqual(['u1', 'u2'], add_words(edits, [{'word': 'Oh,', 'start': 1, 'end': 1.2},
                                                         {'word': 'God.', 'start': 1.25, 'end': 1.5}]))
        update_added(edits, 'u2', {'end': 1.7})
        self.assertEqual(1.7, edits['added'][1]['end'])
        update_added(edits, 'u1', {'to': ''})
        self.assertEqual(['u2'], [a['id'] for a in edits['added']])
        with self.assertRaises(ValueError):
            add_words(edits, [{'word': '', 'start': 1, 'end': 2}])


class LineTests(unittest.TestCase):
    def words(self, line=None, at=2):
        words = [{'start': i*.3, 'end': i*.3+.25, 'word': w} for i, w in enumerate(['one', 'two', 'three', 'four'])]
        if line:
            words[at]['line'] = line
        return words

    def test_break_starts_a_new_line(self):
        self.assertEqual([['one', 'two'], ['three', 'four']],
                         [[w['word'] for w in g] for g in caption_groups(self.words('break'))])

    def test_join_keeps_a_sentence_end_and_a_pause_on_one_line(self):
        words = [{'start': 0, 'end': .3, 'word': 'Yes.'}, {'start': 1.2, 'end': 1.5, 'word': 'Yes.', 'line': 'join'}]
        self.assertEqual(1, len(caption_groups(words)))
        words[1].pop('line')
        self.assertEqual(2, len(caption_groups(words)))

    def test_join_never_exceeds_one_line(self):
        words = [{'start': 0, 'end': .3, 'word': 'x'*20}, {'start': .4, 'end': .7, 'word': 'y'*20, 'line': 'join'}]
        self.assertEqual(2, len(caption_groups(words)))

    def test_clip_words_keep_the_line_override(self):
        transcript = {'segments': [{'words': [{'start': 1, 'end': 1.3, 'word': ' a'}, {'start': 1.4, 'end': 1.7, 'word': ' b', 'line': 'break'}]}]}
        self.assertEqual([None, 'break'], [w.get('line') for w in clip_words(transcript, {'start': 0, 'end': 3})])


class MissedSpeechTests(unittest.TestCase):
    def test_gaps_cover_stretches_without_words(self):
        words = flat(BASE)
        gaps = find_gaps(words, 25.0)
        self.assertEqual([(0.0, 10.0), (11.0, 20.0), (20.5, 25.0)], [(a, b) for a, b, *_ in gaps])

    def test_windows_stay_short(self):
        windows = windows_for((0.0, 100.0, None, None), [(5.0, 70.0)], 100.0)
        self.assertTrue(all(b-a <= 28.0+1e-9 for a, b in windows))
        self.assertLessEqual(windows[0][0], 4.4)
        self.assertGreaterEqual(windows[-1][1], 70.6)

    def test_hallucinations_and_edge_duplicates_are_dropped(self):
        def seg(nsp, lp, *words):
            return SimpleNamespace(no_speech_prob=nsp, avg_logprob=lp, compression_ratio=1.0,
                                   words=[SimpleNamespace(start=s, end=e, word=' '+w, probability=p) for s, e, w, p in words])
        gap = (11.0, 20.0, {'word': ' there.'}, {'word': ' Amazed.'})
        segments = [seg(.85, -.85, (1.0, 1.4, 'Thanks', .01), (1.4, 1.8, 'for', .9), (1.8, 2.2, 'watching!', .95)),
                    seg(.05, -.7, (4.0, 4.3, 'Oh,', .8), (4.3, 4.7, 'God.', .6)),
                    seg(.2, -.5, (8.5, 8.9, 'amazed.', .9), (9.5, 9.8, 'later', .9))]
        kept = keep_words(segments, 11.0, gap)
        self.assertEqual([['Oh,', 'God.']], [[w['word'].strip() for w in s['words']] for s in kept])


if __name__ == '__main__':
    unittest.main()
