import shutil
import subprocess
import unittest
import numpy as np
from media_filters import audio_filters


@unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg is required')
class AudioEnvelopeTests(unittest.TestCase):
    def test_real_filter_fades_both_edges_without_changing_duration(self):
        result=subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=1000:duration=0.2',
            '-af',audio_filters(.2,{'audio_fade_ms':20}),'-ar','48000','-ac','1','-f','f32le','-'],
            capture_output=True,check=True)
        signal=np.frombuffer(result.stdout,dtype='<f4')
        self.assertEqual(9600,len(signal))
        middle=np.sqrt(np.mean(signal[2400:7200]**2))
        self.assertLess(np.max(np.abs(signal[:48])),middle*.12)
        self.assertLess(np.max(np.abs(signal[-48:])),middle*.12)


if __name__ == '__main__':
    unittest.main()
