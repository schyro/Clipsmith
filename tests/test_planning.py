import unittest
from shorts import propose, validate_plan, sentences


class PlanningTests(unittest.TestCase):
    def test_final_partial_frame_can_be_reviewed_without_rounding_it_away(self):
        validate_plan({'source_id':'source','clips':[{'start':0,'end':1.01,
            'focus_overrides':[{'start_frame':30,'end_frame':31,'center_x':.5}]}]},'source',2)

    def test_focus_overrides_reject_overlap_and_out_of_clip_ranges(self):
        for overrides in [
            [{'start_frame':0,'end_frame':90,'center_x':.5},{'start_frame':60,'end_frame':120,'center_x':.8}],
            [{'start_frame':0,'end_frame':301,'center_x':.5}],
            [{'start_frame':0,'end_frame':90,'center_x':1.5}]
        ]:
            with self.subTest(overrides=overrides),self.assertRaises(ValueError):
                validate_plan({'source_id':'source','clips':[{'start':100,'end':110,'focus_overrides':overrides}]},'source',120)

    def test_candidates_stay_in_source_and_do_not_overlap(self):
        transcript = {'segments': [
            {'start': i*4+.2, 'end': i*4+3.8,
             'text': ('Why did he lie about this?' if i%6==0 else 'Here is all of the evidence.')}
            for i in range(60)
        ]}
        clips = propose(transcript, 240, 3)
        self.assertEqual(3, len(clips))
        validate_plan({'source_id':'source','clips':clips}, 'source', 240)
        ordered = sorted(clips, key=lambda c:c['start'])
        self.assertTrue(all(a['end'] <= b['start'] for a,b in zip(ordered,ordered[1:])))
        self.assertTrue(all(24 <= c['end']-c['start'] <= 59 for c in clips))

    def test_other_sources_plan_and_out_of_bounds_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan({'source_id':'other','clips':[{'start':0,'end':10}]}, 'source', 20)
        with self.assertRaises(ValueError):
            validate_plan({'source_id':'source','clips':[{'start':5,'end':30}]}, 'source', 20)

    def test_word_timing_preserves_pause_boundaries(self):
        transcript = {'segments':[{'words':[
            {'start':1,'end':1.4,'word':' Hello'},
            {'start':1.4,'end':2,'word':' there.'},
            {'start':3,'end':3.5,'word':' Stop!'}]}]}
        result = sentences(transcript)
        self.assertEqual([(1,2),(3,3.5)],[(s['start'],s['end']) for s in result])

    def test_long_dialogue_break_is_not_crossed_to_fill_a_clip(self):
        transcript = {'segments':[
            {'start':10+i*4,'end':13.8+i*4,'text':'Why is all of this evidence here?'} for i in range(8)
        ] + [
            {'start':60+i*4,'end':63.8+i*4,'text':'Here is a completely different story.'} for i in range(8)
        ]}
        for clip in propose(transcript,100,5):
            self.assertFalse(clip['start'] < 45 and clip['end'] > 60)


if __name__ == '__main__':
    unittest.main()
