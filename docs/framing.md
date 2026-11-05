# Speaker framing and the plan format


The video always fills 1080x1350. No zoom-out, letterboxing, padding or blurred background is used. Framing is a horizontal crop of the source, with the output aspect ratio fixed throughout.

[Light-ASD](https://github.com/Junhua-Liao/Light-ASD) evaluates synchronized speech features and face-motion sequences. YuNet detects visible faces; tracks are scored with the local pretrained Light-ASD TalkSet weights. This is audiovisual speaker detection, not a largest-face rule. The selected face must belong to the current camera shot and be visible in that shot. Offscreen speech cannot produce a new face: retain a person actually shown by the original camera edit.

Both analysis and rendering use the same 30 fps frame grid. Camera cuts are checked on every frame, and face tracking/smoothing reset at a cut. Commands for frame N are scheduled between frames N-1 and N, preventing decimal timestamp rounding from shifting the crop one frame early. The old 8 fps estimates and cross-shot center changes are not used.

Analysis runs in two stages. The slow stage (cached in `work/<id>/speaker-tracks-*.json`) finds exact cut frames, links YuNet detections into face tracks and scores every track with Light-ASD. Tracking follows the upstream Light-ASD demo: up to 10 missed detections are bridged, tracks shorter than 10 frames are ignored, and a track that breaks and restarts at the same place is stitched back together.

The fast stage chooses the followed face for each shot with dynamic programming over the whole shot, never past its cut:

- Speaking evidence counts only inside Whisper word timings, and only Light-ASD scores above 1 count; the weak positive scores common on crowds are ignored.
- Silence never moves the crop. Inside a shot the crop changes only when another visible person starts a reply that lasts at least `speaker_switch_min_seconds` (default 1.5 s) and that reply fits before the cut. Short interjections keep the current face.
- When the followed face leaves the shot, the crop moves to a remaining visible face, or holds its position if none is visible.
- Face size breaks ties, so a large face is preferred when nobody visibly speaks.

Occluded, very small or low-confidence faces still require editorial review. Each MP4 has a `.focus.json` sidecar with exact cut frames, track score summaries, focus decisions and per-shot `review` flags (focus changes, many faces, speech without a visible speaking face). Each render also writes `Review/<id>/focus/<clip>.jpg`: the middle of every shot and both sides of every focus change, with the crop drawn on the source frame and flagged shots outlined in red. The AI agent should inspect flagged shots and save explicit focus points; it should not invent an unseen person.

```json
{
  "source": "recording.mp4",
  "source_id": "id_from_preparation",
  "selection": "agent-reviewed",
  "clips": [
    {
      "start": 100.2,
      "end": 140.5,
      "title": "Gallery label only",
      "reason": "A complete setup and payoff",
      "focus_overrides": [
        {"start_frame": 30, "end_frame": 90, "center_x": 0.65}
      ]
    }
  ]
}
```

Agent-reviewed plans also carry a `coverage` list with one note per `review.md` block; see AGENTS.md, step 3, and `examples/plan.example.json`.

Frame intervals are clip-relative and half-open: start_frame is included and end_frame is excluded. `center_x` is the source horizontal position from 0 to 1. Optional `center_x_end` creates a gentle reviewed pan inside the same shot. Overrides cannot overlap or cross a camera cut. They do not change crop size or aspect ratio. Legacy zoom-based `framing_overrides` are rejected. Set a clip's `auto_reframe` to `false` to use its fixed `crop_x` instead.
