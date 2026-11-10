import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import numpy as np
from media_filters import audio_filters, loudness_gain, measure_loudness


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


def loudnorm_json(source_args, filters):
    """Run `filters` (whose last filter must print a loudnorm report) and return the report."""
    result=subprocess.run(['ffmpeg','-hide_banner','-nostats',*source_args,'-af',filters,'-f','null','-'],
                          capture_output=True,text=True)
    return json.loads(result.stderr[result.stderr.rindex('{'):result.stderr.rindex('}')+1])


def level_chain(duration, measured):
    """Only the loudness part of the clip chain, without trim and fades."""
    return audio_filters(duration,{},measured).split('asetpts=PTS-STARTPTS,')[1].split(',afade')[0]


class GainTests(unittest.TestCase):
    def test_reachable_target_is_linear_and_unreachable_one_is_limited(self):
        flat={'input_i':-30.0,'input_tp':-20.0,'input_lra':3.0,'input_thresh':-40.0,'target_offset':0.0}
        self.assertEqual((14.0,0.0),loudness_gain(flat))
        self.assertIn('linear=true',audio_filters(10,{},flat))
        peaky={'input_i':-31.0,'input_tp':-11.0,'input_lra':12.0,'input_thresh':-41.0,'target_offset':0.0}
        gain,limited=loudness_gain(peaky)
        self.assertAlmostEqual(15.0,gain)
        self.assertAlmostEqual(5.5,limited)
        self.assertNotIn('loudnorm',audio_filters(10,{},peaky))
        self.assertIn('alimiter',audio_filters(10,{},peaky))
        self.assertAlmostEqual(6.0,loudness_gain({**peaky,'input_i':-45.0})[1])  # the limiter never works harder

    def test_without_a_measurement_the_single_pass_chain_is_kept_for_previews(self):
        self.assertIn('loudnorm=I=-16:TP=-1.5:LRA=11,',audio_filters(10,{}))


@unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg is required')
class TwoPassLoudnessTests(unittest.TestCase):
    TONE=['-f','lavfi','-i','sine=frequency=300:duration=10,volume=-24dB,tremolo=f=0.3:d=0.5']
    BURST=['-f','lavfi','-i',"aevalsrc=exprs='0.9*if(lt(mod(t,1),0.002),1,0)+0.01*sin(2*PI*300*t)':d=10:s=48000"]

    @staticmethod
    def measured(source):
        first=loudnorm_json(source,'loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json')
        return {k:float(first[k]) for k in ('input_i','input_tp','input_lra','input_thresh','target_offset')}

    def test_second_pass_runs_in_linear_mode_and_reaches_the_target(self):
        chain=level_chain(10,self.measured(self.TONE))
        self.assertTrue(chain.startswith('loudnorm='))
        report=loudnorm_json(self.TONE,chain+':print_format=json')
        self.assertEqual('linear',report['normalization_type'])
        self.assertAlmostEqual(-16.0,float(report['output_i']),delta=0.6)

    def test_clip_that_cannot_reach_the_target_keeps_the_peak_limit(self):
        # Clicks over a quiet body: gain up to -16 LUFS would push the bursts past -1.5 dBTP.
        measured=self.measured(self.BURST)
        self.assertGreater(loudness_gain(measured)[1],0)
        out=loudnorm_json(self.BURST,level_chain(10,measured)+',loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json')
        self.assertLessEqual(float(out['input_tp']),-1.4)

    def test_measure_loudness_reads_the_clip_range_and_ignores_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tone.wav'
            subprocess.run(['ffmpeg','-v','error',*self.TONE,str(path)],check=True)
            measured=measure_loudness(path,1,5)
            self.assertAlmostEqual(self.measured(self.TONE)['input_i'],measured['input_i'],delta=3)
            silence=Path(tmp)/'silence.wav'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','anullsrc=d=3',str(silence)],check=True)
            self.assertIsNone(measure_loudness(silence,0,2))


if __name__ == '__main__':
    unittest.main()
