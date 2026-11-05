import tempfile
from pathlib import Path
import unittest
from captions import write_captions, clip_words, caption_groups, animation_tags
import re


class CaptionTests(unittest.TestCase):
    def test_sentence_holds_and_overlapping_asr_stay_in_one_lane(self):
        transcript={'language':'en','segments':[{'words':[
            {'start':0,'end':.55,'word':' First.'},
            {'start':.56,'end':.9,'word':' Next.'},
            {'start':.82,'end':1.1,'word':' Overlapping.'},
            {'start':1.105,'end':1.4,'word':' Last.'}]}]}
        config={'width':1080,'height':1350,'font':'Dosis','font_size':64,
                'subtitle_y':675,'highlight':'&H0068E8FF','max_caption_words':5}
        with tempfile.TemporaryDirectory() as tmp:
            ass,srt=Path(tmp)/'test.ass',Path(tmp)/'test.srt'
            write_captions(transcript,{'start':0,'end':1.5},ass,srt,config)
            def seconds(value):
                h,m,s=value.replace(',','.').split(':')
                return int(h)*3600+int(m)*60+float(s)
            intervals=[tuple(map(seconds,line.split(',')[1:3])) for line in ass.read_text(encoding='utf-8-sig').splitlines() if line.startswith('Dialogue:')]
            self.assertTrue(all(a[1]<=b[0] for a,b in zip(intervals,intervals[1:])))
            cues=[tuple(map(seconds,line.split(' --> '))) for line in srt.read_text().splitlines() if ' --> ' in line]
            self.assertTrue(all(a[1]<=b[0] for a,b in zip(cues,cues[1:])))

    def test_group_entrance_does_not_restart_on_the_second_word(self):
        first=animation_tags(0,4,0,False,{})
        second=animation_tags(4,12,0,True,{})
        a1=list(map(int,re.search(r'fade\(([^)]+)',first).group(1).split(',')))
        a2=list(map(int,re.search(r'fade\(([^)]+)',second).group(1).split(',')))
        self.assertEqual(a1[1],a2[0])
        for fade in (a1,a2):
            self.assertEqual(sorted(fade[3:]),fade[3:])

    def test_word_onsets_and_clip_limits_survive_subtitle_export(self):
        transcript = {'language':'en','segments':[{'words':[
            {'start':10.1,'end':10.5,'word':' Hello'},
            {'start':10.6,'end':11.2,'word':' world.'},
            {'start':12,'end':12.5,'word':' Goodbye.'}]}]}
        config = {'width':1080,'height':1350,'font':'Dosis','font_size':64,
                  'subtitle_y':675,'highlight':'&H0068E8FF','max_caption_words':5}
        with tempfile.TemporaryDirectory() as tmp:
            ass,srt = Path(tmp)/'test.ass',Path(tmp)/'test.srt'
            write_captions(transcript,{'start':10,'end':11},ass,srt,config)
            text = ass.read_text(encoding='utf-8-sig')
            self.assertIn('0:00:00.10,0:00:00.60',text)
            self.assertIn('0:00:00.60,0:00:01.00',text)
            self.assertNotIn('Goodbye',text)
            self.assertIn(r'\an5\pos(540,675)',text)
            self.assertIn('00:00:00,100 --> 00:00:01,000',srt.read_text())

    def test_long_pause_starts_new_caption_group(self):
        groups = caption_groups([{'start':0,'end':.5,'word':'Hello'},
                                 {'start':2,'end':2.5,'word':'there'}])
        self.assertEqual(2,len(groups))

    def test_missing_word_timestamps_are_not_silently_rendered(self):
        with self.assertRaises(ValueError):
            clip_words({'segments':[{'start':0,'end':4,'text':'Hello'}]}, {'start':0,'end':4})

    def test_zero_duration_recognized_words_are_kept(self):
        transcript = {'segments':[{'words':[
            {'start':1,'end':1,'word':' I'},
            {'start':1,'end':1.4,'word':' saw'},
            {'start':1.5,'end':1.8,'word':' it.'}]}]}
        words = clip_words(transcript,{'start':0,'end':2})
        self.assertEqual('I saw',words[0]['word'])
        self.assertEqual(1,words[0]['start'])


if __name__ == '__main__':
    unittest.main()
