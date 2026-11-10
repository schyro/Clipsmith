"""FFmpeg filter construction kept separate for integration checks."""


TARGET_I = -16.0       # integrated loudness, LUFS
TARGET_TP = -1.5       # true-peak ceiling, dBTP
TARGET_LRA = 11.0
# The limiter works on sample peaks, so it keeps a little room below the true-peak ceiling.
LIMITER_CEILING_DB = -2.0
# Fallback only: when a quiet clip cannot reach the target within the true-peak limit by gain
# alone, its peaks may be limited by at most this much; beyond that the clip ends up quieter.
MAX_LIMITED_DB = 6.0


def measure_loudness(source, start, duration):
    """First loudnorm pass over the clip's exact range: the measured values, or None for silence."""
    import json
    import subprocess
    result = subprocess.run(
        ['ffmpeg', '-hide_banner', '-nostats', '-ss', str(start), '-t', str(duration), '-i', str(source),
         '-map', '0:a:0', '-vn',
         '-af', f'loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json', '-f', 'null', '-'],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:])
    text = result.stderr
    raw = json.loads(text[text.rindex('{'):text.rindex('}')+1])
    values = {k: float(raw[k]) for k in ('input_i', 'input_tp', 'input_lra', 'input_thresh', 'target_offset')}
    if not all(abs(v) != float('inf') and v == v for v in values.values()) or values['input_i'] < -69:
        return None
    return {k: round(v, 2) for k, v in values.items()}


def loudness_gain(measured):
    """(gain in dB, limited dB) the clip gets: full gain to the target, capped so the limiter
    never has to take more than MAX_LIMITED_DB off the peaks."""
    wanted = TARGET_I-measured['input_i']
    headroom = TARGET_TP-measured['input_tp']
    return min(wanted, headroom+MAX_LIMITED_DB), max(0.0, min(wanted, headroom+MAX_LIMITED_DB)-headroom)


def audio_filters(duration, settings, measured=None):
    """Clip audio chain. With `measured` (measure_loudness of the same range) the level is set
    by a static gain, so the dialogue keeps its dynamics. Without it, a single dynamic
    loudnorm pass is used (only meant for previews)."""
    fade = min(float(settings.get('audio_fade_ms', 20))/1000, duration/2)
    if measured is None:
        level = f'loudnorm=I={TARGET_I:g}:TP={TARGET_TP:g}:LRA={TARGET_LRA:g}'
    else:
        gain, limited = loudness_gain(measured)
        if limited <= 0:
            # Reachable linearly: loudnorm applies one constant gain.
            level = (f'loudnorm=I={TARGET_I:g}:TP={TARGET_TP:g}:LRA={TARGET_LRA:g}:'
                     f'measured_I={measured["input_i"]}:measured_TP={measured["input_tp"]}:'
                     f'measured_LRA={measured["input_lra"]}:measured_thresh={measured["input_thresh"]}:'
                     f'offset={measured["target_offset"]}:linear=true')
        else:
            # Not reachable within the true-peak limit (quiet clip with a few loud peaks): loudnorm
            # would fall back to dynamic mode, so apply the gain and limit the peaks instead.
            level = (f'volume={gain:.2f}dB,'
                     f'alimiter=limit={10**(LIMITER_CEILING_DB/20):.4f}:attack=5:release=50:level=disabled')
    return (f'atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,{level},'
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
