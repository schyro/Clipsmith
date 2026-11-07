import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.io import wavfile

import music

SR = 44100


def synthetic_track(path, bpm, first_beat, drop, duration, floor=0.0, fade_in=0.0, breaks=()):
    """Quiet hi-hats on every beat before `drop`, a loud kick and bass line after it.
    `floor` adds steady noise (mastered music is never silent between beats); `fade_in`
    fades the start in over that many seconds; `breaks` lists (start, end) breakdowns after the
    drop that go back to the quiet part."""
    def full(at):
        return at >= drop and not any(a <= at < b for a, b in breaks)
    rng = np.random.default_rng(1)
    t = np.arange(int(duration*SR))/SR
    out = np.zeros_like(t)
    period = 60/bpm
    for beat in np.arange(first_beat, duration, period):
        i = int(round(beat*SR))
        if not full(beat):
            n = int(.03*SR)
            hat = rng.standard_normal(n)*np.exp(-np.arange(n)/(.006*SR))*.08
            out[i:i+n] += hat[:len(out)-i]
        else:
            n = int(.18*SR)
            k = np.arange(n)/SR
            kick = np.sin(2*np.pi*(55+90*np.exp(-k/.02))*k)*np.exp(-k/.07)*.9
            out[i:i+n] += kick[:len(out)-i]
    loud = (t >= drop) & ~np.any([(t >= a) & (t < b) for a, b in breaks], axis=0) if breaks else t >= drop
    out += np.where(loud, .25*np.sin(2*np.pi*49*t), .02*np.sin(2*np.pi*440*t))
    if floor:
        out += rng.standard_normal(len(t))*floor
    if fade_in:
        out *= np.clip(t/fade_in, 0, 1)
    wavfile.write(path, SR, (np.clip(out, -1, 1)*32000).astype(np.int16))


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
class MusicAnalysisTests(unittest.TestCase):
    def check(self, bpm, first_beat, drop, duration=40.0):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'track.wav'
            synthetic_track(path, bpm, first_beat, drop, duration)
            found = music.analyse(path)
        period = 60/bpm
        self.assertTrue(all(np.isfinite(c['rise_db']) for c in found['drop_candidates']), found)
        self.assertAlmostEqual(bpm, found['bpm'], delta=bpm*.003, msg=found)
        # Phase on the true grid within 15 ms (half a 30 fps frame).
        offset = (found['first_beat']-first_beat+period/2) % period-period/2
        self.assertLess(abs(offset), .015, found)
        self.assertLess(abs(found['drop']-drop), period/2+.015, found)
        # The detected grid still sits on the beats at the end of the track.
        last = found['first_beat']+np.floor((duration-1-found['first_beat'])*found['bpm']/60)*60/found['bpm']
        late = (last-first_beat+period/2) % period-period/2
        self.assertLess(abs(late), .015, found)

    def test_finds_tempo_phase_and_drop_at_128_bpm(self):
        period = 60/128
        self.check(128, .37, .37+40*period)

    def test_finds_tempo_phase_and_drop_at_95_bpm(self):
        period = 60/95
        self.check(95, .12, .12+24*period)

    def test_finds_tempo_phase_and_drop_at_150_bpm(self):
        period = 60/150
        self.check(150, .2, .2+48*period)

    def test_a_beat_at_the_very_start_still_gives_a_drop(self):
        period = 60/128
        self.check(128, .005, .005+32*period)

    def test_a_loud_track_with_off_beat_noise_keeps_its_tempo(self):
        # Mastered music never falls silent between beats: a noise floor must not make a
        # grid twice as fast look better.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'loud.wav'
            synthetic_track(path, 90, .3, 30.3, 50, floor=.12)
            found = music.analyse(path)
        self.assertAlmostEqual(90, found['bpm'], delta=.4, msg=found)

    def test_the_bass_returning_after_a_breakdown_is_the_drop_not_the_intro(self):
        # Like the mastered tracks: the bass comes in after a 5 s intro, drops out for a
        # breakdown and returns. The return is the drop.
        period = 60/100
        back = .2+60*period
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'breakdown.wav'
            synthetic_track(path, 100, .2, .2+8*period, 60, floor=.05, fade_in=2.0,
                            breaks=[(.2+44*period, back)])
            found = music.analyse(path)
        self.assertLess(abs(found['drop']-back), period/2+.015, found)
        self.assertAlmostEqual(100, found['bpm'], delta=.3, msg=found)

    def test_scan_never_overwrites_a_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            synthetic_track(folder/'song.wav', 120, .5, 20.5, 32)
            music.scan(folder, out=lambda *_: None)
            path = folder/'song.json'
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual('', data['mood'])
            data.update(mood='funny, smug', drop=12.5)
            path.write_text(json.dumps(data), encoding='utf-8')
            music.scan(folder, out=lambda *_: None)
            again = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual('funny, smug', again['mood'])
            self.assertEqual(12.5, again['drop'])
            track, loaded = music.load_track('song.wav', folder)
            self.assertEqual(12.5, loaded['drop'])

    def test_loudness_is_measured_in_lufs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'track.wav'
            synthetic_track(path, 120, .5, 10.5, 24)
            quiet = music.loudness(path, 1, 8)
            loud = music.loudness(path, 12, 8)
        self.assertGreater(loud-quiet, 10)


if __name__ == '__main__':
    unittest.main()
