# Clipsmith: instructions for AI agents

This file is the single source of truth for any AI coding agent (Codex, Claude Code, Cursor, Gemini CLI, GitHub Copilot, Aider, ...). Paths are relative to the repository root. Below, `run` means `./run.cmd` on Windows and `./run.sh` on macOS/Linux; both take the same arguments.

You, the agent in the chat, are the editor: you read the transcripts and pick the clips. Python only transcribes, analyses and renders. No separate LLM API key is needed. Reply to the user in their own language; everything written into the repository (code, comments, CLI messages, UI text, plan titles) is English.

## When the user says they added videos to `Raw/`

Carry the job through to verified MP4 files. A running process is not completion.

1. **Prepare.** Run `run --prepare-review` and wait for it. `Review/manifest.json` lists, per source, the full dialogue (`review.md`), global word indices and times (`words.json`) and timestamped contact sheets.
2. **Select.** For each entry that needs review, read the complete `review.md` and look at the contact sheets. Pick every distinct scene that works on its own: a strong opening, an understandable setup and a completed payoff. There is no fixed clip count. Do not turn every line into a clip, and avoid duplicate or overlapping moments. Reuse an existing reviewed plan unless the user asks for a new selection.
3. **Write the plan** to `Plans/<source_id>.json` (see `examples/plan.example.json`):
   - `source`, `source_id`, `selection: "agent-reviewed"` and `clips`.
   - Each clip has `start` and `end` in source seconds, a short `title` (used only for file names and the gallery) and a `reason`.
   - Check both edges against the word timestamps so no word is cut. Stay inside the source, keep clips at most 180 s.
   - Optional: `cover_time` (seconds into the clip), `focus_overrides` (see Framing), `auto_reframe: false` with a fixed `crop_x` (0..1).
   - Never renumber clip `id`s. A new clip gets the next unused id.
4. **Fix captions.** Look for evident recognition errors in the selected ranges, especially names. Write corrections to `work/<source_id>/caption-edits-medium.json` as `{"model":"medium","edits":[{"index":123,"from":"original","to":"corrected"}]}`. Keep the timing; never invent dialogue. Non-English sources are translated to English by Whisper; check the translation and timing.
5. **Render.** Run `run --require-reviewed` and wait. A nonzero exit code is a failure: fix the cause, do not report a partial set as done.
6. **Check.** Look at rendered frames, caption placement and timing in each distinct scene, and at `Review/<id>/focus/<clip>.jpg` plus every shot with a `review` flag in the clip's `.focus.json`.
7. **Deliver.** Give the user the finished MP4 paths, clip count and durations, any real remaining limitation, and the review link from `run --review-gallery` (`http://127.0.0.1:8765/editor/`).

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

1. When the user asks to apply their review, and before revising or reselecting a source, run `run --apply-review` (unless they already pressed Apply in the editor). It locks approved clips, moves rejected ones to `rejected/`, writes staged framing, applies moved edges without cutting words, re-renders only what changed and prints the entries still open.
2. For each open entry (`needs_changes` or `rejected` with `addressed` not true): follow the comment, fix framing through `focus_overrides`, re-cut on word boundaries if the comment asks for more than the moved edges, and re-render.
3. Set that entry's `addressed: true`, `addressed_at` and a short `agent_note` in the decisions file.
4. Leave approved (locked) clips alone unless the user withdraws the approval. Then give the user the review link again.

Details: `docs/clip-editor.md`.

## Changing the code

- Run the tests: `.venv/Scripts/python -m unittest discover -s tests -v` (Windows) or `.venv/bin/python -m unittest discover -s tests -v`.
- For rendering or caption changes, also do a real short render and look at it.
- Video-only edits (plans, overrides, caption fixes) need no new tests.

More: `docs/reference.md`.
