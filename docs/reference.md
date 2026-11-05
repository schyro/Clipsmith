# Technical reference

On macOS/Linux use `./run.sh` wherever `./run.cmd` appears, and `.venv/bin/python` instead of `.venv/Scripts/python`.

## Current output

- 1080 × 1350 (4:5), 30 fps, H.264 video and AAC audio.
- English Dosis Bold captions centered at `(540,675)`, with word-aligned highlighting.
- One caption lane: event endings are capped at the next onset, including across sentence boundaries and overlapping ASR segments. The SRT sidecar follows the same rule.
- A subtle 70 ms entrance fade, 98–100% settling over 100 ms, and up to 40 ms exit fade. The entrance continues across word events instead of restarting on each word.
- Audio normalized toward -16 LUFS, followed by 20 ms fades at both cut edges. Fades smooth waveform discontinuities; they do not repair words cut in half, so editorial boundaries still matter.
- No added headings or decorative lines. Existing subtitles burned into the recording remain part of the image.

Settings live in `video_settings.json`. Revised exports use `Output/<source>_<id>/4x5-dosis-speaker-v4/`; previous export profiles and Raw originals remain intact.

## Manual commands

```bash
# Reuse saved plans; create heuristic proposals for new sources.
./run.cmd

# Select one source.
./run.cmd --input "Raw/recording.mp4"

# Analyze without rendering.
./run.cmd --input "Raw/recording.mp4" --analyze-only

# Optional limit for new heuristic proposals.
./run.cmd --count 3

# Use a smaller transcription model.
./run.cmd --model small

# Use a specific plan or force re-encoding of the current output profile.
./run.cmd --input "Raw/recording.mp4" --plan "Plans/source_id.json"
./run.cmd --force
```

`--require-reviewed` checks a reviewed plan before it renders anything and lists every problem at once: no clip edge may fall inside a spoken word, and an agent-reviewed plan needs a `coverage` note for every dialogue block of `review.md`, with `why_not_more` for empty blocks and, when there are fewer clips than one per two minutes, for single-clip blocks (AGENTS.md, step 3). Add `--analyze-only` to run only this check. After rendering, it runs `verify_outputs.py` on the output folder and prints the QA sheets and the focus sheets with flagged shots. The editor's Apply skips these agent checks.

A plain run uses keyword/question/speech-density proposals for new sources. This is not equivalent to the model reviewing the full content. Existing plans take precedence on subsequent runs.

## Transcription and corrections

Whisper medium runs locally and caches word timestamps per source. With an NVIDIA GPU and the CUDA libraries from `requirements-gpu.txt` (installed by the launcher when `nvidia-smi` is available), it runs on the GPU in float16, several times faster than on the CPU (in testing, a 45-minute recording took about 2 minutes instead of 12). A short test transcription in a child process decides this; without a working GPU, Whisper uses the CPU in int8. Set `CLIPSMITH_WHISPER_DEVICE=cpu` to skip the GPU libraries and always use the CPU. A cached transcript is never redone, so switching devices does not change the word indices of existing caption corrections. Non-English recordings use Whisper's English translation, which requires additional language/timing review. The video is not uploaded to a cloud editing service.

`Review/<id>/words.json` contains global zero-based word indices. Preserve timing while correcting recognition errors in `work/<id>/caption-edits-medium.json`:

```json
{"model":"medium","edits":[{"index":123,"from":"Jon.","to":"John."}]}
```

The expected original word must match. Each MP4 has an English SRT sidecar; the animated, highlighted appearance is burned into the MP4.

## Installation and verification

Use Python 3.13 (tested) and make `ffmpeg`/`ffprobe` available on PATH. `run.cmd` creates the local `.venv` and installs pinned requirements when needed. The first transcription downloads the model. Dosis, YuNet and the approximately 4 MB Light-ASD weights are bundled under `assets`. CPU PyTorch is installed from the official PyTorch wheel index; analysis remains local and is cached per source clip. Face detection runs on several threads, and the Light-ASD scoring of one camera shot overlaps the decoding and detection of the next; the results are identical to a sequential run.

Completed clips have content signatures. Changes to captions, settings, plans, framing or fonts trigger new exports. Repeating the command skips matching completed clips; interrupted partial exports are regenerated. Run only one pipeline instance at a time. NVIDIA encoding is probed once per run and used when the driver supports FFmpeg's NVENC API; otherwise the CPU encoder is used. Encoding errors are reported, not retried on another encoder.

Every MP4 is checked for dimensions, duration and audio, then fully decoded before completion is recorded. Regression checks cover caption overlap/quantization, animation continuity, real audio fade envelopes, speaker selection, exact cut-frame timing, a fully filled frame, plan boundaries and resume behavior.

```bash
.venv/Scripts/python -m unittest discover -s tests -v
```

Project documentation, code comments, CLI messages, gallery UI and current plan labels use English. Historical exports and recorded dialogue are retained as source/output data.
