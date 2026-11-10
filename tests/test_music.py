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

    def test_scan_adds_beat_profiles_to_an_old_sidecar_without_touching_user_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            synthetic_track(folder/'song.wav', 120, .5, 20.5, 32)
            music.scan(folder, out=lambda *_: None)
            path = folder/'song.json'
            data = json.loads(path.read_text(encoding='utf-8'))
            # An analysis-version-1 sidecar the user edited: a mood, and the drop moved to another beat.
            for c in data['drop_candidates']:
                for key in list(c):
                    if key not in ('time', 'rise_db'):
                        del c[key]
            del data['analysis']
            data.update(mood='dark', drop=24.5)
            path.write_text(json.dumps(data), encoding='utf-8')
            messages = []
            music.scan(folder, out=messages.append)
            again = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(['Beat profiles updated: song.wav'], messages)
            self.assertEqual(('dark', 24.5), (again['mood'], again['drop']))
            self.assertEqual(data['bpm'], again['bpm'])
            self.assertEqual(data['first_beat'], again['first_beat'])
            self.assertEqual({'version': music.ANALYSIS_VERSION, 'bpm': data['bpm'], 'first_beat': data['first_beat'],
                              'drop': 24.5}, again['analysis'])
            self.assertTrue(all(len(c['beats']) == music.PROFILE_BEATS for c in again['drop_candidates']
                                if c['time']+16 < 32), again['drop_candidates'])
            # The user's own drop gets a profile too (on the detected grid, within a few ms).
            self.assertTrue(any(abs(c['time']-24.5) < .015 and c.get('source') == 'sidecar drop'
                                for c in again['drop_candidates']), again['drop_candidates'])
            # Nothing changed since: the next scan leaves the file alone.
            messages.clear()
            music.scan(folder, out=messages.append)
            self.assertEqual([], messages)

    def test_loudness_is_measured_in_lufs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'track.wav'
            synthetic_track(path, 120, .5, 10.5, 24)
            quiet = music.loudness(path, 1, 8)
            loud = music.loudness(path, 12, 8)
        self.assertGreater(loud-quiet, 10)


def accented_track(path, bpm, first_beat, drop, duration, accent):
    """Quiet ticks before `drop`; after it a kick on every beat, and on the beats where
    `accent(k)` is true (k counted from the drop) a loud broadband crash as well."""
    rng = np.random.default_rng(2)
    out = np.zeros(int(duration*SR))
    period = 60/bpm
    for beat in np.arange(first_beat, duration, period):
        i = int(round(beat*SR))
        k = round((beat-drop)/period)
        if k < 0:
            n = int(.02*SR)
            out[i:i+n] += (rng.standard_normal(n)*np.exp(-np.arange(n)/(.004*SR))*.05)[:len(out)-i]
            continue
        n = int(.15*SR)
        t = np.arange(n)/SR
        out[i:i+n] += (np.sin(2*np.pi*(50+80*np.exp(-t/.02))*t)*np.exp(-t/.06)*.5)[:len(out)-i]
        if accent(k):
            m = int(.12*SR)
            out[i:i+m] += (rng.standard_normal(m)*np.exp(-np.arange(m)/(.03*SR))*.6)[:len(out)-i]
    wavfile.write(path, SR, (np.clip(out, -1, 1)*32000).astype(np.int16))


class GridTests(unittest.TestCase):
    TRACK = {'bpm': 120.0, 'first_beat': .1, 'drop': 30.1, 'duration': 60.0}

    def test_the_grid_is_first_beat_plus_whole_beats(self):
        self.assertAlmostEqual(30.1, music.grid_beat(self.TRACK, 30.06)[0])
        self.assertAlmostEqual(-.04, music.grid_beat(self.TRACK, 30.06)[1])
        # Millisecond rounding of the sidecar keeps the time as written; a real offset snaps.
        self.assertEqual(30.101, music.on_grid(self.TRACK, 30.101)[0])
        self.assertAlmostEqual(30.1, music.on_grid(self.TRACK, 30.13)[0])
        # Without first_beat the grid runs through the time itself.
        self.assertEqual((29.97, 0.0), music.grid_beat({'bpm': 120.0}, 29.97))

    def test_a_drop_off_its_grid_is_reported_with_the_first_beat_that_fits_it(self):
        self.assertIsNone(music.drop_grid_warning('song', self.TRACK))
        warning = music.drop_grid_warning('song', {**self.TRACK, 'drop': 30.0})
        self.assertIn('-100 ms off the beat grid', warning)
        self.assertIn('nearest beat, 30.100 s', warning)
        self.assertIn('first_beat to 0.0000', warning)

    def test_strongest_beats_and_bar_labels(self):
        self.assertEqual([0, 4, 6], music.strongest_beats([.9, .1, .2, .1, .8, 0, .7, .1], 3))
        self.assertEqual([0], music.strongest_beats([.9, .1, .2, .1, .8], 2, limit=2)[:1])
        self.assertEqual('+6 (bar 2.3)', music.beat_label(6))
        self.assertEqual(4, music.accent_beat([.1, 0, .1, .9]*4))
        self.assertIsNone(music.accent_beat([.5, .1, .5, .1]*4))  # half-bar hits: no clear bar position


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
class BeatProfileTests(unittest.TestCase):
    def profile(self, accent):
        # Measured on the true grid, as for a sidecar the user has checked.
        track = {'bpm': 100.0, 'first_beat': .3, 'drop': 14.7, 'duration': 40.0, 'drop_candidates': []}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'accent.wav'
            accented_track(path, 100, .3, 14.7, 40, accent)
            candidates, analysis = music.rhythm_fields(path, track)
        self.assertEqual(14.7, analysis['drop'])
        self.assertEqual('sidecar drop', candidates[0]['source'])
        return track, candidates[0]

    def test_accented_beats_are_the_strongest_and_the_bar_position_follows_the_drop(self):
        found, drop = self.profile(lambda k: k % 4 == 0)
        self.assertEqual(music.PROFILE_BEATS, len(drop['beats']))
        self.assertEqual([0, 4, 8, 12], music.strongest_beats(drop['beats'], 4, 16))
        self.assertEqual(1, drop['accent_beat'])
        # The descriptors: louder and busier after the drop.
        self.assertGreater(drop['after_db']-drop['before_db'], 10)
        self.assertGreater(drop['onsets_after'], drop['onsets_before'])

    def test_hits_on_the_fourth_beat_say_the_drop_may_not_be_a_downbeat(self):
        found, drop = self.profile(lambda k: k % 4 == 3)
        self.assertEqual(4, drop['accent_beat'])
        self.assertIn('may not be a downbeat', music.describe_candidate(drop, 60/found['bpm']))


if __name__ == '__main__':
    unittest.main()
