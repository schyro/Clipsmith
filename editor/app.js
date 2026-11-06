// Clip editor front end (Preact + htm, vendored; no build step).
import { h, render } from 'preact';
import { useState, useEffect, useLayoutEffect, useRef, useMemo, useCallback } from 'preact/hooks';
import htm from 'htm';

const html = htm.bind(h);
const FPS = 30;
const STATUS_LABELS = { pending: 'Pending', approved: 'Approved', needs_changes: 'Needs changes', rejected: 'Rejected' };
const CAPTION_EM = 0.82; // CSS em per ASS font size unit for Dosis (libass sizes by line height)

// ---------- helpers ----------

async function request(path, body) {
  const options = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const response = await fetch('/api/' + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const frameOf = (t, start) => Math.floor((t - start) * FPS + 1e-6);
const CONTEXTS = [3, 30, 120, 600]; // timeline padding around the cut, in seconds

// The source's own frames and the render's 30 fps grid (same rules as frame_grid.py).
// A render of a cut starts at the first source frame at or after its start (the
// origin); source frame i first appears at grid frame round((pts[i] - pts[origin]) * 30),
// and grid frame n shows the last source frame whose slot is at most n. The proxy keeps
// the source frames and timestamps, so the frame on screen is known exactly.
function makeGrid(data) {
  const [num, den] = data.time_base;
  const pts = data.pts;
  const offset = data.start_time * den / num;
  const firstAtLeast = (x) => { let lo = 0, hi = pts.length; while (lo < hi) { const m = (lo + hi) >> 1; if (pts[m] < x) lo = m + 1; else hi = m; } return lo; };
  const firstAbove = (x) => { let lo = 0, hi = pts.length; while (lo < hi) { const m = (lo + hi) >> 1; if (pts[m] <= x) lo = m + 1; else hi = m; } return lo; };
  const grid = {
    count: pts.length,
    seconds: (i) => pts[clamp(i, 0, pts.length - 1)] * num / den,
    origin: (start) => Math.min(firstAtLeast(Math.ceil(start * den / num + offset - 1e-6)), pts.length - 1),
    slot: (i, origin) => Math.floor((2 * (pts[i] - pts[origin]) * num * FPS + den) / (2 * den)),
    shown: (n, origin) => Math.max(0, firstAbove(pts[origin] + Math.ceil(den * (2 * n + 1) / (2 * FPS * num) - 1e-7) - 1) - 1),
    // The source frame a video element shows at media time t: the last one starting by t.
    // Browsers keep media times in microseconds, hence the 5 µs allowance.
    at: (t) => Math.max(0, firstAbove(Math.floor((t + 5e-6) * den / num)) - 1),
  };
  grid.frameTime = (n, origin) => grid.seconds(grid.shown(n, origin));
  return grid;
}

function fmt(t, digits = 2) {
  if (t == null || !isFinite(t)) return '–';
  const sign = t < 0 ? '−' : '';
  t = Math.abs(t);
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${sign}${m}:${s.toFixed(digits).padStart(digits + 3, '0')}`;
}

function signed(v, digits = 2) {
  return (v > 0 ? '+' : v < 0 ? '−' : '±') + Math.abs(v).toFixed(digits) + ' s';
}

function assColor(value) {
  const hex = (value || '&H00FFFFFF').replace(/^&H/i, '').replace(/&$/, '').padStart(8, '0').slice(-6);
  return '#' + hex.slice(4, 6) + hex.slice(2, 4) + hex.slice(0, 2);
}

function emptyEntry() {
  return { status: 'pending', options: [], comment: '', start_shift: 0, end_shift: 0 };
}

function overridesFor(clip, entry) {
  if (entry && entry.focus_overrides != null && entry.status === 'needs_changes' && !entry.addressed) return entry.focus_overrides;
  return clip.focus_overrides || [];
}

function centerAt(frame, focus, overrides) {
  for (const o of overrides) {
    if (frame >= o.start_frame && frame < o.end_frame) {
      if (o.center_x_end == null) return o.center_x;
      return o.center_x + (o.center_x_end - o.center_x) * (frame - o.start_frame) / (o.end_frame - o.start_frame);
    }
  }
  const c = focus && focus.centers;
  if (!c || !c.length) return 0.5;
  return c[clamp(frame, 0, c.length - 1)];
}

function shotIndexAt(focus, frame) {
  if (!focus) return -1;
  return focus.shots.findIndex(s => frame >= s.start_frame && frame < s.end_frame);
}

function captionAt(groups, ct) {
  for (let g = 0; g < groups.length; g++) {
    const items = groups[g];
    if (!items.length || ct < items[0].start) continue;
    for (let i = 0; i < items.length; i++) {
      if (items[i].start <= ct && ct < items[i].until) return { g, i };
    }
  }
  return null;
}

function captionHtml(groups, hit, highlight) {
  const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const items = groups[hit.g];
  const before = items.slice(0, hit.i).map(w => esc(w.text)).join(' ');
  const word = items[hit.i].text;
  const sep = before && !word.startsWith('-') ? ' ' : '';
  return before + sep + `<span style="color:${highlight}">${esc(word)}</span>`;
}

function useSize(ref) {
  const [size, setSize] = useState({ w: 0, h: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      setSize(s => (s.w === r.width && s.h === r.height) ? s : { w: r.width, h: r.height });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return size;
}

// ---------- live preview (proxy + crop + captions) ----------

function LivePreview({ ctx, videoRef, onTime, onFrameDraw, onDragCenter }) {
  const boxRef = useRef();
  const capRef = useRef();
  const size = useSize(boxRef);
  const live = useRef(ctx);
  live.current = ctx;
  const { settings, focus } = ctx;
  const ds = size.h / settings.height;
  const sw = (focus ? focus.scaled_width : settings.width * 16 / 9) * ds;
  const sh = (focus ? focus.scaled_height : settings.height) * ds;
  const sizeRef = useRef();
  sizeRef.current = { ds, sw, sh, w: settings.width * ds };
  const canvasRef = useRef();

  // Takes the frame the video element holds together with its own timestamp, and draws
  // it cropped onto the canvas. The picture and its crop reach the screen in the same
  // paint, so a late callback can delay a frame but never show it with another frame's
  // crop (a CSS transform on the video was applied apart from the video frame itself).
  const grab = () => {
    const v = videoRef.current;
    if (!v || v.readyState < 2) return null;
    if (typeof VideoFrame === 'function') {
      try { const f = new VideoFrame(v); return { image: f, t: f.timestamp / 1e6, close: () => f.close() }; } catch (e) { /* fall back */ }
    }
    return { image: v, t: v.currentTime, close: () => {} };
  };

  const paint = useCallback(() => {
    const c = live.current, canvas = canvasRef.current, box = boxRef.current;
    // Measured here rather than taken from layout state, which can lag behind the box.
    const bw = box ? box.clientWidth : 0, bh = box ? box.clientHeight : 0;
    if (!canvas || !bw || !bh) return;
    const shot = grab();
    if (!shot) return;
    const t = shot.t;
    // The crop and the captions follow the source frame on screen, at the grid frame
    // where the render shows that frame: framing in the clip's grid, captions in the cut's.
    let frame, ct;
    if (c.grid) {
      const i = c.grid.at(t);
      frame = c.grid.slot(i, c.clipOrigin);
      ct = c.grid.slot(i, c.cutOrigin) / FPS + 1e-6;
    } else {
      frame = frameOf(t, c.clip.start);
      ct = frameOf(t, c.cut.start) / FPS + 1e-6;
    }
    const center = centerAt(frame, c.focus, c.overrides);
    // Crop in the source picture's own pixels: the output window is width x height of the
    // picture scaled to scaled_width x scaled_height, centred vertically.
    const iw = shot.image.displayWidth || shot.image.videoWidth, ih = shot.image.displayHeight || shot.image.videoHeight;
    const scaledW = c.focus ? c.focus.scaled_width : c.settings.width * 16 / 9;
    const scaledH = c.focus ? c.focus.scaled_height : c.settings.height;
    const k = ih / scaledH;
    const x = clamp(center * scaledW - c.settings.width / 2, 0, scaledW - c.settings.width);
    const dpr = window.devicePixelRatio || 1;
    const cw = Math.round(bw * dpr), chh = Math.round(bh * dpr);
    if (canvas.width !== cw || canvas.height !== chh) { canvas.width = cw; canvas.height = chh; }
    const g = canvas.getContext('2d');
    g.drawImage(shot.image, x * k, (scaledH - c.settings.height) / 2 * k, c.settings.width * k, c.settings.height * k, 0, 0, cw, chh);
    onFrameDraw && onFrameDraw(t, center, shot.image);
    shot.close();
    const hit = ct >= 0 && t < c.cut.end ? captionAt(c.groups, ct) : null;
    const key = hit ? `${hit.g}:${hit.i}:${c.groupsVersion}` : '';
    const el = capRef.current;
    if (el && el.dataset.key !== key) {
      const enter = hit && (!el.dataset.key || el.dataset.key.split(':')[0] !== String(hit.g));
      el.dataset.key = key;
      el.innerHTML = hit ? captionHtml(c.groups, hit, c.highlight) : '';
      if (enter) { el.classList.remove('enter'); void el.offsetWidth; el.classList.add('enter'); }
    }
    boxRef.current && boxRef.current.classList.toggle('outside', t < c.cut.start - 1e-3 || t >= c.cut.end);
    return t;
  }, []);

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    let handle, raf, last = 0;
    const tick = (now) => {
      const t = paint();
      if (t != null && now - last > 80) { last = now; onTime(t); }
      handle = v.requestVideoFrameCallback ? v.requestVideoFrameCallback(tick) : null;
    };
    if (v.requestVideoFrameCallback) handle = v.requestVideoFrameCallback(tick);
    const loop = () => { const t = paint(); if (t != null) onTime(t); raf = requestAnimationFrame(loop); };
    if (!v.requestVideoFrameCallback) raf = requestAnimationFrame(loop);
    const seeked = () => { const t = paint(); onTime(t != null ? t : v.currentTime); };
    v.addEventListener('seeked', seeked);
    v.addEventListener('loadeddata', seeked);
    return () => {
      if (handle && v.cancelVideoFrameCallback) v.cancelVideoFrameCallback(handle);
      cancelAnimationFrame(raf);
      v.removeEventListener('seeked', seeked);
      v.removeEventListener('loadeddata', seeked);
    };
  }, [ctx.proxyUrl]);

  // Repaint when edits change the crop or captions while paused. While playing, only the
  // frame callback paints.
  useEffect(() => { const v = videoRef.current; if (v && v.paused) paint(); });

  const drag = useRef(null);
  const down = (e) => {
    if (!onDragCenter || !ctx.framingEditable) return;
    drag.current = { x: e.clientX, center: centerAt(ctx.frame, ctx.focus, ctx.overrides) };
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const move = (e) => {
    if (!drag.current) return;
    const dx = e.clientX - drag.current.x;
    onDragCenter(clamp(drag.current.center - dx / sizeRef.current.sw, 0, 1), false);
  };
  const up = (e) => {
    if (!drag.current) return;
    const dx = e.clientX - drag.current.x;
    onDragCenter(clamp(drag.current.center - dx / sizeRef.current.sw, 0, 1), true);
    drag.current = null;
  };

  const font = settings.font_size * ds * CAPTION_EM;
  return html`
    <div class="viewport live ${ctx.framingEditable ? 'grab' : ''}" ref=${boxRef}
         onPointerDown=${down} onPointerMove=${move} onPointerUp=${up}>
      <video ref=${videoRef} src=${ctx.proxyUrl} preload="auto" playsinline></video>
      <canvas class="frame" ref=${canvasRef}></canvas>
      <div class="caption" ref=${capRef}
           style=${{ left: settings.width / 2 * ds + 'px', top: settings.subtitle_y * ds + 'px', fontSize: font + 'px',
                     '--stroke': Math.max(1, 3 * ds * 2) + 'px' }}></div>
      <div class="outside-label">Outside the cut</div>
    </div>`;
}

function FileVideo({ src, videoRef, onTime, offset, label }) {
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const update = () => onTime && onTime(offset + v.currentTime);
    v.addEventListener('timeupdate', update);
    v.addEventListener('seeked', update);
    return () => { v.removeEventListener('timeupdate', update); v.removeEventListener('seeked', update); };
  }, [src, offset]);
  return html`<div class="viewport file">
    ${label && html`<div class="tag">${label}</div>`}
    <video ref=${videoRef} src=${src} preload="auto" playsinline controls></video></div>`;
}

// ---------- overview of the full source frame with the crop window ----------

function Overview({ videoRef, ctx, register, onSetCenter }) {
  const canvasRef = useRef();
  const drawRef = useRef();
  // `image` is the frame the live view just drew (a VideoFrame), so both show the same picture.
  drawRef.current = (t, center, image) => {
    const canvas = canvasRef.current, v = videoRef.current;
    if (!canvas || !v || (!image && !v.videoWidth)) return;
    const g = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    g.drawImage(image || v, 0, 0, W, H);
    const f = ctx.focus;
    const frac = f ? ctx.settings.width / f.scaled_width : 0.45;
    const x = clamp(center - frac / 2, 0, 1 - frac) * W;
    g.fillStyle = 'rgba(0,0,0,.55)';
    g.fillRect(0, 0, x, H);
    g.fillRect(x + frac * W, 0, W - x - frac * W, H);
    g.strokeStyle = ctx.overrideActive ? '#f2c14e' : '#5b9cf2';
    g.lineWidth = 2;
    g.strokeRect(x + 1, 1, frac * W - 2, H - 2);
  };
  useEffect(() => register((t, c, image) => drawRef.current(t, c, image)), []);
  const pick = (e, done) => {
    if (!ctx.framingEditable || (e.type === 'pointermove' && !e.buttons)) return;
    const r = canvasRef.current.getBoundingClientRect();
    onSetCenter(clamp((e.clientX - r.left) / r.width, 0, 1), done);
  };
  return html`<canvas class="overview ${ctx.framingEditable ? 'grab' : ''}" ref=${canvasRef} width="320" height="180"
    onPointerDown=${e => { e.currentTarget.setPointerCapture(e.pointerId); pick(e, false); }}
    onPointerMove=${e => pick(e, false)} onPointerUp=${e => pick(e, true)}></canvas>`;
}

// ---------- timeline ----------

function Timeline({ ctx, t, onSeek, onEdge, onEdgeDone, pad, onSelectWord, selectedWord, onWordTiming }) {
  const ref = useRef();
  const { w } = useSize(ref);
  const { clip, cut, snapped, focus, words, duration } = ctx;
  const a = Math.max(0, Math.min(clip.start, cut.start) - pad);
  const b = Math.min(duration, Math.max(clip.end, cut.end) + pad);
  const X = (s) => (s - a) / (b - a) * w;
  const T = (x) => a + clamp(x, 0, w) / w * (b - a);
  const drag = useRef(null);
  const [wordDrag, setWordDrag] = useState(null);

  const down = (e, kind, word) => {
    e.stopPropagation();
    drag.current = kind;
    ref.current.setPointerCapture(e.pointerId);
    if (kind === 'seek') onSeek(T(e.clientX - ref.current.getBoundingClientRect().left));
    if (word) setWordDrag({ index: word.index, start: word.start, end: word.end });
  };
  const move = (e) => {
    if (!drag.current) return;
    const s = T(e.clientX - ref.current.getBoundingClientRect().left);
    if (drag.current === 'seek') onSeek(s);
    else if (drag.current === 'wstart') setWordDrag(d => d && { ...d, start: Math.min(s, d.end - 0.04) });
    else if (drag.current === 'wend') setWordDrag(d => d && { ...d, end: Math.max(s, d.start + 0.04) });
    else onEdge(drag.current, s);
  };
  const up = () => {
    const kind = drag.current;
    drag.current = null;
    if (kind === 'wstart' || kind === 'wend') {
      if (wordDrag) onWordTiming(wordDrag.index, { start: Math.round(wordDrag.start * 1000) / 1000, end: Math.round(wordDrag.end * 1000) / 1000 });
      setWordDrag(null);
    } else if (kind && kind !== 'seek') onEdgeDone();
  };

  const step = (b - a) > 900 ? 60 : (b - a) > 300 ? 30 : (b - a) > 90 ? 10 : (b - a) > 30 ? 5 : 1;
  const ticks = [];
  for (let s = Math.ceil(a / step) * step; s <= b; s += step) ticks.push(s);
  const shotRows = focus ? focus.shots.map((s, i) => {
    const s0 = ctx.frameTime(s.start_frame), s1 = ctx.frameTime(s.end_frame);
    const over = ctx.overrides.some(o => o.start_frame < s.end_frame && o.end_frame > s.start_frame);
    return html`<div class="shot ${i % 2 ? 'odd' : ''} ${over ? 'over' : ''} ${s.review.length ? 'flag' : ''}"
      title=${`Shot ${i + 1}: frames ${s.start_frame}–${s.end_frame - 1}${s.review.length ? ' · review: ' + s.review.map(r => r.reason || r.kind || r).join(', ') : ''}`}
      style=${{ left: X(s0) + 'px', width: Math.max(1, X(s1) - X(s0)) + 'px' }}
      onPointerDown=${e => { e.stopPropagation(); onSeek(s0 + 1e-3); }}>${X(s1) - X(s0) > 22 ? i + 1 : ''}</div>`;
  }) : null;
  const wordRows = (words || []).filter(wd => wd.end >= a && wd.start <= b).map(wd => {
    const timing = wordDrag && wordDrag.index === wd.index ? wordDrag : wd;
    const sel = selectedWord === wd.index;
    return html`
    <div class="word ${wordEdited(wd) ? 'edited' : ''} ${wd.kind} ${sel ? 'sel' : ''} ${wd.line ? 'line-' + wd.line : ''}"
      title=${wordTitle(wd)}
      style=${{ left: X(timing.start) + 'px', width: Math.max(2, X(timing.end) - X(timing.start)) + 'px' }}
      onPointerDown=${e => { e.stopPropagation(); onSeek(wd.start + 1e-3); onSelectWord(wd.index); }}>
      ${sel && ctx.captionsEditable && html`<span class="wh l" title="Drag the word's start" onPointerDown=${e => down(e, 'wstart', wd)}></span>
        <span class="wh r" title="Drag the word's end" onPointerDown=${e => down(e, 'wend', wd)}></span>`}
      ${X(timing.end) - X(timing.start) > 26 ? wd.word : ''}</div>`;
  });
  return html`
    <div class="timeline" ref=${ref} onPointerDown=${e => down(e, 'seek')} onPointerMove=${move} onPointerUp=${up}>
      <div class="ruler">${ticks.map(s => html`<span style=${{ left: X(s) + 'px' }}>${fmt(s, 0)}</span>`)}</div>
      <div class="lane shots">${shotRows}</div>
      <div class="lane words">${wordRows}</div>
      <div class="orig" style=${{ left: X(clip.start) + 'px', width: X(clip.end) - X(clip.start) + 'px' }}></div>
      <div class="cut" style=${{ left: X(cut.start) + 'px', width: X(cut.end) - X(cut.start) + 'px' }}>
        <div class="handle l" title="Drag the start (I sets it at the playhead)" onPointerDown=${e => down(e, 'start')}></div>
        <div class="handle r" title="Drag the end (O sets it at the playhead)" onPointerDown=${e => down(e, 'end')}></div>
      </div>
      ${snapped && !snapped.error && Math.abs(snapped.start - cut.start) > 1e-3 && html`<div class="snap" style=${{ left: X(snapped.start) + 'px' }} title="Applied start (word-safe)"></div>`}
      ${snapped && !snapped.error && Math.abs(snapped.end - cut.end) > 1e-3 && html`<div class="snap" style=${{ left: X(snapped.end) + 'px' }} title="Applied end (word-safe)"></div>`}
      <div class="playhead" style=${{ left: X(t) + 'px' }}></div>
    </div>`;
}

// ---------- inspector panels ----------

function wordEdited(w) {
  return w.kind === 'added' || w.word !== w.original || w.start !== w.original_start || w.end !== w.original_end || !!w.line;
}

function wordTitle(w) {
  const kind = { transcript: 'Transcript word', missed: 'Found by the missed-speech pass', added: 'Added in the editor' }[w.kind];
  const parts = [`${kind} · ${fmt(w.start)}–${fmt(w.end)}`];
  if (w.kind !== 'added' && w.word !== w.original) parts.push(`${w.original} → ${w.word || '(removed)'}`);
  if (w.kind !== 'added' && (w.start !== w.original_start || w.end !== w.original_end)) parts.push(`retimed from ${fmt(w.original_start)}–${fmt(w.original_end)}`);
  if (w.line) parts.push(w.line === 'break' ? 'new caption line starts here' : 'kept on the previous line');
  return parts.join('\n');
}

function Section({ title, children, extra }) {
  return html`<section class="panel"><h3>${title}${extra && html`<span class="extra">${extra}</span>`}</h3>${children}</section>`;
}

// "1:23.45", "83.45" or "0:01:23.45" -> seconds; null when unreadable.
function parseTime(text) {
  const parts = String(text).trim().replace(',', '.').split(':');
  if (!parts.length || parts.length > 3 || parts.some(p => !/^\d+(\.\d*)?$/.test(p))) return null;
  return parts.reduce((total, p) => total * 60 + parseFloat(p), 0);
}

function TimeInput({ value, disabled, onSet }) {
  const [draft, setDraft] = useState(null);
  const commit = (e) => {
    const s = parseTime(e.target.value);
    setDraft(null);
    if (s != null && Math.abs(s - value) > 1e-4) onSet(s);
  };
  return html`<input class="time mono" disabled=${disabled} value=${draft ?? fmt(value)} size="9" title="Type a time (m:ss.ss or seconds) and press Enter"
    onInput=${e => setDraft(e.target.value)} onBlur=${commit}
    onKeyDown=${e => { if (e.key === 'Enter') { e.preventDefault(); commit(e); e.target.blur(); } if (e.key === 'Escape') { setDraft(null); e.target.blur(); } }} />`;
}

function CutPanel({ ctx, t, setShift, nudgeShift, disabled }) {
  const { clip, entry, snapped } = ctx;
  const row = (edge, label, key) => {
    const base = clip[edge], shift = entry[edge + '_shift'] || 0;
    const nudge = (d) => nudgeShift(edge, d);
    return html`<div class="edge">
      <div class="edge-head"><b>${label}</b> <${TimeInput} value=${base + shift} disabled=${disabled} onSet=${s => setShift(edge, s - base)} />
        <span class="muted">${signed(shift)}</span></div>
      <div class="buttons">
        <button disabled=${disabled} onClick=${() => nudge(-5)}>−5 s</button>
        <button disabled=${disabled} onClick=${() => nudge(-0.5)}>−0.5 s</button>
        <button disabled=${disabled} onClick=${() => nudge(-1 / FPS)}>−1 f</button>
        <button disabled=${disabled} onClick=${() => nudge(1 / FPS)}>+1 f</button>
        <button disabled=${disabled} onClick=${() => nudge(0.5)}>+0.5 s</button>
        <button disabled=${disabled} onClick=${() => nudge(5)}>+5 s</button>
        <button disabled=${disabled} onClick=${() => setShift(edge, t - base)} title="Shortcut: ${key}">At playhead (${key})</button>
        <button disabled=${disabled || !shift} onClick=${() => setShift(edge, 0)}>Reset</button>
      </div></div>`;
  };
  let result = 'Edges unchanged.';
  if (entry.start_shift || entry.end_shift) {
    result = snapped == null ? 'Checking word boundaries…'
      : snapped.error ? html`<span class="bad">${snapped.error}</span>`
      : html`Applied as <b class="mono">${fmt(snapped.start)}–${fmt(snapped.end)}</b> (${(snapped.end - snapped.start).toFixed(2)} s); edges never cut a word.`;
  }
  return html`<${Section} title="Cut" extra=${`${(ctx.cut.end - ctx.cut.start).toFixed(1)} s`}>
    ${row('start', 'Start', 'I')}${row('end', 'End', 'O')}<p class="note">${result}</p></${Section}>`;
}

function FramingPanel({ ctx, t, frame, editShot, setFraming, overviewSlot, disabled }) {
  const { focus, overrides } = ctx;
  if (!focus) return html`<${Section} title="Framing"><p class="note">Loading focus data…</p></${Section}>`;
  const index = shotIndexAt(focus, frame);
  const shot = focus.shots[index];
  const inside = !!shot;
  const mine = inside ? overrides.filter(o => o.start_frame < shot.end_frame && o.end_frame > shot.start_frame) : [];
  const full = mine.length === 1 && mine[0].start_frame === shot.start_frame && mine[0].end_frame === shot.end_frame ? mine[0] : null;
  const auto = centerAt(frame, focus, []);
  const staged = ctx.entry.focus_overrides != null;
  const slider = (label, value, key) => html`<label class="slider">${label}
      <input type="range" min="0" max="1" step="0.001" value=${value} disabled=${disabled || !full}
        onInput=${e => editShot(index, { [key]: +e.target.value }, false)} onChange=${e => editShot(index, { [key]: +e.target.value }, true)} />
      <span class="mono">${(value * 100).toFixed(1)}%</span></label>`;
  return html`<${Section} title="Framing" extra=${inside ? `Shot ${index + 1}/${focus.shots.length}` : 'Outside the analysed cut'}>
    ${overviewSlot}
    ${!focus.exact && html`<p class="note warn">Automatic centers come from the rendered file and already include reviewed overrides.</p>`}
    ${inside ? html`
      <p class="note">Frames ${shot.start_frame}–${shot.end_frame - 1} (${((shot.end_frame - shot.start_frame) / FPS).toFixed(2)} s).
        ${full ? (full.center_x_end != null ? ' Reviewed pan.' : ' Reviewed center.') : mine.length ? ' Partial reviewed override.' : ' Automatic speaker focus.'}
        ${shot.review.length ? html` <span class="bad">Flagged for review.</span>` : ''}</p>
      <div class="buttons">
        <button disabled=${disabled} onClick=${() => editShot(index, full ? {} : { center_x: centerAt(frame, focus, overrides) }, true, true)}
          title="Shortcut: F">${full ? 'Keep override' : 'Override this shot (F)'}</button>
        <button disabled=${disabled || !mine.length} onClick=${() => editShot(index, null, true)}>Back to automatic</button>
        <label class="check"><input type="checkbox" disabled=${disabled || !full} checked=${full && full.center_x_end != null}
          onChange=${e => editShot(index, e.target.checked ? { center_x_end: full.center_x } : { center_x_end: undefined }, true)} />Pan</label>
      </div>
      ${full && slider(full.center_x_end != null ? 'Start' : 'Center', full.center_x, 'center_x')}
      ${full && full.center_x_end != null && slider('End', full.center_x_end, 'center_x_end')}
      <p class="note muted">Drag the picture or the overview to move the crop${full && full.center_x_end != null ? ' (first half of the shot moves the start, second half the end)' : ''}. Auto center here: ${(auto * 100).toFixed(1)}%.</p>`
    : html`<p class="note">Framing can only be edited inside the current cut; new parts are analysed when the change is applied.</p>`}
    <div class="buttons">
      <button disabled=${disabled || !staged} onClick=${() => setFraming(null)} title="Drop staged framing; the plan's overrides stay">Discard framing edits</button>
      <button disabled=${disabled || !overrides.length} onClick=${() => setFraming([])}>Clear all overrides</button>
    </div>
  </${Section}>`;
}

const r3 = (s) => Math.round(s * 1000) / 1000;

function CaptionPanel({ ctx, t, selected, onSelect, onSave, onAdd, onRetranscribe, proposals, setProposals, onAccept,
                        editable, busy, lockedNote }) {
  const all = ctx.words || [];
  const pos = selected != null ? all.findIndex(w => w.index === selected) : -1;
  const words = pos >= 0 ? all.slice(Math.max(0, pos - 6), pos + 7) : all.filter(w => w.end >= t - 1.5 && w.start <= t + 3);
  const sel = pos >= 0 ? all[pos] : null;
  const [drafts, setDrafts] = useState({});
  const [addText, setAddText] = useState('');
  const commit = (w, value) => {
    if (value == null || value === w.word) return;
    onSave(w.index, { to: value }).then(() => setDrafts(d => { const n = { ...d }; delete n[w.index]; return n; }));
  };
  const restore = (w) => w.kind === 'added' ? onSave(w.index, { to: '' }) : onSave(w.index, { to: null, start: null, end: null, line: null });
  const toggleLine = (w, kind) => onSave(w.index, { line: w.line === kind ? null : kind });
  const add = () => {
    const parts = addText.trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return;
    // Spread the words from the playhead, about 0.3 s each, up to the next word if it is close.
    const next = all.find(w => w.start > t + 0.01);
    const room = next ? next.start - 0.02 - t : Infinity;
    const total = room >= 0.15 * parts.length ? Math.min(parts.length * 0.32, room) : parts.length * 0.32;
    const each = total / parts.length;
    onAdd(parts.map((p, i) => ({ word: p, start: r3(t + i * each), end: r3(t + (i + 1) * each - 0.01) })))
      .then(ok => ok && setAddText(''));
  };
  // The stretch without words around the playhead, for Whisper to look at again.
  const before = [...all].reverse().find(w => w.end <= t), after = all.find(w => w.start >= t);
  const rangeStart = Math.max(0, t - 8, Math.min(t - 1.5, before ? before.end - 0.5 : t - 4));
  const rangeEnd = Math.min(ctx.duration, t + 8, Math.max(t + 3, after ? after.start + 0.5 : t + 4));
  const kindTag = (w) => w.kind === 'missed' ? html`<span class="kind" title="Found by the missed-speech pass">M</span>`
    : w.kind === 'added' ? html`<span class="kind" title="Added in the editor">+</span>` : null;
  return html`<${Section} title="Captions" extra=${ctx.captionEdits ? `${ctx.captionEdits} edits in this source` : ''}>
    <div class="words-edit">
      ${words.map(w => html`<label class="wordbox ${wordEdited(w) ? 'edited' : ''} ${w.index === selected ? 'sel' : ''} ${w.start <= t && t < w.end ? 'now' : ''} ${w.kind}"
          title=${wordTitle(w)}>
        ${w.line === 'break' && html`<span class="linemark" title="A new caption line starts here">↵</span>`}
        ${w.line === 'join' && html`<span class="linemark" title="Kept on the previous line">⁀</span>`}
        ${kindTag(w)}
        <input value=${drafts[w.index] ?? w.word} size=${Math.max(3, (drafts[w.index] ?? w.word).length + 1)} disabled=${!editable}
          onFocus=${() => onSelect(w.index)}
          onInput=${e => setDrafts(d => ({ ...d, [w.index]: e.target.value }))}
          onKeyDown=${e => { if (e.key === 'Enter') { e.preventDefault(); commit(w, e.target.value); e.target.blur(); } if (e.key === 'Escape') { e.target.value = w.word; setDrafts(d => ({ ...d, [w.index]: undefined })); e.target.blur(); } }}
          onBlur=${e => commit(w, e.target.value)} />
        ${editable && wordEdited(w) && w.kind !== 'added' && html`<button class="revert" title="Restore the original word and timing" onClick=${() => restore(w)}>↺</button>`}
      </label>`)}
      ${!words.length && html`<p class="note muted">No words near the playhead.</p>`}
    </div>
    ${sel && html`<div class="word-tools">
      <div><b>${sel.word || '(removed)'}</b> <span class="mono">${fmt(sel.start)}–${fmt(sel.end)}</span>
        <span class="muted">${{ transcript: 'transcript', missed: 'missed-speech pass', added: 'added' }[sel.kind]}</span>
        <button class="link" onClick=${() => onSelect(null)}>Deselect</button></div>
      <div class="buttons">
        <button disabled=${!editable} onClick=${() => onSave(sel.index, t < sel.end ? { start: r3(t) } : { start: r3(t), end: r3(t + sel.end - sel.start) })}>Start at playhead</button>
        <button disabled=${!editable || t <= sel.start} onClick=${() => onSave(sel.index, { end: r3(t) })}>End at playhead</button>
        <button disabled=${!editable} class=${sel.line === 'break' ? 'on' : ''} onClick=${() => toggleLine(sel, 'break')} title="Start a new caption line at this word">New line here</button>
        <button disabled=${!editable} class=${sel.line === 'join' ? 'on' : ''} onClick=${() => toggleLine(sel, 'join')} title="Keep this word on the previous caption line">Join previous line</button>
        <button disabled=${!editable || !wordEdited(sel)} onClick=${() => restore(sel)}>${sel.kind === 'added' ? 'Delete word' : 'Restore original'}</button>
      </div>
      <p class="note muted">Drag the word's edges on the timeline to retime it.</p></div>`}
    <div class="add-words">
      <input placeholder="Add words at the playhead…" value=${addText} disabled=${!editable}
        onInput=${e => setAddText(e.target.value)} onKeyDown=${e => { if (e.key === 'Enter') { e.preventDefault(); add(); } }} />
      <button disabled=${!editable || !addText.trim()} onClick=${add}>Add</button>
    </div>
    <div class="buttons">
      <button disabled=${!editable || busy} onClick=${() => onRetranscribe(rangeStart, rangeEnd)}
        title="Run Whisper again on ${fmt(rangeStart)}–${fmt(rangeEnd)} and propose the words it hears">Re-transcribe around the playhead</button>
    </div>
    ${proposals && html`<div class="proposals">
      <p class="note">Whisper heard in ${fmt(proposals.start)}–${fmt(proposals.end)}${proposals.words.length ? '' : ': nothing'}.
        Words that overlap an existing word start unticked.</p>
      ${proposals.words.map((p, i) => html`<label class="prop ${p.overlaps ? 'overlap' : ''}">
        <input type="checkbox" checked=${p.on} onChange=${e => setProposals(ps => ({ ...ps, words: ps.words.map((q, j) => j === i ? { ...q, on: e.target.checked } : q) }))} />
        <input value=${p.word} size=${Math.max(3, p.word.length + 1)} onInput=${e => setProposals(ps => ({ ...ps, words: ps.words.map((q, j) => j === i ? { ...q, word: e.target.value } : q) }))} />
        <span class="muted mono">${fmt(p.start)}</span></label>`)}
      <div class="buttons">
        <button class="primary" disabled=${!editable || !proposals.words.some(p => p.on && p.word.trim())} onClick=${onAccept}>Add ticked words</button>
        <button onClick=${() => setProposals(null)}>Dismiss</button></div></div>`}
    <p class="note muted">Enter saves a word; an empty word removes it. Edits apply to every clip of this source on the next Apply.${lockedNote}</p>
  </${Section}>`;
}

// ---------- app ----------

function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [entries, setEntries] = useState({});
  const [focusCache, setFocusCache] = useState({});
  const [captions, setCaptions] = useState(null);
  const [snapped, setSnapped] = useState(null);
  const [mode, setMode] = useState('live');
  const [t, setT] = useState(0);
  const [loop, setLoop] = useState(false);
  const [ctxLevel, setCtxLevel] = useState(0);
  const [grid, setGrid] = useState(null);
  const [proposals, setProposals] = useState(null);
  const [captionsVersion, setCaptionsVersion] = useState(0);
  const [filter, setFilter] = useState('all');
  const [job, setJob] = useState(null);
  const [showLog, setShowLog] = useState(false);
  const [confirmApply, setConfirmApply] = useState(false);
  const [help, setHelp] = useState(false);
  const [toast, setToast] = useState(null);
  const [selWord, setSelWord] = useState(null);
  const [groupsVersion, setGroupsVersion] = useState(0);
  const liveVideo = useRef(), fileVideo = useRef(), leftVideo = useRef();
  const overviewDraw = useRef(null);
  const lastFrame = useRef(null);
  const saveTimers = useRef({});
  const saveQueue = useRef({});
  const entriesRef = useRef({});
  const confirmed = useRef({});

  const notify = (text, kind = 'info') => { setToast({ text, kind }); setTimeout(() => setToast(null), 4000); };

  const load = useCallback(async () => {
    try {
      const d = await request('state');
      setData(d);
      const clips = d.decisions.clips || {};
      entriesRef.current = clips;
      confirmed.current = Object.fromEntries(Object.entries(clips).map(([k, e]) => [k, e.updated ?? null]));
      setEntries(clips);
      setJob(d.job);
      setFocusCache({});
      setSelected(s => (s && d.clips.some(c => c.key === s)) ? s : (d.clips[0] && d.clips[0].key));
      setError(null);
    } catch (e) { setError('The editor server is not reachable: ' + e.message); }
  }, []);
  useEffect(() => { load(); }, []);
  // The source's frame timestamps, for the exact live crop (once per session).
  useEffect(() => {
    request('frames').then(d => setGrid(makeGrid(d))).catch(e => notify('Frame times: ' + e.message, 'bad'));
  }, []);

  // Poll the background job while it runs; reload everything when it ends.
  useEffect(() => {
    if (!job || job.state !== 'running') return;
    const timer = setInterval(async () => {
      try {
        const j = await request('job');
        setJob(j);
        if (j.state !== 'running') {
          clearInterval(timer);
          await load();
          if (j.state === 'done' && j.kind === 'preview') setMode('preview');
          if (j.state === 'done' && j.kind === 'transcribe') showProposals();
          if (j.kind === 'prepare') setCaptionsVersion(v => v + 1);
          notify(j.state === 'done' ? `${j.kind[0].toUpperCase() + j.kind.slice(1)} finished.` : `${j.kind} failed: ${j.error}`, j.state === 'done' ? 'ok' : 'bad');
        }
      } catch (e) { /* keep polling */ }
    }, 700);
    return () => clearInterval(timer);
  }, [job && job.state, job && job.seq]);

  const clip = data && data.clips.find(c => c.key === selected);
  const entry = clip ? { ...emptyEntry(), ...(entries[clip.key] || {}) } : null;
  const locked = !!entry && entry.status === 'approved';
  const applying = job && job.state === 'running' && job.kind === 'apply';
  const focus = clip && focusCache[clip.key];
  const cut = clip ? { start: clip.start + (entry.start_shift || 0), end: clip.end + (entry.end_shift || 0) } : null;
  const effectiveCut = snapped && !snapped.error ? snapped : cut;
  // Grid origins: framing uses the current clip's frames, captions the cut that would be applied.
  const clipOrigin = grid && clip ? grid.origin(clip.start) : null;
  const cutOrigin = grid && effectiveCut ? grid.origin(effectiveCut.start) : null;
  const frameTime = (n) => grid && clipOrigin != null ? grid.frameTime(n, clipOrigin) : clip.start + n / FPS;
  // In Live, the first source frame a render of a cut starting at `start` shows (it can start a little after it).
  const firstFrame = (start) => mode === 'live' && grid ? grid.seconds(grid.origin(start)) + 5e-4 : start;

  // Focus data per clip.
  useEffect(() => {
    if (!clip || focusCache[clip.key]) return;
    request(`clips/${encodeURIComponent(clip.key)}/focus`).then(f => setFocusCache(c => ({ ...c, [clip.key]: f })))
      .catch(e => notify('Focus data: ' + e.message, 'bad'));
  }, [clip && clip.key, focusCache]);

  // Word-safe edges for the moved cut.
  useEffect(() => {
    if (!clip) return;
    if (!entry.start_shift && !entry.end_shift) { setSnapped({ start: clip.start, end: clip.end }); return; }
    setSnapped(null);
    const timer = setTimeout(() => {
      request(`clips/${encodeURIComponent(clip.key)}/snap?start_shift=${entry.start_shift || 0}&end_shift=${entry.end_shift || 0}`)
        .then(setSnapped).catch(e => setSnapped({ error: e.message }));
    }, 200);
    return () => clearTimeout(timer);
  }, [clip && clip.key, clip && clip.start, entry && entry.start_shift, entry && entry.end_shift]);

  // Captions and nearby words for the cut that would be applied.
  const capStart = effectiveCut && effectiveCut.start, capEnd = effectiveCut && effectiveCut.end;
  useEffect(() => {
    if (!clip || capStart == null) return;
    const timer = setTimeout(() => {
      request(`clips/${encodeURIComponent(clip.key)}/captions?start=${capStart}&end=${capEnd}&context=${CONTEXTS[ctxLevel]}`)
        .then(c => { setCaptions(c); setGroupsVersion(v => v + 1); }).catch(e => notify('Captions: ' + e.message, 'bad'));
    }, 150);
    return () => clearTimeout(timer);
  }, [clip && clip.key, capStart, capEnd, data && data.caption_edits, captionsVersion, ctxLevel]);

  // When the selected clip changes, park the playhead at its start.
  useEffect(() => {
    if (!clip) return;
    setSelWord(null);
    setTimeout(() => seek(firstFrame(clip.start)), 0);
  }, [clip && clip.key, mode]);

  useEffect(() => {
    if (!data) return;
    if (!data.source.proxy_url && mode === 'live') setMode('rendered');
  }, [data && data.source.id, data && data.source.proxy_url]);

  // ----- saving -----
  // Local edits live in entriesRef (always the newest). Saves for a clip run one at a time,
  // send the newest local state and name the server version they were based on.
  const bodyOf = (e) => {
    const body = { status: e.status, options: e.options || [], comment: e.comment || '',
                   start_shift: e.start_shift || 0, end_shift: e.end_shift || 0 };
    if (e.focus_overrides != null) body.focus_overrides = e.focus_overrides;
    return body;
  };
  const flush = useCallback(async (key) => {
    const q = saveQueue.current[key] || (saveQueue.current[key] = {});
    if (q.running) { q.again = true; return; }
    q.running = true;
    try {
      do {
        q.again = false;
        const sent = bodyOf({ ...emptyEntry(), ...(entriesRef.current[key] || {}) });
        const saved = await request(`clips/${encodeURIComponent(key)}/decision`, { ...sent, base: confirmed.current[key] ?? null });
        confirmed.current[key] = saved.updated ?? null;
        const now = bodyOf({ ...emptyEntry(), ...(entriesRef.current[key] || {}) });
        if (JSON.stringify(now) === JSON.stringify(sent)) setLocal(key, saved);
        else q.again = true;  // edited while saving: keep the newer local state and save again
      } while (q.again);
    } catch (e) {
      notify('Not saved: ' + e.message, 'bad');
      load();
    } finally { q.running = false; }
  }, []);
  const save = useCallback((key, immediate) => {
    clearTimeout(saveTimers.current[key]);
    if (immediate) flush(key); else saveTimers.current[key] = setTimeout(() => flush(key), 600);
  }, []);
  const setLocal = (key, next) => {
    entriesRef.current = { ...entriesRef.current, [key]: next };
    setEntries(entriesRef.current);
  };

  const edit = (change, immediate = true) => {
    if (!clip) return;
    const next = { ...emptyEntry(), ...(entriesRef.current[clip.key] || {}), ...change };
    setLocal(clip.key, next);
    save(clip.key, immediate);
    return next;
  };

  const latest = () => ({ ...emptyEntry(), ...(entriesRef.current[clip.key] || {}) });

  const stage = (option, change, immediate = true) => {
    if (locked || applying) return;
    const options = Array.from(new Set([...(latest().options || []), option]));
    return edit({ ...change, status: 'needs_changes', options }, immediate);
  };

  const setStatus = (status) => {
    if (applying) return;
    edit({ status: latest().status === status ? 'pending' : status });
  };

  // An edge can move anywhere in the source; the cut stays between 3 and 180 seconds.
  const setShift = (edge, value, immediate = true) => {
    const now = latest();
    const other = edge === 'start' ? clip.end + (now.end_shift || 0) : clip.start + (now.start_shift || 0);
    let pos = Math.round((clip[edge] + value) * 1000) / 1000;
    if (edge === 'start') pos = clamp(pos, Math.max(0, other - 180), other - 3);
    else pos = clamp(pos, other + 3, Math.min(data.source.duration, other + 180));
    stage('recut', { [edge + '_shift']: Math.round((pos - clip[edge]) * 1000) / 1000 }, immediate);
  };

  const nudgeShift = (edge, d) => setShift(edge, Math.round(((latest()[edge + '_shift'] || 0) + d) * 1000) / 1000);

  const overrides = clip ? overridesFor(clip, entry) : [];
  // Live: the grid frame where the render shows the source frame on screen. File modes:
  // the file's own 30 fps frame.
  const frame = !clip ? 0 : mode === 'live' && grid && clipOrigin != null ? grid.slot(grid.at(t), clipOrigin)
    : frameOf(t, mode === 'preview' && clip.preview ? clip.preview.start : clip.start);

  const setFraming = (list) => {
    if (list === null) edit({ focus_overrides: null });
    else stage('fix_framing', { focus_overrides: list });
  };

  // Replace every override touching a shot with one covering the whole shot.
  const editShot = (index, change, immediate, create = false) => {
    if (!focus) return;
    const overrides = overridesFor(clip, latest());
    const shot = focus.shots[index];
    const others = overrides.filter(o => o.end_frame <= shot.start_frame || o.start_frame >= shot.end_frame);
    const current = overrides.find(o => o.start_frame === shot.start_frame && o.end_frame === shot.end_frame);
    let list = others;
    if (change !== null) {
      const base = current || { start_frame: shot.start_frame, end_frame: shot.end_frame, center_x: centerAt(frame, focus, overrides) };
      if (!current && !create && !('center_x' in change)) return;
      const next = { ...base, ...change, reason: 'Reviewed in the clip editor.' };
      if (next.center_x_end === undefined) delete next.center_x_end;
      next.center_x = Math.round(next.center_x * 10000) / 10000;
      if (next.center_x_end != null) next.center_x_end = Math.round(next.center_x_end * 10000) / 10000;
      list = [...others, next].sort((x, y) => x.start_frame - y.start_frame);
    }
    stage('fix_framing', { focus_overrides: list }, immediate);
  };

  const framingEditable = !!focus && !locked && !applying && shotIndexAt(focus, frame) >= 0;
  const dragCenter = (center, done) => {
    const index = shotIndexAt(focus, frame);
    if (index < 0) return;
    const shot = focus.shots[index];
    const current = overridesFor(clip, latest()).find(o => o.start_frame === shot.start_frame && o.end_frame === shot.end_frame);
    const panEnd = current && current.center_x_end != null && frame >= (shot.start_frame + shot.end_frame) / 2;
    editShot(index, { [panEnd ? 'center_x_end' : 'center_x']: center }, done, true);
  };

  const captionsSaved = async (text) => {
    const d = await request('state');
    setData(d);
    setCaptionsVersion(v => v + 1);
    notify(text, 'ok');
  };
  const saveCaption = async (index, changes) => {
    try {
      await request('captions', { index, ...changes });
      await captionsSaved('Caption saved.');
      return true;
    } catch (e) { notify('Caption not saved: ' + e.message, 'bad'); return false; }
  };
  const addCaptionWords = async (words) => {
    try {
      const r = await request('captions/add', { words });
      await captionsSaved(`${r.ids.length} word(s) added.`);
      return true;
    } catch (e) { notify('Words not added: ' + e.message, 'bad'); return false; }
  };
  const runRetranscribe = async (start, end) => {
    try { setProposals(null); setJob(await request('retranscribe', { start, end })); } catch (e) { notify(e.message, 'bad'); }
  };
  const showProposals = async () => {
    try {
      const r = await request('retranscribe');
      const existing = (captions && captions.words) || [];
      // A proposal that mostly overlaps a word the captions already have starts unticked.
      const words = r.words.map(p => {
        const overlaps = existing.some(w => Math.min(w.end, p.end) - Math.max(w.start, p.start) > 0.5 * (p.end - p.start));
        return { ...p, overlaps, on: !overlaps };
      });
      setProposals({ start: r.start, end: r.end, words });
    } catch (e) { notify('Re-transcription: ' + e.message, 'bad'); }
  };
  const acceptProposals = async () => {
    const chosen = proposals.words.filter(p => p.on && p.word.trim()).map(p => ({ word: p.word.trim(), start: p.start, end: p.end }));
    if (await addCaptionWords(chosen)) setProposals(null);
  };

  // ----- playback -----
  const activeVideo = () => (mode === 'live' ? liveVideo.current : fileVideo.current);
  const offsetFor = () => mode === 'live' ? 0 : mode === 'preview' ? (clip.preview ? clip.preview.start : clip.start) : clip.start;
  const seek = (s) => {
    if (!clip) return;
    setT(s);
    const v = activeVideo();
    if (!v) return;
    const target = Math.max(0, s - offsetFor());
    v.currentTime = target;
    if (leftVideo.current) leftVideo.current.currentTime = target;
  };
  const playCut = () => {
    const v = activeVideo();
    if (!v) return;
    if (!v.paused) { v.pause(); leftVideo.current && leftVideo.current.pause(); return; }
    if (mode === 'live' && (t < effectiveCut.start - 0.05 || t >= effectiveCut.end - 0.05)) seek(firstFrame(effectiveCut.start));
    v.play();
    leftVideo.current && leftVideo.current.play();
  };
  // Stop or loop at the end of the cut in the live view.
  useEffect(() => {
    if (mode !== 'live' || !effectiveCut) return;
    const v = liveVideo.current;
    if (!v || v.paused) return;
    if (t >= effectiveCut.end) { if (loop) seek(firstFrame(effectiveCut.start)); else v.pause(); }
  }, [t]);
  // Keep the compare pair in step.
  useEffect(() => {
    if (mode !== 'compare') return;
    const a = fileVideo.current, b = leftVideo.current;
    if (!a || !b) return;
    const sync = () => { if (Math.abs(a.currentTime - b.currentTime) > 0.08) b.currentTime = a.currentTime; };
    const play = () => b.play(), pause = () => b.pause();
    a.addEventListener('timeupdate', sync); a.addEventListener('seeked', sync); a.addEventListener('play', play); a.addEventListener('pause', pause);
    return () => { a.removeEventListener('timeupdate', sync); a.removeEventListener('seeked', sync); a.removeEventListener('play', play); a.removeEventListener('pause', pause); };
  }, [mode, clip && clip.key]);

  // ----- keyboard -----
  const keyRef = useRef();
  keyRef.current = (e) => {
    if (!clip || e.target.closest('input, textarea, select') || e.ctrlKey || e.metaKey || e.altKey) return;
    const shots = focus ? focus.shots.map(s => frameTime(s.start_frame)) : [];
    const k = e.key;
    // Live steps through the source's own frames (a render shows some of them twice).
    const step = (d) => mode === 'live' && grid ? seek(grid.seconds(grid.at(t) + d) + 5e-4) : seek(t + d / FPS);
    const act = {
      ' ': playCut,
      ArrowLeft: () => e.shiftKey ? seek(t - 1) : step(-1),
      ArrowRight: () => e.shiftKey ? seek(t + 1) : step(1),
      ArrowUp: () => moveClip(-1), ArrowDown: () => moveClip(1),
      i: () => setShift('start', t - clip.start), o: () => setShift('end', t - clip.end),
      '[': () => { const prev = shots.filter(s => s < t - 0.02).pop(); seek(prev ?? effectiveCut.start); },
      ']': () => { const next = shots.find(s => s > t + 0.02); if (next != null) seek(next + 1e-3); },
      Home: () => seek(firstFrame(effectiveCut.start)), End: () => seek(effectiveCut.end - 1 / FPS),
      l: () => setLoop(v => !v), w: () => setCtxLevel(v => (v + 1) % CONTEXTS.length),
      f: () => { const i = shotIndexAt(focus, frame); if (i >= 0 && framingEditable) editShot(i, {}, true, true); },
      a: () => setStatus('approved'), n: () => setStatus('needs_changes'), x: () => setStatus('rejected'),
      1: () => data.source.proxy_url && setMode('live'), 2: () => setMode('rendered'),
      3: () => clip.preview_url && setMode('preview'), 4: () => (clip.history_url || clip.preview_url) && setMode('compare'),
      '?': () => setHelp(v => !v),
    }[k.length === 1 ? k.toLowerCase() : k];
    if (act) { e.preventDefault(); act(); }
  };
  useEffect(() => {
    const handler = (e) => keyRef.current(e);
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  const visible = data ? data.clips.filter(c => filter === 'all' || ({ ...emptyEntry(), ...(entries[c.key] || {}) }).status === filter) : [];
  const moveClip = (d) => {
    const list = visible.length ? visible : data.clips;
    const i = list.findIndex(c => c.key === selected);
    const next = list[clamp(i + d, 0, list.length - 1)];
    if (next) setSelected(next.key);
  };

  // ----- pending changes for Apply -----
  const pending = useMemo(() => {
    if (!data) return [];
    const list = [];
    for (const c of data.clips) {
      const e = { ...emptyEntry(), ...(entries[c.key] || {}) };
      if (e.status === 'approved' && !c.locked) list.push(`${c.key}: lock approved clip`);
      if (e.status !== 'approved' && c.locked) list.push(`${c.key}: withdraw approval`);
      if (e.status === 'rejected' && !e.addressed) list.push(`${c.key}: remove rejected clip`);
      if (e.status === 'needs_changes' && !e.addressed) {
        if (e.focus_overrides != null) list.push(`${c.key}: apply framing`);
        if ((e.options || []).includes('recut') && (e.start_shift || e.end_shift)) list.push(`${c.key}: re-cut`);
        if (e.comment) list.push(`${c.key}: comment for the agent`);
      }
    }
    if (data.captions_dirty) list.push('Re-render clips with corrected captions');
    return list;
  }, [data, entries]);

  const runApply = async () => {
    setConfirmApply(false);
    try { setJob(await request('apply', {})); setShowLog(true); } catch (e) { notify(e.message, 'bad'); }
  };
  const runPreview = async () => {
    try { setJob(await request(`clips/${encodeURIComponent(clip.key)}/preview`, {})); } catch (e) { notify(e.message, 'bad'); }
  };
  const runExport = async () => {
    try { const r = await request('export', {}); notify(`${r.files.length} file(s) in ${r.folder}`, 'ok'); } catch (e) { notify(e.message, 'bad'); }
  };
  const runProxy = async () => {
    try { setJob(await request('proxy', {})); } catch (e) { notify(e.message, 'bad'); }
  };

  if (error) return html`<div class="fatal">${error}</div>`;
  if (!data || !clip) return html`<p class="boot">Loading…</p>`;

  const ctx = {
    clip, entry, cut, snapped, focus, overrides, settings: data.settings, limits: data.limits, duration: data.source.duration,
    words: captions && captions.words, groups: (captions && captions.groups) || [], groupsVersion,
    highlight: assColor(data.settings.highlight), proxyUrl: data.source.proxy_url, captionEdits: data.caption_edits,
    framingEditable, overrideActive: overrides.some(o => frame >= o.start_frame && frame < o.end_frame),
    grid, clipOrigin, cutOrigin, frame, frameTime, captionsEditable: !applying,
  };
  ctx.cut = effectiveCut ? { ...effectiveCut } : cut;
  const timelineCtx = { ...ctx, cut, snapped };
  const counts = { all: data.clips.length, pending: 0, approved: 0, needs_changes: 0, rejected: 0 };
  data.clips.forEach(c => counts[({ ...emptyEntry(), ...(entries[c.key] || {}) }).status]++);
  const hasPreview = !!clip.preview_url;
  const previewStale = hasPreview && clip.preview && entry.updated && clip.preview.decision_updated !== entry.updated;
  const jobRunning = job && job.state === 'running';

  let stageView;
  if (mode === 'live') {
    stageView = data.source.proxy_url
      ? html`<${LivePreview} ctx=${ctx} videoRef=${liveVideo} onTime=${setT}
          onFrameDraw=${(tt, c, image) => { lastFrame.current = [tt, c]; overviewDraw.current && overviewDraw.current(tt, c, image); }} onDragCenter=${dragCenter} />`
      : html`<div class="viewport empty"><p>The live preview needs a low-resolution proxy that keeps the source's own frames.</p>
          ${jobRunning && (job.kind === 'proxy' || job.kind === 'prepare') ? html`<p>Creating it… ${job.progress != null ? Math.round(job.progress * 100) + '%' : ''}</p>`
            : html`<button class="primary" disabled=${jobRunning} onClick=${runProxy}>Create proxy</button>`}</div>`;
  } else if (mode === 'rendered') {
    stageView = html`<${FileVideo} src=${clip.video_url} videoRef=${fileVideo} onTime=${setT} offset=${clip.start} label="Rendered" />`;
  } else if (mode === 'preview') {
    stageView = html`<${FileVideo} src=${clip.preview_url} videoRef=${fileVideo} onTime=${setT} offset=${clip.preview ? clip.preview.start : clip.start} label=${previewStale ? 'Preview (older than the latest edit)' : 'Preview render'} />`;
  } else {
    const before = clip.history_url ? { src: clip.history_url, label: 'Before' } : { src: clip.video_url, label: 'Rendered' };
    const after = clip.history_url ? { src: clip.video_url, label: 'After (current)' } : { src: clip.preview_url, label: 'Preview' };
    stageView = html`<div class="pair">
      <${FileVideo} src=${before.src} videoRef=${leftVideo} offset=${clip.start} label=${before.label} />
      <${FileVideo} src=${after.src} videoRef=${fileVideo} onTime=${setT} offset=${clip.start} label=${after.label} /></div>`;
  }

  const lockedClipNote = locked ? ' This clip is approved and keeps its file until the approval is withdrawn.' : '';
  return html`
  <div class="layout">
    <header class="top">
      <div class="brand"><b>Clipsmith</b><span class="muted">${data.source.name} · ${data.profile}</span></div>
      <div class="actions">
        ${job && job.state !== 'idle' && html`<button class="jobpill ${job.state}" onClick=${() => setShowLog(v => !v)}>
          ${job.kind}: ${job.state}${job.state === 'running' && job.progress != null ? ` ${Math.round(job.progress * 100)}%` : ''}</button>`}
        <button onClick=${runExport} title="Copy approved clips to the ready folder">Export approved</button>
        <a class="button" href=${data.gallery_url} target="_blank">Overview</a>
        <button onClick=${() => setHelp(v => !v)}>Shortcuts (?)</button>
        <button class="primary" disabled=${jobRunning || !pending.length} onClick=${() => setConfirmApply(true)}>Apply${pending.length ? ` (${pending.length})` : ''}</button>
      </div>
    </header>

    <aside class="side">
      <div class="filters">${Object.entries({ all: 'All', ...STATUS_LABELS }).map(([k, label]) => html`
        <button class=${filter === k ? 'on' : ''} onClick=${() => setFilter(k)}>${label} <span>${counts[k]}</span></button>`)}</div>
      <ul class="clips">${visible.map(c => {
        const e = { ...emptyEntry(), ...(entries[c.key] || {}) };
        const staged = e.status === 'needs_changes' && !e.addressed && (e.focus_overrides != null || e.start_shift || e.end_shift);
        return html`<li class="${c.key === selected ? 'sel' : ''}" data-status=${e.status} onClick=${() => setSelected(c.key)}>
          <img src=${c.poster_url} alt="" loading="lazy" />
          <div><div class="title"><span class="num">${c.key}</span>${c.title}</div>
            <div class="muted small">${(c.end - c.start).toFixed(1)} s · ${STATUS_LABELS[e.status]}
              ${c.locked ? ' · locked' : ''}${staged ? ' · staged' : ''}${e.addressed ? ' · addressed' : ''}</div></div></li>`;
      })}</ul>
    </aside>

    <main class="main">
      <div class="stage">${stageView}</div>
      <div class="transport">
        <button class="primary" onClick=${playCut} title="Space">Play / pause</button>
        <label class="check"><input type="checkbox" checked=${loop} onChange=${e => setLoop(e.target.checked)} />Loop (L)</label>
        <span class="mono">${fmt(t - ctx.cut.start)} / ${fmt(ctx.cut.end - ctx.cut.start)}</span>
        <span class="muted mono">source ${fmt(t)} · frame ${frame}${focus && shotIndexAt(focus, frame) >= 0 ? ` · shot ${shotIndexAt(focus, frame) + 1}` : ''}</span>
        <div class="seg">
          ${[['live', 'Live', !!data.source.proxy_url || true], ['rendered', 'Rendered', true], ['preview', 'Preview', hasPreview],
             ['compare', 'Compare', !!(clip.history_url || clip.preview_url)]].map(([k, label, ok], i) => html`
            <button class=${mode === k ? 'on' : ''} disabled=${!ok} onClick=${() => setMode(k)} title=${`Shortcut: ${i + 1}`}>${label}</button>`)}
        </div>
        <button onClick=${() => setCtxLevel(v => (v + 1) % CONTEXTS.length)} title="Timeline context around the cut (W)">
          Context ±${CONTEXTS[ctxLevel] >= 60 ? CONTEXTS[ctxLevel] / 60 + ' min' : CONTEXTS[ctxLevel] + ' s'} (W)</button>
      </div>
    </main>

    <div class="tl">
      <${Timeline} ctx=${timelineCtx} t=${t} pad=${CONTEXTS[ctxLevel]} onSeek=${seek}
        onEdge=${(edge, s) => setShift(edge, s - clip[edge], false)} onEdgeDone=${() => save(clip.key, true)}
        onSelectWord=${setSelWord} selectedWord=${selWord} onWordTiming=${saveCaption} />
    </div>

    <aside class="insp">
      <${Section} title=${html`<span class="num">${clip.key}</span>${clip.title}`}>
        <p class="note muted">${clip.reason}</p>
        <div class="seg status">${['approved', 'needs_changes', 'rejected'].map(s => html`
          <button data-v=${s} class=${entry.status === s ? 'on' : ''} disabled=${applying} onClick=${() => setStatus(s)}
            title=${{ approved: 'A', needs_changes: 'N', rejected: 'X' }[s]}>${STATUS_LABELS[s]}</button>`)}</div>
        ${locked && html`<p class="note">Approved clips are locked. Select Approved again to withdraw the approval and edit.</p>`}
        ${entry.addressed && html`<p class="note ok">Addressed${entry.agent_note ? ': ' + entry.agent_note : ''}</p>`}
        <textarea placeholder="Comment for the editorial agent (optional)" value=${entry.comment || ''} disabled=${applying}
          onInput=${e => edit({ comment: e.target.value }, false)}></textarea>
        <div class="buttons">
          <button disabled=${jobRunning} onClick=${runPreview} title="Render the exact result of the staged edits at low resolution">
            ${jobRunning && job.kind === 'preview' ? 'Rendering preview…' : 'Render exact preview'}</button>
          <a class="button" href=${clip.sheet_url} target="_blank">Framing sheet</a>
        </div>
      </${Section}>
      <${CutPanel} ctx=${ctx} t=${t} setShift=${setShift} nudgeShift=${nudgeShift} disabled=${locked || applying} />
      <${FramingPanel} ctx=${ctx} t=${t} frame=${frame} editShot=${editShot} setFraming=${setFraming} disabled=${locked || applying}
        overviewSlot=${mode === 'live' && data.source.proxy_url ? html`<${Overview} videoRef=${liveVideo} ctx=${ctx}
          register=${fn => { overviewDraw.current = fn; if (lastFrame.current) setTimeout(() => fn(...lastFrame.current), 0); }} onSetCenter=${dragCenter} />` : null} />
      <${CaptionPanel} ctx=${ctx} t=${t} selected=${selWord} onSelect=${setSelWord} onSave=${saveCaption} onAdd=${addCaptionWords}
        onRetranscribe=${runRetranscribe} proposals=${proposals} setProposals=${setProposals} onAccept=${acceptProposals}
        editable=${!applying} busy=${jobRunning} lockedNote=${lockedClipNote} />
    </aside>

    ${showLog && job && html`<div class="drawer">
      <div class="drawer-head"><b>${job.kind} · ${job.state}</b><button onClick=${() => setShowLog(false)}>Close</button></div>
      <pre>${(job.log || []).join('\n') || 'No output yet.'}</pre></div>`}
    ${confirmApply && html`<div class="modal" onClick=${() => setConfirmApply(false)}><div class="dialog" onClick=${e => e.stopPropagation()}>
      <h3>Apply these changes?</h3>
      <ul>${pending.map(p => html`<li>${p}</li>`)}</ul>
      <p class="note muted">The plan is updated and changed clips are re-rendered. Approved clips stay locked. Comments stay open for the agent.</p>
      <div class="buttons"><button onClick=${() => setConfirmApply(false)}>Cancel</button><button class="primary" onClick=${runApply}>Apply</button></div>
    </div></div>`}
    ${help && html`<div class="modal" onClick=${() => setHelp(false)}><div class="dialog" onClick=${e => e.stopPropagation()}>
      <h3>Keyboard shortcuts</h3>
      <table class="keys">${[
        ['Space', 'Play or pause the cut'], ['← / →', 'One frame (Shift: one second)'], ['I / O', 'Set start / end at the playhead'],
        ['[ / ]', 'Previous / next camera cut'], ['Home / End', 'Start / end of the cut'], ['↑ / ↓', 'Previous / next clip'],
        ['F', 'Override the framing of the current shot'], ['A / N / X', 'Approve / needs changes / reject'],
        ['1–4', 'Live / rendered / preview / compare'], ['L', 'Loop'], ['W', 'Timeline context: ±3 s, ±30 s, ±2 min, ±10 min'], ['?', 'This help']]
        .map(([k, d]) => html`<tr><td><kbd>${k}</kbd></td><td>${d}</td></tr>`)}</table>
    </div></div>`}
    ${toast && html`<div class="toast ${toast.kind}">${toast.text}</div>`}
  </div>`;
}

render(html`<${App} />`, document.getElementById('app'));
