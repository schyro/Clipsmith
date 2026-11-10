# Clipsmith: instructions for AI agents

This file is the single source of truth for any AI coding agent (Codex, Claude Code, Cursor, Gemini CLI, GitHub Copilot, Aider, ...). Paths are relative to the repository root. Below, `run` means `./run.cmd` on Windows and `./run.sh` on macOS/Linux; both take the same arguments.

You, the agent in the chat, are the editor: you read the transcripts and pick the clips. Python only transcribes, analyses and renders. No separate LLM API key is needed. Reply to the user in their own language; everything written into the repository (code, comments, CLI messages, UI text, plan titles) is English.

## When the user says they added videos to `Raw/`

Carry the job through the user's triage to verified MP4 files. A running process is not completion.

1. **Prepare.** Run `run --prepare-review` and wait for it. `Review/manifest.json` lists, per source, the full dialogue (`review.md`), global word indices and times (`words.json`), timestamped contact sheets and the shot index (`shot_index`, `character_sheet`; see Shot index below). The first run scans each source once for camera cuts and faces, a few minutes per hour of video.
2. **Select.** First read `Plans/review-preferences.md` when it exists: the clips this user approved and rejected so far, with their lengths, the plan's reasons and the reasons the user picked. Use it as evidence of their taste; it does not replace the rules below. For each entry that needs review, read the complete `review.md` and look at the contact sheets. You are building a pool of candidates for short-form social video (Shorts, Reels, TikTok). The user picks the winners, so when in doubt, include the moment. Do not ask the user for a clip count or length; the rules below are the default.
   - **What qualifies:** any moment that would make someone stop scrolling, watch to the end, replay it, comment on it or send it to a friend, for whatever reason. It must make sense to someone who has not seen the rest of the recording. Look for every kind:
     - *Funny:* jokes, banter, comebacks, sarcasm, comic timing, absurd or awkward situations, someone being caught out or put in their place.
     - *Emotional:* grief, love, regret, confessions, vulnerability, comfort, kindness, forgiveness, goodbyes and reunions, someone opening up about their past.
     - *Motivational and inspiring:* advice and life lessons, encouragement, wisdom about purpose, failure, success or perseverance, someone finding courage, standing up for themselves or others, or doing the right thing at a cost.
     - *Tension:* confrontations, arguments, threats, interrogations, ultimatums, power plays, someone losing their temper.
     - *Surprise:* reveals, twists, exposed lies, unexpected answers, someone turning out to be far smarter or worse than expected.
     - *Clever:* deductions, tricks, cold reads, sharp observations, explanations that make the viewer feel smart.
     - *Quotable or debatable:* a line people would put in a caption, a bold or controversial opinion, a question viewers would argue about in the comments.
     - *Relatable:* everyday frustrations and family, work or relationship moments that viewers recognise from their own lives.
     - *Character and chemistry:* a strong introduction, flirting, friendship, rivalry, a moment that shows who someone really is.
     - *Spectacle:* action, stunts or striking images the contact sheets show, even with little dialogue.
   - **Length:** most clips 20–60 s, ideally 30–50 s. A single line with a clear payoff can be 10–20 s. Go up to 90 s only when the payoff needs that much build-up, never more unless the user asks. Split a long scene into its separate beats instead of keeping it whole.
   - **Shape:** start on the hook, the line or question that makes someone stop scrolling, as late as the setup still makes sense. End right after the payoff or the reaction to it. Leave out walk-ins, dead air and scene transitions.
   - **Coverage:** go through the whole runtime in order, one `### Block` of `review.md` at a time, and check every kind above in each block. Write what you find into the plan's `coverage` (step 3). A block with no clip is a decision, not an oversight. There is no fixed count; as a check, dialogue-driven material usually gives at least one candidate per two minutes (20 or more for a 40-minute episode), and far fewer means moments were missed.
   - One clip per moment: two clips never share a punchline. Neighbouring clips may share a few seconds of context.
   - Reuse an existing reviewed plan unless the user asks for a new selection.
3. **Write the plan** to `Plans/<source_id>.json` (see `examples/plan.example.json`):
   - `source`, `source_id`, `selection: "agent-reviewed"` and `clips`.
   - Each clip has `start` and `end` in source seconds, a short `title` (used only for file names and the gallery) and a `reason`.
   - Check both edges against the word timestamps in `words.json` so no word is cut. Stay inside the source. 180 s is only the technical limit; the length rule above applies. The render moves an edge that leaves less than 0.4 s of a neighbouring camera shot onto the cut when no word is in the way, and writes it back to the plan; it prints the edges it had to leave.
   - `coverage`: one entry per `### Block` of `review.md`, `{"start": 600, "note": "..."}`, where `start` is the block's start in seconds (shown in its heading). The note says what happens in the block and which moments you took. A block with no clip also needs `why_not_more`: why nothing in it works on its own. When the plan has fewer clips than one per two minutes of runtime (rejected clips count), every block with a single clip needs a `why_not_more` too. Set the plan's `count_note` only when the user asked for fewer clips.
   - Optional: `cover_time` (seconds into the clip), `focus_overrides` (see Framing), `auto_reframe: false` with a fixed `crop_x` (0..1).
   - Never renumber clip `id`s. A new clip gets the next unused id.
4. **Fix captions.** Look for evident recognition errors in the selected ranges, especially names. Write corrections to `work/<source_id>/caption-edits-medium.json` as `{"model":"medium","edits":[{"index":123,"from":"original","to":"corrected"}]}`, where `index` is the word's `index` in `words.json` (a number, or a `g` id for a word the missed-speech pass found). Keep the timing; never invent dialogue. Keep any other fields and the `added` list the clip editor wrote there. When the audio is unclear, keep the recognised word rather than guess. Non-English sources are translated to English by Whisper; check the translation and timing.
5. **Triage, then render.** Run `run --require-reviewed --analyze-only`. It checks the plan (coverage, clip count, no edge inside a word) and lists every problem at once; fix them all. Then start `run --review-gallery` in the background and give the user the link (`http://127.0.0.1:8765/editor/`), and stop. The user plays each candidate from the proxy with live captions, keeps or rejects it (with a reason), and Apply renders only the kept clips. When the user says the triage is done, run `run --require-reviewed` and wait: it renders only clips that were kept or rendered before and lists the candidates still waiting for triage (`--no-triage` renders them too, only when the user asks to skip the triage). A nonzero exit code is a failure: fix the cause, do not report a partial set as done.
6. **Check.** After rendering, `run --require-reviewed` validates every clip, writes `work/<id>/qa-current/sheet-NN.jpg` (rendered start, middle and end frames, labelled by clip id) and prints those sheets, the focus sheet folder and every shot flagged for framing. Look at all of them: caption placement and timing on the sheets, and each flagged shot in `Review/<id>/focus/<clip>.jpg`. Fix framing with `focus_overrides` where the crop misses the speaker, then render again. `verify_outputs.py` repeats the validation on its own.
7. **Deliver.** Give the user the finished MP4 paths, the clip count, each clip's title and duration, any real remaining limitation, and the review link from `run --review-gallery` (`http://127.0.0.1:8765/editor/`).

## Output rules

- The output format, captions, fonts, fades and animation come from `video_settings.json`. Treat it as the user's preference and do not change it unless asked.
- The picture always fills the output frame. Never letterbox, pad, zoom out or add blurred backgrounds.
- Do not add headings or decorative lines. Subtitles already burned into a recording stay part of its image.
- Never modify `Raw/` originals or earlier output profiles. Never publish to social accounts unless explicitly asked.

## Framing

- Light-ASD scores audiovisual speech per face; YuNet supplies visible face tracks. Follow the visible speaking face. Analysis and rendering share the same 30 fps frame grid, and tracking resets at camera cuts.
- Never point the crop at a face from the next shot or at an offscreen speaker; keep a face that is actually visible.
- Inside a shot, focus changes only for a visible person's reply lasting at least `speaker_switch_min_seconds`. Silence and short interjections never move the crop.
- To correct framing, add reviewed `focus_overrides` to the clip: `{"start_frame": 30, "end_frame": 90, "center_x": 0.65}` with clip-relative, half-open frame ranges, source-normalised `center_x` and optional `center_x_end` for a pan. An override never crosses a camera cut (cut frames are listed in `.focus.json`).
- Changing framing must not change the approved audio or caption settings.

Details: `docs/framing.md`.

## Music edits

A music edit is the fan-edit version of a clip:
- the clip's dialogue up to its payoff line;
- a drop on the music;
- a 4–9 s montage of the same character, one shot per beat;
- a black tail.

Make edits when the user asks for them, or when `Music/` holds tracks while you prepare clips. You pick which clips get one.

1. **Tracks.** Run `run --scan-music`. It lists every track with its BPM, beat length, drop and `mood`, and per drop candidate the level and onset rate before and after it and its strongest beats (`+10 (bar 3.3)`, bars counted from the drop).
   - You cannot hear the music. Pick a track by its `mood`, its tempo and those descriptors. Vocals are not detected.
   - The cuts sit on the grid `first_beat` + k x beat, and the drop used is the grid beat nearest `drop`. Report a "drop ... off the beat grid" or "the drop may not be a downbeat" warning to the user; do not fix the sidecar yourself.
   - When a track has no mood, ask the user to fill in `mood` in `Music/<name>.json`, then continue.
   - Never edit a sidecar's `bpm`, `first_beat` or `drop` unless the user asks.
2. **Clips.** An edit suits a clip that ends on a clear payoff in its last line: a punchline, a comeback, a reveal, a defiant or emotional last word. Its final word must end at least 0.1 s before the clip end. Skip clips whose strongest moment is not at the end; they stay plain clips. Identify who the montage is about separately from who speaks the last word. A reply or reaction can be the reason to feature the other character.
3. **Write the plan.** Add a top-level `edits` block, keyed by clip id (see `docs/edits.md` for every field):
   - `track`: matching the clip's tone. Optional `drop`: another of the track's `drop_candidates` (seconds) when it suits the edit better.
   - `payoff_word`: the `index` of the payoff's last word in `words.json`.
   - `dialogue_start`: an edit runs about 20–30 s, so its dialogue lasts about 10–20 s. When the clip is longer, open the edit on the latest line that still sets up the payoff, in a gap between two words.
   - `effects`: use `"copper"` for new music edits unless the user requests another treatment. Existing plans keep their chosen profile. Copper uses restrained colour, a short music-bed dip before the drop, a four-frame hold at the end of the last shot, and a short black tail.
   - `transition`: use `"cut"` with Copper. Use a clear, immediately recognisable shot of the character on the first frame of the drop. Set `drop_delay` around 0.19–0.25 s when the end of the line leaves room. Leave `grade` unset for the Copper colour look; override it if the source or the user's requested tone calls for it.
   - `montage`: pick distinct moments of the featured character from the same source, outside the clip. Find them in the shot index, for example `run --shots --character 1 --framing close-up --sharp --min-seconds 0.6 --sheet`, and look at the sheet it writes; start each montage shot inside one listed camera shot. Alternate readable close-ups, medium shots and meaningful motion. Aim for about 5–7 s of montage when the track and available shots support it. Each entry starts with `{"start": <source seconds>}`.
   - **Rhythm.** Give shots a length in beats with `"beats"` (whole or half beats) instead of one beat each, for example `2, 2, 1, 1, 0.5, 1, 0.5, 2, 2`: longer shots to establish, halves for a stutter, a hold to land. Put the cut into the strongest shot (and its `flash`) on one of the strongest beats from the scan. Make the total a multiple of 4 beats so the montage ends on a bar line; the check warns otherwise, and you answer with a different pattern or the edit's `bar_note`. Older plans use `"slots"`, which still works.
   - **Copper shot directions.** Use the default eye-anchored push on the first shot when its framing has room. Use `"motion": "none"` if the first shot is already tight or the face is unsuitable for tracking. `center_x` sets the horizontal crop in source coordinates; `anchor: [x, y]` sets a manual push pivot in the cropped frame. Check that the face stays readable. Add `"motion": "ramp"` to one shot with a visible gesture or expression. The check prints where each shot's motion peaks and the `start` that puts it on the cut, on the next beat, or in the ramp's slowest part; use those values instead of guessing, and treat "no clear motion" as free timing. Leave other shots on clean cuts unless a second push is motivated by the action. Use `"flash": "white"` on at most three separated accents after the drop. Use `practical_light: [x, y]` only where a real bright source is visible, and `"cool_background": true` only on shots whose vivid violet or blue distracts from the face. Choose a final pose that reads clearly during the short hold. Skip silhouette echoes and routine shake in this treatment.
   - `reason`.
4. **Check.** Run `run --require-reviewed --analyze-only`. It lists every problem with the edits; a shot that crosses a camera cut comes with the times to move it to. Answer every printed warning (bar line, drop off the grid). A speed ramp can need more source footage than its beat slot. It also writes `Review/<id>/edits/<stem>-montage.jpg`. Review Copper's entry, motion and exit frames. Fix misplaced crops with `center_x` and adjust the push anchor or shot timing where needed.
5. **Render.** `run --require-reviewed` renders the edits after the clips into `<profile>/edits/`. A failed acceptance check fails the run: cuts on the beat grid, the drop's loudness jump, the drop delay or the frame count. Fix the cause. Look at `work/<id>/qa-current/edits/edit-<clip id>.jpg`.
6. **Deliver.** Per edit, give the user:
   - both files, `<stem>.edit.mp4` (with music) and `<stem>.edit-nomusic.mp4`;
   - the duration and the track;
   - the `music_hint` from `<stem>.edit.json`, which says where to start the track in an app.

Edit defaults live in `edit_settings.json`. Treat them like `video_settings.json`: change them only when the user asks.

## Shot index

`Review/<id>/shot-index.json` lists every camera shot of a source: its time range and, per visible face, its size (`close-up`, `medium`, `wide`), position, sharpness and a `character` id shared by the same person across the source. `run --prepare-review` builds it; `run --shots` builds it on its own and prints the characters.

- Look at `Review/<id>/shots/characters.jpg` first to learn which id is which person. Ids belong to one source.
- Query with `run --shots` plus any of `--character N`, `--framing close-up|medium|wide`, `--sharp`, `--solo`, `--min-seconds S`, `--from S`, `--to S`. Add `--sheet` to get a contact sheet of the matches, each shown at the moment the face is clearest.
- Use it to pick montage shots, to find a character's best close-ups and to check who is on screen in a shot you are framing.
- A `character` of `null` means the face was too small or too rare to identify, or the identity model is not installed. Check such shots on a sheet.

Details: `docs/shot-index.md`.

## Applying the user's review

The user reviews clips in the clip editor (`run --review-gallery`), which saves `review-decisions.json` in the output profile folder, keyed by clip id.

1. When the user asks to apply their review, and before revising or reselecting a source, run `run --apply-review` (unless they already pressed Apply in the editor). It locks approved clips, moves rejected ones to `rejected/`, writes staged framing, applies moved edges without cutting words, re-renders only what changed and prints the entries still open, with their notes.
2. For each open entry (`needs_changes` or `rejected` with `addressed` not true; a rejection may carry `reasons` such as `needs_context` or `too_long` besides its comment): follow the comment and every timeline note (`notes`: `{"t": <source seconds>, "text": "..."}`, pinned to the moment it is about), fix framing through `focus_overrides`, re-cut on word boundaries if the comment or a note asks for more than the moved edges, and re-render.
3. Set that entry's `addressed: true`, `addressed_at` and a short `agent_note` in the decisions file.
4. Leave approved (locked) clips alone unless the user withdraws the approval. Then give the user the review link again.
5. Music edits have their own decisions (`edits` in the decisions file; the editor's *Edit* tab). Apply locks approved edits and removes rejected ones without touching their clips. For each open edit, listed as `[<id> edit]`, follow the comment and re-render: another track, other montage shots, another grade or transition. Then mark it addressed the same way.

Details: `docs/clip-editor.md`.

## Changing the code

- Run the tests: `.venv/Scripts/python -m unittest discover -s tests -v` (Windows) or `.venv/bin/python -m unittest discover -s tests -v`.
- For rendering or caption changes, also do a real short render and look at it.
- Video-only edits (plans, overrides, caption fixes) need no new tests.

More: `docs/reference.md`.
