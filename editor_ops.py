"""Operations behind the clip editor: preview proxy, quick exact preview renders,
caption word corrections, live-preview data and the ready-to-post export.

Long operations also run as a CLI so the editor server can stream their output:
    python editor_ops.py proxy <source_id>
    python editor_ops.py prepare <source_id>          (proxy and missed-speech pass, when missing)
    python editor_ops.py preview <output folder> <clip id>
    python editor_ops.py retranscribe <source_id> <start> <end>
"""
from __future__ import annotations
import copy
from fractions import Fraction
import json
from pathlib import Path
import shutil
import sys

FPS = 30
PROXY_HEIGHT = 540
PREVIEW_SIZE = (432, 540)  # 0.4 of 1080x1350
CONTEXT = 30.0  # seconds of source shown around a clip on the editor timeline


def review_dir(root: Path, source_id):
    return Path(root)/'Review'/source_id


PROXY_VERSION = 2  # 2: the source's own frames and timestamps (1 was resampled to 30 fps)


def proxy_path(root: Path, source_id):
    return review_dir(root, source_id)/'proxy.mp4'


def proxy_ready(root: Path, source_id):
    meta = proxy_path(root, source_id).with_suffix('.json')
    try:
        return proxy_path(root, source_id).exists() and json.loads(meta.read_text(encoding='utf-8'))['version'] == PROXY_VERSION
    except (OSError, ValueError, KeyError):
        return False


def preview_dir(root: Path, source_id):
    return review_dir(root, source_id)/'preview'


# ---------- captions ----------

def raw_english(source, cache, model):
    """The unedited English transcript that caption edit indices and `from` refer to."""
    from shorts import transcribe
    original = transcribe(source, cache, model)
    return original if original['language'] == 'en' else transcribe(source, cache, model, 'translate')


def task_of(source, cache, model):
    from shorts import transcribe
    return 'transcribe' if transcribe(source, cache, model)['language'] == 'en' else 'translate'


def original_words(source, cache, model):
    """{index: unedited word} for transcript words (int index) and missed-speech words ("g" ids)."""
    from missed_speech import load_gaps
    words = {i: w for i, w in enumerate(w for s in raw_english(source, cache, model)['segments'] for w in s.get('words', []))}
    for segment in (load_gaps(cache, model, task_of(source, cache, model)) or {}).get('segments', []):
        words.update({w['id']: w for w in segment['words']})
    return words


def indexed_words(source, cache, model):
    """Every caption word in time order: its index or id, kind (transcript, missed
    speech or added), edited text and timing, the original text and timing, and
    its line break override."""
    from shorts import english_transcript
    originals = original_words(source, cache, model)
    result = []
    for segment in english_transcript(source, cache, model, find_missed=False)['segments']:
        for w in segment.get('words', []):
            index = w['index']
            kind = 'transcript' if isinstance(index, int) else 'missed' if index.startswith('g') else 'added'
            o = originals.get(index)
            result.append({'index': index, 'kind': kind, 'start': round(w['start'], 3), 'end': round(w['end'], 3),
                           'word': w['word'].strip(), 'original': o['word'].strip() if o else '',
                           'original_start': round(o['start'], 3) if o else None,
                           'original_end': round(o['end'], 3) if o else None, 'line': w.get('line')})
    result.sort(key=lambda w: (w['start'], w['end']))
    return result


def edits_path(cache: Path, model):
    return Path(cache)/f'caption-edits-{model}.json'


def set_caption_edit(edits, index, original, to):
    """Change one word's text in an edits document (in place). The edit's `from` is
    always the unedited word; setting a word back to its original removes the edit."""
    from transcript_edits import update_edit
    return update_edit(edits, index, {'word': original}, {'to': to})


def load_edits(cache: Path, model):
    from transcript_edits import empty_edits
    path = edits_path(cache, model)
    edits = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else empty_edits(model)
    edits.setdefault('edits', [])
    edits.setdefault('added', [])
    return edits


def save_caption_edit(source, cache, model, index, changes):
    """Apply editor changes (`to`, `start`, `end`, `line`; None restores) to one word."""
    from shorts import write_json
    from transcript_edits import update_added, update_edit
    if not isinstance(changes, dict) or not changes:
        raise ValueError('Nothing to change.')
    edits = load_edits(cache, model)
    if isinstance(index, str) and index.startswith('u'):
        update_added(edits, index, changes)
    else:
        original = original_words(source, cache, model).get(index) if type(index) in (int, str) else None
        if original is None:
            raise ValueError('Unknown word index.')
        update_edit(edits, index, original, changes)
    write_json(edits_path(cache, model), edits)
    return edits


def add_caption_words(cache, model, words):
    from shorts import write_json
    from transcript_edits import add_words
    edits = load_edits(cache, model)
    ids = add_words(edits, words)
    write_json(edits_path(cache, model), edits)
    return ids


def caption_events(transcript, start, end, config):
    """The caption lane of a cut from `start` to `end`, as the renderer schedules it:
    groups of words, each word shown from its onset until `until` (clip seconds)."""
    from captions import caption_groups, clip_words
    words = clip_words(transcript, {'start': start, 'end': end})
    groups = caption_groups(words, config['max_caption_words'])
    duration = end-start
    result, offset = [], 0
    for group in groups:
        items = []
        for i, word in enumerate(group):
            index = offset+i
            following = words[index+1]['start'] if index+1 < len(words) else duration
            until = min(word['end']+(.15 if i+1 < len(group) else .12), following, duration)
            items.append({'start': round(word['start'], 3), 'until': round(until, 3), 'text': word['word']})
        result.append(items)
        offset += len(group)
    return result


# ---------- framing ----------

def auto_focus(source, clip, cache, config, transcript, output=None):
    """Per-frame automatic centers for a clip, without any reviewed overrides, plus
    the exact shots. Uses the cached analysis; falls back to the rendered .focus.json."""
    from speaker_focus import analysis_key, focus_plan
    key, _ = analysis_key(clip, config)
    if (Path(cache)/f'speaker-tracks-{key}.json').exists():
        bare = {k: v for k, v in clip.items() if k != 'focus_overrides'}
        plan = focus_plan(source, bare, cache, config, transcript, commands_path=False)
        exact = True
    else:
        from shorts import clip_stem
        plan = json.loads((Path(output)/(clip_stem(clip)+'.focus.json')).read_text(encoding='utf-8'))
        exact = False
    return {'frame_count': plan['frame_count'], 'scaled_width': plan['scaled_width'],
            'scaled_height': plan['scaled_height'], 'exact': exact,
            'shots': [{'start_frame': s['start_frame'], 'end_frame': s['end_frame'], 'review': s.get('review', [])}
                      for s in plan['shots']],
            'centers': [round(f['center_x'], 4) for f in plan['frames']]}


def draft_clip(clip, entry, words, duration, grid=None):
    """The clip as it would be after Apply: staged framing first (it uses the
    current clip's frames), then the word-safe re-cut."""
    from review_apply import shift_clip_timing, snap_edges
    clip = copy.deepcopy(clip)
    entry = entry or {}
    if entry.get('status') != 'needs_changes' or entry.get('addressed'):
        return clip
    if entry.get('focus_overrides') is not None:
        clip.pop('focus_overrides', None)
        if entry['focus_overrides']:
            clip['focus_overrides'] = copy.deepcopy(entry['focus_overrides'])
    if 'recut' in entry.get('options', []) and (entry.get('start_shift') or entry.get('end_shift')):
        start, end = snap_edges(words, clip['start'], clip['end'], entry.get('start_shift', 0),
                                entry.get('end_shift', 0), duration)
        clip = shift_clip_timing(clip, start, end, grid)
    return clip


# ---------- media jobs ----------

def make_proxy(root: Path, source_id):
    """A one-time low-resolution copy of the whole source that keeps every source
    frame at its own timestamp (no frame-rate conversion), with a keyframe about
    every half second so the editor can seek instantly. The editor then knows
    exactly which source frame is on screen and maps it to the render's 30 fps
    grid (frame_grid.py), so the live crop always matches the picture."""
    from shorts import find_source, nvenc_available, probe, write_json
    from frame_grid import load_frames
    source = find_source(source_id)
    dest = proxy_path(root, source_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name('proxy.partial.mp4')
    info = probe(source)
    duration = float(info['format']['duration'])
    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
    num, den = (int(x) for x in video['time_base'].split('/'))
    rate = video.get('avg_frame_rate') or video.get('r_frame_rate') or '30/1'
    try:
        gop = max(1, round(float(Fraction(rate))/2))
    except (ValueError, ZeroDivisionError):
        gop = 15
    print(f'Creating the preview proxy for {source.name} ({duration:.0f} s)', flush=True)
    print(f'duration={duration:.3f}', flush=True)
    codec = (['-c:v', 'h264_nvenc', '-preset', 'p3', '-rc', 'vbr', '-cq', '30', '-b:v', '0']
             if nvenc_available() else ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '28'])
    import subprocess
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostats', '-progress', 'pipe:1', '-y', '-i', str(source),
               '-map', '0:v:0', '-map', '0:a:0?', '-vf', f'scale=-2:{PROXY_HEIGHT}', '-fps_mode', 'passthrough',
               '-enc_time_base:v', f'{num}:{den}', '-video_track_timescale', str(den),
               *codec, '-g', str(gop), '-bf', '0', '-pix_fmt', 'yuv420p',
               '-c:a', 'aac', '-b:a', '96k', '-ac', '2', '-movflags', '+faststart', str(temp)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
    for line in process.stdout:
        if line.startswith('out_time_us='):
            value = line.split('=', 1)[1].strip()
            if value.isdigit():
                print(f'progress={min(1, int(value)/1e6/duration):.4f}', flush=True)
    if process.wait():
        raise RuntimeError('FFmpeg could not create the preview proxy.')
    check_proxy_frames(temp, load_frames(source, Path(root)/'work'/source_id))
    temp.replace(dest)
    write_json(dest.with_suffix('.json'), {'version': PROXY_VERSION, 'source_id': source_id})
    print(f'Proxy ready: {dest}', flush=True)
    return dest


def check_proxy_frames(proxy: Path, frames):
    """Refuse a proxy whose frame times differ from the source's: the live crop relies on them."""
    from shorts import run
    listing = run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'packet=pts_time',
                   '-of', 'csv=p=0', proxy])
    times = sorted(float(x) for x in listing.split() if x.replace('.', '', 1).lstrip('-').isdigit())
    num, den = frames['time_base']
    source = [p*num/den for p in frames['pts']]
    if len(times) != len(source) or max(abs(a-b) for a, b in zip(times, source)) > 1e-3:
        raise RuntimeError(f'The proxy frames do not match the source frames ({len(times)} vs {len(source)}).')


def preview_render(root: Path, output: Path, clip_id):
    """Render the exact result of the clip's staged edits at 0.4 scale. It uses the
    real render settings (so the speaker analysis cache matches) and writes only
    into Review/<source>/preview, never into the output folder."""
    from gallery import load_decisions
    from media_filters import audio_filters, video_filters
    from captions import write_captions
    from shorts import (ROOT, english_transcript, find_source, nvenc_available, probe, run, settings, write_json)
    from speaker_focus import focus_plan
    output = Path(output)
    clips = {c['id']: c for c in json.loads((output/'clips.json').read_text(encoding='utf-8-sig'))}
    plan = json.loads((output/'plan.json').read_text(encoding='utf-8-sig'))
    source_id = plan['source_id']
    source = find_source(source_id)
    cache = Path(root)/'work'/source_id
    config = settings()
    duration = float(probe(source)['format']['duration'])
    transcript = english_transcript(source, cache, 'medium')
    words = [w for s in transcript['segments'] for w in s.get('words', [])]
    from frame_grid import load_grid
    from shot_edges import snap_clip_to_cuts
    grid = load_grid(source, cache)
    entry = ((load_decisions(output) or {}).get('clips', {})).get(clip_id)
    clip = draft_clip({k: v for k, v in clips[clip_id].items() if k != 'file'}, entry, words, duration, grid)
    clip, notes = snap_clip_to_cuts(source, clip, cache, config, words, grid)  # as Apply does
    for note in notes:
        print(f'Clip {clip_id}: {note}', flush=True)
    folder = preview_dir(root, source_id)
    folder.mkdir(parents=True, exist_ok=True)
    print(f'Preview of clip {clip_id}: {clip["start"]:.2f}-{clip["end"]:.2f} s', flush=True)
    ass = folder/f'{clip_id}.ass'
    write_captions(transcript, clip, ass, folder/f'{clip_id}.srt', config)
    framing = focus_plan(source, clip, cache, config, transcript, commands_path=folder/f'{clip_id}.cmd')
    graph = video_filters(framing, clip, config, ass, ROOT/'assets/fonts')
    width, height = PREVIEW_SIZE
    graph = graph.replace('format=yuv420p[v]', f'scale={width}:{height}:flags=bicubic,format=yuv420p[v]')
    length = clip['end']-clip['start']
    temp = folder/f'{clip_id}.partial.mp4'
    codec = (['-c:v', 'h264_nvenc', '-preset', 'p2', '-cq', '28'] if nvenc_available()
             else ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28'])
    print('Rendering the preview...', flush=True)
    run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-ss', clip['start'], '-i', source, '-t', length,
         '-filter_complex', graph, '-map', '[v]', '-map', '0:a:0', '-af', audio_filters(length, config),
         '-c:a', 'aac', '-b:a', '128k', '-ar', '48000', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
         *codec, temp])
    dest = folder/f'{clip_id}.mp4'
    temp.replace(dest)
    write_json(folder/f'{clip_id}.json', {'start': clip['start'], 'end': clip['end'],
                                          'decision_updated': (entry or {}).get('updated')})
    print(f'Preview ready: {dest}', flush=True)
    return dest


def export_ready(output: Path, decisions):
    """Copy approved clips (video and captions) into <output>/ready. Files there
    that are no longer approved are removed; nothing is published anywhere."""
    output = Path(output)
    approved = {k for k, d in (decisions or {}).get('clips', {}).items() if d.get('status') == 'approved'}
    clips = [c for c in json.loads((output/'clips.json').read_text(encoding='utf-8-sig')) if c['id'] in approved]
    folder = output/'ready'
    folder.mkdir(exist_ok=True)
    keep = set()
    for clip in clips:
        stem = Path(clip['file']).stem
        for name in (clip['file'], stem+'.en.srt'):
            src = output/name
            if src.exists():
                dst = folder/name
                if not dst.exists() or dst.stat().st_size != src.stat().st_size or dst.stat().st_mtime < src.stat().st_mtime:
                    shutil.copy2(src, dst)
                keep.add(name)
    for path in folder.iterdir():
        if path.is_file() and path.suffix.lower() in ('.mp4', '.srt') and path.name not in keep:
            path.unlink()
    return folder, sorted(keep)


def retranscribe_path(root: Path, source_id):
    return review_dir(root, source_id)/'retranscribe.json'


def retranscribe(root: Path, source_id, start, end):
    """Whisper on a short range without VAD, for the editor to propose missed words.
    Writes Review/<id>/retranscribe.json; nothing changes until the user accepts words."""
    from faster_whisper import decode_audio
    from missed_speech import SAMPLE_RATE, load_model, transcribe_window
    from shorts import find_source, transcribe, write_json
    import subprocess
    import tempfile
    source = find_source(source_id)
    cache = Path(root)/'work'/source_id
    original = transcribe(source, cache, 'medium')
    task = 'transcribe' if original['language'] == 'en' else 'translate'
    language = original.get('source_language') or original['language']
    start, end = max(0.0, float(start)), float(end)
    if not 0 < end-start <= 30:
        raise ValueError('Re-transcribe at most 30 seconds at a time.')
    print(f'Transcribing {start:.2f}-{end:.2f} s', flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp)/'range.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(start), '-i', str(source), '-t', str(end-start),
                        '-vn', '-ar', str(SAMPLE_RATE), '-ac', '1', str(wav)], check=True)
        audio = decode_audio(str(wav), sampling_rate=SAMPLE_RATE)
    model, _ = load_model('medium')
    words = []
    for segment in transcribe_window(model, audio, 0, len(audio)/SAMPLE_RATE, language, task):
        for w in segment.words or []:
            if w.word.strip() and w.end > w.start:
                words.append({'start': round(start+w.start, 3), 'end': round(start+w.end, 3), 'word': w.word.strip(),
                              'probability': round(w.probability, 3), 'no_speech_prob': round(segment.no_speech_prob, 3)})
    write_json(retranscribe_path(root, source_id), {'start': start, 'end': end, 'words': words})
    print(f'{len(words)} word(s) found.', flush=True)
    return words


def prepare(root: Path, source_id):
    """What the editor needs once per source: the proxy and the missed-speech pass."""
    from shorts import english_transcript, find_source
    if not proxy_ready(root, source_id):
        make_proxy(root, source_id)
    english_transcript(find_source(source_id), Path(root)/'work'/source_id, 'medium')


def main(argv):
    from shorts import ROOT
    if len(argv) == 2 and argv[0] == 'proxy':
        make_proxy(ROOT, argv[1])
    elif len(argv) == 2 and argv[0] == 'prepare':
        prepare(ROOT, argv[1])
    elif len(argv) == 3 and argv[0] == 'preview':
        preview_render(ROOT, Path(argv[1]), argv[2])
    elif len(argv) == 4 and argv[0] == 'retranscribe':
        retranscribe(ROOT, argv[1], argv[2], argv[3])
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
