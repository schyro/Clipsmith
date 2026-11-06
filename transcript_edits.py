"""Caption corrections on top of the transcript, without changing it.

`work/<id>/caption-edits-<model>.json`:

    {"model": "medium",
     "edits": [{"index": 123, "from": "Tune.", "to": "June."},
               {"index": 124, "from": "on", "start": 61.20, "end": 61.42, "line": "break"},
               {"index": "g7", "from": "God.", "to": ""}],
     "added": [{"id": "u1", "start": 335.96, "end": 336.40, "word": "God."}]}

An edit names a word by `index`: the zero-based position of a transcript word,
or the id of a word the missed-speech pass found ("g7"). `from` is that word's
recognized text and must match. Optional fields: `to` (new text; empty removes
the word), `start`/`end` (new timing in source seconds) and `line` ("break"
starts a new caption line at the word, "join" keeps it on the previous line).
`added` holds words typed in or accepted in the clip editor, with ids "u1"...
"""
from __future__ import annotations
import copy
import math

MAX_WORD = 64
MAX_WORD_SECONDS = 10.0
LINES = ('break', 'join')
FIELDS = ('to', 'start', 'end', 'line')


def empty_edits(model):
    return {'model': model, 'edits': [], 'added': []}


def merged_transcript(base, gaps, edits, model):
    """The transcript with missed-speech words, corrections and added words.
    Every word carries `index`: its transcript position, or a "g"/"u" id."""
    result = copy.deepcopy(base)
    raw = [w for s in result['segments'] for w in s.get('words', [])]
    for i, word in enumerate(raw):
        word['index'] = i
    extra = []
    for segment in (gaps or {}).get('segments', []):
        segment = copy.deepcopy(segment)
        for word in segment['words']:
            word['index'] = word.pop('id')
        extra.append(segment)
    by_index = {w['index']: w for w in raw}
    by_index.update({w['index']: w for s in extra for w in s['words']})
    if edits:
        if edits.get('model') != model:
            raise ValueError('Caption corrections belong to a different model.')
        for edit in edits.get('edits', []):
            word = by_index.get(edit['index'])
            if word is None or word['word'].strip() != edit['from']:
                raise ValueError(f'Caption correction source word mismatch: {edit}')
            if 'to' in edit:
                word['word'] = ' '+edit['to']
            for key in ('start', 'end', 'line'):
                if key in edit:
                    word[key] = edit[key]
        for item in edits.get('added', []):
            word = {'index': item['id'], 'start': item['start'], 'end': item['end'], 'word': ' '+item['word'].strip()}
            if item.get('line'):
                word['line'] = item['line']
            extra.append({'start': item['start'], 'end': item['end'], 'text': item['word'].strip(), 'words': [word]})
    result['segments'] = sorted(result['segments']+extra, key=lambda s: s['start'])
    return result


def _check_text(text):
    if not isinstance(text, str) or len(text) > MAX_WORD or any(c in text for c in '\r\n\t'):
        raise ValueError(f'A word must be text of at most {MAX_WORD} characters on one line.')
    return text.strip()


def _check_time(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f'{name} must be a time in seconds.')
    return round(float(value), 3)


def _check_span(start, end):
    if not 0 < end-start <= MAX_WORD_SECONDS:
        raise ValueError(f'A word must last between 0 and {MAX_WORD_SECONDS:g} seconds.')


def update_edit(edits, index, original, changes):
    """Change one transcript or missed-speech word (in place). `original` is its
    unedited {'word', 'start', 'end'}; `changes` may set `to`, `start`, `end` and
    `line`, where None restores the original. An edit that changes nothing is dropped."""
    unknown = set(changes)-set(FIELDS)
    if unknown:
        raise ValueError(f'Unknown caption fields: {sorted(unknown)}')
    current = next((e for e in edits['edits'] if e['index'] == index), None)
    entry = {k: v for k, v in (current or {}).items() if k in FIELDS}
    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        elif key == 'to':
            entry['to'] = _check_text(value)
        elif key == 'line':
            if value not in LINES:
                raise ValueError(f'line must be one of {list(LINES)} or null.')
            entry['line'] = value
        else:
            entry[key] = _check_time(value, key)
    if 'start' in entry or 'end' in entry:
        _check_span(entry.get('start', original['start']), entry.get('end', original['end']))
    word = original['word'].strip()
    if entry.get('to') == word:
        entry.pop('to')
    for key in ('start', 'end'):
        if key in entry and key in original and abs(entry[key]-original[key]) < 5e-4:
            entry.pop(key)
    rest = [e for e in edits['edits'] if e['index'] != index]
    if entry:
        rest.append({'index': index, 'from': word, **entry})
    edits['edits'] = sorted(rest, key=lambda e: (isinstance(e['index'], str), str(e['index']).zfill(12)))
    return edits


def update_added(edits, word_id, changes):
    """Change or (with an empty `to`) remove a word added in the editor."""
    items = edits.setdefault('added', [])
    item = next((a for a in items if a['id'] == word_id), None)
    if item is None:
        raise ValueError('Unknown added word.')
    unknown = set(changes)-set(FIELDS)
    if unknown:
        raise ValueError(f'Unknown caption fields: {sorted(unknown)}')
    if 'to' in changes and not _check_text(changes['to'] or ''):
        edits['added'] = [a for a in items if a['id'] != word_id]
        return edits
    updated = dict(item)
    for key, value in changes.items():
        if key == 'to':
            updated['word'] = _check_text(value)
        elif key == 'line':
            if value is not None and value not in LINES:
                raise ValueError(f'line must be one of {list(LINES)} or null.')
            updated.pop('line', None)
            if value:
                updated['line'] = value
        elif value is not None:
            updated[key] = _check_time(value, key)
    _check_span(updated['start'], updated['end'])
    item.clear()
    item.update(updated)
    return edits


def add_words(edits, words):
    """Append words typed or accepted in the editor; returns their new ids."""
    items = edits.setdefault('added', [])
    if not isinstance(words, list) or not 0 < len(words) <= 100:
        raise ValueError('Add between 1 and 100 words.')
    used = [int(a['id'][1:]) for a in items if a['id'][1:].isdigit()]
    number = max(used, default=0)
    ids = []
    for word in words:
        if not isinstance(word, dict):
            raise ValueError('Each added word needs start, end and word.')
        text = _check_text(word.get('word', ''))
        if not text:
            raise ValueError('An added word cannot be empty.')
        start, end = _check_time(word.get('start'), 'start'), _check_time(word.get('end'), 'end')
        _check_span(start, end)
        number += 1
        item = {'id': f'u{number}', 'start': start, 'end': end, 'word': text}
        if word.get('line') in LINES:
            item['line'] = word['line']
        items.append(item)
        ids.append(item['id'])
    items.sort(key=lambda a: a['start'])
    return ids
