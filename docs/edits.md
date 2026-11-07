# Music edits

On macOS/Linux use `./run.sh` wherever `./run.cmd` appears.

A music edit turns a reviewed clip into the fan-edit format: the clip's dialogue runs up to its payoff line, the music drops right after it, a 4–9 s montage of the same character cuts on every beat, and the picture ends on a short black tail. Each edit is rendered twice: with the music, and without it (same cuts, dialogue only) for platforms where the music is added in the app.

```
| dialogue (the rendered clip, its captions and framing) | drop | montage: one shot per beat slot, graded, muted | black tail |
                                                payoff word ends ^  ^ 0.1–0.5 s later: first shot, music at full level
```

## Music

Put the tracks in `Music/` (ignored by git; the user supplies them and is responsible for their licence). Then:

```bash
./run.cmd --scan-music
```

For every track without one, this writes a sidecar `Music/<name>.json`:

```json
{"file": "song.mp3", "mood": "", "bpm": 128.03, "first_beat": 0.009, "drop": 30.47, "duration": 64.0,
 "drop_candidates": [{"time": 30.47, "rise_db": 21.9}, {"time": 15.47, "rise_db": 11.3}],
 "detected": {"bpm": 128.03, "first_beat": 0.009, "drop": 30.47, "version": 1, "file_hash": "..."}}
```

- `mood` is for the user: a few words about the feel ("funny, smug", "sad, slow build"). The agent cannot hear the music and picks tracks by this field.
- `bpm`, `first_beat` and `drop` (seconds) are detected and can be corrected by hand. `drop_candidates` lists the next-best drops.
- A scan never overwrites a sidecar. If the audio file changes, the scan warns; delete the sidecar to detect again.

Detection (`music.py`, numpy and scipy only) takes the positive spectral flux of the log spectrum as an onset envelope and fits the constant-tempo grid (60–200 BPM, any phase) that collects the most onset strength over the whole track, so a grid twice too slow or too fast loses. The envelope peak sits a fixed 36 ms before the heard onset (measured, ±3 ms), which is corrected. The drop is the beat with the largest energy rise from the 8 beats before to the 8 beats after it, broadband and bass combined. Tests check tempo, phase (within 15 ms, half a 30 fps frame) and drop on synthetic tracks at 95, 128 and 150 BPM.

## The plan

Edits live in the plan's top-level `edits` block, keyed by clip id. A clip's own fields stay untouched, so adding an edit never re-renders the clip.

```json
"edits": {
  "26": {
    "track": "song.mp3",
    "payoff_word": 3283,
    "effects": "copper",
    "transition": "cut",
    "drop_delay": 0.19,
    "montage": [
      {"start": 60.0, "center_x": 0.64, "anchor": [0.42, 0.34], "practical_light": [0.1, 0.56]},
      {"start": 240.0},
      {"start": 360.0, "flash": "white"},
      {"start": 900.0, "center_x": 0.74},
      {"start": 1350.0, "motion": "ramp"},
      {"start": 1800.0, "flash": "white", "cool_background": true},
      {"start": 2369.55, "slots": 2}
    ],
    "reason": "Defiant one-line payoff."
  }
}
```

| Field | Meaning |
| --- | --- |
| `track` | File name in `Music/`. |
| `payoff_word` | `index` of the payoff's last word in `Review/<id>/words.json`. The drop follows its end by `drop_delay`, so it survives caption fixes and moved clip edges. The drop must fall inside the clip. |
| `drop_delay` | Optional, seconds after the payoff word (default 0.25, allowed 0.1–0.5). |
| `dialogue_start` | Optional source seconds where the edit opens, inside the clip and between two words (the check names the word a start would cut). An edit runs about 20–30 s, so a long clip opens on a later line that still sets up the payoff. At least 3 s of dialogue stays before the drop. |
| `grade` | Optional montage grade: `none`, `bw` or `warm` (`edit_settings.json`). When omitted, Copper has its own muted colour grade and other profiles use `none`. The grade applies to the montage only. |
| `transition` | At the drop: `white_flash`, `black_flash` or `cut`. |
| `effects` | `copper` for new editorial plans. `soft` (when omitted), `strong` and `none` retain their existing behaviour. |
| `cut_beats` | Optional beats per montage slot: 0.5, 1, 2 or 4. By default, the fewest beats that give at least 0.45 s per cut. |
| `montage` | Shots in source seconds, in order. Each fills one slot (`slots` holds it longer). The montage must last 4–9 s; the check names the slot count that fits. `center_x` (0–1, source-normalised) overrides the crop centre; `flash` (`white`, `black` or `none`) selects an accent on that shot after the drop. The edit's `transition` controls the drop itself. See the extra Copper shot fields below. |
| `music_lufs` | Optional loudness of the music after the drop (default −9 LUFS). |
| `approved` | Set by `--apply-review`; locks the edit. |

Defaults (bed level, tail length, flash frames, grades, limiter, checks) live in `edit_settings.json`; changing them re-renders edits only, never clips.

## Effects

### Copper

Copper is based on the reviewed *The Truth Is Mine* experiments. Give the drop a clear view of the featured character, then use a few intentional accents across the beat grid. The dialogue picture comes from the reviewed clip without Copper colour or zoom. The final picture holds for four frames before a beat-aligned cut to a 0.3 s black tail.

| Shot field | Copper behaviour |
| --- | --- |
| `motion` | `push` (the default on the first shot), `ramp`, or `none` (the default elsewhere). Push starts near the original crop, quickly approaches and holds. Ramp accelerates and settles around a gesture without changing the beat slot. Use a source window that contains the full action and stays within one camera shot. |
| `zoom` | Optional push amount from 0.01 to 0.20. The default is 0.14. A close-up often calls for a smaller value or `none`. |
| `anchor` | Optional `[x, y]` in the cropped frame, normalised from the top left. The push defaults to the visible eye line nearest the crop centre, with an upper-centre fallback when no face is found. Review the pivot when there are several faces or the subject is difficult to detect. |
| `practical_light` | Optional `[x, y]` in the cropped frame, normalised from the top left. Adds warm, feathered halation driven by the source brightness around that position. Use a visible light, away from the face. |
| `cool_background` | Optional boolean. Pulls highly blue/violet chroma toward a quieter blue in shots with a distracting studio background. Check the skin and clothing in the preview. |

On push and ramp shots, the renderer samples the source frames directly. A push combines the crop and zoom in one transform, with subframe samples along the zoom curve for motion blur. A ramp uses a smooth source-time curve sampled on the fixed 30 fps output grid. An explicit flash blends a small amount of white or black over the first two frames. There is no automatic flash pattern. Plan white accents at separated musical hits, and use a clean `cut` at the drop.

The music bed briefly dips up to 3.5 dB during the final 0.11 s before the drop, with the window shortened if the payoff ends closer to it. Dialogue and music use the picture clock, including when the rendered clip's audio has timestamp gaps.

### Earlier profiles

| | `soft` | `strong` |
| --- | --- | --- |
| Push-in over each montage shot | 6 % | 10 % |
| Punch on each cut (starts larger, settles over 3–4 frames) | 7 % | 22 % |
| Shake with the punch | none | 26 px, dying out with it |
| Flash on every fourth cut (the drop's colour, 2 frames) | yes | yes |
| One-frame black strobe on every second cut | no | yes |
| Push-in over the dialogue | 3 % | 4 % |

The dialogue gets only its slow push-in. Its burned captions grow with it, by a few per cent.

Zoom, punch and shake use `perspective`, which maps an inner rectangle onto the whole frame. It interpolates below the pixel (cubic), so the motion is smooth: `zoompan` steps visibly and per-frame `scale` does not work. The frame stays filled, because the rectangle always lies inside the picture and the shake is limited to the zoom margin. `perspective` counts its frames from 1, which the expressions correct, so the punch peaks on the cut frame. A test measures the scale frame by frame.

## Look

The profiles share an HD look (`look` in `edit_settings.json`). The earlier profiles apply it to dialogue without colour grade or glow. Copper applies a gentler version to the montage and uses the reviewed dialogue picture as rendered. The montage look has these stages:

1. **Levels.** The picture is stretched 60 % of the way towards its own dark and bright points: the 0.5th and 99.5th luma percentiles, with the channels linked so colours keep their balance. The levels are measured once, on nine frames for the dialogue and on the middle frame for each montage shot, and applied as fixed numbers. A per-frame normalisation would pump at every camera cut inside the dialogue.
2. **Curve.** A contrast curve that also lifts the mid-tones; the series is dark, often around 40/255.
3. **Colour grade.** `bw` desaturates; `warm` adds saturation and a warm balance.
4. **Glow.** An 8 % glow from the parts brighter than 170. It screens only the brightness plane, so colour and black-and-white stay as graded.
5. **Detail.** After glow, luma-only unsharp masks add local contrast (`clarity`, 13x13, 0.3) and fine detail (`detail`, 5x5, 0.85), followed by moderate contrast-adaptive sharpening (`sharpen`, CAS 0.45). Chroma stays unsharpened to avoid coloured noise. Set any amount to zero to disable that stage.

Montage scaling uses Lanczos. Edit exports preserve fine detail with NVENC p6/HQ at CQ 16, or x264 slow at CRF 17 when NVENC is unavailable. The no-music file copies the same encoded video. These settings affect music edits only.

The montage sheet shows each shot with this look, as rendered.

## Checks before rendering

`./run.cmd --require-reviewed --analyze-only` checks the edits with the plan: every field, the payoff word inside its clip, the drop before the clip end, the montage length, enough track after the drop, and every shot inside the source. With edits in the plan it also runs the speaker analysis on each montage shot. A shot that crosses a camera cut is refused with the times to move it to. The crop centre follows the visible (speaking) face, as in a clip, or uses the reviewed `center_x`. The run writes `Review/<id>/edits/<stem>-montage.jpg`. Copper shows entry, motion and exit frames through the export filters. Earlier profiles show a graded frame from the middle of each shot. Use the sheet to verify the featured character, the pivot and the source window.

## Rendering

`./run.cmd --require-reviewed` renders the clips, then every edit, into `<profile>/edits/`:

- `<stem>.edit.mp4` and `<stem>.edit-nomusic.mp4`;
- `<stem>.edit.json`: the timeline (drop frame, cut frames, music offset), the acceptance checks and `music_hint`, which says where to start the track in an app so its drop lands on the cut;
- `<stem>.edit.jpg` (cover), `<stem>.edit.done.json` (signature) and `edits.json` (index).

Timeline, on the 30 fps grid:

- **Dialogue.** The rendered clip MP4 is trimmed from `dialogue_start` to the drop frame. Its reviewed framing and captions are reused as they are, which costs one extra encode generation.
- **Montage.** Cut *k* sits on frame round(D + k × interval × 30), computed from absolute times so rounding never adds up. Each shot is cut to its exact frame count with `trim=end_frame`. Montage shots are muted and carry no captions.
- **Audio.**
  1. The clip's dialogue, already normalised, starts and ends with a 30 ms fade.
  2. Before the drop, the music plays as a bed. Its level comes from two measurements (EBU R128): the dialogue in the edit, and the part of the track that actually plays under it, which in most tracks is a quieter intro or breakdown. The bed is set `bed_below_dialogue_db` (9 dB) under the dialogue and dips a few dB more under speech (`sidechaincompress`, ratio 2.5).
  3. From the drop, it plays at its full level: the post-drop part of the track is measured (EBU R128) and brought to −9 LUFS.
  4. The music fades out over the tail.
  5. Only a limiter runs on the final mix, so the drop keeps its jump. A whole-mix `loudnorm` would flatten it.
- **No-music file.** The same picture (stream copy) with the dialogue only, silent from the drop.

An edit whose inputs are unchanged is reused. An approved edit is kept as it is until the approval is withdrawn.

## Acceptance checks

Each rendered edit is checked, and a failure fails the run:

- **Frames:** the frame count matches the timeline.
- **Cuts:** around every expected montage cut and the cut to the black tail, the frame-to-frame picture change must start on that frame (the first frame that reaches half of the largest change nearby, so a flash or strobe right after the cut does not count as the cut). This works in black and white too. More than one frame off fails. A cut between two very similar shots has no clear peak and is listed as unclear.
- **Drop:** the second after the drop is at least 6 dB louder than the second before it.
- **Bed:** the music under the dialogue (the music file minus the no-music file) sits 4–16 dB under the dialogue, so it is heard without covering the words.
- **Delay:** the drop lies 0.1–0.5 s after the payoff word.

The QA sheet `work/<id>/qa-current/edits/edit-<clip id>.jpg` shows the last dialogue frame, the drop, every montage shot, and the tail. Copper adds the first, motion and last frames of each shot and the second frame after the drop. Play the completed Copper edit to judge the speed ramp, the clarity of the face on the beat and the short music dip.

## Review

The clip editor shows an *Edit* tab for clips that have an edit. It holds:

- both files, with and without music;
- the track, mood, tempo, drop and look;
- the checks;
- Approve, Needs changes and Reject, and a comment for the agent.

Decisions are saved under `edits` in `review-decisions.json`, apart from the clip's own decision. `--apply-review`:

- locks approved edits;
- moves rejected ones to the plan's `rejected_edits`, and their files to `edits/rejected/`, without touching the clip;
- lists the open edits as `[<id> edit]`.

*Export approved* also copies approved edits, both files, to `ready/`.
