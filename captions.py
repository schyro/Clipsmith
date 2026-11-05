"""Word-aligned English ASS captions and portable SRT sidecars."""
from pathlib import Path
import re


def clip_words(transcript, clip):
    words = []
    pending = []
    for segment in transcript['segments']:
        if not segment.get('words') and segment['end'] > clip['start'] and segment['start'] < clip['end']:
            raise ValueError('Word timestamps are missing; transcribe again with word_timestamps enabled.')
        for word in segment.get('words') or []:
            start, end = max(0, word['start']-clip['start']), min(clip['end']-clip['start'], word['end']-clip['start'])
            text = word['word'].strip()
            if not text:
                continue
            if end > start:
                words.append({'start':start,'end':end,'word':' '.join(pending+[text])})
                pending = []
            elif clip['start'] <= word['start'] < clip['end'] and word['end']==word['start']:
                # Whisper sometimes assigns a zero-width timestamp to a short
                # spoken word. Reveal it with the next word instead of losing it.
                pending.append(text)
    if pending and words:
        words[-1]['word'] += ' '+' '.join(pending)
    # ASS uses centiseconds. Merge simultaneous onsets before scheduling a
    # single caption lane; overlapping ASR segments must not create two lines.
    ordered = []
    for word in sorted(words,key=lambda w:(w['start'],w['end'])):
        if ordered and round(word['start']*100)==round(ordered[-1]['start']*100):
            ordered[-1]['word'] += ' '+word['word']
            ordered[-1]['end'] = max(ordered[-1]['end'],word['end'])
        else:
            ordered.append(word)
    return ordered


def caption_groups(words, max_words=5):
    result, group = [], []
    for word in words:
        if group and (len(group) >= max_words or word['end']-group[0]['start'] > 2.4
                      or word['start']-group[-1]['end'] > .45
                      or len(' '.join(w['word'] for w in group+[word])) > 34):
            result.append(group)
            group = []
        group.append(word)
        if re.search(r'[.!?]["\u201d\u2019]*$', word['word']):
            result.append(group)
            group = []
    if group:
        result.append(group)
    return result


def ass_stamp(t):
    cs = max(0, round(t*100))
    return f'{cs//360000}:{cs//6000%60:02}:{cs//100%60:02}.{cs%100:02}'


def safe(text):
    return text.replace('\\','∖').replace('{','(').replace('}',')').replace('\n',' ')


def animation_tags(start_cs, end_cs, group_cs, is_last, settings):
    """Continue one entrance envelope across successive word events."""
    elapsed=(start_cs-group_cs)*10
    duration=(end_cs-start_cs)*10
    fade_in=max(0,int(settings.get('caption_fade_in_ms',70)))
    fade_out=max(0,int(settings.get('caption_fade_out_ms',40))) if is_last else 0
    remaining=max(0,fade_in-elapsed)
    alpha=round(255*remaining/fade_in) if fade_in else 0
    in_end=min(remaining,duration)
    middle=round(255*max(0,fade_in-elapsed-in_end)/fade_in) if fade_in else 0
    fade_out=min(fade_out,max(0,(duration-in_end)//2))
    tags=f'\\fade({alpha},{middle},{255 if fade_out else middle},0,{in_end},{duration-fade_out},{duration})'
    settle=max(0,int(settings.get('caption_settle_ms',100)))
    initial=float(settings.get('caption_start_scale',98))
    if elapsed<settle and settle:
        scale=initial+(100-initial)*elapsed/settle
        tags+=f'\\fscx{scale:.2f}\\fscy{scale:.2f}\\t(0,{settle-elapsed},\\fscx100\\fscy100)'
    return tags


def write_captions(transcript, clip, ass_path: Path, srt_path: Path, settings):
    if transcript.get('language') != 'en':
        raise ValueError('English captions require an English transcript or translation.')
    words = clip_words(transcript,clip)
    groups = caption_groups(words,settings['max_caption_words'])
    if not words:
        raise ValueError('No timestamped speech was found in this clip.')
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: {settings['width']}
PlayResY: {settings['height']}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{settings['font']},{settings['font_size']},&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    events = []
    scheduled=[]
    offset=0
    clip_end_cs=round((clip['end']-clip['start'])*100)
    for group in groups:
        for i, word in enumerate(group):
            # Reveal words on onset; previous words remain in this short group.
            index=offset+i
            start_cs=round(word['start']*100)
            next_cs=round(words[index+1]['start']*100) if index+1<len(words) else clip_end_cs
            end_cs=min(round((word['end']+(.15 if i+1<len(group) else .12))*100),next_cs,clip_end_cs)
            if end_cs <= start_cs:
                continue
            before = ' '.join(safe(w['word']) for w in group[:i])
            current = '{\\c'+settings['highlight']+'&}'+safe(word['word'])+'{\\c&H00FFFFFF&}'
            separator = '' if word['word'].startswith('-') else ' '
            animation=animation_tags(start_cs,end_cs,round(group[0]['start']*100),i==len(group)-1,settings)
            text = '{\\an5\\pos('+str(settings['width']//2)+','+str(settings['subtitle_y'])+')'+animation+'}' + (before+separator+current).strip()
            scheduled.append((start_cs,end_cs))
            events.append(f"Dialogue: 0,{ass_stamp(start_cs/100)},{ass_stamp(end_cs/100)},Default,,0,0,0,,{text}")
        offset+=len(group)
    if any(a[1]>b[0] for a,b in zip(scheduled,scheduled[1:])):
        raise ValueError('Caption scheduling produced overlapping events.')
    ass_path.write_text(header+'\n'.join(events)+'\n',encoding='utf-8-sig')
    def stamp(t):
        ms = max(0,round(t*1000))
        return f'{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}'
    duration = clip['end']-clip['start']
    srt_entries=[]
    for i,group in enumerate(groups):
        end=min(duration,group[-1]['end']+.12,groups[i+1][0]['start'] if i+1<len(groups) else duration)
        if end>group[0]['start']:
            srt_entries.append(f'{len(srt_entries)+1}\n{stamp(group[0]["start"])} --> {stamp(end)}\n'+' '.join(w['word'] for w in group))
    srt_path.write_text('\n\n'.join(srt_entries)+'\n',encoding='utf-8')
    return len(events)
