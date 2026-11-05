import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import numpy as np
from media_filters import video_filters
from speaker_focus import write_commands
from shorts import ROOT


@unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg is required')
class FrameExactRenderTests(unittest.TestCase):
    def test_crop_changes_on_the_actual_cut_frame_without_one_frame_anticipation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ass=Path(tmp)/'empty.ass'
            ass.write_text('[Script Info]\nScriptType: v4.00+\nPlayResX: 160\nPlayResY: 200\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,Dosis,12,&HFFFFFF,&HFFFFFF,&H000000,&H000000,0,0,0,0,100,100,0,0,1,1,0,5,0,0,0,1\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n',encoding='utf-8')
            commands=Path(tmp)/'focus.cmd'
            # The red subject moves from left to right exactly on frame 17.
            frames=[{'frame':i,'center_x':.25 if i<17 else .75} for i in range(24)]
            write_commands(commands,frames,640,288)
            plan={'scaled_width':640,'scaled_height':360,'commands_path':str(commands)}
            graph=video_filters(plan,{}, {'width':288,'height':360},ass,ROOT/'assets/fonts')
            source='color=blue:s=640x360:r=30:d=0.8,drawbox=x=80:y=0:w=160:h=360:color=red:t=fill:enable=lt(n\\,17),drawbox=x=400:y=0:w=160:h=360:color=red:t=fill:enable=gte(n\\,17)'
            result=subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',source,
                '-filter_complex',graph,'-map','[v]','-an','-f','rawvideo','-pix_fmt','rgb24','-'],capture_output=True,check=True)
            output=np.frombuffer(result.stdout,np.uint8).reshape(-1,360,288,3)
            self.assertEqual(24,len(output))
            for i in (0,16,17,23):
                red,green,blue=map(int,output[i,180,144])
                self.assertGreater(red,blue+100,f'Wrong focus on frame {i}')
            # A padded/letterboxed output would contain black pixels at edges.
            self.assertGreater(int(output.max(axis=3).min()),150)


if __name__=='__main__':
    unittest.main()
