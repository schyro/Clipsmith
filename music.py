"""Music tracks for edits: tempo, beat grid and drop, kept in a sidecar per track.

`run --scan-music` analyses every audio file in `Music/` once and writes
`Music/<name>.json` next to it. The sidecar is the user's file from then on:
they add a `mood` the agent uses to pick a track, and correct `bpm`,
`first_beat` or `drop` when detection got them wrong. A scan never overwrites
an existing sidecar.

Detection uses numpy/scipy only:
- onset envelope: positive spectral flux of the log spectrum (11.6 ms hops);
- tempo and beat grid: the constant-tempo grid (60-200 BPM, any phase) that
  collects the most onset strength over the whole track. Edit music is produced
  on a click, so one grid covers the track;
- drop: the beat with the largest energy rise from about 4 s before it to 4 s after
  it, broadband and bass combined, at least 8 s into the track.

The beat grid is `first_beat` + k x beat. An edit's drop is always a grid beat: the
beat nearest the sidecar's `drop` (or the edit's own `drop`), so correcting
`first_beat` moves every cut. A sidecar `drop` more than `DROP_WARN` off the grid is
reported, since either the drop or the grid is then wrong.

For the stretch after each drop candidate the sidecar also stores per-beat onset
strength (beat 0 is the drop; bar positions assume the drop is a downbeat, since no
reliable downbeat estimate was found for the user's tracks) and a few descriptors
that help an agent that cannot hear: level and onset density before and after the
drop. Vocals are not detected: no numpy-only measure separated sung or rapped parts
from melodic synths reliably enough to report.
"""
from __future__ import annotations
import json
import math
from pathlib import Path
import subprocess

import numpy as np

ANALYSIS_VERSION = 2
EXTENSIONS = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg', '.opus', '.webm'}
SR = 22050
HOP = 256
N_FFT = 1024
# A transient shows up in the log flux as soon as it enters the analysis window, so the
# envelope peak sits this long before the onset (measured on synthetic hits, +-3 ms).
ONSET_LAG = .036
MIN_BPM, MAX_BPM = 60.0, 200.0
EDITABLE = ('mood', 'bpm', 'first_beat', 'drop')
# Sidecar times are rounded to the millisecond: an offset below this is the same beat.
GRID_TOLERANCE = .002
DROP_WARN = .010            # a sidecar drop further than this from its grid is reported
PROFILE_BEATS = 32          # beats of strength stored after each drop candidate
DESCRIPTOR_SECONDS = 8.0    # stretch before and after a drop that the descriptors cover
ONSET_PEAK = .15            # envelope peaks at least this strong count as onsets


def music_dir(root: Path | None = None):
    if root is None:
        from shorts import ROOT
        root = ROOT
    return Path(root)/'Music'


def decode(path, sr=SR):
    """Mono float32 samples of an audio file."""
    result = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-ac', '1', '-ar', str(sr),
                             '-f', 'f32le', '-'], capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', 'replace')[-2000:])
    return np.frombuffer(result.stdout, np.float32)


def spectrogram(signal, hop=HOP, n_fft=N_FFT):
    if len(signal) < n_fft:
        signal = np.pad(signal, (0, n_fft-len(signal)))
    count = 1+(len(signal)-n_fft)//hop
    frames = np.lib.stride_tricks.as_strided(signal, (count, n_fft), (signal.strides[0]*hop, signal.strides[0]))
    return np.abs(np.fft.rfft(frames*np.hanning(n_fft).astype(np.float32), axis=1))


def onset_envelope(spec):
    """Positive spectral flux with a local mean removed; one value per hop.
    Value i belongs to the hop that starts at sample i*HOP; `ONSET_LAG`
    converts it to the time the onset is heard."""
    logs = np.log1p(1000*spec)
    flux = np.r_[0, np.maximum(0, np.diff(logs, axis=0)).sum(1)]
    window = 16
    kernel = np.ones(window)/window
    local = np.convolve(flux, kernel, mode='same')
    env = np.maximum(0, flux-local)
    peak = env.max()
    return env/peak if peak > 0 else env


def _peaks(env):
    """Each hop takes the strongest of its neighbours, so a beat a few ms off still counts.
    The mean of that maximum (not of the raw envelope) is removed, so a grid point on
    ordinary noise scores zero on average and a faster grid gains nothing from noise."""
    padded = np.pad(env, 1)
    peaks = np.maximum.reduce([padded[:-2], padded[1:-1], padded[2:]])
    return peaks-peaks.mean()


def grid_scores(peaks, rate, period, phases):
    """Summed onset strength (minus its mean) on the grid of each phase. A grid twice as
    slow as the music hits half the beats; one twice as fast hits silence half the time."""
    k = np.arange(0, (len(peaks)/rate-phases.max())/period)
    idx = np.round((phases[:, None]+k[None, :]*period)*rate).astype(int)
    idx = np.clip(idx, 0, len(peaks)-1)
    return peaks[idx].sum(1)


def fit_grid(env, rate):
    """(period, phase) of the constant-tempo grid that fits the envelope best."""
    peaks = _peaks(env)
    def best_for(bpm):
        period = 60/bpm
        phases = np.arange(0, period, 1/rate)
        scores = grid_scores(peaks, rate, period, phases)
        i = int(np.argmax(scores))
        return scores[i], period, float(phases[i])
    coarse = max((best_for(b) for b in np.arange(MIN_BPM, MAX_BPM+.01, .25)), key=lambda r: r[0])
    bpm = 60/coarse[1]
    fine = max((best_for(b) for b in np.arange(bpm*.994, bpm*1.006, .005)), key=lambda r: r[0])
    return fine[1], fine[2]


def beat_energy(spec, beats, rate, sr=SR):
    """Broadband and bass (<150 Hz) dB per beat interval."""
    freqs = np.fft.rfftfreq(N_FFT, 1/sr)
    power = spec**2
    total = power.sum(1)
    bass = power[:, freqs < 150].sum(1)
    broad, low = [], []
    for a, b in zip(beats[:-1], beats[1:]):
        # A beat close to the start can begin before the first frame; clip it to the frames.
        last = len(total)-1
        i = min(last, max(0, int(a*rate)))
        j = min(len(total), max(i+1, int(b*rate)))
        broad.append(10*math.log10(total[i:j].mean()+1e-12))
        low.append(10*math.log10(bass[i:j].mean()+1e-12))
    return np.array(broad), np.array(low)


def drop_candidates(beats, broad, low, duration, window=4.0, keep=3, min_before=8.0, min_after=10.0):
    """Beat times ranked by energy rise; the first is the detected drop.

    The rise compares about `window` seconds after a beat with the same before it, broadband
    and bass combined, so fast and slow tracks are judged over the same time. A drop needs
    `min_before` seconds of track before it: the bass entering after a short intro is not
    the drop an edit wants (the part before the drop plays under the dialogue), the return
    after a breakdown is."""
    energy = .5*(broad-broad.max())+.5*(low-low.max())
    period = float(beats[1]-beats[0]) if len(beats) > 1 else .5
    span = int(min(16, max(4, round(window/period))))
    scored = []
    for i in range(span, len(energy)-span):
        t = beats[i]
        if t < min_before or t > duration-min_after:
            continue
        rise = energy[i:i+span].mean()-energy[i-span:i].mean()
        scored.append((rise, i))
    scored.sort(reverse=True)
    chosen = []
    for rise, i in scored:
        if all(abs(i-j) >= span for _, j in chosen):
            chosen.append((rise, i))
        if len(chosen) == keep:
            break
    return [{'time': round(float(beats[i]), 3), 'rise_db': round(float(r), 1)} for r, i in chosen]


def grid_beat(track, t):
    """(the beat of the sidecar grid `first_beat` + k x beat nearest `t`, `t` minus that beat).
    Without a `first_beat` the grid runs through `t` itself."""
    period = 60/float(track['bpm'])
    first = float(track.get('first_beat', t))
    beat = first+round((float(t)-first)/period)*period
    return beat, float(t)-beat


def on_grid(track, t):
    """`t` moved onto the beat grid, and how far it was off. Rounding noise keeps `t` as written."""
    beat, offset = grid_beat(track, t)
    return (float(t) if abs(offset) < GRID_TOLERANCE else round(beat, 6)), offset


def drop_grid_warning(name, track):
    """A sentence when the sidecar's drop is off its own beat grid, else None."""
    if not all(isinstance(track.get(k), (int, float)) for k in ('bpm', 'first_beat', 'drop')):
        return None
    beat, offset = grid_beat(track, track['drop'])
    if abs(offset) <= DROP_WARN:
        return None
    period = 60/track['bpm']
    return (f'{name}: drop {track["drop"]:.3f} s is {offset*1000:+.0f} ms off the beat grid (first_beat '
            f'{track["first_beat"]} s + k x {period:.4f} s); edits use the nearest beat, {beat:.3f} s. '
            f'If the drop is right, the grid is wrong: set first_beat to {track["drop"] % period:.4f}.')


def _strength(env, rate, t):
    """Onset strength heard at time t: the envelope's maximum within +-2 hops (+-23 ms)."""
    i = int(round((t-ONSET_LAG)*rate))
    lo, hi = max(0, i-2), min(len(env), i+3)
    return float(env[lo:hi].max()) if hi > lo else 0.0


def _onsets_per_second(env, rate, a, b):
    i, j = max(1, int((a-ONSET_LAG)*rate)), min(len(env)-1, int((b-ONSET_LAG)*rate))
    if j-i < 2 or b <= a:
        return 0.0
    part = env[i-1:j+1]
    middle = part[1:-1]
    peaks = (middle >= ONSET_PEAK) & (middle >= part[:-2]) & (middle > part[2:])
    return float(peaks.sum())/(b-a)


def _level_db(power, rate, a, b):
    i, j = max(0, int(a*rate)), min(len(power), int(b*rate))
    return 10*math.log10(power[i:j].mean()+1e-12) if j > i else None


def accent_beat(strength):
    """Bar position (1-4, counted from the drop) where the hits clearly concentrate, else None.
    'Clearly' means that position averages at least twice the strength of any other."""
    if len(strength) < 8:
        return None
    means = [float(np.mean(strength[k::4])) for k in range(4)]
    order = sorted(range(4), key=lambda k: means[k], reverse=True)
    best, second = means[order[0]], means[order[1]]
    return order[0]+1 if best > .05 and best >= 2*second else None


def drop_profile(env, power, rate, track, drop, duration):
    """Per-beat onset strength after `drop` (0..1 of the track's strongest onset) and the
    descriptors around it: level in dB against the whole track, onsets per second."""
    period = 60/float(track['bpm'])
    beats = drop+period*np.arange(PROFILE_BEATS)
    beats = beats[beats < duration-.05]
    strength = [round(_strength(env, rate, t), 2) for t in beats]
    whole = 10*math.log10(power.mean()+1e-12)
    a, b = max(0.0, drop-DESCRIPTOR_SECONDS), min(duration, drop+DESCRIPTOR_SECONDS)
    before, after = _level_db(power, rate, a, drop), _level_db(power, rate, drop, b)
    return {'before_db': None if before is None else round(before-whole, 1),
            'after_db': None if after is None else round(after-whole, 1),
            'onsets_before': round(_onsets_per_second(env, rate, a, drop), 2),
            'onsets_after': round(_onsets_per_second(env, rate, drop, b), 2),
            'accent_beat': accent_beat(strength), 'beats': strength}


def rhythm_fields(path, track, signal=None):
    """Beat profiles for every drop candidate and the sidecar's own drop, on the sidecar's grid.
    Returns the new `drop_candidates` list and the `analysis` record; no user field changes."""
    signal = decode(path) if signal is None else signal
    duration = len(signal)/SR
    spec = spectrogram(signal)
    env = onset_envelope(spec)
    power = (spec**2).sum(1)
    rate = SR/HOP
    period = 60/float(track['bpm'])
    candidates = [{k: v for k, v in c.items() if k in ('time', 'rise_db', 'source')}
                  for c in track.get('drop_candidates') or [] if isinstance(c, dict) and 'time' in c]
    drop, _ = on_grid(track, track['drop'])
    if not any(abs(c['time']-drop) < period/4 for c in candidates):
        candidates.append({'time': round(drop, 3), 'rise_db': None, 'source': 'sidecar drop'})
    for c in candidates:
        at, _ = on_grid(track, c['time'])
        c.update(drop_profile(env, power, rate, track, at, duration))
    analysis = {'version': ANALYSIS_VERSION, **{k: track[k] for k in ('bpm', 'first_beat', 'drop')}}
    return candidates, analysis


def needs_rhythm(data):
    """True when the stored beat profiles are missing or were measured on another grid or drop."""
    if not all(isinstance(data.get(k), (int, float)) for k in ('bpm', 'first_beat', 'drop')):
        return False
    done = data.get('analysis') or {}
    return (done.get('version') != ANALYSIS_VERSION
            or any(done.get(k) != data[k] for k in ('bpm', 'first_beat', 'drop')))


def analyse(path):
    signal = decode(path)
    duration = len(signal)/SR
    spec = spectrogram(signal)
    env = onset_envelope(spec)
    rate = SR/HOP
    period, phase = fit_grid(env, rate)
    phase = (phase+ONSET_LAG) % period
    beats = np.arange(phase, duration, period)
    broad, low = beat_energy(spec, beats-N_FFT/2/SR, rate)
    drops = drop_candidates(beats, broad, low, duration)
    found = {'duration': round(duration, 3), 'bpm': round(60/period, 3), 'first_beat': round(float(phase), 4),
             'drop': drops[0]['time'] if drops else None, 'drop_candidates': drops}
    if found['drop'] is not None:
        found['drop_candidates'], found['analysis'] = rhythm_fields(path, found, signal)
    return found


def file_hash(path):
    from shorts import _head_tail_hash
    return _head_tail_hash(Path(path), f'{Path(path).stat().st_size}')


def tracks(folder: Path):
    files = sorted(p for p in Path(folder).glob('*') if p.suffix.lower() in EXTENSIONS and p.is_file())
    stems = {}
    for p in files:
        if p.stem.lower() in stems:
            raise ValueError(f'Two tracks share the name "{p.stem}" ({stems[p.stem.lower()].name}, {p.name}); rename one.')
        stems[p.stem.lower()] = p
    return files


def sidecar_path(track: Path):
    return Path(track).with_suffix('.json')


def sidecar(track: Path, detected):
    return {
        'file': Path(track).name,
        'mood': '',
        'bpm': detected['bpm'],
        'first_beat': detected['first_beat'],
        'drop': detected['drop'],
        'duration': detected['duration'],
        'drop_candidates': detected['drop_candidates'],
        **({'analysis': detected['analysis']} if 'analysis' in detected else {}),
        'detected': {**{k: detected[k] for k in ('bpm', 'first_beat', 'drop')},
                     'version': ANALYSIS_VERSION, 'file_hash': file_hash(track)},
        'help': ('Write a few words about the feel in "mood" (for example "funny, smug" or "sad, slow build"); '
                 'the agent picks tracks by it. Correct "bpm", "first_beat" (seconds) or "drop" (seconds) when '
                 'detection is wrong; beats lie on first_beat + k x 60/bpm and edits drop on the beat nearest '
                 '"drop". A scan never changes these fields; it only refreshes drop_candidates and analysis.'),
    }


def scan(folder: Path | None = None, out=print):
    """Write a sidecar for every track that has none; return [(track, sidecar)]."""
    from shorts import write_json
    folder = Path(folder) if folder else music_dir()
    folder.mkdir(exist_ok=True)
    result = []
    for track in tracks(folder):
        path = sidecar_path(track)
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if data.get('detected', {}).get('file_hash') not in (None, file_hash(track)):
                out(f'Warning: {track.name} changed since its sidecar was written; check bpm, first_beat and drop '
                    f'in {path.name} or delete it to detect again.')
            elif needs_rhythm(data):
                # Only the derived fields change; mood, bpm, first_beat and drop stay as the user left them.
                data['drop_candidates'], data['analysis'] = rhythm_fields(track, data)
                write_json(path, data)
                out(f'Beat profiles updated: {track.name}')
        else:
            data = sidecar(track, analyse(track))
            write_json(path, data)
            out(f'Detected: {track.name}')
        result.append((track, data))
    return result


def load_track(name, folder: Path | None = None):
    """Track path and its sidecar values, checked."""
    folder = Path(folder) if folder else music_dir()
    track = folder/name
    if not track.is_file():
        raise ValueError(f'Music track not found: {track}')
    path = sidecar_path(track)
    if not path.exists():
        raise ValueError(f'{track.name} has no sidecar yet; run --scan-music.')
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    for key in ('bpm', 'first_beat', 'drop', 'duration'):
        if not isinstance(data.get(key), (int, float)):
            raise ValueError(f'{path.name}: "{key}" must be a number.')
    if not MIN_BPM/2 <= data['bpm'] <= MAX_BPM*2:
        raise ValueError(f'{path.name}: bpm {data["bpm"]} is outside {MIN_BPM/2:.0f}-{MAX_BPM*2:.0f}.')
    if not 0 <= data['drop'] < data['duration']:
        raise ValueError(f'{path.name}: drop {data["drop"]} s is outside the track.')
    return track, data


def strongest_beats(strength, count=4, limit=None):
    """Beat numbers after the drop with the most onset strength, in time order."""
    values = list(strength[:limit] if limit else strength)
    ranked = sorted(range(len(values)), key=lambda k: values[k], reverse=True)[:count]
    return sorted(k for k in ranked if values[k] > 0)


def beat_label(k):
    """'+6 (bar 2.3)': beat k after the drop, bars counted from the drop as a downbeat."""
    return f'+{k} (bar {k//4+1}.{k % 4+1})'


def describe_candidate(c, period):
    parts = [f'{c["time"]:.3f} s']
    if c.get('rise_db') is not None:
        parts.append(f'rise {c["rise_db"]:+.1f} dB')
    if c.get('source'):
        parts.append(c['source'])
    if c.get('before_db') is not None and c.get('after_db') is not None:
        parts.append(f'level {c["before_db"]:+.0f} -> {c["after_db"]:+.0f} dB, onsets/s '
                     f'{c["onsets_before"]:.1f} -> {c["onsets_after"]:.1f}')
    if c.get('beats'):
        reach = int(9/period)+1  # the beats a montage of at most 9 s can reach
        parts.append('strongest beats '+', '.join(beat_label(k) for k in strongest_beats(c['beats'], 4, reach)))
    if c.get('accent_beat') not in (None, 1):
        parts.append(f'hits concentrate on beat {c["accent_beat"]} of the bar: the drop may not be a downbeat')
    return '; '.join(parts)


def describe(track, data):
    mood = data.get('mood') or '(no mood yet: ask the user to fill it in)'
    period = 60/data['bpm']
    lines = [f'{track.name}: {data["bpm"]:.1f} BPM, beat {period:.3f} s ({period*30:.1f} frames), drop at '
             f'{data["drop"]:.2f} s of {data["duration"]:.1f} s, mood: {mood}']
    warning = drop_grid_warning(track.name, data)
    if warning:
        lines.append('    Warning: '+warning)
    for c in data.get('drop_candidates') or []:
        lines.append('    drop '+describe_candidate(c, period))
    return '\n'.join(lines)


def loudness(track: Path, start: float, duration: float):
    """Integrated loudness (LUFS) of a stretch of the track."""
    result = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-ss', f'{start:.3f}', '-t', f'{duration:.3f}',
                             '-i', str(track), '-vn', '-af', 'ebur128=framelog=quiet', '-f', 'null', '-'],
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    marker = result.stderr.rfind('Integrated loudness:')
    for line in result.stderr[marker:].splitlines():
        line = line.strip()
        if line.startswith('I:'):
            value = float(line.split()[1])
            if math.isfinite(value):
                return value
    raise RuntimeError(f'Could not measure the loudness of {track.name}.')


def main():
    found = scan()
    if not found:
        print(f'No tracks in {music_dir()}. Copy audio files there and run again.')
        return
    for track, data in found:
        print(describe(track, data))
    missing = [t.name for t, d in found if not d.get('mood')]
    if missing:
        print('Tracks without a mood: '+', '.join(missing)+'. Fill in "mood" in their sidecars.')


if __name__ == '__main__':
    main()
