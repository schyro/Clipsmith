# Clipsmith: instructions for AI agents

This file is the single source of truth for any AI coding agent (Codex, Claude Code, Cursor, Gemini CLI, GitHub Copilot, Aider, ...). Paths are relative to the repository root. Below, `run` means `./run.cmd` on Windows and `./run.sh` on macOS/Linux; both take the same arguments.

You, the agent in the chat, are the editor: you read the transcripts and pick the clips. Python only transcribes, analyses and renders. No separate LLM API key is needed. Reply to the user in their own language; everything written into the repository (code, comments, CLI messages, UI text, plan titles) is English.

## When the user says they added videos to `Raw/`

Carry the job through to verified MP4 files. A running process is not completion.

1. **Prepare.** Run `run --prepare-review` and wait for it. `Review/manifest.json` lists, per source, the full dialogue (`review.md`), global word indices and times (`words.json`) and timestamped contact sheets.
2. **Select.** For each entry that needs review, read the complete `review.md` and look at the contact sheets. You are building a pool of candidates for short-form social video (Shorts, Reels, TikTok). The user picks the winners, so when in doubt, include the moment. Do not ask the user for a clip count or length; the rules below are the default.
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
5. **Render.** Run `run --require-reviewed` and wait. It first checks the plan (coverage, clip count, no edge inside a word), lists every problem at once and renders nothing until they are fixed; `run --require-reviewed --analyze-only` runs only that check. A nonzero exit code is a failure: fix the cause, do not report a partial set as done.
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

## Applying the user's review

The user reviews clips in the clip editor (`run --review-gallery`), which saves `review-decisions.json` in the output profile folder, keyed by clip id.

1. When the user asks to apply their review, and before revising or reselecting a source, run `run --apply-review` (unless they already pressed Apply in the editor). It locks approved clips, moves rejected ones to `rejected/`, writes staged framing, applies moved edges without cutting words, re-renders only what changed and prints the entries still open, with their notes.
2. For each open entry (`needs_changes` or `rejected` with `addressed` not true): follow the comment and every timeline note (`notes`: `{"t": <source seconds>, "text": "..."}`, pinned to the moment it is about), fix framing through `focus_overrides`, re-cut on word boundaries if the comment or a note asks for more than the moved edges, and re-render.
3. Set that entry's `addressed: true`, `addressed_at` and a short `agent_note` in the decisions file.
4. Leave approved (locked) clips alone unless the user withdraws the approval. Then give the user the review link again.

Details: `docs/clip-editor.md`.

## Changing the code

- Run the tests: `.venv/Scripts/python -m unittest discover -s tests -v` (Windows) or `.venv/bin/python -m unittest discover -s tests -v`.
- For rendering or caption changes, also do a real short render and look at it.
- Video-only edits (plans, overrides, caption fixes) need no new tests.

More: `docs/reference.md`.
