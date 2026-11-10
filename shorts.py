"""Local, resumable Raw -> vertical clips pipeline. See README.md."""
from __future__ import annotations
import argparse
import functools
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
from captions import write_captions

ROOT = Path(__file__).resolve().parent
MODELS = ['tiny','base','small','medium','large-v3']
VERSION = 6
EXTENSIONS = {'.mp4', '.mov', '.mkv', '.m4v', '.webm'}
LONG_CLIP_SECONDS = 90  # editorial guideline for short-form clips; 180 s is the hard limit


def run(args):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError(result.stderr[-5000:] or result.stdout[-5000:])
    return result.stdout


def write_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def probe(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', path]))


def _head_tail_hash(path, prefix):
    size = path.stat().st_size
    h = hashlib.sha256(prefix.encode())
    with path.open('rb') as stream:
        h.update(stream.read(1024 * 1024))
        stream.seek(max(0, size - 1024 * 1024))
        h.update(stream.read())
    return h.hexdigest()[:12]


def source_id(path):
    """Content-based id: renaming, copying or touching a file keeps its plans and caches."""
    return _head_tail_hash(path, f'{path.stat().st_size}')


def legacy_source_id(path):
    """The earlier id, which also hashed the file name and modification time."""
    stat = path.stat()
    return _head_tail_hash(path, f'{path.name}|{stat.st_size}|{stat.st_mtime_ns}')


def migrate_source_id(source, new_id, root=None):
    """Move plans, caches, review files and outputs from the legacy id to the new one."""
    root = Path(root or ROOT)
    old_id = legacy_source_id(source)
    if old_id == new_id or (root/'Plans'/f'{new_id}.json').exists():
        return False
    moves = [(root/'work'/old_id, root/'work'/new_id), (root/'Review'/old_id, root/'Review'/new_id),
             (root/'Plans'/f'{old_id}.json', root/'Plans'/f'{new_id}.json')]
    moves += [(folder, folder.with_name(folder.name[:-len(old_id[:6])]+new_id[:6]))
              for folder in (root/'Output').glob(f'*_{old_id[:6]}') if folder.is_dir()]
    moves = [(a, b) for a, b in moves if a.exists()]
    if not moves:
        return False
    for a, b in moves:
        if b.exists():
            raise ValueError(f'Cannot migrate {a} to {b}: the destination already exists.')
    for a, b in moves:
        a.rename(b)
    def retag(path):
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8-sig'))
            if isinstance(data, dict) and data.get('source_id') == old_id:
                data['source_id'] = new_id
                write_json(path, data)
    retag(root/'Plans'/f'{new_id}.json')
    for folder in (root/'Output').glob(f'*_{new_id[:6]}/*'):
        retag(folder/'plan.json')
        retag(folder/'review-decisions.json')
    print(f'Migrated source id {old_id} -> {new_id}: {source.name}', flush=True)
    return True


def raw_sources():
    raw = ROOT/'Raw'
    raw.mkdir(exist_ok=True)
    return sorted(p for p in raw.iterdir() if p.suffix.lower() in EXTENSIONS)


def migrate_all():
    """Bring every Raw source's folders to its content-based id."""
    for source in raw_sources():
        migrate_source_id(source, source_id(source))


def find_source(wanted):
    for source in raw_sources():
        if source_id(source) == wanted:
            return source
    raise ValueError(f'No video in Raw has source id {wanted}.')


@functools.lru_cache(maxsize=1)
def enable_cuda_libraries():
    """Put the cuBLAS/cuDNN DLLs from requirements-gpu.txt on PATH (Windows).
    CTranslate2 loads them by name, so os.add_dll_directory alone is not enough.
    On Linux, run.sh puts the same libraries on LD_LIBRARY_PATH."""
    if sys.platform != 'win32':
        return
    import importlib.util
    spec = importlib.util.find_spec('nvidia')
    folders = [str(f) for base in (spec.submodule_search_locations if spec else []) for f in sorted(Path(base).glob('*/bin'))]
    if folders:
        os.environ['PATH'] = os.pathsep.join(folders+[os.environ.get('PATH', '')])


@functools.lru_cache(maxsize=None)
def whisper_device(model_path):
    """('cuda', 'float16') when a test transcription works on the GPU, else ('cpu', 'int8').
    The test runs in a child process because a missing CUDA library can abort a process."""
    if os.environ.get('CLIPSMITH_WHISPER_DEVICE', '').lower() == 'cpu':
        return 'cpu', 'int8'
    try:
        import ctranslate2
        if not ctranslate2.get_cuda_device_count():
            return 'cpu', 'int8'
    except Exception:
        return 'cpu', 'int8'
    # A passed test is remembered, so short editor transcriptions skip it. A later GPU
    # failure still falls back to the CPU where the model runs.
    memo = ROOT/'work'/'whisper-device.json'
    key = f'{model_path}|{ctranslate2.__version__}'
    try:
        if json.loads(memo.read_text(encoding='utf-8')).get(key) == 'cuda':
            return 'cuda', 'float16'
    except (OSError, ValueError):
        pass
    test = ('import numpy, shorts; shorts.enable_cuda_libraries(); from faster_whisper import WhisperModel; '
            f'model = WhisperModel({str(model_path)!r}, device="cuda", compute_type="float16"); '
            'list(model.transcribe(numpy.zeros(32000, numpy.float32), word_timestamps=True)[0])')
    try:
        result = subprocess.run([sys.executable, '-c', test], cwd=ROOT, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=600)
        failed = result.returncode != 0
        reason = (result.stderr.strip().splitlines() or ['exit code '+str(result.returncode)])[-1]
    except subprocess.TimeoutExpired:
        failed, reason = True, 'the GPU test timed out'
    if failed:
        print(f'GPU transcription is unavailable ({reason}); using the CPU.', flush=True)
        return 'cpu', 'int8'
    try:
        known = json.loads(memo.read_text(encoding='utf-8')) if memo.exists() else {}
        memo.parent.mkdir(exist_ok=True)
        write_json(memo, {**known, key: 'cuda'})
    except (OSError, ValueError):
        pass
    return 'cuda', 'float16'


def run_whisper(source, cache, model_path, model_name, task, device, compute_type):
    from faster_whisper import WhisperModel
    if device == 'cuda':
        enable_cuda_libraries()
    print(f'Transcribing: {source.name} ({model_name}, {device.upper()})', flush=True)
    model = WhisperModel(str(model_path), device=device, compute_type=compute_type,
                         cpu_threads=min(8, os.cpu_count() or 4))
    segments, info = model.transcribe(str(source), task=task, word_timestamps=True, vad_filter=True,
                                      beam_size=5, condition_on_previous_text=False)
    data = {'language': 'en' if task=='translate' else info.language, 'source_language': info.language,
            'model': model_name, 'task': task, 'device': device, 'compute_type': compute_type, 'segments': []}
    suffix = '-english' if task == 'translate' else ''
    with (cache / f'transcript-{model_name}{suffix}.txt').open('w', encoding='utf-8') as out:
        for segment in segments:
            data['segments'].append(asdict(segment))
            out.write(f'{segment.start:.2f} - {segment.end:.2f}: {segment.text.strip()}\n')
            out.flush()
            print(f'  {segment.end:.0f} seconds processed', flush=True)
    return data


def transcribe(source, cache, model_name, task='transcribe'):
    suffix = '-english' if task == 'translate' else ''
    dest = cache / f'transcript-{model_name}{suffix}.json'
    if dest.exists():
        cached = json.loads(dest.read_text(encoding='utf-8'))
        cached.setdefault('model',model_name)
        cached.setdefault('task',task)
        return cached
    from faster_whisper.utils import download_model
    model_path = download_model(model_name)
    device, compute_type = whisper_device(model_path)
    try:
        data = run_whisper(source, cache, model_path, model_name, task, device, compute_type)
    except RuntimeError as exc:
        if device == 'cpu':
            raise
        # For example the GPU ran out of memory: start again on the CPU.
        print(f'GPU transcription failed ({exc}); starting again on the CPU.', flush=True)
        data = run_whisper(source, cache, model_path, model_name, task, 'cpu', 'int8')
    write_json(dest, data)
    return data


def english_transcript(source, cache, model_name, find_missed=True):
    """The English transcript with the missed-speech words (missed_speech.py) and the
    caption corrections (transcript_edits.py) merged in. Every word has an `index`.
    `find_missed=False` only uses an existing missed-speech pass instead of running it."""
    from missed_speech import load_gaps, missed_speech
    from transcript_edits import merged_transcript
    original = transcribe(source,cache,model_name)
    task = 'transcribe' if original['language']=='en' else 'translate'
    result = original if task=='transcribe' else transcribe(source,cache,model_name,'translate')
    language = original.get('source_language') or original['language']
    gaps = (missed_speech(source,cache,model_name,result,task,language) if find_missed
            else load_gaps(cache,model_name,task))
    edits_path = cache/f'caption-edits-{model_name}.json'
    edits = json.loads(edits_path.read_text(encoding='utf-8-sig')) if edits_path.exists() else None
    return merged_transcript(result,gaps,edits,model_name)


def settings():
    config = json.loads((ROOT/'video_settings.json').read_text(encoding='utf-8'))
    if config['width']%2 or config['height']%2 or not 0 < config['picture_height'] <= config['height']:
        raise ValueError('Invalid video dimensions.')
    return config


def sentences(transcript):
    """Use word timing and real pauses rather than fixed-length chopping."""
    words = []
    for segment in transcript['segments']:
        words.extend(segment.get('words') or [{'start': segment['start'], 'end': segment['end'], 'word': segment['text']}])
    result, group = [], []
    for i, word in enumerate(words):
        group.append(word)
        pause = i == len(words)-1 or words[i+1]['start'] - word['end'] > .75
        if pause or re.search(r'[.!?]["\u201d\u2019]*$', word['word'].strip()):
            result.append({'start': group[0]['start'], 'end': group[-1]['end'],
                           'text': ''.join(w['word'] for w in group).strip()})
            group = []
    return result


def propose(transcript, duration, count):
    """Offline heuristic, not a semantic/virality model. Plans remain editable."""
    units = sentences(transcript)
    candidates = []
    hooks = r'\b(why|how|what|secret|dead|killer|murder|truth|lie|money|million|never|actually|wrong|imagine)\b'
    for i, first in enumerate(units):
        if first['start'] < 3 or first['start'] > duration - 20:
            continue
        # A bare reply such as "A what?" needs the previous speaker's context.
        if len(first['text'].split()) < 5:
            continue
        possible = []
        for j in range(i, len(units)):
            # Treat a long dialogue break as a section boundary. Do not join an
            # interrogation to an unrelated scene merely to fill the duration.
            if j > i and units[j]['start'] - units[j-1]['end'] >= 2.5:
                break
            length = units[j]['end'] - first['start']
            if length > 58:
                break
            if length >= 24:
                possible.append(j)
        if not possible:
            continue
        # Prefer endings after a sentence and before a pause.
        j = max(possible, key=lambda k: min(2, units[k+1]['start']-units[k]['end']) if k+1 < len(units) else 2)
        text = ' '.join(u['text'] for u in units[i:j+1])
        length = units[j]['end'] - first['start']
        speech = sum(u['end']-u['start'] for u in units[i:j+1]) / length
        score = len(re.findall(hooks, text.lower())) + 2 * len(re.findall(hooks, first['text'].lower()))
        score += 1.5 * ('?' in first['text']) + speech * 2
        score -= 2 * bool(re.match(r'^(and|but|so|he|she|it)\b', first['text'], re.I))
        candidates.append({'start': max(0, round(first['start']-.18, 2)),
                           'end': min(duration, round(units[j]['end']+.3, 2)),
                           'title': ' '.join(first['text'].split()[:12]),
                           'reason': 'Local keyword, question and speech-density score; heuristic proposal.',
                           'score': round(score, 2)})
    chosen = []
    ranked = sorted(candidates, key=lambda c: c['score'], reverse=True)
    for clip in ranked:
        if count is None and clip['score'] < max(3.5, ranked[0]['score']*.55):
            break
        if all(clip['end']+3 <= c['start'] or clip['start'] >= c['end']+3 for c in chosen):
            chosen.append(clip)
        if count is not None and len(chosen) == count:
            break
    if not chosen:
        raise ValueError('Not enough speech was found. Create a reviewed start/end plan under Plans.')
    return chosen


def validate_plan(plan, fingerprint, duration):
    if plan.get('source_id') != fingerprint:
        raise ValueError('The plan belongs to a different source (source_id mismatch).')
    if not plan.get('clips'):
        raise ValueError('The plan is empty.')
    ids = [c['id'] for c in plan['clips']+plan.get('rejected_clips',[]) if 'id' in c]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r'\d{2,}', i) for i in ids):
        raise ValueError('Clip ids must be unique numbers such as "01".')
    for clip in plan['clips']:
        start, end = float(clip['start']), float(clip['end'])
        if not (0 <= start < end <= duration + .05):
            raise ValueError(f'Invalid time range: {clip}')
        if end-start > 180:
            raise ValueError('A clip cannot exceed 180 seconds.')
        if not 0 <= clip.get('crop_x',.5) <= 1:
            raise ValueError('crop_x must be between 0 and 1.')
        previous_end=0
        if clip.get('framing_overrides'):
            raise ValueError('Legacy zoom overrides are unsupported. Use frame-indexed focus_overrides.')
        for override in sorted(clip.get('focus_overrides',[]),key=lambda o:o['start_frame']):
            a,b=override['start_frame'],override['end_frame']
            if not (isinstance(a,int) and isinstance(b,int) and previous_end<=a<b<=math.ceil((end-start)*30-1e-9)):
                raise ValueError('Focus overrides must use non-overlapping frame indices inside the clip.')
            if not 0<=override['center_x']<=1:
                raise ValueError('Focus center_x must be between 0 and 1.')
            if not 0<=override.get('center_x_end',override['center_x'])<=1:
                raise ValueError('Focus center_x_end must be between 0 and 1.')
            previous_end=b


def verify(path, expected, config=None):
    config = config or settings()
    data = probe(path)
    video = next(s for s in data['streams'] if s['codec_type']=='video')
    audio = next((s for s in data['streams'] if s['codec_type']=='audio'), None)
    if (video['width'], video['height']) != (config['width'],config['height']) or not audio:
        raise ValueError(f'Output validation failed: {path}')
    if abs(float(data['format']['duration'])-expected)>.3:
        raise ValueError(f'Unexpected output duration: {path}')
    return data


@functools.lru_cache(maxsize=1)
def nvenc_available():
    """Probe the GPU encoder once, so a real filter error is never retried silently."""
    try:
        run(['ffmpeg','-v','error','-f','lavfi','-i','color=black:s=256x256:d=0.1','-c:v','h264_nvenc','-f','null','-'])
        return True
    except RuntimeError:
        print('GPU encoding is unavailable; using the CPU encoder.', flush=True)
        return False


def filter_path(path):
    return str(Path(path).resolve()).replace('\\','/').replace(':','\\:').replace("'", "'\\''")


def ensure_clip_ids(plan):
    """Give every clip a permanent id. Existing clips keep their plan position as
    id; new clips get the next unused number, so removing one renames nothing."""
    used = [int(c['id']) for c in plan['clips']+plan.get('rejected_clips',[]) if 'id' in c]
    changed = False
    for position, clip in enumerate(plan['clips'], 1):
        if 'id' not in clip:
            number = position if not used else max(used)+1
            while number in used:
                number += 1
            clip['id'] = f'{number:02}'
            used.append(number)
            changed = True
    return changed


def clip_stem(clip):
    slug = re.sub(r'[^\w-]+','_',clip['title'],flags=re.UNICODE).strip('_')[:64]
    return f"{clip['id']}_{slug}"


# Plan fields that do not change the rendered pixels or sound.
NON_RENDER_KEYS = {'id','title','reason','approved','kept','cover_time'}


def render_signature(clip, config, framing, captions, loudness=None):
    framing = {k: v for k, v in framing.items() if k != 'commands_path'}
    return hashlib.sha256(json.dumps({'clip':{k: v for k, v in clip.items() if k not in NON_RENDER_KEYS},
                                      'version':VERSION,'settings':config,'loudness':loudness,'reframe':framing,'captions':captions,
                                      'font':hashlib.sha256((ROOT/'assets/fonts/Dosis-Bold.ttf').read_bytes()).hexdigest()},
                                     sort_keys=True).encode()).hexdigest()


def is_locked(clip, dest, force):
    """An approved clip keeps its finished file until --force or the approval is withdrawn."""
    return bool(clip.get('approved')) and not force and dest.exists() and dest.with_suffix('.done.json').exists()


def render(source, clip, dest, cache, force, transcript=None, config=None):
    config = config or settings()
    if transcript is None:
        raise ValueError('Rendering requires a word-timed English transcript.')
    if is_locked(clip, dest, force):
        verify(dest, clip['end']-clip['start'], config)
        print(f'Approved and locked: {dest.name}', flush=True)
        return
    ass = cache/(dest.stem+'-en.ass')
    write_captions(transcript,clip,ass,dest.with_suffix('.en.srt'),config)
    from speaker_focus import focus_plan
    from focus_review import write_focus_sheet
    framing = focus_plan(source,clip,cache,config,transcript)
    write_json(dest.with_suffix('.focus.json'),framing)
    from media_filters import audio_filters, measure_loudness, video_filters
    duration = clip['end']-clip['start']
    # First loudnorm pass; the measurement is part of the signature because it sets the clip's gain.
    loudness = measure_loudness(source, clip['start'], duration)
    digest = render_signature(clip, config, framing, ass.read_text(encoding='utf-8-sig'), loudness)
    marker = dest.with_suffix('.done.json')
    sheet = ROOT/'Review'/cache.name/'focus'/(dest.stem+'.jpg')
    if not force and dest.exists() and marker.exists() and json.loads(marker.read_text())['signature']==digest:
        verify(dest, clip['end']-clip['start'],config)
        if not sheet.exists():
            write_focus_sheet(source,clip,framing,sheet,config['width'])
        print(f'Already complete: {dest.name}', flush=True)
        return
    write_focus_sheet(source,clip,framing,sheet,config['width'])
    temp = dest.with_name(dest.stem+'.partial.mp4')
    graph = video_filters(framing,clip,config,ass,ROOT/'assets/fonts')
    command = ['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',clip['start'],'-i',source,
               '-t',duration,'-filter_complex',graph,
               '-map','[v]','-map','0:a:0','-af',audio_filters(duration,config,loudness),
               '-c:a','aac','-b:a','192k','-ar','48000','-pix_fmt','yuv420p',
               '-movflags','+faststart','-map_metadata','-1']
    if nvenc_available():
        run(command+['-c:v','h264_nvenc','-preset','p4','-rc','vbr','-cq','20','-b:v','0',temp])
    else:
        run(command+['-c:v','libx264','-preset','veryfast','-crf','20',temp])
    data = verify(temp,duration,config)
    # Decode every output frame/audio packet before marking complete.
    run(['ffmpeg','-v','error','-xerror','-i',temp,'-f','null','-'])
    temp.replace(dest)
    write_json(marker, {'signature':digest,'duration':data['format']['duration']})


CLIP_SUFFIXES = ('.mp4','.jpg','.en.srt','.done.json','.focus.json')


def clip_files(folder, stem):
    return [folder/(stem+suffix) for suffix in CLIP_SUFFIXES if (folder/(stem+suffix)).exists()]


def tidy_outputs(output, plan):
    """Delete superseded files of a clip whose title changed, and move files of
    clips that are no longer in the plan into rejected/."""
    current = {clip_stem(c) for c in plan['clips']}
    ids = {c['id'] for c in plan['clips']}
    for path in sorted(output.glob('*.mp4')):
        stem = path.stem
        match = re.match(r'(\d{2,})_', stem)
        if stem in current or not match or stem.endswith('.partial'):
            continue
        files = clip_files(output, stem)
        if match.group(1) in ids:
            for f in files:
                f.unlink()
            print(f'Removed superseded files: {stem}', flush=True)
        else:
            archive = output/'rejected'
            archive.mkdir(exist_ok=True)
            for f in files:
                f.replace(archive/f.name)
            print(f'Moved to rejected/: {stem} (no longer in the plan)', flush=True)


def output_name(plan):
    # Named after the plan's original source name, so renaming the Raw file moves nothing.
    return Path(plan['source']).stem+'_'+plan['source_id'][:6]


def output_folder(plan, config):
    return ROOT/'Output'/output_name(plan)/config['profile']


def snap_plan_to_cuts(source, plan, cache, config, transcript, output, force, plan_path=None, skip=()):
    """Move clip edges off a few frames of a neighbouring camera shot (shot_edges.py).
    Locked clips and `skip` (candidates waiting for triage) keep their edges. A changed
    plan is written back to `plan_path`."""
    from frame_grid import load_grid
    from shot_edges import snap_clip_to_cuts
    grid = load_grid(source, cache)
    words = [w for s in transcript['segments'] for w in s.get('words',[])]
    changed = False
    for number,clip in enumerate(plan['clips']):
        if clip['id'] in skip or is_locked(clip, output/(clip_stem(clip)+'.mp4'), force):
            continue
        moved, notes = snap_clip_to_cuts(source,clip,cache,config,words,grid)
        for note in notes:
            print(f'Clip {clip["id"]}: {note}',flush=True)
        if moved is not clip:
            plan['clips'][number] = moved
            changed = True
    if changed and plan_path:
        write_json(Path(plan_path),plan)
    return changed


def render_plan(source, plan, cache, config, force, model='medium', plan_path=None, skip=()):
    """Render every clip of a validated plan except `skip` (candidates waiting for the
    editor's triage, see review_apply.awaiting_triage); return the output folder."""
    output = output_folder(plan, config)
    output.mkdir(parents=True,exist_ok=True)
    index = []
    transcript = english_transcript(source,cache,model)
    snap_plan_to_cuts(source,plan,cache,config,transcript,output,force,plan_path,skip)
    write_json(output/'plan.json',plan)
    clips = [c for c in plan['clips'] if c['id'] not in skip]
    for clip in clips:
        if clip['end']-clip['start'] > LONG_CLIP_SECONDS:
            print(f'Note: clip {clip["id"]} runs {clip["end"]-clip["start"]:.0f} s. Short-form clips stay within '
                  f'{LONG_CLIP_SECONDS} s unless the user asks for longer (AGENTS.md, step 2).',flush=True)
    for number,clip in enumerate(clips,1):
        dest = output/(clip_stem(clip)+'.mp4')
        print(f'Clip {number}/{len(clips)} [{clip["id"]}]: {clip["title"]} ({clip["end"]-clip["start"]:.1f}s)',flush=True)
        render(source,clip,dest,cache,force,transcript,config)
        cover = dest.with_suffix('.jpg')
        if not is_locked(clip, dest, force) or not cover.exists():
            run(['ffmpeg','-v','error','-y','-ss',min(clip.get('cover_time',2),clip['end']-clip['start']-.1),'-i',dest,'-frames:v','1','-update','1',cover])
        index.append({**clip,'file':dest.name})
    tidy_outputs(output, plan)
    write_json(output/'clips.json',index)
    from gallery import write_gallery
    write_gallery(output,ROOT,index,plan)
    write_json(cache/'last-render.json',{'profile':config['profile'],'output':str(output),'clips':len(index),'model':model})
    print(f'Completed: {output}',flush=True)
    return output


def load_plan(plan_path, fingerprint, duration):
    plan = json.loads(plan_path.read_text(encoding='utf-8-sig'))
    if ensure_clip_ids(plan):
        write_json(plan_path, plan)
    validate_plan(plan,fingerprint,duration)
    return plan


def main():
    if '--shots' in sys.argv[1:]:  # shot index: its own options, see shot_index.py
        import shot_index
        return shot_index.main([a for a in sys.argv[1:] if a != '--shots'])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, help='Default: all videos in the Raw folder')
    parser.add_argument('--count', type=int, default=None, help='Optional proposal count limit; no fixed limit by default')
    parser.add_argument('--model', choices=MODELS, help='Whisper model (default: medium; the editor and --apply-review '
                        'default to the model the source was transcribed with)')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--analyze-only', action='store_true')
    parser.add_argument('--debug', action='store_true', help='Print the full traceback when a source fails')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--prepare-review', action='store_true', help='Prepare the full transcript and timestamped frames for editorial review')
    parser.add_argument('--require-reviewed', action='store_true', help='Require an editorially reviewed plan before rendering')
    parser.add_argument('--review-gallery', nargs='?', const='', metavar='OUTPUT', help='Open the clip editor for an output folder and record decisions (default: latest output folder)')
    parser.add_argument('--write-gallery', nargs='?', const='', metavar='OUTPUT', help='Regenerate only index.html for an output folder (default: latest)')
    parser.add_argument('--apply-review', nargs='?', const='', metavar='OUTPUT', help='Apply gallery decisions to the plan and re-render changed clips (default: latest output folder)')
    parser.add_argument('--scan-music', action='store_true', help='Detect tempo, beat grid and drop of new tracks in Music/ and list all tracks')
    parser.add_argument('--no-triage', action='store_true', help='Also render candidates nobody kept in the editor triage yet')
    parser.add_argument('--port', type=int, default=8765, help='Clip editor port')
    parser.add_argument('--no-open', action='store_true', help='Do not open a browser for --review-gallery')
    args = parser.parse_args()
    migrate_all()
    if args.scan_music:
        import music
        music.main()
        return 0
    if args.apply_review is not None:
        from gallery import latest_output
        from review_apply import apply_review
        output = Path(args.apply_review).resolve() if args.apply_review else latest_output(ROOT)
        _, _, edit_error = apply_review(output, ROOT, settings(), args.force, args.model)
        return 1 if edit_error else 0
    if args.review_gallery is not None:
        # Opens a plan before anything is rendered too (triage), see docs/clip-editor.md.
        from editor_server import editor_target, serve
        output, fingerprint = editor_target(ROOT, args.review_gallery, args.input, settings())
        serve(ROOT,output,args.port,not args.no_open,args.model,fingerprint)
        return 0
    if args.write_gallery is not None:
        from gallery import latest_output, write_gallery
        output = Path(args.write_gallery).resolve() if args.write_gallery else latest_output(ROOT)
        if not (output/'clips.json').exists():
            parser.error(f'No clips.json in {output}')
        print(f'Gallery written: {write_gallery(output,ROOT)}',flush=True)
        return 0
    args.model = args.model or 'medium'
    if args.count is not None and not 1 <= args.count <= 200:
        parser.error('--count must be between 1 and 200')
    config = settings()
    review_entries = []
    sources = [args.input.resolve()] if args.input else raw_sources()
    if not sources:
        raise ValueError('No videos were found in the Raw folder. Copy your recordings into Raw/ and run again.')
    if args.plan and len(sources)!=1:
        parser.error('Use --input to select one video when passing --plan')
    failures = []
    for source in sources:
        try:
            fingerprint = source_id(source)
            migrate_source_id(source, fingerprint)  # also covers --input outside Raw
            cache = ROOT/'work'/fingerprint
            cache.mkdir(parents=True,exist_ok=True)
            plans = ROOT/'Plans'
            plans.mkdir(exist_ok=True)
            plan_path = args.plan or plans/(fingerprint+'.json')
            data = probe(source)
            if not any(s['codec_type']=='audio' for s in data['streams']):
                raise ValueError('This speech-based workflow requires an audio stream.')
            duration = float(data['format']['duration'])
            if args.prepare_review:
                from review import prepare_review
                transcript = english_transcript(source,cache,args.model)
                review_entries.append(prepare_review(source,fingerprint,cache,transcript,duration,plan_path))
                continue
            if plan_path.exists():
                plan = load_plan(plan_path,fingerprint,duration)
            else:
                if args.require_reviewed:
                    raise ValueError('No reviewed plan exists. Run --prepare-review and prepare a Plans JSON file first.')
                transcript = transcribe(source,cache,args.model)
                plan = {'source':source.name,'source_id':fingerprint,'selection':'offline-heuristic',
                        'clips':propose(transcript,duration,args.count)}
                ensure_clip_ids(plan)
                validate_plan(plan,fingerprint,duration)
                write_json(plan_path,plan)
            if args.require_reviewed and plan.get('selection') not in ('editor-reviewed','agent-reviewed'):
                raise ValueError('The plan has not been editorially reviewed.')
            if args.require_reviewed:
                # Coverage, clip count and word-safe edges; nothing renders until all pass.
                from selection import check_reviewed_plan
                # Edit problems never stop the clips; they are reported and fail the run after the clips.
                check_reviewed_plan(plan,duration,english_transcript(source,cache,args.model),
                                    include_edits=args.analyze_only)
            if args.analyze_only:
                if plan.get('edits'):
                    # Montage framing and camera cuts need the speaker analysis; show the sheets to review.
                    from edits import preview_edits
                    sheets, problems = preview_edits(source,plan,cache,config,english_transcript(source,cache,args.model))
                    print('Montage sheets (graded and framed as rendered):',flush=True)
                    for sheet in sheets:
                        print(f'    {sheet}',flush=True)
                    if problems:
                        lines = ''.join(f'\n  - {p}' for p in problems)
                        raise ValueError(f'The edits are not ready to render:{lines}')
                print(f'Plan ready: {plan_path}',flush=True)
                continue
            from review_apply import awaiting_triage, report_waiting, triage_view
            # A reviewed plan renders only candidates the user kept in the editor's triage.
            waiting = awaiting_triage(plan,ROOT) if args.require_reviewed and not args.no_triage else []
            if waiting and len(waiting) == len(plan['clips']):
                report_waiting(waiting)
                continue
            output = render_plan(source,plan,cache,config,args.force,args.model,plan_path,waiting)
            if args.require_reviewed:
                from verify_outputs import verify_folder
                verify_folder(output,config)
            # Music edits build on the finished, validated clips. A broken edit does not stop the
            # other edits or the clips, but it fails the run.
            from edits import render_edits
            transcript = english_transcript(source,cache,args.model)
            view = triage_view(plan,waiting)  # edits of candidates waiting for triage are left alone
            found = []
            if args.require_reviewed:
                from selection import edit_plan_problems
                try:
                    found = edit_plan_problems(view,duration,transcript)
                except ValueError as exc:
                    found = [str(exc)]
                if found:
                    print(f'WARNING — {source.name}: the clips are rendered, but the music edits have problems:'
                          +''.join(f'\n  - {p}' for p in found),file=sys.stderr,flush=True)
            render_error = None
            try:
                render_edits(source,view,cache,config,output,transcript,args.force)
            except ValueError as exc:
                render_error = str(exc)
            report_waiting(waiting)
            if found:
                raise ValueError(f'{len(found)} music edit problem(s), listed above; the clips and the other edits are done.'
                                 +(f'\n{render_error}' if render_error and not all(p in render_error for p in found) else ''))
            if render_error:
                raise ValueError(render_error)
        except Exception as exc:
            if args.debug:
                traceback.print_exc()
            print(f'ERROR — {source.name}: {exc}',file=sys.stderr,flush=True)
            failures.append(source)
    if args.prepare_review:
        review_root = ROOT/'Review'
        review_root.mkdir(exist_ok=True)
        write_json(review_root/'manifest.json',{'sources':review_entries,'failures':[str(p) for p in failures]})
        print(f'Review manifest: {review_root/"manifest.json"}',flush=True)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
