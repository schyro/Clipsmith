"""FFmpeg filter construction kept separate for integration checks."""


def audio_filters(duration,settings):
    fade=min(float(settings.get('audio_fade_ms',20))/1000,duration/2)
    return (f'atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,'
            f'loudnorm=I=-16:TP=-1.5:LRA=11,'
            f'afade=t=in:st=0:d={fade:.6f},'
            f'afade=t=out:st={duration-fade:.6f}:d={fade:.6f}')


def video_filters(plan,clip,settings,ass_path,font_path):
    from shorts import filter_path
    w,h=settings['width'],settings['height']
    return (f'[0:v]setpts=PTS-STARTPTS,fps=30,'
            f"scale={plan['scaled_width']}:{plan['scaled_height']},"
            f"sendcmd=filename='{filter_path(plan['commands_path'])}',"
            f'crop@focus=w={w}:h={h}:x=0:y=(ih-oh)/2,setsar=1,'
            f"ass=filename='{filter_path(ass_path)}':fontsdir='{filter_path(font_path)}',format=yuv420p[v]")
