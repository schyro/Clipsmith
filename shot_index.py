"""Shot index: every camera shot of a source with the faces visible in it.

Built from the source scan (source_analysis.py) for the editorial agent, who picks
montage shots and checks framing with it. Each shot lists its time range and, per
face, size, position, sharpness and a character id from clustering SFace identity
embeddings across the whole source. `find` filters it, `write_sheet` shows the result.

    run --shots                                  build / summarise every source in Raw/
    run --shots --character 3 --framing close-up --sharp --min-seconds 0.6 --sheet
"""
from __future__ import annotations
import argparse
import json
import math
import subprocess
from pathlib import Path

INDEX_VERSION = 1
CLOSE_UP = .30           # face height as a share of the frame height
MEDIUM = .14
IDENTITY_FACE = 40       # smallest face side (px at analysis width) trusted for identity
SAME_PERSON = .50        # largest average cosine distance inside one character
MIN_CHARACTER_SHOTS = 3  # smaller clusters stay unidentified
TILE_W, TILE_H, COLUMNS, MAX_TILES = 480, 270, 4, 48


def framing(size):
    return 'close-up' if size >= CLOSE_UP else 'medium' if size >= MEDIUM else 'wide'


def group_faces(samples, gap):
    """Link the faces of a shot's sampled frames into people. `samples` holds, per
    sampled frame, a list of (row number, box); `gap` is the sample spacing in 30 fps frames."""
    from active_speaker import association_cost
    people = []
    for position, faces in enumerate(samples):
        pairs = []
        for d, (_, box) in enumerate(faces):
            for k, person in enumerate(people):
                cost = association_cost(person['box'], box, (position-person['position'])*gap)
                if cost is not None:
                    pairs.append((cost, k, d))
        used_people, used_faces = set(), set()
        for _, k, d in sorted(pairs):
            if k in used_people or d in used_faces:
                continue
            used_people.add(k); used_faces.add(d)
            people[k]['rows'].append(faces[d][0])
            people[k].update(box=faces[d][1], position=position)
        for d, (row, box) in enumerate(faces):
            if d not in used_faces:
                people.append({'rows': [row], 'box': box, 'position': position})
    return [person['rows'] for person in people]


def cluster_identities(vectors, weights):
    """Character number (from 0, most screen time first) per embedding, or None.
    Average-linkage clustering on cosine distance; `weights` is each item's screen time."""
    import numpy as np
    if len(vectors) == 0:
        return []
    if len(vectors) == 1:
        return [None]
    from scipy.cluster.hierarchy import fcluster, linkage
    labels = fcluster(linkage(np.asarray(vectors), 'average', metric='cosine'), SAME_PERSON, 'distance')
    totals, counts = {}, {}
    for label, weight in zip(labels, weights):
        totals[label] = totals.get(label, 0)+weight
        counts[label] = counts.get(label, 0)+1
    order = sorted((label for label in totals if counts[label] >= MIN_CHARACTER_SHOTS), key=lambda l: -totals[l])
    number = {label: n for n, label in enumerate(order)}
    return [number.get(label) for label in labels]


def build(scan, grid):
    """The index as a dict, from a source scan and the source's frame grid."""
    import numpy as np
    from source_analysis import FPS, frame_rate, frame_time
    width, height = scan['width'], scan['height']
    gap = scan['sample_step']/(frame_rate(grid)/FPS)
    rows, sharp, embed = scan['face_row'], scan['face_sharp'], scan['face_embed']
    sample_frames = scan['sample_frame'].tolist()
    face_frames = scan['face_frame'].tolist()
    by_frame = {}
    for number, frame in enumerate(face_frames):
        by_frame.setdefault(frame, []).append(number)
    shots, identities = [], []
    cursor = 0
    total = scan['frame_count']
    for number, (start, end) in enumerate(zip(scan['starts'], scan['ends'])):
        frames = []
        while cursor < len(sample_frames) and sample_frames[cursor] < end:
            frames.append(sample_frames[cursor])
            cursor += 1
        begin = frame_time(grid, start)
        finish = frame_time(grid, end) if end < total else frame_time(grid, total-1)+1/frame_rate(grid)
        faces = []
        for members in group_faces([[(n, rows[n, :4]) for n in by_frame.get(f, [])] for f in frames], gap):
            boxes = rows[members]
            best = max(members, key=lambda n: float(sharp[n])*float(rows[n, 3]))
            face = {'character': None,
                    'size': round(float(np.median(boxes[:, 3]))/height, 3),
                    'center_x': round(float(np.median(boxes[:, 0]+boxes[:, 2]/2))/width, 3),
                    'center_y': round(float(np.median(boxes[:, 1]+boxes[:, 3]/2))/height, 3),
                    'sharpness': round(float(np.median(sharp[members])), 1),
                    'visible': round(len(members)/len(frames), 2),
                    'best_time': round(frame_time(grid, face_frames[best]), 3),
                    'best_box': [round(float(v), 4) for v in (rows[best, 0]/width, rows[best, 1]/height,
                                                              rows[best, 2]/width, rows[best, 3]/height)]}
            face['framing'] = framing(face['size'])
            if scan['embedded']:
                trusted = [n for n in members if min(rows[n, 2], rows[n, 3]) >= IDENTITY_FACE]
                if trusted:
                    unit = embed[trusted]/np.maximum(1e-9, np.linalg.norm(embed[trusted], axis=1, keepdims=True))
                    identities.append((face, unit.mean(axis=0), (finish-begin)*face['visible']))
            faces.append(face)
        faces.sort(key=lambda f: -f['size'])
        shots.append({'shot': number, 'start': round(begin, 3), 'end': round(finish, 3),
                      'seconds': round(finish-begin, 3), 'start_frame': start, 'end_frame': end,
                      'sampled_frames': len(frames), 'faces': faces})
    for (face, _, _), character in zip(identities, cluster_identities([v for _, v, _ in identities],
                                                                      [w for _, _, w in identities])):
        face['character'] = None if character is None else character+1
    # Sharpness is ranked among faces of the same framing, so a soft close-up is not
    # called sharp merely because close-ups have more detail than wide shots.
    for kind in ('close-up', 'medium', 'wide'):
        members = sorted((f for s in shots for f in s['faces'] if f['framing'] == kind), key=lambda f: f['sharpness'])
        for rank, face in enumerate(members):
            face['sharpness_rank'] = round((rank+.5)/len(members), 2)
    characters = {}
    for shot in shots:
        for face in shot['faces']:
            if face['character'] is not None:
                entry = characters.setdefault(face['character'], {'id': face['character'], 'shots': 0, 'seconds': 0.0,
                                                                  'close_ups': 0})
                entry['shots'] += 1
                entry['seconds'] = round(entry['seconds']+shot['seconds']*face['visible'], 2)
                entry['close_ups'] += face['framing'] == 'close-up'
    return {'version': INDEX_VERSION, 'scan_frames': total, 'identities': bool(scan['embedded']),
            'sample_seconds': round(scan['sample_step']/frame_rate(grid), 3),
            'characters': [characters[k] for k in sorted(characters)], 'shots': shots}


def index_path(source_id):
    from shorts import ROOT
    return ROOT/'Review'/source_id/'shot-index.json'


def build_index(source, cache, source_id, force=False):
    """Scan the source if needed, write `Review/<id>/shot-index.json` and return (index, path)."""
    from frame_grid import load_grid
    from shorts import ROOT, write_json
    from source_analysis import SFACE_MODEL, scan
    path = index_path(source_id)
    wanted = (ROOT/SFACE_MODEL).exists()
    if path.exists() and not force:
        index = json.loads(path.read_text(encoding='utf-8'))
        grid = load_grid(source, cache)
        if index.get('version') == INDEX_VERSION and index.get('scan_frames') == len(grid.pts) \
                and (index['identities'] or not wanted):
            return index, path
    grid = load_grid(source, cache)
    index = build(scan(source, cache, force=force, identities=True), grid)
    index.update(source=Path(source).name, source_id=source_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, index)
    return index, path


def find(index, character=None, framing=None, min_seconds=0.0, sharp=False, solo=False, start=None, end=None):
    """(shot, face) pairs matching every given condition, in time order. Without a
    face condition a shot matches through its largest face, or with `face` None when
    it shows nobody."""
    matches = []
    for shot in index['shots']:
        if shot['seconds'] < min_seconds or (start is not None and shot['end'] <= start) \
                or (end is not None and shot['start'] >= end) or (solo and len(shot['faces']) != 1):
            continue
        faces = [f for f in shot['faces']
                 if (character is None or f['character'] == character) and (framing is None or f['framing'] == framing)
                 and (not sharp or f.get('sharpness_rank', 0) >= .5)]
        if faces:
            matches.append((shot, faces[0]))
        elif character is None and framing is None and not sharp and not shot['faces']:
            matches.append((shot, None))
    return matches


def describe(shot, face):
    text = f'shot {shot["shot"]:4}  {shot["start"]:8.2f}-{shot["end"]:8.2f} s  {shot["seconds"]:5.2f} s  faces {len(shot["faces"])}'
    if face:
        who = f'character {face["character"]}' if face['character'] is not None else 'unidentified'
        text += (f'  {who}, {face["framing"]}, size {face["size"]:.2f}, sharp {face.get("sharpness_rank", 0):.2f}, '
                 f'x {face["center_x"]:.2f}, visible {face["visible"]:.0%}')
    return text


def grab(source, seconds):
    command = ['ffmpeg', '-v', 'error', '-ss', f'{seconds:.3f}', '-i', str(source), '-frames:v', '1',
               '-vf', f'scale={TILE_W}:{TILE_H}', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    return subprocess.run(command, capture_output=True, check=True).stdout


def write_sheet(source, matches, dest, labels=None):
    """Contact sheet of (shot, face) pairs: the frame where the face is clearest, the
    face outlined, and the shot's number and time range underneath."""
    from concurrent.futures import ThreadPoolExecutor
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from shorts import ROOT
    matches = matches[:MAX_TILES]
    if not matches:
        return None
    times = [face['best_time'] if face else (shot['start']+shot['end'])/2 for shot, face in matches]
    with ThreadPoolExecutor(8) as pool:
        raws = list(pool.map(lambda t: grab(source, t), times))
    font = ImageFont.truetype(str(ROOT/'assets/fonts/Dosis-Bold.ttf'), 18)
    rows = math.ceil(len(matches)/COLUMNS)
    sheet = Image.new('RGB', (COLUMNS*TILE_W, rows*(TILE_H+28)), '#10151d')
    draw = ImageDraw.Draw(sheet)
    for slot, ((shot, face), raw) in enumerate(zip(matches, raws)):
        x0, y0 = slot % COLUMNS*TILE_W, slot//COLUMNS*(TILE_H+28)
        if len(raw) == TILE_W*TILE_H*3:
            sheet.paste(Image.fromarray(np.frombuffer(raw, np.uint8).reshape(TILE_H, TILE_W, 3)), (x0, y0))
        if face:
            x, y, w, h = face['best_box']
            draw.rectangle([x0+x*TILE_W, y0+y*TILE_H, x0+(x+w)*TILE_W, y0+(y+h)*TILE_H], outline='#ffe14d', width=2)
        label = labels[slot] if labels else ''
        draw.text((x0+6, y0+TILE_H+3), f'{label}shot {shot["shot"]} | {shot["start"]:.2f}-{shot["end"]:.2f} s '
                  f'({shot["seconds"]:.1f} s)', font=font, fill='white')
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest, quality=88)
    return dest


def best_shots(index, character, count):
    """A character's clearest shots: large, sharp, alone, at least half a second."""
    pairs = find(index, character=character, min_seconds=.5) or find(index, character=character)
    pairs.sort(key=lambda p: -(p[1]['size']*(.5+p[1].get('sharpness_rank', 0))*(1 if len(p[0]['faces']) == 1 else .6)))
    return sorted(pairs[:count], key=lambda p: p[0]['start'])


def write_character_sheet(source, index, dest, per_character=4):
    """One sheet that shows who each character id is: its clearest shots, a row each."""
    matches, labels = [], []
    for entry in index['characters'][:MAX_TILES//per_character]:
        picks = best_shots(index, entry['id'], per_character)
        matches += picks
        labels += [f'CHARACTER {entry["id"]} | ']*len(picks)
        while len(matches) % COLUMNS:  # each character starts a new row
            matches.append(matches[-1]); labels.append(labels[-1])
    return write_sheet(source, matches, dest, labels)


def summary(index, path, sheet):
    lines = [f'Shot index: {path}',
             f'  {len(index["shots"])} camera shots, faces sampled every {index["sample_seconds"]} s']
    if not index['identities']:
        lines.append('  No character ids: the SFace model is not installed (docs/shot-index.md).')
    for entry in index['characters'][:12]:
        lines.append(f'  character {entry["id"]:2}: {entry["shots"]:4} shots, {entry["seconds"]:7.1f} s on screen, '
                     f'{entry["close_ups"]} close-ups')
    if sheet:
        lines.append(f'Character sheet: {sheet}')
    return lines


def main(argv=None):
    from shorts import ROOT, raw_sources, source_id
    parser = argparse.ArgumentParser(prog='run --shots', description='Build and query the shot index of a source.')
    parser.add_argument('--input', type=Path, help='Default: every video in the Raw folder')
    parser.add_argument('--character', type=int, help='Only shots that show this character id')
    parser.add_argument('--framing', choices=['close-up', 'medium', 'wide'], help='Size of the matching face')
    parser.add_argument('--sharp', action='store_true', help='Only faces in the sharper half of their framing')
    parser.add_argument('--solo', action='store_true', help='Only shots with exactly one face')
    parser.add_argument('--min-seconds', type=float, default=0.0, help='Shortest shot to list')
    parser.add_argument('--from', dest='start', type=float, help='Only shots after this source time (seconds)')
    parser.add_argument('--to', dest='end', type=float, help='Only shots before this source time (seconds)')
    parser.add_argument('--limit', type=int, default=60, help='Most shots to list (default 60)')
    parser.add_argument('--sheet', action='store_true', help='Also write a contact sheet of the listed shots')
    parser.add_argument('--rebuild', action='store_true', help='Scan the source again')
    args = parser.parse_args(argv)
    query = any([args.character is not None, args.framing, args.sharp, args.solo, args.min_seconds,
                 args.start is not None, args.end is not None])
    sources = [args.input.resolve()] if args.input else raw_sources()
    if not sources:
        raise ValueError('No videos were found in the Raw folder.')
    for source in sources:
        fingerprint = source_id(source)
        cache = ROOT/'work'/fingerprint
        cache.mkdir(parents=True, exist_ok=True)
        index, path = build_index(source, cache, fingerprint, args.rebuild)
        folder = path.parent/'shots'
        if not query:
            sheet = folder/'characters.jpg'
            if index['characters'] and (args.rebuild or not sheet.exists()):
                write_character_sheet(source, index, sheet)
            print('\n'.join(summary(index, path, sheet if sheet.exists() else None)), flush=True)
            continue
        matches = find(index, args.character, args.framing, args.min_seconds, args.sharp, args.solo, args.start, args.end)
        print(f'{source.name}: {len(matches)} matching shot(s)' +
              (f', first {args.limit} listed' if len(matches) > args.limit else ''), flush=True)
        for shot, face in matches[:args.limit]:
            print('  '+describe(shot, face), flush=True)
        if args.sheet and matches:
            parts = [f'character-{args.character}' if args.character is not None else '', args.framing or '',
                     'sharp' if args.sharp else '', 'solo' if args.solo else '',
                     f'min{args.min_seconds:g}s' if args.min_seconds else '',
                     f'from{args.start:g}' if args.start is not None else '', f'to{args.end:g}' if args.end is not None else '']
            dest = folder/('query-'+'-'.join(p for p in parts if p)+'.jpg')
            print(f'Contact sheet (first {min(len(matches), args.limit, MAX_TILES)}): '
                  f'{write_sheet(source, matches[:args.limit], dest)}', flush=True)
    return 0
