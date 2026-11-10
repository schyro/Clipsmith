import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

import edits

sys.path.insert(0, str(Path(__file__).parent))
from test_music import synthetic_track  # noqa: E402

SETTINGS = edits.edit_settings()
CLIP = {'id': '03', 'title': 'Payoff', 'start': 100.0, 'end': 110.0}
WORD = {'index': 7, 'word': ' gone.', 'start': 107.2, 'end': 107.6}
TRACK = {'bpm': 120.0, 'drop': 30.0, 'duration': 90.0, 'first_beat': .0}


def edit(shots=8, **extra):
    return {'track': 'song.mp3', 'payoff_word': 7, 'montage': [{'start': 10.0*i} for i in range(shots)], **extra}


class TimelineTests(unittest.TestCase):
    def test_drop_follows_the_payoff_word_by_the_default_delay(self):
        t = edits.timeline(CLIP, edit(), WORD, TRACK, SETTINGS)
        self.assertEqual(round((WORD['end']-CLIP['start']+.25)*30), t['drop_frame'])
        self.assertGreaterEqual(t['drop_delay'], .1)
        self.assertLessEqual(t['drop_delay'], .5)

    def test_cuts_come_from_absolute_beat_times_without_drift(self):
        # 128 BPM: a beat is 14.0625 frames, so summed rounded lengths would drift.
        t = edits.timeline(CLIP, edit(shots=16, cut_beats=1), WORD, {**TRACK, 'bpm': 128.0}, SETTINGS)
        period = 60/128
        for k, frame in enumerate(t['cut_frames']):
            self.assertLessEqual(abs(frame-(t['drop_frame']+k*period*30)), .5)
        self.assertEqual(t['cut_frames'][-1]-t['drop_frame'], sum(t['shot_frames']))

    def test_copper_keeps_the_beat_grid_and_shortens_the_tail(self):
        legacy = edits.timeline(CLIP, edit(), WORD, TRACK, SETTINGS)
        styled = edits.timeline(CLIP, edit(effects='copper'), WORD, TRACK, SETTINGS)
        self.assertEqual(legacy['cut_frames'], styled['cut_frames'])
        self.assertEqual(9, styled['tail_frames'])
        self.assertEqual(24, legacy['tail_frames'])

    def test_cut_beats_are_chosen_inside_the_interval_range(self):
        self.assertEqual(1, edits.choose_cut_beats(60/95, .45, .9))    # 0.63 s
        self.assertEqual(2, edits.choose_cut_beats(60/160, .45, .9))   # 0.375 s per beat -> 0.75 s
        self.assertEqual(.5, edits.choose_cut_beats(60/50, .45, .9))   # 1.2 s per beat -> 0.6 s

    def test_slots_hold_a_shot_for_several_cuts(self):
        e = edit(shots=6)
        e['montage'][-1]['slots'] = 3
        t = edits.timeline(CLIP, e, WORD, TRACK, SETTINGS)
        self.assertEqual(6, len(t['shot_frames']))
        self.assertEqual(t['shot_frames'][0]*3, t['shot_frames'][-1])

    def test_montage_length_outside_the_range_is_refused_with_a_slot_count(self):
        with self.assertRaisesRegex(ValueError, 'use 8-18 slots'):
            edits.timeline(CLIP, edit(shots=3), WORD, TRACK, SETTINGS)

    def test_drop_after_the_clip_end_is_refused(self):
        late = {**WORD, 'start': 109.6, 'end': 109.9}
        with self.assertRaisesRegex(ValueError, 'after the clip end'):
            edits.timeline(CLIP, edit(), late, TRACK, SETTINGS)

    def test_track_too_short_after_its_drop_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'too early'):
            edits.timeline(CLIP, edit(), WORD, {**TRACK, 'duration': 33.0}, SETTINGS)

    def test_a_later_dialogue_start_shortens_the_edit_and_keeps_the_drop(self):
        full = edits.timeline(CLIP, edit(), WORD, TRACK, SETTINGS)
        short = edits.timeline(CLIP, edit(dialogue_start=104.0), WORD, TRACK, SETTINGS)
        self.assertEqual(120, short['head_frame'])
        self.assertEqual(full['drop_frame']-120, short['drop_frame'])
        self.assertEqual(full['clip_drop_frame'], short['clip_drop_frame'])
        self.assertEqual(full['total_frames']-120, short['total_frames'])
        self.assertAlmostEqual(TRACK['drop'], short['track_start']+short['drop_at'], places=6)
        self.assertAlmostEqual(full['drop_delay'], short['drop_delay'], places=6)
        with self.assertRaisesRegex(ValueError, 'leaves'):
            edits.timeline(CLIP, edit(dialogue_start=106.0), WORD, TRACK, SETTINGS)

    def test_music_offset_puts_the_track_drop_on_the_drop_frame(self):
        t = edits.timeline(CLIP, edit(), WORD, TRACK, SETTINGS)
        self.assertAlmostEqual(TRACK['drop'], t['track_start']+t['drop_at'], places=6)
        early = edits.timeline(CLIP, edit(), WORD, {**TRACK, 'drop': 3.0}, SETTINGS)
        self.assertGreater(early['music_delay'], 0)
        self.assertEqual(0, early['track_from'])


class GridAndDropTests(unittest.TestCase):
    def test_the_drop_is_the_grid_beat_nearest_the_sidecar_drop(self):
        on = edits.timeline(CLIP, edit(), WORD, TRACK, SETTINGS)
        # The user moved the drop 40 ms off the grid: the cuts stay on the beats of first_beat + k x beat.
        moved = edits.timeline(CLIP, edit(), WORD, {**TRACK, 'drop': 30.04}, SETTINGS)
        self.assertEqual(30.0, moved['track_drop'])
        self.assertAlmostEqual(.04, moved['drop_offset'], places=6)
        self.assertEqual(on['track_start'], moved['track_start'])
        # Correcting first_beat moves the drop with the grid.
        shifted = edits.timeline(CLIP, edit(), WORD, {**TRACK, 'first_beat': .1}, SETTINGS)
        self.assertAlmostEqual(30.1, shifted['track_drop'], places=6)
        self.assertAlmostEqual(30.1, shifted['track_start']+shifted['drop_at'], places=6)
        warnings = edits.timeline_warnings(edit(), moved, {**TRACK, 'drop': 30.04}, 'song.mp3')
        self.assertTrue(any('+40 ms off the beat grid' in w for w in warnings), warnings)

    def test_millisecond_rounding_keeps_existing_edits_frame_identical(self):
        # The user's track: first_beat 0.0012 + 32 beats = 18.5752 s against a drop of 18.575 s.
        track = {'bpm': 103.37, 'first_beat': .0012, 'drop': 18.575, 'duration': 120.0}
        t = edits.timeline(CLIP, edit(shots=9), WORD, track, SETTINGS)
        self.assertEqual(18.575, t['track_drop'])
        self.assertAlmostEqual(18.575-t['drop_at'], t['track_start'], places=6)
        self.assertEqual([], edits.timeline_warnings({**edit(shots=9), 'bar_note': 'test'}, t, track))

    def test_an_edit_can_use_another_drop_of_its_track(self):
        t = edits.timeline(CLIP, edit(drop=60.0), WORD, TRACK, SETTINGS)
        self.assertEqual(60.0, t['track_drop'])
        self.assertAlmostEqual(60.0, t['track_start']+t['drop_at'], places=6)
        # A few ms of rounding snap onto the beat; a time between beats is refused.
        self.assertEqual(60.0, edits.timeline(CLIP, edit(drop=60.008), WORD, TRACK, SETTINGS)['track_drop'])
        with self.assertRaisesRegex(ValueError, r"off the track's beat grid; use a beat such as 60\.000 s"):
            edits.timeline(CLIP, edit(drop=60.2), WORD, TRACK, SETTINGS)
        with self.assertRaisesRegex(ValueError, 'outside the track'):
            edits.timeline(CLIP, edit(drop=95.0), WORD, TRACK, SETTINGS)
        with self.assertRaisesRegex(ValueError, 'too early'):
            edits.timeline(CLIP, edit(drop=86.0), WORD, TRACK, SETTINGS)
        problems = edits.structure_problems({'clips': [CLIP], 'edits': {'03': edit(drop='late')}}, 600, SETTINGS)
        self.assertTrue(any('"drop" must be' in p for p in problems), problems)


class RhythmTests(unittest.TestCase):
    PATTERN = [2, 2, 1, 1, .5, .5, .5, .5, 4]

    def pattern_edit(self, pattern, **extra):
        e = edit(shots=len(pattern), **extra)
        for shot, beats in zip(e['montage'], pattern):
            shot['beats'] = beats
        return e

    def test_shot_lengths_follow_a_beat_pattern_on_absolute_beat_times(self):
        track = {**TRACK, 'bpm': 103.37}
        t = edits.timeline(CLIP, self.pattern_edit(self.PATTERN), WORD, track, SETTINGS)
        period = 60/103.37
        bounds = np.cumsum([0]+self.PATTERN)
        self.assertEqual([t['drop_frame']+round(b*period*30) for b in bounds], t['cut_frames'])
        self.assertEqual(self.PATTERN, t['shot_beats'])
        self.assertEqual(12, t['montage_beats'])
        self.assertEqual([35, 35, 17, 17, 9, 9, 9, 8, 70], t['shot_frames'])
        self.assertEqual([], edits.timeline_warnings(self.pattern_edit(self.PATTERN), t))

    def test_slots_still_work_and_mix_with_beats(self):
        legacy = edit(shots=8)
        legacy['montage'][-1]['slots'] = 2
        mixed = edit(shots=8)
        mixed['montage'][-1]['beats'] = 2
        a = edits.timeline(CLIP, legacy, WORD, TRACK, SETTINGS)
        b = edits.timeline(CLIP, mixed, WORD, TRACK, SETTINGS)
        self.assertEqual(a['cut_frames'], b['cut_frames'])

    def test_a_montage_ending_mid_bar_warns_until_the_agent_answers(self):
        t = edits.timeline(CLIP, self.pattern_edit([2, 2, 1, 1, 1, 1, 3]), WORD, TRACK, SETTINGS)
        self.assertEqual(11, t['montage_beats'])
        warnings = edits.timeline_warnings(self.pattern_edit([2, 2, 1, 1, 1, 1, 3]), t)
        self.assertEqual(1, len(warnings))
        self.assertIn('make it 8 or 12 beats', warnings[0])
        answered = self.pattern_edit([2, 2, 1, 1, 1, 1, 3], bar_note='Ends on the slam, mid-bar on purpose.')
        self.assertEqual([], edits.timeline_warnings(answered, t))

    def test_mid_bar_is_a_warning_not_a_problem(self):
        from unittest import mock
        e = edit(shots=9, effects='copper')   # like the user's edits 17 and 23: 10 beats
        e['montage'][-1]['slots'] = 2
        plan = {'clips': [CLIP], 'edits': {'03': e}}
        with mock.patch('music.load_track', return_value=(Path('song.mp3'), {**TRACK, 'bpm': 103.37})):
            self.assertEqual([], edits.edit_problems(plan, 600, {'segments': [{'words': [WORD]}]}, settings=SETTINGS))

    def test_shots_too_short_for_the_tempo_name_the_smallest_beat_value(self):
        fast = {**TRACK, 'bpm': 188.8}
        self.assertEqual(1, edits.min_shot_beats(60/188.8))
        self.assertEqual(.5, edits.min_shot_beats(60/103.37))
        e = self.pattern_edit([2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, .5, .5, 1])
        with self.assertRaisesRegex(ValueError, r'montage shot 13 would last 4 frames .* at least 1 beat, so give it 1 beat or more'):
            edits.timeline(CLIP, e, WORD, fast, SETTINGS)
        with self.assertRaisesRegex(ValueError, r'give the shots 4-9 beats in total'):
            edits.timeline(CLIP, self.pattern_edit([1, 1, 1]), WORD, {**TRACK, 'bpm': 60.0}, SETTINGS)

    def test_beats_must_be_halves_and_not_combined_with_slots(self):
        e = self.pattern_edit([2, .3, 0, 1])
        e['montage'][3]['slots'] = 2
        problems = ' '.join(edits.structure_problems({'clips': [CLIP], 'edits': {'03': e}}, 600, SETTINGS))
        self.assertEqual(2, problems.count('whole or half beats'), problems)
        self.assertIn('either beats or slots', problems)
        empty_note = {**self.pattern_edit(self.PATTERN), 'bar_note': ' '}
        self.assertIn('"bar_note" must say why', ' '.join(
            edits.structure_problems({'clips': [CLIP], 'edits': {'03': empty_note}}, 600, SETTINGS)))


class FadeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
    def test_the_music_holds_its_level_to_the_last_cut_and_fades_in_the_tail(self):
        for profile, tail in (('copper', .3), ('soft', .8)):
            with self.subTest(effects=profile):
                e = edit(effects=profile)
                t = edits.timeline(CLIP, e, WORD, TRACK, SETTINGS)
                self.assertAlmostEqual(tail, t['tail_frames']/30, places=6)
                pcm = subprocess.check_output(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=r=48000:d=10',
                                               '-f', 'lavfi', '-i', 'sine=f=110:r=48000:d=40',
                                               '-filter_complex', edits.music_graph(t, e, SETTINGS, (-9, -6), 1),
                                               '-map', '[a]', '-f', 'f32le', '-'])
                audio = np.frombuffer(pcm, np.float32).reshape(-1, 2)[:, 0]
                rate = 48000
                last_cut = t['cut_frames'][-1]/30
                level = lambda a, b: edits.rms_db(audio[int(a*rate):int(b*rate)])
                steady = level(last_cut-1.0, last_cut-.5)
                # The last shot ends at full level; the fade fits the tail and reaches silence.
                self.assertAlmostEqual(steady, level(last_cut-.1, last_cut), delta=.2)
                self.assertLess(level(last_cut+tail-.05, last_cut+tail), steady-15)


class LockedEditTests(unittest.TestCase):
    def locked_plan(self, tmp, files=True):
        clip = {'id': '01', 'title': 'A', 'start': 0.0, 'end': 20.0}
        plan = {'clips': [clip], 'edits': {'01': edit(track='gone.wav', approved={'at': 't0'})}}
        folder = tmp/'out'/'edits'
        folder.mkdir(parents=True)
        info = {'id': '01', 'file': '01_A.edit.mp4', 'track': 'gone.wav', 'effects': 'copper',
                'timeline': {'drop_frame': 10, 'drop_at': 10/30, 'cut_frames': [10, 20, 30], 'total_frames': 40}}
        if files:
            make_clip(folder/'01_A.edit.mp4', 40/30)
            for name in ('01_A.edit-nomusic.mp4', '01_A.edit.done.json'):
                (folder/name).write_text('{}', encoding='utf-8')
            (folder/'01_A.edit.json').write_text(json.dumps(info), encoding='utf-8')
        return plan, folder, info

    def test_an_approved_edit_with_its_files_needs_no_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            plan, folder, info = self.locked_plan(tmp)
            (tmp/'Music').mkdir()
            self.assertEqual([], edits.edit_problems(plan, 600, {'segments': [{'words': [WORD]}]},
                                                     music_folder=tmp/'Music', settings=SETTINGS, folder=folder))
            self.assertEqual(info, edits.locked_edit(plan, '01', plan['edits']['01'], folder))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
    def test_render_keeps_the_locked_edit_and_its_index_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            plan, folder, info = self.locked_plan(tmp)
            index = edits.render_edits(tmp/'source.mp4', plan, tmp/'cache', {}, tmp/'out', {'segments': []},
                                       music_folder=tmp/'Music')
            self.assertEqual([info], index)
            self.assertTrue((folder/'01_A.edit.mp4').exists())
            self.assertTrue((tmp/'cache'/'qa-current'/'edits'/'edit-01.jpg').exists())

    def test_an_approved_edit_without_its_files_still_needs_its_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            plan, folder, _ = self.locked_plan(tmp, files=False)
            (tmp/'Music').mkdir()
            problems = edits.edit_problems(plan, 600, {'segments': [{'words': [WORD]}]},
                                           music_folder=tmp/'Music', settings=SETTINGS, folder=folder)
            self.assertEqual(1, len(problems), problems)
            self.assertIn('Music track not found', problems[0])


class MotionTests(unittest.TestCase):
    def test_the_peak_maps_to_cut_beat_and_ramp_start_values(self):
        t = edits.timeline(CLIP, edit(shots=6, cut_beats=2), WORD, TRACK, SETTINGS)   # 2 beats = 30 frames
        changes = np.full(30, .01)
        changes[12] = .2
        shot = {'start': 50.0}
        m = edits.motion_peak(changes, shot, 1, 30, t, edit())
        self.assertEqual((12, True), (m['frame'], m['clear']))
        self.assertEqual(50.4, m['to_cut'])
        self.assertEqual(15, m['beat_frame'])               # the beat in the middle of a two-beat shot
        self.assertEqual(round(50.4-15/30, 2), m['to_beat'])
        self.assertNotIn('slow_frame', m)
        # A one-beat shot has no beat inside it; a flat window has no clear peak.
        one = edits.timeline(CLIP, edit(cut_beats=1), WORD, TRACK, SETTINGS)
        self.assertNotIn('beat_frame', edits.motion_peak(changes[:15], shot, 0, 15, one, edit()))
        self.assertFalse(edits.motion_peak(np.full(15, .01), shot, 0, 15, one, edit())['clear'])

    def test_repeated_film_frames_are_ignored(self):
        # 24 fps film on the 30 fps grid: every fifth frame repeats the one before it.
        t = edits.timeline(CLIP, edit(shots=6, cut_beats=2), WORD, TRACK, SETTINGS)
        changes = np.full(30, .02)
        changes[::5] = 0
        changes[16] = .028          # 1.4x the typical change: not a peak, although the zeros halve the median
        m = edits.motion_peak(changes, {'start': 50.0}, 0, 30, t, edit())
        self.assertEqual(.02, m['typical'])
        self.assertFalse(m['clear'])
        self.assertIn('no clear motion peak', edits.describe_motion(m))
        changes[16] = .05
        changes[15] = 0             # the strongest frame right after a repeat still counts
        m = edits.motion_peak(changes, {'start': 50.0}, 0, 30, t, edit())
        self.assertEqual((16, True), (m['frame'], m['clear']))

    def test_a_ramp_peak_is_placed_through_the_ramp_curve(self):
        import copper
        styled = edit(shots=6, cut_beats=2, effects='copper')
        styled['montage'][1]['motion'] = 'ramp'
        t = edits.timeline(CLIP, styled, WORD, TRACK, SETTINGS)
        changes = np.full(32, .01)
        changes[20] = .3
        m = edits.motion_peak(changes, styled['montage'][1], 1, 30, t, styled)
        offsets = copper.ramp_times(30)
        self.assertEqual(int(np.argmin(np.abs(offsets-20/30))), m['frame'])
        self.assertEqual(int(np.argmin(np.diff(offsets))), m['slow_frame'])
        self.assertAlmostEqual(styled['montage'][1]['start']+20/30-offsets[m['slow_frame']], m['to_slow'], delta=.006)
        self.assertIn('slowest part of the ramp', edits.describe_motion(m))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
    def test_the_change_is_measured_on_the_cropped_source_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)/'source.mp4'
            make_source(source, 6)   # the brightness flips at 2 s and 4 s
            size, config = {'scaled_width': 640, 'scaled_height': 360}, {'width': 288, 'height': 360}
            changes = edits.motion_changes(source, {'start': 1.5}, 0, 30, .5, size, config, edit())
        self.assertEqual(30, len(changes))
        self.assertEqual(15, int(np.argmax(changes)))


class StructureTests(unittest.TestCase):
    def test_reports_every_problem_at_once(self):
        plan = {'clips': [CLIP], 'edits': {
            '03': {'track': '', 'payoff_word': True, 'grade': 'sepia', 'transition': 'wipe',
                   'montage': [{'start': -1}, {'start': 5, 'center_x': 2, 'slots': 0}], 'extra': 1},
            '09': edit()}}
        problems = ' '.join(edits.structure_problems(plan, 600, SETTINGS))
        for text in ('no clip 09', 'unknown field(s) extra', '"track"', 'payoff_word', 'grade must',
                     'transition must', 'outside the source', 'center_x', 'slots'):
            self.assertIn(text, problems)

    def test_dialogue_start_inside_a_word_is_refused(self):
        from unittest import mock
        plan = {'clips': [CLIP], 'edits': {'03': edit(dialogue_start=104.1)}}
        transcript = {'segments': [{'words': [{'word': ' hello', 'start': 104.0, 'end': 104.4},
                                              dict(WORD)]}]}
        with mock.patch('music.load_track', return_value=(Path('song.mp3'), TRACK)):
            problems = edits.edit_problems(plan, 600, transcript, settings=SETTINGS)
        self.assertEqual(1, len(problems), problems)
        self.assertIn('cuts "hello"', problems[0])

    def test_a_plan_without_edits_has_no_problems(self):
        self.assertEqual([], edits.structure_problems({'clips': [CLIP]}, 600, SETTINGS))

    def test_copper_rejects_invalid_shot_controls(self):
        styled = edit(effects='copper')
        styled['montage'][0].update(anchor=[.5, -1], zoom=.30, practical_light='left')
        styled['montage'][1].update(motion='ramp', anchor=[.5, .4], cool_background=1)
        problems = ' '.join(edits.structure_problems({'clips': [CLIP], 'edits': {'03': styled}}, 600, SETTINGS))
        for field in ('anchor must', 'zoom must', 'practical_light must', 'require push', 'cool_background must'):
            self.assertIn(field, problems)

    def test_ramp_validates_the_source_window_instead_of_the_beat_length(self):
        from unittest import mock
        styled = edit(effects='copper')
        styled['montage'][-1] = {'start': 599.49, 'motion': 'ramp'}
        transcript = {'segments': [{'words': [WORD]}]}
        with mock.patch('music.load_track', return_value=(Path('song.mp3'), TRACK)):
            problems = edits.edit_problems({'clips': [CLIP], 'edits': {'03': styled}}, 600, transcript, settings=SETTINGS)
        self.assertEqual(1, len(problems), problems)
        self.assertIn('runs past the source end', problems[0])


def make_source(path, seconds=24):
    """A full-frame picture whose hue turns and brightness flips every 2 s, plus a moving bar."""
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    f'color=c=red:s=640x360:r=30:d={seconds},hue=h=t*30,'
                    r'eq=brightness=if(mod(floor(t/2)\,2)\,0.3\,-0.2):eval=frame,'
                    'drawbox=x=mod(t*200\\,600):y=0:w=40:h=360:color=white@0.5:t=fill',
                    '-f', 'lavfi', '-i', f'anoisesrc=d={seconds}:a=0.3',
                    '-c:v', 'libx264', '-g', '15', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)],
                   check=True)


def make_clip(path, seconds):
    """A stand-in for a rendered clip: grey picture, a steady 'dialogue' tone at about -21 dB RMS."""
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c=gray:s=288x360:r=30:d={seconds}',
                    '-f', 'lavfi', '-i', f'sine=frequency=300:duration={seconds}',
                    '-map', '0:v', '-map', '1:a',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ar', '48000', str(path)], check=True)
    path.with_suffix('.done.json').write_text(json.dumps({'signature': 'clip-sig'}), encoding='utf-8')


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
class RenderTests(unittest.TestCase):
    def test_renders_both_files_on_the_beat_grid_with_a_loud_drop(self):
        for profile in ('soft', 'strong', 'copper'):
            with self.subTest(effects=profile):
                self.render_and_check(profile)

    def render_and_check(self, profile):
        config = {'width': 288, 'height': 360}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source, clip_mp4, track_path = tmp/'source.mp4', tmp/'03_Payoff.mp4', tmp/'song.wav'
            make_source(source)
            make_clip(clip_mp4, 6)
            synthetic_track(track_path, 120, .5, 10.5, 30)
            clip = {'id': '03', 'title': 'Payoff', 'start': 50.0, 'end': 56.0}
            word = {'index': 7, 'word': ' gone.', 'start': 53.6, 'end': 54.0}
            track = {'bpm': 120.0, 'drop': 10.5, 'duration': 30.0, 'first_beat': .5, 'mood': 'test'}
            e = {'track': 'song.wav', 'payoff_word': 7, 'grade': 'bw', 'transition': 'white_flash',
                 'dialogue_start': 51.0, 'effects': profile, 'montage': [{'start': 2.0*i+.5} for i in range(12)]}
            if profile == 'copper':
                e['montage'][0].update(anchor=[.49,.35], practical_light=[.13,.56])
                e['montage'][1].update(motion='ramp', cool_background=True)
                e['montage'][4]['flash'] = 'white'
            plan_t = edits.timeline(clip, e, word, track, SETTINGS)
            ctx = type('Ctx', (), {})()
            ctx.source, ctx.clip, ctx.edit, ctx.word = source, clip, e, word
            ctx.track_path, ctx.track, ctx.plan_t = track_path, track, plan_t
            ctx.size = {'scaled_width': 640, 'scaled_height': 360}
            ctx.centers, ctx.problems = [.5]*12, []
            info = edits.render_edit(ctx, clip_mp4, tmp/'edits', '03_Payoff', config, SETTINGS, out=lambda *_: None)
            checks = info['checks']
            self.assertTrue(all(c['passed'] for c in checks.values()), checks)
            self.assertEqual(plan_t['total_frames'], checks['frames']['found'])
            # Brightness flips every 2 s: every cut must be seen, on its own grid frame, in black and white.
            self.assertEqual(checks['cuts']['of'], checks['cuts']['seen'], checks['cuts'])
            self.assertEqual(checks['cuts']['of'], checks['cuts']['exact'], checks['cuts'])
            self.assertGreaterEqual(checks['drop_gain_db']['gain'], SETTINGS['min_drop_gain_db'])
            # The bed sits about bed_below_dialogue_db under the dialogue, whatever the track's intro level.
            low, high = SETTINGS['bed_check_range_db']
            self.assertTrue(low <= checks['bed_db']['level'] <= high, checks['bed_db'])
            self.assertEqual(30, plan_t['head_frame'])
            frames = edits.decode_gray(tmp/'edits'/info['file'])
            drop = plan_t['drop_frame']
            # Dialogue picture up to the drop (the clip's grey, lifted a little by the look's curve,
            # still neutral), a white flash on it, black-and-white montage, black tail.
            last = frames[drop-1].astype(int)
            self.assertTrue(120 < last.mean() < 180, last.mean())
            self.assertLess(np.abs(last[..., 0]-last[..., 2]).mean(), 4)
            if profile == 'copper':
                self.assertLess(frames[drop].mean(), 200)
                held = frames[plan_t['cut_frames'][-1]-4:plan_t['cut_frames'][-1]].astype(int)
                self.assertLess(np.abs(held-held[0]).mean(), 1.0)
            else:
                self.assertGreater(frames[drop].mean(), 200)
            middle = frames[(plan_t['cut_frames'][1]+plan_t['cut_frames'][2])//2].astype(int)
            self.assertLess(np.abs(middle[..., 0]-middle[..., 2]).mean(), 4)
            self.assertLess(frames[-1].mean(), 8)
            # The no-music file: dialogue, then silence; same picture length.
            dry = edits.decode_audio(tmp/'edits'/info['nomusic_file'])
            d = int(plan_t['drop_at']*48000)
            self.assertGreater(edits.rms_db(dry[d-48000:d-2400]), -30)
            self.assertLess(edits.rms_db(dry[d+4800:]), -60)
            # The limiter caps the mix; AAC adds a few hundredths at most.
            wet = edits.decode_audio(tmp/'edits'/info['file'], channels=2)
            self.assertLess(np.abs(wet).max(), SETTINGS['limiter']+.05)
            self.assertTrue((tmp/'edits'/info['cover']).exists())
            self.assertIn('so its drop lands on the cut', info['music_hint'])
            # Unchanged input: the next run reuses the files.
            messages = []
            edits.render_edit(ctx, clip_mp4, tmp/'edits', '03_Payoff', config, SETTINGS, out=messages.append)
            self.assertTrue(messages[0].startswith('Already complete'))

    def test_copper_bed_dip_stays_on_the_picture_clock_with_gapped_audio(self):
        e = edit(effects='copper')
        t = edits.timeline(CLIP, e, WORD, TRACK, SETTINGS)
        dry_fx = {**SETTINGS['effects']['copper'], 'bed_dip_seconds': 0}
        no_dip = {**SETTINGS, 'effects': {**SETTINGS['effects'], 'copper': dry_fx}}
        results = []
        for settings in (SETTINGS, no_dip):
            graph = edits.music_graph(t, e, settings, (-9, 0), 1)
            pcm = subprocess.check_output(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                                           "sine=r=48000:d=10,aselect='not(between(t,2,2.09))'",
                                           '-f', 'lavfi', '-i', 'sine=f=110:r=48000:d=20',
                                           '-filter_complex', graph, '-map', '[a]', '-f', 'f32le', '-'])
            results.append(np.frombuffer(pcm, np.float32).reshape(-1, 2))
        self.assertEqual(t['total_frames']*1600, len(results[0]))
        samples = np.flatnonzero(np.max(abs(results[0]-results[1]), axis=1) > 1e-6)/48000
        self.assertAlmostEqual(t['drop_at']-.11, samples[0], delta=.003)
        self.assertAlmostEqual(t['drop_at'], samples[-1], delta=.003)


class CutCheckTests(unittest.TestCase):
    def frames(self, cuts, total):
        levels, level, out = [40, 200], 0, []
        for i in range(total):
            if i in cuts:
                level = 1-level
            out.append(np.full((12, 10, 3), levels[level], np.uint8))
        return np.stack(out)

    def plan_t(self):
        return {'drop_frame': 30, 'cut_frames': [30, 44, 58, 72, 86], 'shot_frames': [14]*4}

    def test_cuts_on_their_frames_pass(self):
        result = edits.cut_check(self.frames({44, 58, 72, 86}, 100), self.plan_t(), SETTINGS)
        self.assertTrue(result['passed'])
        self.assertEqual(4, result['exact'])

    def test_a_cut_two_frames_late_fails(self):
        result = edits.cut_check(self.frames({44, 60, 72, 86}, 100), self.plan_t(), SETTINGS)
        self.assertFalse(result['passed'])
        self.assertEqual([58], result['off_grid'])

    def test_a_cut_between_identical_shots_is_unclear_not_failed(self):
        result = edits.cut_check(self.frames({44, 72, 86}, 100), self.plan_t(), SETTINGS)
        self.assertTrue(result['passed'])
        self.assertEqual([58], result['unclear'])


class RenderEditsTests(unittest.TestCase):
    def test_a_broken_edit_is_reported_and_the_index_is_still_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp/'Music').mkdir()
            plan = {'clips': [{'id': '01', 'title': 'A', 'start': 0.0, 'end': 20.0}],
                    'edits': {'01': edit(track='missing.wav')}}
            with self.assertRaisesRegex(ValueError, 'Edit 01: Music track not found'):
                edits.render_edits(tmp/'source.mp4', plan, tmp/'cache', {}, tmp/'out', {'segments': []},
                                   music_folder=tmp/'Music')
            self.assertEqual([], json.loads((tmp/'out'/'edits'/'edits.json').read_text(encoding='utf-8')))
            # Without edits, a stale index is emptied so the editor offers nothing that is gone.
            (tmp/'out'/'edits'/'edits.json').write_text('[{"id": "01"}]', encoding='utf-8')
            edits.render_edits(tmp/'source.mp4', {'clips': plan['clips']}, tmp/'cache', {}, tmp/'out',
                               {'segments': []})
            self.assertEqual([], json.loads((tmp/'out'/'edits'/'edits.json').read_text(encoding='utf-8')))


class EffectTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
    def test_detail_restores_luma_edges_without_sharpening_chroma(self):
        source = 'testsrc2=s=288x360:r=30:d=1,format=yuv444p,gblur=sigma=1.3'
        def frame(chain):
            result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', source,
                                     '-vf', 'null'+chain+',format=yuv444p', '-frames:v', '1',
                                     '-f', 'rawvideo', '-'], capture_output=True, check=True)
            return np.frombuffer(result.stdout, np.uint8).reshape(3, 360, 288).astype(float)
        original = frame('')
        detailed = frame(edits.detail_chain(SETTINGS))
        energy = lambda y: np.mean(np.diff(y, axis=0)**2)+np.mean(np.diff(y, axis=1)**2)
        self.assertGreater(energy(detailed[0]), 1.1*energy(original[0]))
        np.testing.assert_array_equal(original[1:], detailed[1:])
        disabled = {'look': {'sharpen': 0, 'detail': 0, 'clarity': 0}}
        self.assertEqual('', edits.detail_chain(disabled))

    def test_flash_pattern_follows_the_profile_and_shot_overrides(self):
        e = edit(shots=10, transition='black_flash')
        soft, strong = SETTINGS['effects']['soft'], SETTINGS['effects']['strong']
        pattern = lambda fx: [edits.shot_flash(k, e['montage'][k], e, SETTINGS, fx) for k in range(9)]
        self.assertEqual(('black', SETTINGS['flash_frames']), pattern(soft)[0])
        self.assertEqual([4, 8], [k for k, f in enumerate(pattern(soft)) if f and k])
        flashes = pattern(strong)
        self.assertEqual(('black', 1), flashes[2])            # strobe on every second cut
        self.assertEqual(('black', strong['flash_frames']), flashes[4])  # the drop's colour every fourth
        e['montage'][4]['flash'] = 'none'
        e['montage'][3]['flash'] = 'white'
        self.assertIsNone(edits.shot_flash(4, e['montage'][4], e, SETTINGS, strong))
        self.assertEqual('white', edits.shot_flash(3, e['montage'][3], e, SETTINGS, strong)[0])
        self.assertIsNone(edits.shot_flash(5, e['montage'][5], e, SETTINGS, SETTINGS['effects']['none']))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
    def test_the_punch_peaks_on_the_first_frame_and_the_push_in_follows_the_formula(self):
        import cv2
        zoom, punch, settle, count = .06, .2, 3.0, 12
        vf = edits.zoom_filter(count, zoom, punch, settle)
        source = 'testsrc2=s=288x360:r=30:d=1,trim=end_frame=1,loop=loop=-1:size=1,trim=end_frame=12,setpts=N/30/TB'
        def gray(filters):
            out = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', source, '-vf', filters+'format=gray',
                                  '-f', 'rawvideo', '-'], capture_output=True, check=True).stdout
            return np.frombuffer(out, np.uint8).reshape(-1, 360, 288).astype(np.float32)
        still, frames = gray('')[0], gray(vf+',')
        for n, frame in enumerate(frames):
            expected = (1+zoom*n/(count-1))*(1+punch*np.exp(-n/settle))
            errors = {}
            for z in np.arange(1.0, 1.32, .002):
                m = np.float32([[z, 0, (1-z)*144], [0, z, (1-z)*180]])
                errors[z] = np.mean(np.abs(cv2.warpAffine(still, m, (288, 360))[80:-80, 80:-80]-frame[80:-80, 80:-80]))
            self.assertAlmostEqual(expected, min(errors, key=errors.get), delta=.006, msg=f'frame {n}')

    def look(self, source, levels, grade=None, glow=True):
        chain = edits.look_chain('t', levels, SETTINGS, grade, glow)
        result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', source, '-frames:v', '1', '-filter_complex',
                                 f'[0:v]null{chain},format=rgb24[o]', '-map', '[o]', '-f', 'rawvideo', '-'],
                                capture_output=True, check=True)
        return np.frombuffer(result.stdout, np.uint8).reshape(360, 288, 3).astype(int)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
    def test_the_look_lifts_dark_footage_and_the_glow_keeps_its_colour(self):
        import cv2
        # Dark footage like the series: an orange-ish face on black, nothing near white.
        dark = 'color=c=0x1a1410:s=288x360,drawbox=x=90:y=90:w=110:h=150:color=0x6e4a32:t=fill'
        plain = self.look(dark, (0.0, 1.0), glow=False)
        original = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', dark, '-frames:v', '1', '-pix_fmt', 'rgb24',
                                   '-f', 'rawvideo', '-'], capture_output=True, check=True).stdout
        original = np.frombuffer(original, np.uint8).reshape(360, 288, 3).astype(int)
        lows, highs = original.min()/255, original.max()/255
        lifted = self.look(dark, (lows, highs))
        # The subject comes up out of the dark; the dark background stays dark (that is the contrast).
        self.assertGreater(lifted[150, 145].mean(), original[150, 145].mean()+40)
        self.assertLess(lifted[20, 20].mean(), original[20, 20].mean()+8)
        self.assertGreater(lifted[150, 145].astype(int).max()-lifted[20, 20].max(), plain[150, 145].max()-plain[20, 20].max())
        # The glow must not drain the colour (it once screened the chroma planes too).
        sat = lambda img: cv2.cvtColor(img.astype(np.uint8), cv2.COLOR_RGB2HSV)[..., 1].mean()
        bright = 'color=c=0xf0b070:s=288x360'
        self.assertGreater(sat(self.look(bright, (0.0, 1.0), SETTINGS['grades']['warm'])),
                           .8*sat(self.look(bright, (0.0, 1.0), SETTINGS['grades']['warm'], glow=False)))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
    def test_zoom_punch_and_shake_keep_the_frame_filled(self):
        fx = SETTINGS['effects']['strong']
        zoom = edits.zoom_filter(30, fx['zoom'], fx['punch'], fx['punch_frames'], fx['shake_px'])
        # A grey picture with a bright border: anything outside the picture would show as black.
        source = 'color=c=gray:s=288x360:r=30:d=1,drawbox=x=0:y=0:w=288:h=360:color=white:t=40'
        result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', source, '-vf', zoom+',format=gray',
                                 '-f', 'rawvideo', '-'], capture_output=True, check=True)
        frames = np.frombuffer(result.stdout, np.uint8).reshape(-1, 360, 288)
        self.assertEqual(30, len(frames))
        self.assertGreater(int(frames.min()), 100)
        # The shake moves the picture on the first frames and dies out with the punch.
        border = [int((f[180, :144] > 200).sum()) for f in frames]
        self.assertNotEqual(border[0], border[1])
        self.assertEqual('', edits.zoom_filter(30))


class ReviewTests(unittest.TestCase):
    def plan(self):
        return {'clips': [{'id': '01', 'title': 'A', 'start': 0, 'end': 20},
                          {'id': '02', 'title': 'B', 'start': 30, 'end': 50}],
                'edits': {'01': edit(), '02': edit()}}

    def test_edit_decisions_are_kept_apart_from_clip_decisions(self):
        from gallery import actionable, apply_edit_decision, empty_decisions
        decisions = empty_decisions()
        clip = {'id': '01', 'title': 'A'}
        entry = apply_edit_decision(decisions, clip, {'status': 'needs_changes', 'comment': ' slower track '}, 't1')
        self.assertEqual('slower track', entry['comment'])
        self.assertEqual({}, decisions['clips'])
        self.assertEqual(['01'], list(actionable(decisions, 'edits')))
        self.assertEqual({}, actionable(decisions))
        with self.assertRaises(ValueError):
            apply_edit_decision(decisions, clip, {'status': 'maybe'})

    def test_apply_locks_approved_and_removes_rejected_edits_but_keeps_the_clips(self):
        from review_apply import apply_edits_to_plan
        plan = self.plan()
        decisions = {'clips': {}, 'edits': {'01': {'status': 'approved', 'updated': 't0'},
                                            '02': {'status': 'rejected', 'comment': 'too busy'}}}
        notes = apply_edits_to_plan(plan, decisions, 't1')
        self.assertEqual({'at': 't0'}, plan['edits']['01']['approved'])
        self.assertNotIn('02', plan['edits'])
        self.assertEqual('too busy', plan['rejected_edits']['02']['review_comment'])
        self.assertEqual(['01', '02'], [c['id'] for c in plan['clips']])
        self.assertEqual({'01', '02'}, set(notes))
        decisions['edits']['01']['status'] = 'pending'
        apply_edits_to_plan(plan, decisions, 't2')
        self.assertNotIn('approved', plan['edits']['01'])

    def test_an_edit_leaves_with_its_rejected_clip(self):
        from review_apply import apply_edits_to_plan
        plan = self.plan()
        plan['clips'] = plan['clips'][:1]
        apply_edits_to_plan(plan, {'clips': {}, 'edits': {}}, 't1')
        self.assertEqual(['01'], list(plan['edits']))
        self.assertIn('02', plan['rejected_edits'])


if __name__ == '__main__':
    unittest.main()
