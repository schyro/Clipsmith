# Clip editor and review decisions

On macOS/Linux use `./run.sh` wherever `./run.cmd` appears.

## Commands

```bash
# Open the clip editor for the latest output (or pass an output folder).
./run.cmd --review-gallery
./run.cmd --review-gallery "Output/<source>_<id>/<profile>" --port 8765 --no-open
# Apply the saved decisions and re-render only what changed (the editor's Apply button runs the same).
./run.cmd --apply-review
# Regenerate only the read-only index.html without rendering.
./run.cmd --write-gallery "Output/<source>_<id>/<profile>"
```

## The editor

`--review-gallery` starts a local server (`editor_server.py`, 127.0.0.1 only) and opens `http://127.0.0.1:8765/editor/`. The front end lives in `editor/` (Preact and htm vendored in `editor/vendor/`, no CDN or build step). The server serves the editor, the fonts, and media from `Output` and `Review` only. It refuses cross-origin and non-JSON writes.

- **Live preview.** On first start the server makes a low-resolution proxy of the whole source (`Review/<id>/proxy.mp4`, 30 fps, a keyframe every half second, about a minute with NVENC). The browser draws the 4:5 crop and the word-highlighted captions on it at once while you edit. Crop centres come from the cached speaker analysis plus your staged overrides. Captions use the renderer's own grouping and timing for the word-safe cut that would be applied. The live view is approximate (proxy quality, unnormalised audio).
- **Exact preview.** *Render exact preview* renders the result of the staged edits at 432×540 with the real filters, captions and speaker analysis. It writes to `Review/<id>/preview/`, never to the output folder. A moved edge needs a new speaker analysis for the new range, which adds some seconds.
- **Modes.** Live, Rendered (the output file), Preview, and Compare. Compare is before/after, from `history/` after a re-render, or rendered/preview when there is no history yet.
- **Timeline.** Ruler, camera shots (overridden shots in yellow, flagged shots underlined in red), words (corrected words in purple), the current cut (dashed) and the new cut with drag handles. Word-safe applied edges show as green dashed lines. *Context* widens the view to ±30 s.
- **Cut.** Move each edge by a frame or 0.5 s, set it at the playhead (I/O), or drag it. The panel shows the word-safe range that Apply will use. Edges can move up to ±30 s.
- **Framing.** One reviewed centre per camera shot, with an optional pan from the shot's start to its end, so an override never crosses a cut. Drag the picture or the full-frame overview, or use the sliders. *Back to automatic* removes the shot's overrides. *Discard framing edits* drops the staged list and keeps the plan's overrides.
- **Captions.** Edit any word near the playhead, or click a word on the timeline. Enter saves to `work/<id>/caption-edits-medium.json`; ↺ restores the original, and an empty word removes it. Corrections apply to every clip of the source on the next Apply, except approved (locked) clips.
- **Decision.** Approved / Needs changes / Rejected and a comment. Editing the cut or framing sets *Needs changes* with the matching option. Approved clips are read-only until the approval is withdrawn.
- **Apply** lists the pending changes, runs `--apply-review` in the background and streams its log. Only one job (proxy, preview or Apply) runs at a time, and edits are refused while Apply runs. Afterwards the editor reloads the new files.
- **Export approved** copies approved clips' MP4 and SRT files to `<output>/ready/` and removes files there that are no longer approved. Nothing is published.
- **Keyboard.** Space plays the cut, ←/→ steps a frame (Shift: a second), I/O set edges, [ / ] jump between cuts, ↑/↓ change clips, F overrides the current shot, A/N/X set the decision, 1–4 switch modes, L loops, W widens the timeline and ? shows the list.

Every edit is saved immediately to `review-decisions.json` in that output profile folder, keyed by clip id. Each save names the version it was edited from, so a stale second window cannot overwrite newer edits:

```json
{"version": 1, "source_id": "...", "profile": "...", "clips": {
  "01": {"status": "needs_changes", "options": ["recut", "fix_framing"], "comment": "...", "start_shift": -0.5, "end_shift": 1.0,
         "focus_overrides": [{"start_frame": 264, "end_frame": 458, "center_x": 0.2, "reason": "Reviewed in the clip editor."}],
         "file": "01_Title.mp4", "title": "...", "start": 94.95, "end": 128.75, "updated": "...", "addressed": false}}}
```

`status` is `pending`, `approved`, `needs_changes` or `rejected`; `options` is a subset of `recut` and `fix_framing`; shifts are seconds within ±30. `focus_overrides`, when present, is the clip's complete staged override list in the current clip's frames; absent means no framing change. Changing a decision clears `addressed`. `index.html` in the output folder is a read-only overview that also opens from disk. Regenerating it never touches the decisions file.

`--apply-review` changes `Plans/<id>.json` and then renders:

- **Approved** clips get `"approved": {"at": ...}` and are locked: later runs keep the finished file even when settings or framing code change. `--force` or withdrawing the approval in the editor re-renders them.
- **Rejected** clips move to the plan's `rejected_clips` (with the review comment) and their files to `rejected/` inside the output folder.
- **Staged framing** replaces the clip's `focus_overrides`. It is applied before a re-cut because it uses the current clip's frame numbers.
- **Moved edges** never cut a word. An edge that lands in silence stays where it was put; one that lands inside a word leaves it in the direction it was moved (the start 0.15 s before an onset, the end 0.25 s after a word end, never past the neighbouring word). The start moves by whole frames, and reviewed `focus_overrides` and `cover_time` are shifted so they stay on the same source moments.
- An entry is marked addressed when every requested option was handled and it has no comment. Comments, and *Fix framing* without staged overrides, stay open for the editorial agent; the command lists them.
- Before rendering, the current clip files are hard-linked into `history/`. Afterwards only clips whose file actually changed keep `history/<id>.mp4`, which the editor's Compare mode uses.

Each clip has a permanent `id` that prefixes its file names (`<id>_<title>.mp4`). Existing clips received their plan position as id; new clips get the next unused number, so adding or removing a clip never renames or re-renders the others. Retitling a clip replaces its files; files of clips that are no longer in the plan move to `rejected/`. Titles, reasons and cover times do not trigger re-encoding.

Source ids are computed from the file's size and its first and last megabyte, so renaming, copying or touching a Raw file keeps its plans and caches. Output folders are named after the plan's original source name. Folders created under the earlier name-and-time id are migrated on the next run.
