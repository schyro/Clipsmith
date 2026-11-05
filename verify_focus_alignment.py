"""Compare decoded crop pixels with exact source frames around a camera cut."""
import argparse
import json
from pathlib import Path
import subprocess
import numpy as np
from shorts import ROOT,english_transcript,settings,write_json
from speaker_focus import focus_plan


def decoded_frames(command,width,height):
    result=subprocess.run(command,capture_output=True,check=True)
    return np.frombuffer(result.stdout,np.uint8).reshape(-1,height,width,3)


def verify_alignment(folder,clip_number,cut_frame,radius=3):
    config=settings()
    manifest=json.loads((folder/'plan.json').read_text(encoding='utf-8'))
    clips=json.loads((folder/'clips.json').read_text(encoding='utf-8'))
    clip=clips[clip_number-1]
    source=ROOT/'Raw'/manifest['source']
    cache=ROOT/'work'/manifest['source_id']
    focus=focus_plan(source,clip,cache,config,english_transcript(source,cache,'medium'))
    first=max(0,cut_frame-radius)
    last=min(focus['frame_count']-1,cut_frame+radius)
    select=f"select='between(n,{first},{last})',setpts=PTS-STARTPTS"
    sw,sh=focus['scaled_width'],focus['scaled_height']
    w,h=config['width'],config['height']
    raw=decoded_frames(['ffmpeg','-v','error','-ss',str(clip['start']),'-i',str(source),'-t',str(clip['end']-clip['start']),
        '-vf',f'setpts=PTS-STARTPTS,fps=30,scale={sw}:{sh},'+select,
        '-frames:v',str(last-first+1),'-an','-f','rawvideo','-pix_fmt','rgb24','-'],sw,sh)
    rendered=decoded_frames(['ffmpeg','-v','error','-i',str(folder/clip['file']),'-vf',select,
        '-frames:v',str(last-first+1),'-an','-f','rawvideo','-pix_fmt','rgb24','-'],w,h)
    if len(raw)!=last-first+1 or len(rendered)!=len(raw):
        raise ValueError('Decoded frame counts do not match the requested comparison window.')
    mask=np.ones((h,w),bool)
    mask[max(0,config['subtitle_y']-150):min(h,config['subtitle_y']+150)]=False
    rows=[]
    for offset,index in enumerate(range(first,last+1)):
        center=focus['frames'][index]['center_x']
        x=max(0,min(sw-w,round((center*sw-w/2)/2)*2))
        y=(sh-h)//2
        expected=raw[offset,y:y+h,x:x+w]
        error=float(np.abs(expected.astype(np.int16)-rendered[offset].astype(np.int16))[mask].mean())
        if error>10:
            raise ValueError(f'Source/render crop alignment failed on frame {index}: MAE={error:.3f}')
        rows.append({'frame':index,'seconds':index/30,'center_x':center,'pixel_mae':round(error,4)})
    report={'clip':clip['file'],'cut_frame':cut_frame,'source_alignment':'passed','frames':rows}
    write_json(folder/f'cut-alignment-{clip_number:02}-{cut_frame}.json',report)
    print(json.dumps(report,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder',type=Path)
    parser.add_argument('--clip',type=int,default=1)
    parser.add_argument('--frame',type=int,required=True)
    args=parser.parse_args()
    folder=args.folder or sorted((ROOT/'Output').glob('*/'+settings()['profile']))[0]
    verify_alignment(folder,args.clip,args.frame)


if __name__=='__main__':
    main()
