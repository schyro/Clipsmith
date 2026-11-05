"""Validate current-profile exports and build a visual review sheet."""
import argparse
import json
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
from shorts import ROOT,probe,run,settings,write_json


def seconds(value):
    h,m,s=value.replace(',','.').split(':')
    return int(h)*3600+int(m)*60+float(s)


def check_intervals(intervals,label):
    if any(a<0 or b<=a for a,b in intervals):
        raise ValueError(f'Invalid caption interval: {label}')
    if any(a[1]>b[0]+.00001 for a,b in zip(intervals,intervals[1:])):
        raise ValueError(f'Overlapping captions: {label}')


def verify_folder(folder,config):
    clips=json.loads((folder/'clips.json').read_text(encoding='utf-8'))
    plan=json.loads((folder/'plan.json').read_text(encoding='utf-8'))
    cache=ROOT/'work'/plan['source_id']
    font=ImageFont.truetype(str(ROOT/'assets/fonts/Dosis-Bold.ttf'),20)
    canvas=Image.new('RGB',(5*300,((len(clips)+4)//5)*440),'#10151d')
    draw=ImageDraw.Draw(canvas)
    qa=cache/'qa-current'
    qa.mkdir(exist_ok=True)
    rows=[]
    for i,clip in enumerate(clips):
        path=folder/clip['file']
        data=probe(path)
        video=next(s for s in data['streams'] if s['codec_type']=='video')
        audio=next(s for s in data['streams'] if s['codec_type']=='audio')
        assert (video['width'],video['height'])==(config['width'],config['height'])
        assert video['avg_frame_rate']=='30/1' and audio['codec_name']=='aac'
        duration=clip['end']-clip['start']
        assert abs(float(data['format']['duration'])-duration)<.3
        ass=cache/(path.stem+'-en.ass')
        content=ass.read_text(encoding='utf-8-sig')
        events=[line for line in content.splitlines() if line.startswith('Dialogue:')]
        intervals=[tuple(map(seconds,line.split(',')[1:3])) for line in events]
        check_intervals(intervals,ass)
        position=f'\\an5\\pos({config["width"]//2},{config["subtitle_y"]})'
        assert events and all(position in e and '\\fade(' in e for e in events)
        assert max(b for a,b in intervals)<=duration+.011
        srt=path.with_suffix('.en.srt')
        srt_intervals=[tuple(map(seconds,line.split(' --> '))) for line in srt.read_text(encoding='utf-8').splitlines() if ' --> ' in line]
        check_intervals(srt_intervals,srt)
        for label,time in [('start',.8),('middle',duration/2),('end',duration-.7)]:
            run(['ffmpeg','-v','error','-y','-ss',min(max(0,time),duration-.01),'-i',path,
                 '-frames:v','1','-vf','scale=324:405','-update','1',qa/f'{i+1:02}-{label}.jpg'])
        thumbnail=Image.open(path.with_suffix('.jpg')).resize((284,355))
        x,y=(i%5)*300+8,(i//5)*440+8
        canvas.paste(thumbnail,(x,y))
        draw.text((x,y+365),f'{i+1:02} | {duration:.1f}s',font=font,fill='white')
        rows.append({'file':path.name,'duration':float(data['format']['duration']),
                     'caption_events':len(events),'caption_overlap':False,'caption_animation':True})
    canvas.save(folder/'preview.jpg',quality=92)
    for page in range((len(clips)+4)//5):
        sheet=Image.new('RGB',(972,440*5),'#10151d')
        d=ImageDraw.Draw(sheet)
        for row,index in enumerate(range(page*5,min(page*5+5,len(clips)))):
            for col,label in enumerate(['start','middle','end']):
                sheet.paste(Image.open(qa/f'{index+1:02}-{label}.jpg'),(col*324,row*440))
                d.text((col*324+8,row*440+409),f'{index+1:02} {label}',font=font,fill='white')
        sheet.save(qa/f'sheet-{page+1:02}.jpg',quality=92)
    write_json(folder/'validation.json',{'clips':rows,'profile':config['profile'],
               'audio_fade_ms':config['audio_fade_ms'],'caption_overlap_check':'passed',
               'caption_position':[config['width']//2,config['subtitle_y']],
               'decode_check':'Every MP4 was fully decoded before its completion signature was written.'})
    print(f'Validated {len(rows)} clips: {folder}')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder',type=Path)
    args=parser.parse_args()
    config=settings()
    folders=[args.folder] if args.folder else sorted((ROOT/'Output').glob('*/'+config['profile']))
    if not folders:
        raise ValueError('No output folders match the current profile.')
    for folder in folders:
        verify_folder(folder,config)


if __name__=='__main__':
    main()
