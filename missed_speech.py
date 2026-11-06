"""Find speech that the long-form Whisper pass skipped.

The long-form pass sometimes drops short or quiet lines (a whispered "Oh, God.")
or skips a passage. This pass looks only at the gaps of at least MIN_GAP
seconds between recognized words. A sensitive VAD looks for speech in each gap,
Whisper transcribes a short window around it without VAD, and words that lie
inside the gap and come from confident segments are kept. Whisper's typical
hallucinations in silence ("Thanks for watching!") come with a high no-speech
probability and are dropped.

The result is cached next to the transcript (`transcript-<model>-gaps.json`)
with its own word ids ("g0", "g1", ...). `shorts.english_transcript` merges it,
so the transcript file and its word indices never change.
"""
from __future__ import annotations
import json
from pathlib import Path
import re

VERSION = 1
SAMPLE_RATE = 16000
MIN_GAP = 1.0            # seconds without a recognized word
PAD = .6                 # audio context on each side of a transcription window
MAX_WINDOW = 28.0
VAD = {'threshold': .25, 'min_speech_duration_ms': 100, 'min_silence_duration_ms': 300, 'speech_pad_ms': 200}
MAX_NO_SPEECH = .6
MIN_LOGPROB = -.9
MAX_COMPRESSION = 2.4
MIN_WORD_PROBABILITY = .1
EDGE_TOLERANCE = .02
DUPLICATE_SECONDS = 1.5  # a gap word this close to an edge that repeats the neighbouring word is the same word


def gaps_path(cache: Path, model, task):
    suffix = '-english' if task == 'translate' else ''
    return Path(cache)/f'transcript-{model}{suffix}-gaps.json'


def settings():
    return {'version': VERSION, 'min_gap': MIN_GAP, 'pad': PAD, 'max_window': MAX_WINDOW, 'vad': VAD,
            'max_no_speech': MAX_NO_SPEECH, 'min_logprob': MIN_LOGPROB, 'max_compression': MAX_COMPRESSION,
            'min_word_probability': MIN_WORD_PROBABILITY}


def load_gaps(cache: Path, model, task):
    path = gaps_path(cache, model, task)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    return data if data.get('settings') == settings() else None


def spoken_words(transcript):
    return sorted((w for s in transcript['segments'] for w in s.get('words', []) if w['end'] > w['start']),
                  key=lambda w: w['start'])


def find_gaps(words, duration):
    """(start, end, previous word, next word) for every stretch without a word."""
    gaps, previous, last_end = [], None, 0.0
    for word in words:
        if word['start']-last_end >= MIN_GAP:
            gaps.append((last_end, word['start'], previous, word))
        if word['end'] > last_end:
            last_end, previous = word['end'], word
    if duration-last_end >= MIN_GAP:
        gaps.append((last_end, duration, previous, None))
    return gaps


def windows_for(gap, speech, duration):
    """Transcription windows (seconds) around the VAD speech chunks of one gap."""
    a, b = gap[0], gap[1]
    windows = []
    for s, e in speech:
        lo, hi = max(0.0, a-PAD, s-PAD), min(duration, b+PAD, e+PAD)
        if windows and lo <= windows[-1][1] and hi-windows[-1][0] <= MAX_WINDOW:
            windows[-1][1] = max(windows[-1][1], hi)
        else:
            while hi-lo > MAX_WINDOW:
                windows.append([lo, lo+MAX_WINDOW])
                lo += MAX_WINDOW-2*PAD
            windows.append([lo, hi])
    return windows


def normal(text):
    return re.sub(r"[^\w']+", '', text.lower())


def keep_words(segments, offset, gap):
    """Words worth keeping from one window's Whisper segments (times in source seconds)."""
    a, b, previous, following = gap
    kept = []
    for segment in segments:
        if (segment.no_speech_prob > MAX_NO_SPEECH or segment.avg_logprob < MIN_LOGPROB
                or segment.compression_ratio > MAX_COMPRESSION):
            continue
        words = []
        for w in segment.words or []:
            start, end, text = offset+w.start, offset+w.end, w.word.strip()
            if not text or end <= start or w.probability < MIN_WORD_PROBABILITY:
                continue
            if start < a-EDGE_TOLERANCE or end > b+EDGE_TOLERANCE:
                continue
            if previous and start-a < DUPLICATE_SECONDS and normal(text) == normal(previous['word']):
                continue
            if following and b-end < DUPLICATE_SECONDS and normal(text) == normal(following['word']):
                continue
            words.append({'start': round(start, 3), 'end': round(end, 3), 'word': ' '+text,
                          'probability': round(w.probability, 3)})
        if words:
            kept.append({'start': words[0]['start'], 'end': words[-1]['end'],
                         'text': ' '.join(w['word'].strip() for w in words), 'words': words,
                         'no_speech_prob': round(segment.no_speech_prob, 3), 'avg_logprob': round(segment.avg_logprob, 3)})
    return kept


def load_model(model_name):
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model
    from shorts import enable_cuda_libraries, whisper_device
    path = download_model(model_name)
    device, compute_type = whisper_device(path)
    if device == 'cuda':
        enable_cuda_libraries()
    return WhisperModel(str(path), device=device, compute_type=compute_type), device


def transcribe_window(model, audio, start, end, language, task):
    segments, _ = model.transcribe(audio[int(start*SAMPLE_RATE):int(end*SAMPLE_RATE)], language=language, task=task,
                                   word_timestamps=True, vad_filter=False, beam_size=5,
                                   condition_on_previous_text=False)
    return list(segments)


def missed_speech(source, cache: Path, model_name, transcript, task, language=None):
    """Cached words from the gaps of `transcript` (see the module docstring)."""
    from faster_whisper import decode_audio
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    from shorts import write_json
    cached = load_gaps(cache, model_name, task)
    if cached is not None:
        return cached
    print(f'Looking for speech the transcript missed: {Path(source).name}', flush=True)
    audio = decode_audio(str(source), sampling_rate=SAMPLE_RATE)
    duration = len(audio)/SAMPLE_RATE
    gaps = find_gaps(spoken_words(transcript), duration)
    jobs = []
    for gap in gaps:
        chunk = audio[int(gap[0]*SAMPLE_RATE):int(gap[1]*SAMPLE_RATE)]
        speech = [(gap[0]+s['start']/SAMPLE_RATE, gap[0]+s['end']/SAMPLE_RATE)
                  for s in get_speech_timestamps(chunk, VadOptions(**VAD))]
        jobs += [(gap, lo, hi) for lo, hi in windows_for(gap, speech, duration)]
    print(f'  {len(gaps)} gaps, {len(jobs)} windows with possible speech', flush=True)
    model, device = load_model(model_name)
    segments = []
    for number, (gap, lo, hi) in enumerate(jobs, 1):
        try:
            found = transcribe_window(model, audio, lo, hi, language, task)
        except RuntimeError:
            if device == 'cpu':
                raise
            print('GPU transcription failed; continuing on the CPU.', flush=True)
            from faster_whisper import WhisperModel
            from faster_whisper.utils import download_model
            model, device = WhisperModel(str(download_model(model_name)), device='cpu', compute_type='int8'), 'cpu'
            found = transcribe_window(model, audio, lo, hi, language, task)
        segments += keep_words(found, lo, gap)
        print(f'progress={number/len(jobs):.4f}', flush=True)
    # Overlapping windows can find the same words twice; keep the first.
    segments.sort(key=lambda s: s['start'])
    unique, last_end, counter = [], -1.0, 0
    for segment in segments:
        words = [w for w in segment['words'] if w['start'] >= last_end-EDGE_TOLERANCE]
        if not words:
            continue
        for w in words:
            w['id'] = f'g{counter}'
            counter += 1
        last_end = words[-1]['end']
        unique.append({**segment, 'start': words[0]['start'], 'end': words[-1]['end'],
                       'text': ' '.join(w['word'].strip() for w in words), 'words': words})
    data = {'model': model_name, 'task': task, 'settings': settings(), 'segments': unique}
    write_json(gaps_path(cache, model_name, task), data)
    print(f'  {counter} missed word(s) found in {len(unique)} passage(s).', flush=True)
    return data
