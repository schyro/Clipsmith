"""Build a compact editorial bundle for the AI agent conducting the review."""
from pathlib import Path
import json
import math


def prepare_review(source, fingerprint, cache, transcript, duration, plan_path):
    from PIL import Image, ImageDraw, ImageFont
    from shorts import ROOT, run, sentences
    folder = ROOT/'Review'/fingerprint
    folder.mkdir(parents=True,exist_ok=True)
    sheets = []
    font = ImageFont.truetype(str(ROOT/'assets/fonts/Dosis-Bold.ttf'),22)
    times = list(range(0,math.ceil(duration),30))
    for page in range(math.ceil(len(times)/20)):
        sheet = folder/f'contact-{page+1:02}.jpg'
        if not sheet.exists():
            canvas = Image.new('RGB',(1920,5*306),'#141923')
            draw = ImageDraw.Draw(canvas)
            for slot,second in enumerate(times[page*20:(page+1)*20]):
                frame = folder/f'frame-{second:05}.jpg'
                if not frame.exists():
                    run(['ffmpeg','-v','error','-y','-ss',second,'-i',source,'-frames:v','1','-vf','scale=480:270','-update','1',frame])
                x,y = slot%4*480,slot//4*306
                canvas.paste(Image.open(frame),(x,y))
                draw.text((x+12,y+276),f'{second//60:02}:{second%60:02} / {second}s',font=font,fill='white')
            canvas.save(sheet,quality=90)
        sheets.append(str(sheet))
    lines = [f'# {source.name}',f'Source ID: {fingerprint}',f'Duration: {duration:.3f} seconds',
             f'English word timestamps: {cache / ("transcript-"+transcript["model"]+("-english" if transcript.get("task")=="translate" else "")+".json")}',
             f'Plan destination: {plan_path}',
             'Review the complete dialogue and contact sheets, one block at a time. Select every moment with short-form '
             'potential that makes sense on its own: funny, emotional, motivational or inspiring, tense, surprising, '
             'clever, quotable or debatable, relatable, a strong character moment, or spectacle. The user picks the '
             'winners, so include a moment when in doubt. There is no fixed clip count.',
             'Most clips run 20-60 s, ideally 30-50 s; a one-line payoff can be 10-20 s; never more than 90 s unless the '
             'user asks. Start on the hook and end right after the payoff. Full rules: AGENTS.md, step 2.',
             'Give every block below a "coverage" entry in the plan, {"start": <block start>, "note": "..."}, plus '
             '"why_not_more" where AGENTS.md, step 3 asks for it; the render refuses a plan without them.',
             'Check boundaries against word timing and nearby frames; do not cut off setup, payoff or a spoken word.',
             'Framing always fills 4:5 without widening or padding. Local Light-ASD scores synchronized audio/face tracks. Review uncertain speaker choices and use focus_overrides with exact clip-relative start_frame/end_frame (30 fps), center_x and optional center_x_end. Never cross a camera cut or target an offscreen face early. Per-clip focus sidecars list exact cut frames.',
             f'Caption corrections: {cache/("caption-edits-"+transcript["model"]+".json")}. Schema: {{"model":"'+transcript['model']+'","edits":[{"index":0,"from":"original stripped word","to":"corrected word"}]}. Use each word\'s "index" from words.json: a number for transcript words, a "g" id for words the missed-speech pass found. Preserve word times.',
             '', '## Contact sheets',*sheets,'','## Full English dialogue']
    from selection import block_label, block_start
    block = None
    for unit in sentences(transcript):
        # Five-minute headings; the render's coverage check uses the same blocks.
        if block_start(unit['start'], duration) != block:
            block = block_start(unit['start'], duration)
            lines += ['', f'### Block {block_label(block, duration)} (coverage start {block})', '']
        lines.append(f'{unit["start"]:.2f}–{unit["end"]:.2f}: {unit["text"]}')
    review_path = folder/'review.md'
    review_path.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    words = [w for s in transcript['segments'] for w in s.get('words',[])]
    words.sort(key=lambda w: w['start'])
    (folder/'words.json').write_text(json.dumps([{**w,'index':w.get('index',i)} for i,w in enumerate(words)],ensure_ascii=False,indent=2),encoding='utf-8')
    selection = json.loads(plan_path.read_text(encoding='utf-8-sig')).get('selection') if plan_path.exists() else None
    entry = {'source':str(source),'source_id':fingerprint,'duration':duration,'review':str(review_path),
             'contact_sheets':sheets,'plan':str(plan_path),'selection':selection,
             'needs_editorial_review':selection not in ('editor-reviewed','agent-reviewed')}
    print(f'Review ready: {review_path}',flush=True)
    return entry
