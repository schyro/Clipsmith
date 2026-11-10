# Source analysis and the shot index

## What is analysed, and when

Speaker analysis works on the source's own frames and is cached per source in `work/<id>/` (`source_analysis.py`):

| File | Made | Holds |
| --- | --- | --- |
| `source-scan.npz` | once per source, by `run --prepare-review`, `run --shots` or the first render | every camera cut as a source frame number; the faces found on one frame every 0.25 s of each shot (box, landmarks, sharpness, identity embedding) |
| `audio-mfcc.npy` | once per source, with the first shot analysis | Light-ASD's speech features, 100 per second |
| `shots/<first frame>.json` | on demand, per camera shot | the shot's face tracks and Light-ASD speaking scores, one value per source frame |

A clip's analysis is a slice of these. `frame_grid.FrameGrid` says which source frame each frame of the clip's 30 fps grid shows; the slice gives that frame's track values to the grid frame. So:

- moving a clip edge, or adding a montage shot, analyses only the camera shots it newly touches, and nothing when they are already known;
- a face track and its speaking scores no longer depend on where a clip happens to start inside a shot;
- camera cuts are the same frames for every clip, the editor and the shot index.

A shot is always analysed whole, also when a clip uses only part of it. Frames are read as a stream. Only the 112x112 face patches Light-ASD needs are kept; shots of up to 128 frames keep their frames between detection and patch extraction (about 88 MB), longer shots are decoded a second time instead.

`speaker_focus.analyze` and `focus_plan` keep their signatures. `analyze` still writes `speaker-tracks-<key>.json`, now a copy of the slice. `source_analysis.analysis_ready(source, clip, cache)` tells whether a clip needs any decoding.

## The shot index

`run --shots` builds `Review/<id>/shot-index.json` from the scan (`run --prepare-review` does the same and lists the file in the manifest). Per camera shot:

```json
{
  "shot": 400, "start": 926.842, "end": 929.22, "seconds": 2.377,
  "start_frame": 22220, "end_frame": 22277, "sampled_frames": 9,
  "faces": [
    {"character": 1, "framing": "close-up", "size": 0.326, "center_x": 0.411, "center_y": 0.282,
     "sharpness": 37.5, "sharpness_rank": 0.38, "visible": 1.0,
     "best_time": 928.719, "best_box": [0.3584, 0.1186, 0.118, 0.3269]}
  ]
}
```

- `start`/`end` are source seconds, as used in plans; `end` is the first frame of the next shot.
- One `faces` entry per person in the shot, largest first. `size` is the face height as a share of the frame height: `close-up` from 0.30, `medium` from 0.14, `wide` below. `center_x`/`center_y` are 0..1 in the source frame. `visible` is the share of the shot's sampled frames that show the face.
- `sharpness` is the Laplacian variance of the face patch; `sharpness_rank` (0..1) ranks it among the source's faces of the same framing, so 0.5 or more is the sharper half.
- `best_time` is the sampled frame where the face is largest and sharpest; `best_box` is the face there (x, y, width, height, 0..1).
- `character` numbers the people of the source, 1 for the most screen time. It comes from clustering SFace identity embeddings of every face at least 40 px wide (at the 640 px analysis width) across the whole source; small or rarely seen faces stay `null`. The top-level `characters` list gives each id's shot count, seconds on screen and close-ups. Ids are stable for a source but mean nothing across sources; look at the character sheet to see who is who.

Faces are sampled, not tracked, so a face that appears for under a quarter of a second can be missing, and a shot shorter than four frames has no sample.

### Identity model

Character ids need OpenCV's SFace model, `assets/models/face_recognition_sface_2021dec.onnx` ([opencv_zoo](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface), Apache 2.0). Without the file the index is complete except that every `character` is `null`. After adding the file, `run --shots` scans the source once more to add the embeddings.

### Queries

```bash
run --shots                                   # build if needed; summary and Review/<id>/shots/characters.jpg
run --shots --character 3 --framing close-up --sharp --min-seconds 0.6
run --shots --character 3 --solo --from 600 --to 1500 --sheet
```

| Option | Keeps |
| --- | --- |
| `--character N` | shots that show character N |
| `--framing close-up\|medium\|wide` | shots where the matching face has that size |
| `--sharp` | faces in the sharper half of their framing |
| `--solo` | shots with exactly one face |
| `--min-seconds S` | shots at least S long |
| `--from S`, `--to S` | shots inside a part of the source |
| `--limit N` | the first N matches (default 60) |
| `--sheet` | also writes `Review/<id>/shots/query-<conditions>.jpg`, at most 48 tiles |
| `--input FILE` | one source instead of everything in `Raw/` |
| `--rebuild` | scans the source again |

Each listed line gives the shot number, its time range and length, the number of faces and the matching face. A contact sheet shows each shot at the face's `best_time` with the face outlined. `characters.jpg` shows the four clearest shots of each of the twelve most seen characters, one row per character.

From Python: `shot_index.build_index(source, cache, source_id)` returns the index, `shot_index.find(index, character=3, framing='close-up', sharp=True, min_seconds=.6)` the matching `(shot, face)` pairs.

### Choosing montage shots

A montage shot must lie inside one camera shot. Take `start` at least 0.04 s after the shot's `start` and make sure the shot's `end` is at least 0.04 s after the montage shot ends; `run --require-reviewed --analyze-only` still checks this and gives the times to move to. A shot's `best_time` is a good moment to have inside the montage shot.
