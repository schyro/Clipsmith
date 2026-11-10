"""Face tracking, Light-ASD speaking scores and per-shot focus selection."""
from functools import lru_cache
import math
import numpy as np

# Tracking follows the upstream Light-ASD demo: tolerate up to 10 missed
# detections and ignore tracks shorter than 10 frames. The numbers count 30 fps
# frames; tracking on a source's own frames scales them by its frame rate.
MAX_GAP = 10
MIN_TRACK = 10
STITCH_GAP = 20


@lru_cache(maxsize=1)
def load_model():
    import torch
    from shorts import ROOT
    from third_party.light_asd.Model import ASD_Model
    torch.set_num_threads(6)
    state = torch.load(ROOT/'assets/models/light_asd_talkset.model', map_location='cpu', weights_only=True)
    model = ASD_Model().eval()
    model.load_state_dict({k[len('model.'):]: v for k, v in state.items() if k.startswith('model.')})
    classifier = torch.nn.Linear(128, 2).eval()
    classifier.load_state_dict({k[len('lossAV.FC.'):]: v for k, v in state.items() if k.startswith('lossAV.FC.')})
    return model, classifier


def face_patch(frame, box):
    """Match the upstream padded crop and central 112x112 grayscale input."""
    import cv2
    x, y, w, h = box[:4]
    half = max(w, h)/2
    size = max(2, int(round(half*2.8)))
    cx = max(0, min(frame.shape[1]-1, float(x+w/2)))
    cy = max(0, min(frame.shape[0]-1, float(y+h/2+.4*half)))
    crop = cv2.getRectSubPix(frame, (size, size), (cx, cy))
    gray = cv2.cvtColor(cv2.resize(crop, (224, 224)), cv2.COLOR_BGR2GRAY)
    return gray[56:168, 56:168]


def score_track(patches, audio_features, times, rate=1.0):
    """One synchronized audio-visual speaking logit per frame of a face track.

    `patches` are the track's face patches on the source's own frames, `times` the
    frames' seconds on the clock of `audio_features` (100 per second), and `rate`
    the source's frames per 30 fps frame. Light-ASD works at 25 fps, so the patch
    nearest in time is used for each of its steps.
    """
    count = len(patches)
    if count < max(2, round(8*rate)):
        return np.full(count, -10., dtype=np.float32)
    import torch
    model, classifier = load_model()
    relative = np.asarray(times, dtype=float)-times[0]
    duration = relative[-1]+float(np.median(np.diff(relative)))
    n25 = max(2, int(round(duration*25)))
    steps = np.arange(n25)/25
    indices = np.searchsorted((relative[1:]+relative[:-1])/2, steps, side='right')
    visual = np.asarray(patches)[indices]
    audio_start = max(0, int(round(times[0]*100)))
    audio = np.asarray(audio_features[audio_start:audio_start+4*n25], dtype=np.float32)
    if len(audio) < 4*n25:
        audio = np.pad(audio, ((0, 4*n25-len(audio)), (0, 0)), mode='edge')
    estimates = []
    # Two context lengths reduce sensitivity to a single arbitrary chunk edge.
    with torch.inference_mode():
        for window in (50, 100):
            values = np.zeros(n25, np.float32)
            counts = np.zeros(n25, np.float32)
            for begin in range(0, n25, max(1, window//2)):
                end = min(n25, begin+window)
                if end-begin < 2:
                    continue
                a = torch.from_numpy(audio[4*begin:4*end].copy()).unsqueeze(0).float()
                v = torch.from_numpy(visual[begin:end].astype(np.float32)).unsqueeze(0)
                audio_embed = model.forward_audio_frontend(a)
                visual_embed = model.forward_visual_frontend(v)
                logits = classifier(model.forward_audio_visual_backend(audio_embed, visual_embed))[:, 1].cpu().numpy()
                values[begin:end] += logits
                counts[begin:end] += 1
            estimates.append(values/np.maximum(counts, 1))
    scores = np.mean(estimates, axis=0)
    # Smoothed over five 30 fps frames of this face track only; it never crosses a camera cut.
    from scipy.ndimage import uniform_filter1d
    grid = np.arange(max(1, int(round(duration*30))))/30
    smooth = uniform_filter1d(np.interp(grid, steps, scores), size=5, mode='nearest')
    return np.interp(relative, grid, smooth).astype(np.float32)


def iou(a, b):
    x = max(a[0], b[0]); y = max(a[1], b[1])
    right = min(a[0]+a[2], b[0]+b[2]); bottom = min(a[1]+a[3], b[1]+b[3])
    intersection = max(0, right-x)*max(0, bottom-y)
    return intersection/max(1, a[2]*a[3]+b[2]*b[3]-intersection)


def association_cost(a, b, gap=1):
    """Lower is better; None when two boxes cannot be the same face."""
    size = math.sqrt(max(1, a[2]*a[3]))
    distance = math.hypot(a[0]+a[2]/2-b[0]-b[2]/2, a[1]+a[3]/2-b[1]-b[3]/2)/size
    ratio = math.sqrt(max(1, b[2]*b[3])/max(1, a[2]*a[3]))
    if distance > .6+.04*gap or not .65 <= ratio <= 1.55:
        return None
    return distance+abs(math.log(ratio))-iou(a, b)


def build_tracks(detections, rate=1.0):
    """Link per-frame boxes into face tracks inside one shot.

    `rate` is the number of these frames per 30 fps frame (0.8 for a 23.976 fps
    source), so the gap and length limits mean the same time at any frame rate.

    Matching uses centre distance relative to face size as well as overlap, so
    a moving or briefly missed face keeps one identity instead of breaking
    into fragments that look like new people.
    """
    tracks = []
    for index, faces in enumerate(detections):
        active = [k for k, t in enumerate(tracks) if index-t['indices'][-1] <= MAX_GAP*rate]
        pairs = []
        for d, box in enumerate(faces):
            for k in active:
                cost = association_cost(tracks[k]['boxes'][-1], box, (index-tracks[k]['indices'][-1])/rate)
                if cost is not None:
                    pairs.append((cost, k, d))
        used_tracks, used_faces = set(), set()
        for cost, k, d in sorted(pairs):
            if k in used_tracks or d in used_faces:
                continue
            used_tracks.add(k); used_faces.add(d)
            tracks[k]['indices'].append(index)
            tracks[k]['boxes'].append(faces[d])
        for d, box in enumerate(faces):
            if d not in used_faces:
                tracks.append({'indices': [index], 'boxes': [box]})
    tracks = stitch_tracks(tracks, rate)
    return [t for t in tracks if len(t['indices']) >= MIN_TRACK*rate]


def stitch_tracks(tracks, rate=1.0):
    """Join a track that ends with a later one that starts at the same place."""
    tracks = sorted(tracks, key=lambda t: t['indices'][0])
    merged = True
    while merged:
        merged = False
        for a in tracks:
            candidates = []
            for b in tracks:
                gap = b['indices'][0]-a['indices'][-1]
                if b is a or not 0 < gap <= STITCH_GAP*rate:
                    continue
                cost = association_cost(a['boxes'][-1], b['boxes'][0], gap/rate)
                if cost is not None:
                    candidates.append((cost, id(b), b))
            if candidates:
                b = min(candidates)[2]
                a['indices'] += b['indices']
                a['boxes'] += b['boxes']
                tracks.remove(b)
                merged = True
                break
    return tracks


def fill_gaps(track):
    """Interpolate boxes across missed detections.

    Boxes are deliberately not median-filtered: on this material smoothing
    shifted the mouth region enough to drop a clear speaker's mean Light-ASD
    score from 1.8 to 0.4. The crop centre is smoothed separately.
    """
    begin, end = track['indices'][0], track['indices'][-1]+1
    positions = np.arange(begin, end)
    raw = np.asarray(track['boxes'], dtype=float)
    boxes = np.stack([np.interp(positions, track['indices'], raw[:, j]) for j in range(4)], axis=1)
    return positions, boxes


def face_boxes(detector, frame):
    """YuNet boxes large enough for Light-ASD's mouth crop."""
    _, detected = detector.detect(frame)
    return [] if detected is None else [f[:4].astype(float) for f in detected if min(f[2], f[3]) >= 24]


# Path selection weights. Costs are per frame; lower is better.
ASD_WEIGHT = 1.0       # per logit of speaking evidence during speech
ASD_THRESHOLD = 1.0    # weaker logits are common on crowds and are ignored
SIZE_WEIGHT = .5       # cost for a face much smaller than the largest one;
                       # kept low so a small, clearly speaking face beats a
                       # large silent one in the foreground
SILENT_SIZE_SCALE = .3 # in silence size only breaks ties; it must not outvote speech
NO_FACE_COST = 2.5     # cost for holding no face while a face is visible
SWITCH_COST = 6.0      # one voluntary change of focus inside a shot
SWITCH_EVIDENCE = .5   # mean evidence a new speaker needs over the minimum segment
FORCED_COST = .5       # change because the followed face left the shot


def frame_costs(tracks, length, speech):
    """Cost matrix (tracks + 'none') x frames, inf where a face is not visible,
    plus a matrix of speaking evidence for the tracks."""
    count = len(tracks)
    costs = np.full((count+1, length), np.inf)
    evidence = np.zeros((count+1, length))
    area = np.zeros((count, length))
    for k, track in enumerate(tracks):
        span = slice(track['start'], track['start']+len(track['area']))
        area[k, span] = track['area']
    largest = area.max(axis=0) if count else np.zeros(length)
    for k, track in enumerate(tracks):
        begin = track['start']
        for offset, (a, score) in enumerate(zip(track['area'], track['score'])):
            t = begin+offset
            relative = math.sqrt(a/largest[t]) if largest[t] > 0 else 1
            if speech[t]:
                evidence[k, t] = min(max(score-ASD_THRESHOLD, 0), 4)
            size = SIZE_WEIGHT*(1-relative)*(1 if speech[t] else SILENT_SIZE_SCALE)
            costs[k, t] = size-ASD_WEIGHT*evidence[k, t]
    costs[count] = np.where(largest > 0, NO_FACE_COST, 0)
    return costs, evidence


def switch_targets(visible, evidence, dwell, none):
    """Where a voluntary change to each face may start.

    The new person must be speaking at that frame, stay visible for the whole
    minimum segment (so the segment cannot end early through a forced exit or
    the cut) and speak clearly on average across it.
    """
    states, length = visible.shape
    allowed = np.zeros((states, length), bool)
    for j in range(none):
        run = 0
        remaining = np.zeros(length, int)
        for t in range(length-1, -1, -1):
            run = run+1 if visible[j, t] else 0
            remaining[t] = run
        window = np.convolve(evidence[j], np.ones(dwell), 'full')[dwell-1:dwell-1+length]/dwell
        allowed[j] = (remaining >= dwell) & (evidence[j] > 0) & (window >= SWITCH_EVIDENCE)
    return allowed


def choose_path(tracks, length, speech, min_dwell):
    """Pick the followed face for every frame of one shot.

    The whole shot is known, so the choice is made with dynamic programming
    instead of reacting frame by frame. A voluntary focus change starts a
    segment that must last at least `min_dwell` frames and must fit before the
    shot ends; a short interjection by another person therefore does not move
    the crop, while a long reply does. A change is forced (and free of the
    minimum) only when the followed face leaves the shot, and a voluntary
    change can only go to a person who is speaking at that moment, so silence
    never moves the crop. The search never
    looks past the shot, so a face from the next shot cannot be chosen early.
    Returns one track index per frame, or None while no face is followed.
    """
    if length == 0:
        return []
    none = len(tracks)
    states = none+1
    dwell = max(1, min(min_dwell, length))
    costs, evidence = frame_costs(tracks, length, speech)
    visible = np.isfinite(costs)
    switch_ok = switch_targets(visible, evidence, dwell, none)
    # total[s, c]: best cost with c frames (capped at dwell) spent in state s.
    total = np.full((states, dwell+1), np.inf)
    total[:, 1] = costs[:, 0]
    back = np.zeros((length, states, dwell+1, 2), dtype=np.int32)
    for t in range(1, length):
        new = np.full_like(total, np.inf)
        pointer = np.zeros((states, dwell+1, 2), dtype=np.int32)
        for c in range(2, dwell+1):
            new[:, c] = total[:, c-1]
            pointer[:, c] = np.stack([np.arange(states), np.full(states, c-1)], axis=1)
        keep = total[:, dwell] < new[:, dwell]
        new[keep, dwell] = total[keep, dwell]
        pointer[keep, dwell] = np.stack([np.flatnonzero(keep), np.full(keep.sum(), dwell)], axis=1)
        for j in range(states):
            if not visible[j, t]:
                continue
            best = (new[j, 1], pointer[j, 1, 0], pointer[j, 1, 1])
            for k in range(states):
                if k == j:
                    continue
                if not visible[k, t] or k == none:
                    c = int(np.argmin(total[k]))
                    option = (total[k, c]+FORCED_COST, k, c)
                    if option[0] < best[0]:
                        best = option
                if k != none and switch_ok[j, t]:
                    option = (total[k, dwell]+SWITCH_COST, k, dwell)
                    if option[0] < best[0]:
                        best = option
            new[j, 1] = best[0]
            pointer[j, 1] = best[1:]
        new += costs[:, t][:, None]
        back[t] = pointer
        total = new
    state, c = np.unravel_index(int(np.argmin(total)), total.shape)
    path = [0]*length
    for t in range(length-1, -1, -1):
        path[t] = int(state)
        if t:
            state, c = back[t, state, c]
    return [None if s == none else s for s in path]


def focus_decisions(tracks, length, speech, min_dwell, first_frame, fallback):
    """Turn a chosen path into per-frame crop centres and readable modes."""
    path = choose_path(tracks, length, speech, min_dwell)
    centers, decisions = [], []
    lookup = {}
    for k, track in enumerate(tracks):
        for offset, (x, score) in enumerate(zip(track['center_x'], track['score'])):
            lookup[(k, track['start']+offset)] = (x, score)
    visible_any = [any((k, t) in lookup for k in range(len(tracks))) for t in range(length)]
    first_face = next((lookup[(path[t], t)][0] for t in range(length) if path[t] is not None), None)
    previous = None
    for t, k in enumerate(path):
        if k is not None:
            x, score = lookup[(k, t)]
            # Light smoothing inside one segment; reset on every focus change.
            center = x if previous is None or path[t-1] != k else .35*x+.65*previous
            mode = 'active-speaker' if speech[t] and score > 0 else 'visible-face'
        else:
            center = previous if previous is not None else (first_face if first_face is not None else fallback)
            mode = 'hold' if visible_any[t] or previous is not None else 'shot-fallback'
        center = round(max(0, min(1, center)), 6)
        centers.append(center)
        decisions.append({'frame': first_frame+t, 'center_x': center, 'track': k, 'mode': mode})
        previous = center
    return decisions


def shot_review(tracks, decisions, speech):
    """Flags that mark a shot for a human look; they never change the crop."""
    flags = []
    speaking = sum(speech)
    chosen = [d['track'] for d in decisions]
    switches = sum(1 for a, b in zip(chosen, chosen[1:]) if a != b and a is not None and b is not None)
    if switches:
        flags.append(f'{switches} focus change(s) inside the shot')
    if len(tracks) > 3:
        flags.append(f'{len(tracks)} face tracks')
    if possible_missed_reply(tracks, chosen, speech):
        flags.append('another visible face speaks clearly while the followed face does not')
    if speaking >= 15 and not any(d['mode'] == 'active-speaker' for d in decisions):
        flags.append('speech without a visible speaking face' if tracks else 'speech with no face detected')
    return flags


def possible_missed_reply(tracks, chosen, speech, window=45):
    """True when, over a mostly spoken window, a fully visible face that is not
    followed scores clearly as speaking while the followed face does not."""
    length = len(chosen)
    scores = np.full((len(tracks), length), np.nan)
    for k, track in enumerate(tracks):
        scores[k, track['start']:track['start']+len(track['score'])] = track['score']
    for begin in range(0, max(1, length-window+1), 5):
        frames = [t for t in range(begin, min(length, begin+window)) if speech[t]]
        if len(frames) < window*.6:
            continue
        followed = [scores[chosen[t], t] for t in frames if chosen[t] is not None]
        current = float(np.nanmean(followed)) if followed else -10.
        for k in range(len(tracks)):
            values = scores[k, frames]
            if k in {chosen[t] for t in frames} or np.isnan(values).mean() > .1:
                continue
            if np.nanmean(values) >= 2 and current <= .5:
                return True
    return False
