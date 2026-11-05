"""Contact sheet of framing decisions, for reviewing speaker focus quickly."""
import subprocess
import numpy as np

TILE_W, TILE_H = 480, 270
COLUMNS = 4
MAX_TILES = 48


def sample_frames(focus):
    """Middle of every shot, plus both sides of every focus change."""
    picks = []
    frames = focus['frames']
    for number, shot in enumerate(focus['shots'], 1):
        a, b = shot['start_frame'], shot['end_frame']
        label = 'REVIEW: '+'; '.join(shot['review']) if shot['review'] else ''
        picks.append((a+(b-a)//2, f'shot {number} mid', label))
        for i in range(a+1, b):
            if frames[i]['track'] != frames[i-1]['track'] and abs(frames[i]['center_x']-frames[i-1]['center_x']) >= .05:
                picks.append((max(a, i-6), f'shot {number} before change', label))
                picks.append((min(b-1, i+6), f'shot {number} after change', label))
    unique = {}
    for pick in picks:
        unique.setdefault(pick[0], pick)
    return [unique[k] for k in sorted(unique)][:MAX_TILES]


def write_focus_sheet(source, clip, focus, dest, output_width):
    from PIL import Image, ImageDraw, ImageFont
    from shorts import ROOT
    picks = sample_frames(focus)
    if not picks:
        return None
    select = '+'.join(f'eq(n\\,{p[0]})' for p in picks)
    # -t must limit the input: select drops the frames after the last pick, so an
    # output limit is never reached and ffmpeg would decode the rest of the source.
    command = ['ffmpeg', '-v', 'error', '-ss', str(clip['start']), '-t', str(clip['end']-clip['start']),
               '-i', str(source), '-an',
               '-vf', f"setpts=PTS-STARTPTS,fps=30,select='{select}',scale={TILE_W}:{TILE_H}",
               '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    raw = subprocess.run(command, capture_output=True, check=True).stdout
    images = np.frombuffer(raw, np.uint8).reshape(-1, TILE_H, TILE_W, 3)
    font = ImageFont.truetype(str(ROOT/'assets/fonts/Dosis-Bold.ttf'), 18)
    rows = (len(images)+COLUMNS-1)//COLUMNS
    sheet = Image.new('RGB', (COLUMNS*TILE_W, rows*(TILE_H+26)), '#10151d')
    draw = ImageDraw.Draw(sheet)
    crop_w = TILE_W*output_width/focus['scaled_width']
    for slot, (image, (frame, name, flag)) in enumerate(zip(images, picks)):
        x0, y0 = slot % COLUMNS*TILE_W, slot//COLUMNS*(TILE_H+26)
        sheet.paste(Image.fromarray(image), (x0, y0))
        decision = focus['frames'][frame]
        left = max(0, min(TILE_W-crop_w, decision['center_x']*TILE_W-crop_w/2))
        colour = '#ff5a5a' if flag else '#ffe14d'
        draw.rectangle([x0+left, y0, x0+left+crop_w, y0+TILE_H-1], outline=colour, width=3)
        draw.text((x0+6, y0+TILE_H+3), f'f{frame} {name} | {decision["mode"]} | track {decision["track"]}',
                  font=font, fill='white')
        if flag:
            draw.text((x0+6, y0+4), flag, font=font, fill=colour)
    dest.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(dest, quality=88)
    return dest

