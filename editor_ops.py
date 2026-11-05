"""Operations behind the clip editor: preview proxy, quick exact preview renders,
caption word corrections, live-preview data and the ready-to-post export.

Long operations also run as a CLI so the editor server can stream their output:
    python editor_ops.py proxy <source_id>
    python editor_ops.py preview <output folder> <clip id>
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
import shutil
import sys

FPS = 30
PROXY_HEIGHT = 540
PREVIEW_SIZE = (432, 540)  # 0.4 of 1080x1350
CONTEXT = 30.0  # seconds of source shown around a clip on the editor timeline
MAX_WORD = 64


def review_dir(root: Path, source_id):
    return Path(root)/'Review'/source_id


def proxy_path(root: Path, source_id):
    return review_dir(root, source_id)/'proxy.mp4'


def preview_dir(root: Path, source_id):
    return review_dir(root, source_id)/'preview'


# ---------- captions ----------

def raw_english(source, cache, model):
    """The unedited English transcript that caption edit indices and `from` refer to."""
    from shorts import transcribe
    original = transcribe(source, cache, model)
    return original if original['language'] == 'en' else transcribe(source, cache, model, 'translate')


def indexed_words(source, cache, model):
    """Every transcript word with its global index, edited text and original text."""
    from shorts import english_transcript
    raw = [w for s in raw_english(source, cache, model)['segments'] for w in s.get('words', [])]
    edited = [w for s in english_transcript(source, cache, model)['segments'] for w in s.get('words', [])]
    return [{'index': i, 'start': round(e['start'], 3), 'end': round(e['end'], 3), 'word': e['word'].strip(),
             'original': r['word'].strip()} for i, (r, e) in enumerate(zip(raw, edited))]


def edits_path(cache: Path, model):
    return Path(cache)/f'caption-edits-{model}.json'


def set_caption_edit(edits, index, original, to):
    """Change one word in an edits document (in place). The edit's `from` is always
    the unedited word; setting a word back to its original removes the edit."""
    if not isinstance(to, str) or len(to) > MAX_WORD or any(c in to for c in '\r\n\t'):
        raise ValueError(f'A word must be text of at most {MAX_WORD} characters on one line.')
    to = to.strip()
    rest = [e for e in edits['edits'] if e['index'] != index]
    if to != original:
        rest.append({'index': index, 'from': original, 'to': to})
    edits['edits'] = sorted(rest, key=lambda e: e['index'])
    return edits


def save_caption_edit(source, cache, model, index, to):
    from shorts import write_json
    words = [w for s in raw_english(source, cache, model)['segments'] for w in s.get('words', [])]
    if type(index) is not int or not 0 <= index < len(words):
        raise ValueError('Unknown word index.')
    path = edits_path(cache, model)
    edits = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {'model': model, 'edits': []}
    set_caption_edit(edits, index, words[index]['word'].strip(), to)
    write_json(path, edits)
    return edits


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


def draft_clip(clip, entry, words, duration):
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
        clip = shift_clip_timing(clip, start, end)
    return clip


# ---------- media jobs ----------

def make_proxy(root: Path, source_id):
    """A one-time low-resolution copy of the whole source on the render's 30 fps
    grid, with a keyframe every half second so the editor can seek instantly."""
    from shorts import find_source, nvenc_available, probe
    source = find_source(source_id)
    dest = proxy_path(root, source_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name('proxy.partial.mp4')
    duration = float(probe(source)['format']['duration'])
    print(f'Creating the preview proxy for {source.name} ({duration:.0f} s)', flush=True)
    print(f'duration={duration:.3f}', flush=True)
    codec = (['-c:v', 'h264_nvenc', '-preset', 'p3', '-rc', 'vbr', '-cq', '30', '-b:v', '0']
             if nvenc_available() else ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '28'])
    import subprocess
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostats', '-progress', 'pipe:1', '-y', '-i', str(source),
               '-vf', f'fps={FPS},scale=-2:{PROXY_HEIGHT}', *codec, '-g', '15', '-bf', '0', '-pix_fmt', 'yuv420p',
               '-c:a', 'aac', '-b:a', '96k', '-ac', '2', '-movflags', '+faststart', str(temp)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
    for line in process.stdout:
        if line.startswith('out_time_us='):
            value = line.split('=', 1)[1].strip()
            if value.isdigit():
                print(f'progress={min(1, int(value)/1e6/duration):.4f}', flush=True)
    if process.wait():
        raise RuntimeError('FFmpeg could not create the preview proxy.')
    temp.replace(dest)
    print(f'Proxy ready: {dest}', flush=True)
    return dest


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
    entry = ((load_decisions(output) or {}).get('clips', {})).get(clip_id)
    clip = draft_clip({k: v for k, v in clips[clip_id].items() if k != 'file'}, entry, words, duration)
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


def main(argv):
    from shorts import ROOT
    if len(argv) == 2 and argv[0] == 'proxy':
        make_proxy(ROOT, argv[1])
    elif len(argv) == 3 and argv[0] == 'preview':
        preview_render(ROOT, Path(argv[1]), argv[2])
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
