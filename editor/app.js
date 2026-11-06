// Clip editor front end (Preact + htm, vendored; no build step).
import { h, render } from 'preact';
import { useState, useEffect, useLayoutEffect, useRef, useMemo, useCallback } from 'preact/hooks';
import htm from 'htm';

const html = htm.bind(h);
const FPS = 30;
const STATUS_LABELS = { pending: 'Pending', approved: 'Approved', needs_changes: 'Needs changes', rejected: 'Rejected' };
const CAPTION_EM = 0.82; // CSS em per ASS font size unit for Dosis (libass sizes by line height)
const MIN_SPAN = 2; // seconds: the closest timeline zoom
const LOCKED_NOTE = 'Approved clips are read-only. Select Approved again to withdraw the approval.';

// ---------- helpers ----------

async function request(path, body) {
  const options = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
  const response = await fetch('/api/' + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const r3 = (s) => Math.round(s * 1000) / 1000;
const frameOf = (t, start) => Math.floor((t - start) * FPS + 1e-6);
const CONTEXTS = [3, 30, 120, 600]; // timeline padding around the cut, in seconds

function readNumber(key, fallback) {
  try { const v = parseFloat(localStorage.getItem(key)); return isFinite(v) ? v : fallback; } catch (e) { return fallback; }
}
function writeSetting(key, value) {
  try { localStorage.setItem(key, String(value)); } catch (e) { /* a per-browser convenience only */ }
}

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
  return `${sign}${m}:${s.toFixed(digits).padStart(digits ? digits + 3 : 2, '0')}`;
}

function signed(v, digits = 2) {
  return (v > 0 ? '+' : v < 0 ? '−' : '±') + Math.abs(v).toFixed(digits) + ' s';
}

function assColor(value) {
  const hex = (value || '&H00FFFFFF').replace(/^&H/i, '').replace(/&$/, '').padStart(8, '0').slice(-6);
  return '#' + hex.slice(4, 6) + hex.slice(2, 4) + hex.slice(0, 2);
}

function emptyEntry() {
  return { status: 'pending', options: [], comment: '', start_shift: 0, end_shift: 0, notes: [] };
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

// ---------- icons (inline SVG, no icon font) ----------

const ICONS = {
  play: '<path d="M8 5.5v13l10.5-6.5z" fill="currentColor"/>',
  pause: '<rect x="7" y="5.5" width="3.5" height="13" rx="1" fill="currentColor" stroke="none"/><rect x="13.5" y="5.5" width="3.5" height="13" rx="1" fill="currentColor" stroke="none"/>',
  left: '<path d="M14.5 6l-6 6 6 6"/>', right: '<path d="M9.5 6l6 6-6 6"/>',
  prevcut: '<path d="M7 6v12"/><path d="M17.5 6.5L10 12l7.5 5.5z" fill="currentColor"/>',
  nextcut: '<path d="M17 6v12"/><path d="M6.5 6.5L14 12l-7.5 5.5z" fill="currentColor"/>',
  first: '<path d="M6 6v12"/><path d="M17 6l-6 6 6 6"/>', last: '<path d="M18 6v12"/><path d="M7 6l6 6-6 6"/>',
  loop: '<path d="M17 3l3 3-3 3"/><path d="M4 12v-2a4 4 0 0 1 4-4h12"/><path d="M7 21l-3-3 3-3"/><path d="M20 12v2a4 4 0 0 1-4 4H4"/>',
  undo: '<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
  redo: '<path d="M15 14l5-5-5-5"/><path d="M20 9H9.5a5.5 5.5 0 0 0 0 11H13"/>',
  history: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>', x: '<path d="M6 6l12 12M18 6L6 18"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  flag: '<path d="M5 21V4"/><path d="M5 4h11l-2 4 2 4H5"/>',
  alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17.5v.5"/>',
  pin: '<path d="M9 4h6l-1 6 3 3H7l3-3z"/><path d="M12 16v5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  cut: '<circle cx="6" cy="17" r="2.5"/><circle cx="18" cy="17" r="2.5"/><path d="M8 15.5L17 4M16 15.5L7 4"/>',
  crop: '<path d="M7 3v14h14"/><path d="M3 7h14v14"/>',
  captions: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 12h4M13 12h4M7 15.5h7"/>',
  review: '<path d="M4 5h16v11H9l-5 4z"/>',
  film: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4"/>',
  text: '<path d="M4 6h16M4 10h16M4 14h10M4 18h12"/>',
  keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.5M9.5 10h.5M13 10h.5M16.5 10h.5M7 14h10"/>',
  export: '<path d="M12 15V4M8 8l4-4 4 4"/><path d="M5 13v6h14v-6"/>',
  grid: '<rect x="4" y="4" width="7" height="7" rx="1"/><rect x="13" y="4" width="7" height="7" rx="1"/><rect x="4" y="13" width="7" height="7" rx="1"/><rect x="13" y="13" width="7" height="7" rx="1"/>',
  zoomin: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5M8.5 11h5M11 8.5v5"/>',
  zoomout: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5M8.5 11h5"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M12.5 15H17"/>',
  restore: '<path d="M4 12a8 8 0 1 0 2.5-5.8"/><path d="M4 4v4h4"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  newline: '<path d="M19 5v7a3 3 0 0 1-3 3H6"/><path d="M9.5 11.5L6 15l3.5 3.5"/>',
  wave: '<path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
  bolt: '<path d="M13 3L5 13.5h6L10 21l8-10.5h-6z"/>',
  preview: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 9.5v5l4.5-2.5z" fill="currentColor"/>',
  compare: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M12 3v18"/>',
  done: '<path d="M2.5 12.5L7 17l9.5-9.5"/><path d="M12 15.5l1.5 1.5L23 7.5"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4.5 6h.5M4.5 12h.5M4.5 18h.5"/>',
  find: '<circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5.5 5.5"/>',
};
function Icon({ n, cls = '' }) {
  return html`<svg class="ic ${cls}" viewBox="0 0 24 24" aria-hidden="true" dangerouslySetInnerHTML=${{ __html: ICONS[n] || '' }}></svg>`;
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
      <div class="tag">Live</div>
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
    g.strokeStyle = ctx.overrideActive ? '#f2c94c' : '#38bdf8';
    g.lineWidth = 3;
    g.strokeRect(x + 1.5, 1.5, frac * W - 3, H - 3);
  };
  useEffect(() => { register((t, c, image) => drawRef.current(t, c, image)); return () => register(null); }, []);
  const pick = (e, done) => {
    if (!ctx.framingEditable || (e.type === 'pointermove' && !e.buttons)) return;
    const r = canvasRef.current.getBoundingClientRect();
    onSetCenter(clamp((e.clientX - r.left) / r.width, 0, 1), done);
  };
  return html`<canvas class="overview ${ctx.framingEditable ? 'grab' : ''}" ref=${canvasRef} width="640" height="360"
    onPointerDown=${e => { e.currentTarget.setPointerCapture(e.pointerId); pick(e, false); }}
    onPointerMove=${e => pick(e, false)} onPointerUp=${e => pick(e, true)}></canvas>`;
}

// ---------- timeline ----------

function wordEdited(w) {
  return w.kind === 'added' || w.word !== w.original || w.start !== w.original_start || w.end !== w.original_end || !!w.line;
}

// A word the recogniser was unsure about and nobody has corrected yet.
function lowConfidence(w, threshold) {
  return w.probability != null && w.probability < threshold && !!w.word && !wordEdited(w);
}

function wordTitle(w) {
  const kind = { transcript: 'Transcript word', missed: 'Found by the missed-speech pass', added: 'Added in the editor' }[w.kind];
  const parts = [`${kind} · ${fmt(w.start)}–${fmt(w.end)}${w.probability != null ? ` · confidence ${Math.round(w.probability * 100)}%` : ''}`];
  if (w.kind !== 'added' && w.word !== w.original) parts.push(`${w.original} → ${w.word || '(removed)'}`);
  if (w.kind !== 'added' && (w.start !== w.original_start || w.end !== w.original_end)) parts.push(`retimed from ${fmt(w.original_start)}–${fmt(w.original_end)}`);
  if (w.line) parts.push(w.line === 'break' ? 'new caption line starts here' : 'kept on the previous line');
  return parts.join('\n');
}

const LANE = { ov: 22, ruler: 22, shots: 34, words: 42, props: 28, notes: 28 };
const STATUS_COLOR = { pending: 'var(--faint)', approved: 'var(--ok)', needs_changes: 'var(--warn)', rejected: 'var(--bad)' };

function Timeline({ ctx, t, view, setView, onSeek, onEdge, onEdgeDone, onSelectWord, selectedWord, onWordTiming, threshold,
                    proposals, notes, sourceClips, statusOf, zoomTip }) {
  const ref = useRef();
  const { w } = useSize(ref);
  const { clip, cut, snapped, focus, words, duration } = ctx;
  const [a, b] = view;
  const X = (s) => (s - a) / (b - a) * w;
  const T = (x) => a + clamp(x, 0, w) / w * (b - a);
  const OX = (s) => s / duration * w;
  const drag = useRef(null);
  const [wordDrag, setWordDrag] = useState(null);
  const live = useRef();
  live.current = { view, setView, w, duration };

  // Ctrl + wheel zooms around the cursor; the plain wheel scrolls the view sideways.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const wheel = (e) => {
      const { view: [va, vb], setView, w, duration } = live.current;
      if (!w) return;
      e.preventDefault();
      const span = vb - va, x = clamp(e.clientX - el.getBoundingClientRect().left, 0, w);
      if (e.ctrlKey || e.metaKey) {
        const at = va + x / w * span, next = clamp(span * Math.exp(e.deltaY * 0.0022), MIN_SPAN, duration);
        setView([at - (at - va) * next / span, at + (vb - at) * next / span], true);
      } else {
        const d = (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY) / w * span;
        setView([va + d, vb + d], true);
      }
    };
    el.addEventListener('wheel', wheel, { passive: false });
    return () => el.removeEventListener('wheel', wheel);
  }, []);

  const xOf = (e) => e.clientX - ref.current.getBoundingClientRect().left;
  const moveView = (x) => { const span = b - a, at = clamp(x, 0, w) / w * duration; setView([at - span / 2, at + span / 2]); };
  const down = (e, kind, word) => {
    e.stopPropagation();
    drag.current = kind;
    ref.current.setPointerCapture(e.pointerId);
    if (kind === 'seek') onSeek(T(xOf(e)));
    if (kind === 'ov') moveView(xOf(e));
    if (word) setWordDrag({ index: word.index, start: word.start, end: word.end });
  };
  const move = (e) => {
    if (!drag.current) return;
    const s = T(xOf(e));
    if (drag.current === 'seek') onSeek(s);
    else if (drag.current === 'ov') moveView(xOf(e));
    else if (drag.current === 'wstart') setWordDrag(d => d && { ...d, start: Math.min(s, d.end - 0.04) });
    else if (drag.current === 'wend') setWordDrag(d => d && { ...d, end: Math.max(s, d.start + 0.04) });
    else onEdge(drag.current, s);
  };
  const up = () => {
    const kind = drag.current;
    drag.current = null;
    if (kind === 'wstart' || kind === 'wend') {
      if (wordDrag) onWordTiming(wordDrag.index, { start: r3(wordDrag.start), end: r3(wordDrag.end) });
      setWordDrag(null);
    } else if (kind && kind !== 'seek' && kind !== 'ov') onEdgeDone();
  };

  const hasProps = !!(proposals && proposals.words.length && proposals.end >= a && proposals.start <= b);
  const top = { ruler: LANE.ov, shots: LANE.ov + LANE.ruler };
  top.words = top.shots + LANE.shots;
  top.props = top.words + LANE.words;
  top.notes = top.props + (hasProps ? LANE.props : 0);
  const bottom = top.notes + LANE.notes, y0 = LANE.ov, hh = bottom - y0;

  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  const step = steps.find(s => w && s / (b - a) * w >= 80) || 600;
  const ticks = [];
  for (let s = Math.ceil(a / step) * step; s <= b; s += step) ticks.push(s);
  const shotRows = focus ? focus.shots.map((s, i) => {
    const s0 = ctx.frameTime(s.start_frame), s1 = ctx.frameTime(s.end_frame);
    if (s1 < a || s0 > b) return null;
    const over = ctx.overrides.some(o => o.start_frame < s.end_frame && o.end_frame > s.start_frame);
    return html`<div class="shot ${i % 2 ? 'odd' : ''} ${over ? 'over' : ''} ${s.review.length ? 'flag' : ''}"
      title=${`Shot ${i + 1}: frames ${s.start_frame}–${s.end_frame - 1}${s.review.length ? ' · review: ' + s.review.map(r => r.reason || r.kind || r).join(', ') : ''}`}
      style=${{ left: X(s0) + 'px', width: Math.max(1, X(s1) - X(s0)) + 'px' }}
      onPointerDown=${e => { e.stopPropagation(); onSeek(s0 + 1e-3); }}>${X(s1) - X(s0) > 22 ? i + 1 : ''}</div>`;
  }) : null;
  const wordRows = (words || []).filter(wd => wd.end >= a && wd.start <= b).map(wd => {
    const timing = wordDrag && wordDrag.index === wd.index ? wordDrag : wd;
    const sel = selectedWord === wd.index;
    const width = Math.max(2, X(timing.end) - X(timing.start));
    return html`
    <div class="word ${wordEdited(wd) ? 'edited' : ''} ${wd.kind} ${!wd.word ? 'removed' : ''} ${lowConfidence(wd, threshold) ? 'low' : ''} ${sel ? 'sel' : ''} ${wd.line ? 'line-' + wd.line : ''}"
      title=${wordTitle(wd)}
      style=${{ left: X(timing.start) + 'px', width: width + 'px' }}
      onPointerDown=${e => { e.stopPropagation(); onSeek(wd.start + 1e-3); onSelectWord(wd.index); }}>
      ${sel && ctx.captionsEditable && html`<span class="wh l" title="Drag the word's start" onPointerDown=${e => down(e, 'wstart', wd)}></span>
        <span class="wh r" title="Drag the word's end" onPointerDown=${e => down(e, 'wend', wd)}></span>`}
      ${width > 26 ? (wd.word || wd.original) : ''}</div>`;
  });
  return html`<div class="tlbody">
    <div class="heads">
      <div style=${{ height: LANE.ov + 'px' }}><${Icon} n="film" />Source</div>
      <div style=${{ height: LANE.ruler + 'px' }}></div>
      <div style=${{ height: LANE.shots + 'px' }}><${Icon} n="crop" />Shots</div>
      <div style=${{ height: LANE.words + 'px' }}><${Icon} n="captions" />Words</div>
      ${hasProps && html`<div style=${{ height: LANE.props + 'px' }}><${Icon} n="wave" />Whisper</div>`}
      <div style=${{ height: LANE.notes + 'px' }}><${Icon} n="pin" />Notes</div>
    </div>
    <div class="timeline" ref=${ref} onPointerDown=${e => down(e, 'seek')} onPointerMove=${move} onPointerUp=${up}>
      <div class="ovstrip" style=${{ height: LANE.ov + 'px' }} title="The whole source: drag to move the view" onPointerDown=${e => down(e, 'ov')}>
        ${sourceClips.map(c => html`<span class="oc ${c.key === clip.key ? 'cur' : ''}"
          style=${{ left: OX(c.start) + 'px', width: Math.max(2, OX(c.end) - OX(c.start)) + 'px', background: STATUS_COLOR[statusOf(c.key)] }}></span>`)}
        <span class="win" style=${{ left: OX(a) + 'px', width: Math.max(4, OX(b) - OX(a)) + 'px' }}></span>
        <span class="oph" style=${{ left: OX(t) + 'px' }}></span>
      </div>
      <div class="lane ruler" style=${{ top: top.ruler + 'px', height: LANE.ruler + 'px' }}>
        ${ticks.map(s => html`<span style=${{ left: X(s) + 'px' }}>${fmt(s, step < 1 ? 1 : 0)}</span><i style=${{ left: X(s) + 'px' }}></i>`)}</div>
      <div class="lane" style=${{ top: top.shots + 'px', height: LANE.shots + 'px' }}>${shotRows}</div>
      <div class="lane" style=${{ top: top.words + 'px', height: LANE.words + 'px' }}>${wordRows}</div>
      ${hasProps && html`<div class="lane" style=${{ top: top.props + 'px', height: LANE.props + 'px' }}>${proposals.words.map(p => html`
        <div class="proposed ${p.on ? 'on' : ''} ${p.overlaps ? 'overlap' : ''}" title=${p.word}
          style=${{ left: X(p.start) + 'px', width: Math.max(3, X(p.end) - X(p.start)) + 'px' }}>${X(p.end) - X(p.start) > 24 ? p.word : ''}</div>`)}</div>`}
      <div class="lane" style=${{ top: top.notes + 'px', height: LANE.notes + 'px' }}>${notes.filter(n => n.t >= a && n.t <= b).map(n => html`
        <div class="pin" title=${n.text} style=${{ left: X(n.t) + 'px', maxWidth: Math.max(40, w - X(n.t)) + 'px' }}
          onPointerDown=${e => { e.stopPropagation(); onSeek(n.t); }}><${Icon} n="pin" />${n.text}</div>`)}</div>
      <div class="dim" style=${{ top: y0 + 'px', height: hh + 'px', left: 0, width: Math.max(0, X(cut.start)) + 'px' }}></div>
      <div class="dim" style=${{ top: y0 + 'px', height: hh + 'px', left: X(cut.end) + 'px', right: 0 }}></div>
      ${(cut.start !== clip.start || cut.end !== clip.end) && html`<div class="orig" title="Current cut"
        style=${{ top: y0 + 'px', height: hh + 'px', left: X(clip.start) + 'px', width: X(clip.end) - X(clip.start) + 'px' }}></div>`}
      <div class="cut" style=${{ top: y0 + 'px', height: hh + 'px', left: X(cut.start) + 'px', width: X(cut.end) - X(cut.start) + 'px' }}>
        <div class="handle l" title="Drag the start (I sets it at the playhead)" onPointerDown=${e => down(e, 'start')}></div>
        <div class="handle r" title="Drag the end (O sets it at the playhead)" onPointerDown=${e => down(e, 'end')}></div>
      </div>
      ${snapped && !snapped.error && Math.abs(snapped.start - cut.start) > 1e-3 && html`<div class="snap" style=${{ top: y0 + 'px', height: hh + 'px', left: X(snapped.start) + 'px' }} title="Applied start (word-safe)"></div>`}
      ${snapped && !snapped.error && Math.abs(snapped.end - cut.end) > 1e-3 && html`<div class="snap" style=${{ top: y0 + 'px', height: hh + 'px', left: X(snapped.end) + 'px' }} title="Applied end (word-safe)"></div>`}
      <div class="playhead" style=${{ top: y0 + 'px', left: X(t) + 'px' }}></div>
      ${zoomTip && html`<div class="zoomtip">${fmt(a, 1)}–${fmt(b, 1)} · ${(b - a).toFixed(1)} s</div>`}
    </div></div>`;
}

// ---------- inspector panels ----------

function Card({ title, icon, extra, children, cls = '' }) {
  return html`<section class="card ${cls}">${title && html`<h3>${icon && html`<${Icon} n=${icon} />`}${title}${extra != null && extra !== '' && html`<span class="x">${extra}</span>`}</h3>`}${children}</section>`;
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
  return html`<input class="field time" disabled=${disabled} value=${draft ?? fmt(value)} size="9" title="Type a time (m:ss.ss or seconds) and press Enter"
    onInput=${e => setDraft(e.target.value)} onBlur=${commit}
    onKeyDown=${e => { if (e.key === 'Enter') { e.preventDefault(); commit(e); e.target.blur(); } if (e.key === 'Escape') { setDraft(null); e.target.blur(); } }} />`;
}

function LockedLine({ locked }) {
  return locked ? html`<div class="infoline"><${Icon} n="lock" /><span>${LOCKED_NOTE}</span></div>` : null;
}

function ReviewPanel({ ctx, clip, entry, locked, applying, jobRunning, job, t, onStatus, onApproveNext, onComment, notes, noteDraft,
                       onDraftNote, onSaveDraft, onEditNote, onDeleteNote, issues, issueIndex, onIssue, onPreview }) {
  const draftRef = useRef();
  useEffect(() => { if (noteDraft && draftRef.current) draftRef.current.focus(); }, [noteDraft && noteDraft.t]);
  const rel = (s) => fmt(s - ctx.cut.start);
  const options = (entry.options || []).map(o => html`<span class="opt"><${Icon} n=${o === 'recut' ? 'cut' : 'crop'} />${o === 'recut' ? 'Re-cut' : 'Fix framing'}</span>`);
  const noteKey = (e, save) => { if (e.key === 'Enter') { e.preventDefault(); save(e.target.value); e.target.blur(); } if (e.key === 'Escape') { e.target.blur(); } };
  return html`
    <${Card}>
      <div class="clipttl"><span class="id">${clip.key}</span><h2>${clip.title}</h2><span class="d">${(ctx.cut.end - ctx.cut.start).toFixed(1)} s</span></div>
      <p class="note">${clip.reason}</p>
      <div class="decide">${[['approved', 'Approve', 'check', 'A'], ['needs_changes', 'Needs changes', 'review', 'N'], ['rejected', 'Reject', 'x', 'X']].map(([v, label, icon, key]) => html`
        <button data-v=${v} class=${entry.status === v ? 'on' : ''} disabled=${applying} onClick=${() => onStatus(v)} title=${`${STATUS_LABELS[v]} (${key})`}>
          <${Icon} n=${icon} />${label}<kbd>${key}</kbd></button>`)}</div>
      <${LockedLine} locked=${locked} />
      ${entry.addressed && html`<div class="infoline ok"><${Icon} n="check" /><span>Addressed${entry.agent_note ? ': ' + entry.agent_note : ''}</span></div>`}
      <div class="staged">${options.length ? html`Staged: ${options}` : 'Nothing staged for this clip.'}</div>
      <button class="btn primary wide" disabled=${applying} onClick=${onApproveNext} title="Approve this clip and open the next pending one (Shift+A)">
        <${Icon} n="done" />Approve and open the next pending clip<kbd>Shift+A</kbd></button>
    </${Card}>
    <${Card} title="Comment for the agent" icon="review">
      <textarea class="field" placeholder="What should the editorial agent change? (optional)" value=${entry.comment || ''} disabled=${applying || locked}
        onInput=${e => onComment(e.target.value)}></textarea>
    </${Card}>
    <${Card} title="Notes on the timeline" icon="pin" extra=${notes.length ? `${notes.length} · M adds one` : 'M adds one'}>
      <div class="notes">
        ${notes.map((n, i) => html`<div class="noterow" key=${n.t + ':' + i}>
          <button class="btn link" onClick=${() => onIssue({ t: n.t })} title=${`Source ${fmt(n.t)}`}>${rel(n.t)}</button>
          <input class="field" value=${n.text} disabled=${applying || locked} onBlur=${e => onEditNote(i, e.target.value)} onKeyDown=${e => noteKey(e, v => onEditNote(i, v))} />
          <button class="btn icon sm ghost" disabled=${applying || locked} onClick=${() => onDeleteNote(i)} aria-label="Delete note" title="Delete note"><${Icon} n="trash" /></button></div>`)}
        ${noteDraft && html`<div class="noterow">
          <span class="mono small muted">${rel(noteDraft.t)}</span>
          <input class="field" ref=${draftRef} placeholder="What should change here?" onBlur=${e => onSaveDraft(e.target.value)} onKeyDown=${e => noteKey(e, onSaveDraft)} />
          <span></span></div>`}
      </div>
      ${!notes.length && !noteDraft && html`<p class="note">Pin a note to a moment so the agent knows exactly where to look.</p>`}
      <div class="buttons"><button class="btn sm" disabled=${applying || locked} onClick=${onDraftNote}><${Icon} n="plus" />Add note at the playhead<kbd>M</kbd></button></div>
    </${Card}>
    <${Card} title="Check before deciding" icon="flag" extra=${`${issues.length} issue${issues.length === 1 ? '' : 's'} · E / Shift+E`}>
      ${issues.length ? html`<div>${issues.map((x, k) => html`<button class="issue ${issueIndex === k ? 'cur' : ''}" onClick=${() => onIssue(x, k)}>
        <${Icon} n=${x.kind === 'shot' ? 'flag' : 'alert'} cls=${x.kind === 'shot' ? 'f' : 'w'} /><span class="t">${rel(x.t)}</span>
        <span class="txt"><b>${x.label}</b> · ${x.text}</span></button>`)}</div>`
        : html`<p class="note">No flagged shots or doubtful words in this cut.</p>`}
    </${Card}>
    <${Card}>
      <div class="buttons">
        <button class="btn" disabled=${jobRunning} onClick=${onPreview} title="Render the exact result of the staged edits at low resolution">
          <${Icon} n="preview" />${jobRunning && job.kind === 'preview' ? 'Rendering preview…' : 'Render exact preview'}</button>
        <a class="btn" href=${clip.sheet_url} target="_blank"><${Icon} n="grid" />Framing sheet</a>
      </div>
      <p class="note">The exact preview renders the staged edits at 432 × 540 into Review/, never into the output folder.</p>
    </${Card}>`;
}

function CutPanel({ ctx, t, doShift, disabled, locked }) {
  const { clip, entry, snapped } = ctx;
  const row = (edge, label, key) => {
    const base = clip[edge], shift = entry[edge + '_shift'] || 0;
    const nudge = (d) => doShift(edge, Math.round((shift + d) * 1000) / 1000, `${clip.key}: ${edge} ${signed(Math.round((shift + d) * 1000) / 1000)}`);
    return html`<div class="edge">
      <div class="edge-head"><b>${label}</b><${TimeInput} value=${base + shift} disabled=${disabled} onSet=${s => doShift(edge, s - base, `${clip.key}: ${edge} typed`)} />
        <span class="shift">${signed(shift)}</span><span class="grow"></span>
        <button class="btn sm" disabled=${disabled} onClick=${() => doShift(edge, t - base, `${clip.key}: ${edge} at the playhead`)} title=${`Shortcut: ${key}`}>At playhead<kbd>${key}</kbd></button>
        <button class="btn sm ghost" disabled=${disabled || !shift} onClick=${() => doShift(edge, 0, `${clip.key}: ${edge} reset`)}><${Icon} n="restore" />Reset</button></div>
      <div class="nudges">${[['−5 s', -5], ['−0.5 s', -0.5], ['−1 f', -1 / FPS], ['+1 f', 1 / FPS], ['+0.5 s', 0.5], ['+5 s', 5]].map(([l, d]) => html`
        <button class="btn" disabled=${disabled} onClick=${() => nudge(d)}>${l}</button>`)}</div></div>`;
  };
  let result = html`<div class="result"><${Icon} n="cut" /><span>Edges unchanged: <b class="mono">${fmt(clip.start)}–${fmt(clip.end)}</b>.</span></div>`;
  if (entry.start_shift || entry.end_shift) {
    result = snapped == null ? html`<div class="result"><${Icon} n="cut" /><span>Checking word boundaries…</span></div>`
      : snapped.error ? html`<div class="result badline"><${Icon} n="alert" /><span>${snapped.error}</span></div>`
      : html`<div class="result good"><${Icon} n="check" /><span>Apply will use <b class="mono">${fmt(snapped.start)}–${fmt(snapped.end)}</b> (${(snapped.end - snapped.start).toFixed(2)} s); edges never cut a word.</span></div>`;
  }
  return html`<${LockedLine} locked=${locked} />
    <${Card} title="Cut" icon="cut" extra=${`${(ctx.cut.end - ctx.cut.start).toFixed(2)} s · 3 to 180 s`}>
      ${row('start', 'Start', 'I')}${row('end', 'End', 'O')}${result}
      <p class="note">Drag the blue handles on the timeline, or press I and O at the playhead. An edge can move anywhere in the source.</p>
    </${Card}>`;
}

function FramingPanel({ ctx, frame, editShot, setFraming, overviewSlot, disabled, locked, onSeek }) {
  const { focus, overrides } = ctx;
  if (!focus) return html`<${Card} title="Framing" icon="crop"><p class="note">Loading focus data…</p></${Card}>`;
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
  return html`<${LockedLine} locked=${locked} />
  <${Card} title="Framing" icon="crop" extra=${inside ? `Shot ${index + 1} of ${focus.shots.length}` : 'Outside the analysed cut'}>
    ${overviewSlot}
    ${!focus.exact && html`<p class="note warn">Automatic centers come from the rendered file and already include reviewed overrides.</p>`}
    ${inside ? html`
      <p class="note">Frames ${shot.start_frame}–${shot.end_frame - 1} (${((shot.end_frame - shot.start_frame) / FPS).toFixed(2)} s).
        ${full ? (full.center_x_end != null ? ' Reviewed pan.' : ' Reviewed center.') : mine.length ? ' Partial reviewed override.' : ' Automatic speaker focus.'}
        ${shot.review.length ? html` <span class="bad">Flagged: ${shot.review.map(r => r.reason || r.kind || r).join(' · ')}.</span>` : ''}</p>
      <div class="buttons">
        <button class="btn sm ${full ? 'on' : ''}" disabled=${disabled} onClick=${() => editShot(index, full ? {} : { center_x: centerAt(frame, focus, overrides) }, true, true)}
          title="Shortcut: F"><${Icon} n="crop" />${full ? 'Keep override' : 'Override this shot'}<kbd>F</kbd></button>
        <button class="btn sm" disabled=${disabled || !mine.length} onClick=${() => editShot(index, null, true)}><${Icon} n="restore" />Back to automatic</button>
        <label class="check"><input type="checkbox" disabled=${disabled || !full} checked=${full && full.center_x_end != null}
          onChange=${e => editShot(index, e.target.checked ? { center_x_end: full.center_x } : { center_x_end: undefined }, true)} />Pan</label>
      </div>
      ${full && slider(full.center_x_end != null ? 'Start' : 'Center', full.center_x, 'center_x')}
      ${full && full.center_x_end != null && slider('End', full.center_x_end, 'center_x_end')}
      <p class="note">Drag the picture or the overview to move the crop${full && full.center_x_end != null ? ' (first half of the shot moves the start, second half the end)' : ''}. Automatic center here: ${(auto * 100).toFixed(1)}%.</p>`
    : html`<p class="note">Framing can only be edited inside the current cut; new parts are analysed when the change is applied.</p>`}
  </${Card}>
  <${Card} title="Shots in this clip" icon="list" extra=${`${focus.shots.filter(s => s.review.length).length} flagged`}>
    <div class="shotlist">${focus.shots.map((s, i) => {
      const o = overrides.find(x => x.start_frame < s.end_frame && x.end_frame > s.start_frame);
      return html`<button class="shotrow ${i === index ? 'cur' : ''}" onClick=${() => onSeek(ctx.frameTime(s.start_frame) + 1e-3)}
        title=${s.review.length ? s.review.map(r => r.reason || r.kind || r).join(' · ') : ''}>
        <span class="n">${i + 1}</span><span class="mono">${s.start_frame}–${s.end_frame - 1}</span><span class="mono">${((s.end_frame - s.start_frame) / FPS).toFixed(2)} s</span>
        <span class=${o ? 'mode-over' : ''}>${o ? (o.center_x_end != null ? `Pan ${Math.round(o.center_x * 100)}% → ${Math.round(o.center_x_end * 100)}%` : `Override ${Math.round(o.center_x * 100)}%`) : 'Automatic'}</span>
        ${s.review.length ? html`<${Icon} n="flag" />` : html`<span></span>`}</button>`;
    })}</div>
  </${Card}>
  <${Card}>
    <div class="buttons">
      <button class="btn sm" disabled=${disabled || !staged} onClick=${() => setFraming(null)} title="Drop staged framing; the plan's overrides stay">Discard framing edits</button>
      <button class="btn sm" disabled=${disabled || !overrides.length} onClick=${() => setFraming([])}>Clear all overrides</button>
    </div>
    <p class="note">Discard drops the staged list and keeps the plan's overrides. Clear removes every override of this clip.</p>
  </${Card}>`;
}

function CaptionPanel({ ctx, t, selected, onSelect, onSave, onAdd, onRetranscribe, proposals, setProposals, onAccept,
                        editable, busy, locked, threshold, job }) {
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
  const existing = proposals ? all.filter(w => w.end > proposals.start - 0.4 && w.start < proposals.end + 0.4 && w.word) : [];
  const ticked = proposals ? proposals.words.filter(p => p.on && p.word.trim()).length : 0;
  return html`<${LockedLine} locked=${locked} />
    <${Card} title="Words near the playhead" icon="captions" extra=${ctx.captionEdits ? `${ctx.captionEdits} edits in this source` : ''}>
    <div class="words-edit">
      ${words.map(w => html`<label class="wordbox ${wordEdited(w) ? 'edited' : ''} ${w.index === selected ? 'sel' : ''} ${w.start <= t && t < w.end ? 'now' : ''} ${w.kind} ${lowConfidence(w, threshold) ? 'low' : ''}"
          title=${wordTitle(w)}>
        ${w.line === 'break' && html`<span class="linemark" title="A new caption line starts here">↵</span>`}
        ${w.line === 'join' && html`<span class="linemark" title="Kept on the previous line">⁀</span>`}
        ${kindTag(w)}
        <input value=${drafts[w.index] ?? w.word} placeholder=${w.original || ''} size=${Math.max(3, (drafts[w.index] ?? (w.word || w.original)).length + 1)} disabled=${!editable}
          onFocus=${() => onSelect(w.index)}
          onInput=${e => setDrafts(d => ({ ...d, [w.index]: e.target.value }))}
          onKeyDown=${e => { if (e.key === 'Enter') { e.preventDefault(); commit(w, e.target.value); e.target.blur(); } if (e.key === 'Escape') { e.target.value = w.word; setDrafts(d => ({ ...d, [w.index]: undefined })); e.target.blur(); } }}
          onBlur=${e => commit(w, e.target.value)} />
        ${editable && wordEdited(w) && w.kind !== 'added' && html`<button class="revert" title="Restore the original word and timing" aria-label="Restore" onClick=${() => restore(w)}><${Icon} n="restore" /></button>`}
      </label>`)}
      ${!words.length && html`<p class="note">No words near the playhead.</p>`}
    </div>
    <p class="note">Enter saves a word; an empty word removes it. Click a word on the timeline or in the transcript to select it.</p>
    </${Card}>
    ${sel && html`<${Card} cls="selword">
      <div class="wh"><b>${sel.word || '(removed)'}</b><span class="mono small muted">${fmt(sel.start)}–${fmt(sel.end)}</span>
        <span class="badge st-pending">${{ transcript: 'Transcript', missed: 'Missed-speech pass', added: 'Added' }[sel.kind]}</span>
        ${sel.kind !== 'added' && sel.word !== sel.original && html`<span class="small muted">was “${sel.original}”</span>`}
        ${sel.probability != null && html`<span class="small muted">confidence ${Math.round(sel.probability * 100)}%</span>`}
        <button class="btn sm ghost push" onClick=${() => onSelect(null)}>Deselect</button></div>
      <div class="tools">
        <button class="btn sm" disabled=${!editable} onClick=${() => onSave(sel.index, t < sel.end ? { start: r3(t) } : { start: r3(t), end: r3(t + sel.end - sel.start) })}><${Icon} n="first" />Start at playhead</button>
        <button class="btn sm" disabled=${!editable || t <= sel.start} onClick=${() => onSave(sel.index, { end: r3(t) })}><${Icon} n="last" />End at playhead</button>
        <button class="btn sm ${sel.line === 'break' ? 'on' : ''}" disabled=${!editable} onClick=${() => toggleLine(sel, 'break')} title="Start a new caption line at this word"><${Icon} n="newline" />New line here</button>
        <button class="btn sm ${sel.line === 'join' ? 'on' : ''}" disabled=${!editable} onClick=${() => toggleLine(sel, 'join')} title="Keep this word on the previous caption line"><${Icon} n="link" />Join previous line</button>
        <button class="btn sm" disabled=${!editable || !wordEdited(sel)} onClick=${() => restore(sel)}><${Icon} n=${sel.kind === 'added' ? 'trash' : 'restore'} />${sel.kind === 'added' ? 'Delete word' : 'Restore original'}</button>
      </div>
      <p class="note">Drag the word's blue edges on the timeline to retime it. A line never grows past 34 characters.</p>
    </${Card}>`}
    <${Card} title="Add words at the playhead" icon="plus">
      <div class="add-words">
        <input class="field" placeholder="Type words, then Enter" value=${addText} disabled=${!editable}
          onInput=${e => setAddText(e.target.value)} onKeyDown=${e => { if (e.key === 'Enter') { e.preventDefault(); add(); } }} />
        <button class="btn sm" disabled=${!editable || !addText.trim()} onClick=${add}>Add</button>
      </div>
    </${Card}>
    <${Card} title="Re-transcribe around the playhead" icon="wave">
      <div class="buttons">
        <button class="btn sm" disabled=${!editable || busy} onClick=${() => onRetranscribe(rangeStart, rangeEnd)}
          title=${`Run Whisper again on ${fmt(rangeStart)}–${fmt(rangeEnd)} and propose the words it hears`}>
          <${Icon} n="wave" />${busy && job && job.kind === 'transcribe' ? 'Listening…' : 'Run Whisper again'}</button>
        <span class="mono small muted">${fmt(rangeStart)}–${fmt(rangeEnd)}, without VAD</span>
      </div>
      ${proposals && html`<div class="props">
        <p class="note text">Whisper heard ${proposals.words.length ? proposals.words.length + ' word' + (proposals.words.length === 1 ? '' : 's') : 'nothing'} in <span class="mono">${fmt(proposals.start)}–${fmt(proposals.end)}</span>.
          ${proposals.words.length && proposals.words.every(p => p.overlaps) ? ' All of them overlap words the captions already have, so none start ticked; tick a word to add it anyway.'
            : ' Words that overlap an existing word start unticked.'}</p>
        <div class="alg"><span>Now</span><div class="ws">${existing.length ? existing.map(w => html`<span class="ew ${w.kind}" title=${wordTitle(w)}>${w.word}</span>`) : html`<span class="muted">no words</span>`}</div></div>
        <div class="alg"><span>Whisper</span><div class="ws">${proposals.words.map((p, i) => html`<label class="prop ${p.on ? 'on' : ''} ${p.overlaps ? 'overlap' : ''}"
          title=${p.overlaps ? 'Overlaps a word the captions already have' : 'No existing word here'}>
          <input type="checkbox" checked=${p.on} onChange=${e => setProposals(ps => ({ ...ps, words: ps.words.map((q, j) => j === i ? { ...q, on: e.target.checked } : q) }))} />
          <input class="pt" value=${p.word} size=${Math.max(3, p.word.length + 1)} onInput=${e => setProposals(ps => ({ ...ps, words: ps.words.map((q, j) => j === i ? { ...q, word: e.target.value } : q) }))} />
          <span class="mono">${fmt(p.start - ctx.cut.start, 1)}</span></label>`)}</div></div>
        <div class="buttons">
          <button class="btn sm primary" disabled=${!editable || !ticked} onClick=${onAccept}><${Icon} n="plus" />Add ticked words (${ticked})</button>
          <button class="btn sm" onClick=${() => setProposals(null)}>Dismiss</button></div></div>`}
    </${Card}>
    <p class="foot">Caption edits apply to every clip of this source on the next Apply, except approved (locked) clips.</p>`;
}

// ---------- transcript (left panel) ----------

function TranscriptPanel({ words, groups, cut, t, selected, onWord, threshold, setThreshold, findText, setFindText, replText, setReplText,
                           matches, onReplaceAll, onJump, editable }) {
  const curRef = useRef();
  const hit = useMemo(() => new Set(matches.flatMap(m => m.words.map(w => w.index))), [matches]);
  const here = matches.filter(m => m.start < cut.end && m.end > cut.start);
  const lines = useMemo(() => {
    const out = [];
    const chunk = (list) => { for (let i = 0; i < list.length; i += 8) out.push({ words: list.slice(i, i + 8), out: true }); };
    chunk(words.filter(w => w.end <= cut.start));
    out.push({ mark: `Cut starts ${fmt(cut.start)}` });
    // Lines inside the cut follow the renderer's caption groups.
    const inner = groups.map(g => ({ start: cut.start + g[0].start, end: cut.start + g[g.length - 1].until, words: [] }));
    words.filter(w => w.start < cut.end && w.end > cut.start).forEach(w => {
      let host = null;
      for (const line of inner) if (line.start <= w.start + 0.02) host = line;
      (host || inner[0] || (inner[0] = { start: w.start, end: w.end, words: [] }, inner[0])).words.push(w);
    });
    inner.filter(l => l.words.length).forEach(l => out.push(l));
    out.push({ mark: `Cut ends ${fmt(cut.end)}` });
    chunk(words.filter(w => w.start >= cut.end));
    return out;
  }, [words, groups, cut.start, cut.end]);
  const curKey = lines.findIndex(l => l.start != null && t >= l.start && t < l.end);
  useEffect(() => { if (curRef.current) curRef.current.scrollIntoView({ block: 'nearest' }); }, [curKey]);
  const pct = Math.round(threshold * 100);
  return html`<div class="find">
      <div class="row"><input class="field" id="find-input" placeholder="Find in the source (Ctrl+F)" value=${findText} onInput=${e => setFindText(e.target.value)}
          onKeyDown=${e => { if (e.key === 'Enter') { e.preventDefault(); onJump(e.shiftKey ? -1 : 1); } }} />
        <input class="field" placeholder="Replace with" value=${replText} disabled=${!editable} onInput=${e => setReplText(e.target.value)} /></div>
      <div class="res"><${Icon} n="find" /><span>${findText.trim() ? `${matches.length} match${matches.length === 1 ? '' : 'es'} in the source · ${here.length} in this cut` : 'Search every caption word of the source.'}</span>
        <span style=${{ flex: 1 }}></span>
        <button class="btn icon sm ghost" disabled=${!matches.length} onClick=${() => onJump(-1)} aria-label="Previous match" title="Previous match (Shift+Enter)"><${Icon} n="left" /></button>
        <button class="btn icon sm ghost" disabled=${!matches.length} onClick=${() => onJump(1)} aria-label="Next match" title="Next match (Enter)"><${Icon} n="right" /></button>
        <button class="btn sm" disabled=${!editable || !matches.length || !replText.trim()} onClick=${onReplaceAll} title="Replace every match in the source; Ctrl+Z undoes it">Replace all</button></div>
      <label class="conf"><span>Underline words below</span><input type="range" min="50" max="100" step="5" value=${pct} onInput=${e => setThreshold(+e.target.value / 100)} /><b>${pct}%</b></label>
    </div>
    <div class="tx">${lines.map((l, k) => l.mark ? html`<div class="cutmark"><${Icon} n="cut" />${l.mark}</div>` : html`
      <div class="ln ${l.out ? 'out' : ''} ${k === curKey ? 'cur' : ''}" ref=${k === curKey ? curRef : null}>
        <span class="tm">${fmt(l.words[0].start - cut.start, 1)}</span>
        <span>${l.words.map((w, i) => html`${i ? ' ' : ''}<span class=${['tw', w.kind !== 'transcript' ? w.kind : '', wordEdited(w) && w.word ? 'edited' : '', !w.word ? 'removed' : '',
          lowConfidence(w, threshold) ? 'low' : '', hit.has(w.index) ? 'match' : '', w.word && w.start <= t && t < w.end ? 'now' : '', selected === w.index ? 'sel' : ''].join(' ')}
          title=${wordTitle(w)} onClick=${() => onWord(w)}>${w.word || w.original}</span>`)}</span></div>`)}
      ${!words.length && html`<p class="empty-note">No caption words around this clip.</p>`}
    </div>`;
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
  const [view, setViewState] = useState(null);
  const [zoomTip, setZoomTip] = useState(false);
  const [grid, setGrid] = useState(null);
  const [proposals, setProposals] = useState(null);
  const [captionsVersion, setCaptionsVersion] = useState(0);
  const [filter, setFilter] = useState('all');
  const [query, setQuery] = useState('');
  const [binTab, setBinTab] = useState('clips');
  const [tab, setTab] = useState('review');
  const [job, setJob] = useState(null);
  const [showLog, setShowLog] = useState(false);
  const [confirmApply, setConfirmApply] = useState(false);
  const [help, setHelp] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [toast, setToast] = useState(null);
  const [selWord, setSelWord] = useState(null);
  const [groupsVersion, setGroupsVersion] = useState(0);
  const [threshold, setThresholdState] = useState(() => clamp(readNumber('clipsmith.confidence', 0.8), 0.5, 1));
  const [findText, setFindText] = useState('');
  const [replText, setReplText] = useState('');
  const [matches, setMatches] = useState([]);
  const [issueIndex, setIssueIndex] = useState(-1);
  const [noteDraft, setNoteDraft] = useState(null);
  const [playing, setPlaying] = useState(false);
  const [, setHistTick] = useState(0);
  const liveVideo = useRef(), fileVideo = useRef(), leftVideo = useRef();
  const overviewDraw = useRef(null);
  const lastFrame = useRef(null);
  const saveTimers = useRef({});
  const saveQueue = useRef({});
  const entriesRef = useRef({});
  const confirmed = useRef({});
  const captionsRef = useRef(null);
  const toastTimer = useRef(0);
  const tipTimer = useRef(0);
  const history = useRef({ undo: [], redo: [] });
  const tracking = useRef(null);
  const listRef = useRef();
  captionsRef.current = captions;

  const notify = (text, kind = 'info') => {
    clearTimeout(toastTimer.current);
    setToast({ text, kind });
    toastTimer.current = setTimeout(() => setToast(null), 4000);
  };
  const setThreshold = (v) => { setThresholdState(v); writeSetting('clipsmith.confidence', v); };

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
          if (j.state === 'done' && j.kind === 'transcribe') { showProposals(); setTab('captions'); }
          if (j.kind === 'prepare') setCaptionsVersion(v => v + 1);
          // Apply rewrites the plan and the decisions, so earlier edits can no longer be undone.
          if (j.kind === 'apply') { history.current = { undo: [], redo: [] }; setHistTick(v => v + 1); setCaptionsVersion(v => v + 1); }
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
  const duration = data ? data.source.duration : 1;
  // Grid origins: framing uses the current clip's frames, captions the cut that would be applied.
  const clipOrigin = grid && clip ? grid.origin(clip.start) : null;
  const cutOrigin = grid && effectiveCut ? grid.origin(effectiveCut.start) : null;
  const frameTime = (n) => grid && clipOrigin != null ? grid.frameTime(n, clipOrigin) : clip.start + n / FPS;
  // In Live, the first source frame a render of a cut starting at `start` shows (it can start a little after it).
  const firstFrame = (start) => mode === 'live' && grid ? grid.seconds(grid.origin(start)) + 5e-4 : start;

  // Timeline view: the cut with the chosen context, or wherever Ctrl + wheel and the overview strip took it.
  const pad = CONTEXTS[ctxLevel];
  const tlView = view || (clip ? [Math.max(0, Math.min(clip.start, cut.start) - pad), Math.min(duration, Math.max(clip.end, cut.end) + pad)] : [0, 1]);
  const setView = (range, tip) => {
    const span = clamp(range[1] - range[0], MIN_SPAN, duration);
    const a = clamp(range[0], 0, Math.max(0, duration - span));
    setViewState([a, a + span]);
    if (tip) { setZoomTip(true); clearTimeout(tipTimer.current); tipTimer.current = setTimeout(() => setZoomTip(false), 900); }
  };
  // Words are loaded for as much context as the view shows (the server allows 30 s to 10 min).
  const wordContext = !cut ? 30 : view ? Math.min(600, Math.max(30, Math.ceil(Math.max(cut.start - view[0], view[1] - cut.end, 0) / 30) * 30))
    : CONTEXTS[ctxLevel];

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
      request(`clips/${encodeURIComponent(clip.key)}/captions?start=${capStart}&end=${capEnd}&context=${wordContext}`)
        .then(c => { setCaptions(c); setGroupsVersion(v => v + 1); }).catch(e => notify('Captions: ' + e.message, 'bad'));
    }, 150);
    return () => clearTimeout(timer);
  }, [clip && clip.key, capStart, capEnd, data && data.caption_edits, captionsVersion, wordContext]);

  // Find in the whole source (debounced).
  useEffect(() => {
    const text = findText.trim();
    if (!text) { setMatches([]); return; }
    const timer = setTimeout(() => {
      request(`find?q=${encodeURIComponent(text)}`).then(r => setMatches(r.matches)).catch(e => notify('Find: ' + e.message, 'bad'));
    }, 250);
    return () => clearTimeout(timer);
  }, [findText, captionsVersion]);

  // When the selected clip changes, park the playhead at its start.
  useEffect(() => {
    if (!clip) return;
    setSelWord(null);
    setIssueIndex(-1);
    setNoteDraft(null);
    setTimeout(() => seek(firstFrame(clip.start)), 0);
  }, [clip && clip.key, mode]);
  useEffect(() => { setViewState(null); }, [clip && clip.key]);
  // Keep the selected clip visible in the list.
  useEffect(() => { const el = listRef.current && listRef.current.querySelector('li.sel'); if (el) el.scrollIntoView({ block: 'nearest' }); }, [selected, binTab]);

  useEffect(() => {
    if (!data) return;
    if (!data.source.proxy_url && mode === 'live') setMode('rendered');
  }, [data && data.source.id, data && data.source.proxy_url]);

  // A zoomed view follows the playhead when it runs past an edge.
  useEffect(() => {
    if (!view) return;
    const span = view[1] - view[0];
    if (t > view[1] || t < view[0]) setView([t - span * 0.1, t + span * 0.9]);
  }, [t]);

  // ----- saving -----
  // Local edits live in entriesRef (always the newest). Saves for a clip run one at a time,
  // send the newest local state and name the server version they were based on.
  const bodyOf = (e) => {
    const body = { status: e.status, options: e.options || [], comment: e.comment || '',
                   start_shift: e.start_shift || 0, end_shift: e.end_shift || 0, notes: e.notes || [] };
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

  // ----- undo and redo -----
  // Decision edits are recorded as before/after snapshots of the clip's entry; a drag is one
  // step from its first move to its release. Caption edits record the batch that undoes them.
  const pushHistory = (item) => {
    const hst = history.current;
    hst.undo.push(item);
    if (hst.undo.length > 200) hst.undo.shift();
    hst.redo = [];
    setHistTick(v => v + 1);
  };
  const snapshotOf = (key) => {
    const e = { ...emptyEntry(), ...(entriesRef.current[key] || {}) };
    return { status: e.status, options: [...(e.options || [])], comment: e.comment || '', start_shift: e.start_shift || 0,
             end_shift: e.end_shift || 0, focus_overrides: e.focus_overrides ?? null, notes: (e.notes || []).map(n => ({ ...n })) };
  };
  const restoreEntry = (key, snap) => {
    setLocal(key, { ...emptyEntry(), ...(entriesRef.current[key] || {}), ...snap });
    save(key, true);
  };
  const finishTracking = (label) => {
    const tr = tracking.current;
    tracking.current = null;
    if (!tr) return;
    const after = snapshotOf(tr.key);
    // Only the fields this step changed are put back, so undo never touches a comment
    // (which is not recorded) or anything edited later.
    const keys = Object.keys(after).filter(k => JSON.stringify(after[k]) !== JSON.stringify(tr.before[k]));
    if (!keys.length) return;
    const pick = (snap) => Object.fromEntries(keys.map(k => [k, snap[k]]));
    const { key, before } = tr;
    pushHistory({ label, undo: async () => restoreEntry(key, pick(before)), redo: async () => restoreEntry(key, pick(after)) });
  };
  const tracked = (label, immediate, fn) => {
    if (!clip) return;
    if (!tracking.current || tracking.current.key !== clip.key) tracking.current = { key: clip.key, before: snapshotOf(clip.key) };
    fn();
    if (immediate) finishTracking(label);
  };
  const runHistory = async (direction) => {
    if (applying) return notify('Apply is running; undo waits until it finishes.', 'bad');
    const hst = history.current;
    const from = direction === 'undo' ? hst.undo : hst.redo, to = direction === 'undo' ? hst.redo : hst.undo;
    const item = from.pop();
    if (!item) return notify(direction === 'undo' ? 'Nothing to undo.' : 'Nothing to redo.');
    try {
      await item[direction]();
      to.push(item);
      if (item.captions) await captionsSaved(null);
      notify(`${direction === 'undo' ? 'Undone' : 'Redone'}: ${item.label}`, 'ok');
    } catch (e) { notify(`Could not ${direction} “${item.label}”: ${e.message}`, 'bad'); }
    setHistTick(v => v + 1);
  };

  const stage = (option, change, immediate = true) => {
    if (locked || applying) return;
    const options = Array.from(new Set([...(latest().options || []), option]));
    return edit({ ...change, status: 'needs_changes', options }, immediate);
  };

  const setStatus = (status) => {
    if (applying) return;
    const next = latest().status === status ? 'pending' : status;
    tracked(`${clip.key}: ${STATUS_LABELS[next]}`, true, () => edit({ status: next }));
  };

  const approveNext = () => {
    if (applying || !clip) return;
    const key = clip.key;
    if (latest().status !== 'approved') tracked(`${key}: Approved`, true, () => edit({ status: 'approved' }));
    const order = data.clips, i = order.findIndex(c => c.key === key);
    const next = [...order.slice(i + 1), ...order.slice(0, i)].find(c => ({ ...emptyEntry(), ...(entriesRef.current[c.key] || {}) }).status === 'pending');
    if (next) setSelected(next.key);
    notify(next ? `Approved ${key}; opened ${next.key}, the next pending clip.` : `Approved ${key}. No pending clips left.`, 'ok');
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
  const doShift = (edge, value, label) => {
    if (locked) return notify(LOCKED_NOTE);
    tracked(label || `${clip.key}: ${edge} moved`, true, () => setShift(edge, value, true));
  };

  const overrides = clip ? overridesFor(clip, entry) : [];
  // Live: the grid frame where the render shows the source frame on screen. File modes:
  // the file's own 30 fps frame.
  const frame = !clip ? 0 : mode === 'live' && grid && clipOrigin != null ? grid.slot(grid.at(t), clipOrigin)
    : frameOf(t, mode === 'preview' && clip.preview ? clip.preview.start : clip.start);

  const setFraming = (list) => {
    tracked(list === null ? `${clip.key}: framing edits discarded` : `${clip.key}: all overrides cleared`, true, () => {
      if (list === null) edit({ focus_overrides: null });
      else stage('fix_framing', { focus_overrides: list });
    });
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
  const editShotTracked = (index, change, immediate, create = false) =>
    tracked(`${clip.key}: framing of shot ${index + 1}`, immediate, () => editShot(index, change, immediate, create));

  const framingEditable = !!focus && !locked && !applying && shotIndexAt(focus, frame) >= 0;
  const dragCenter = (center, done) => {
    const index = shotIndexAt(focus, frame);
    if (index < 0) return;
    const shot = focus.shots[index];
    const current = overridesFor(clip, latest()).find(o => o.start_frame === shot.start_frame && o.end_frame === shot.end_frame);
    const panEnd = current && current.center_x_end != null && frame >= (shot.start_frame + shot.end_frame) / 2;
    tracked(`${clip.key}: crop of shot ${index + 1} moved`, done, () => editShot(index, { [panEnd ? 'center_x_end' : 'center_x']: center }, done, true));
  };

  // ----- notes pinned to the timeline -----
  const notes = entry ? entry.notes || [] : [];
  const draftNote = () => {
    if (!clip || applying) return;
    if (locked) return notify(LOCKED_NOTE);
    setTab('review');
    setNoteDraft({ key: clip.key, t: r3(t) });
  };
  const saveDraftNote = (text) => {
    const draft = noteDraft;
    setNoteDraft(null);
    if (!draft || draft.key !== clip.key || !text.trim()) return;
    const list = [...notes, { t: draft.t, text: text.trim() }].sort((x, y) => x.t - y.t);
    tracked(`${clip.key}: note at ${fmt(draft.t - effectiveCut.start)}`, true,
      () => edit(latest().status === 'pending' ? { notes: list, status: 'needs_changes' } : { notes: list }));
  };
  const editNote = (i, text) => {
    if (!notes[i] || notes[i].text === text.trim()) return;
    tracked(`${clip.key}: note ${text.trim() ? 'edited' : 'deleted'}`, true,
      () => edit({ notes: text.trim() ? notes.map((n, j) => j === i ? { ...n, text: text.trim() } : n) : notes.filter((_, j) => j !== i) }));
  };
  const deleteNote = (i) => tracked(`${clip.key}: note deleted`, true, () => edit({ notes: notes.filter((_, j) => j !== i) }));

  // ----- captions -----
  const captionsSaved = async (text) => {
    const d = await request('state');
    setData(d);
    setCaptionsVersion(v => v + 1);
    if (text) notify(text, 'ok');
  };
  const wordState = (index) => ((captionsRef.current && captionsRef.current.words) || []).find(w => w.index === index);
  const near = (x, y) => y != null && Math.abs(x - y) < 5e-4;
  // The change that puts a word back the way it was.
  const inverseOf = (w) => w.kind === 'added'
    ? { index: w.index, to: w.word, start: w.start, end: w.end, line: w.line ?? null }
    : { index: w.index, to: w.word === w.original ? null : w.word, start: near(w.start, w.original_start) ? null : w.start,
        end: near(w.end, w.original_end) ? null : w.end, line: w.line ?? null };
  const describe = (w, change) => {
    const name = `“${(w && (w.word || w.original)) || '?'}”`;
    if ('to' in change && change.to === null && 'start' in change) return `Caption: ${name} restored`;
    if ('to' in change) return change.to ? `Caption: ${name} → “${change.to}”` : `Caption: ${name} removed`;
    if ('line' in change) return change.line ? `Caption: ${name} ${change.line === 'break' ? 'starts a new line' : 'joins the previous line'}` : `Caption: ${name} line mark removed`;
    return `Caption: ${name} retimed`;
  };
  // Deleting an added word is undone by adding it again (it gets a new id).
  const deleteAdded = async (w) => {
    try {
      await request('captions', { index: w.index, to: '' });
      let id = w.index;
      pushHistory({ label: `Caption: “${w.word}” deleted`, captions: true,
        undo: async () => { const r = await request('captions/add', { words: [{ word: w.word, start: w.start, end: w.end, ...(w.line ? { line: w.line } : {}) }] }); id = r.ids[0]; },
        redo: async () => { await request('captions', { index: id, to: '' }); } });
      await captionsSaved('Word deleted.');
      return true;
    } catch (e) { notify('Caption not saved: ' + e.message, 'bad'); return false; }
  };
  const changeCaptions = async (label, changes, before, okText = 'Caption saved.') => {
    try {
      await request('captions/batch', { changes });
      if (before.length === changes.length) {
        const undoChanges = before.map(inverseOf);
        pushHistory({ label, captions: true, undo: () => request('captions/batch', { changes: undoChanges }), redo: () => request('captions/batch', { changes }) });
      }
      await captionsSaved(okText);
      return true;
    } catch (e) { notify('Caption not saved: ' + e.message, 'bad'); return false; }
  };
  const saveCaption = async (index, changes) => {
    const w = wordState(index);
    if (w && w.kind === 'added' && changes.to === '') return deleteAdded(w);
    return changeCaptions(describe(w, changes), [{ index, ...changes }], w ? [w] : []);
  };
  const addCaptionWords = async (words) => {
    try {
      const r = await request('captions/add', { words });
      let ids = r.ids;
      pushHistory({ label: `Caption: added “${words.map(w => w.word).join(' ')}”`, captions: true,
        undo: () => request('captions/batch', { changes: ids.map(id => ({ index: id, to: '' })) }),
        redo: async () => { ids = (await request('captions/add', { words })).ids; } });
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
      const existing = (captionsRef.current && captionsRef.current.words) || [];
      // A proposal that mostly overlaps a word the captions already have starts unticked.
      const words = r.words.map(p => {
        const overlaps = existing.some(w => w.word && Math.min(w.end, p.end) - Math.max(w.start, p.start) > 0.5 * (p.end - p.start));
        return { ...p, overlaps, on: !overlaps };
      });
      setProposals({ start: r.start, end: r.end, words });
    } catch (e) { notify('Re-transcription: ' + e.message, 'bad'); }
  };
  const acceptProposals = async () => {
    const chosen = proposals.words.filter(p => p.on && p.word.trim()).map(p => ({ word: p.word.trim(), start: p.start, end: p.end }));
    if (await addCaptionWords(chosen)) setProposals(null);
  };
  // Replace every match of the search in the source. A match of several words becomes one
  // word with their combined timing; the last word's trailing punctuation is kept.
  const replaceAll = () => {
    const text = replText.trim().replace(/[.,!?;:…]+$/, '');
    if (!text || !matches.length) return;
    const changes = [], before = [];
    for (const m of matches) {
      const ws = m.words, lastWord = ws[ws.length - 1];
      if (ws.slice(1).some(w => w.kind === 'added')) continue;
      const tail = (lastWord.word.match(/[.,!?;:…]+$/) || [''])[0];
      changes.push({ index: ws[0].index, to: text + tail, ...(ws.length > 1 ? { end: lastWord.end } : {}) });
      before.push(ws[0]);
      ws.slice(1).forEach(w => { changes.push({ index: w.index, to: '' }); before.push(w); });
    }
    if (!changes.length) return;
    changeCaptions(`Captions: “${findText.trim()}” → “${text}” (${matches.length}×)`, changes, before,
      `Replaced ${matches.length} match${matches.length === 1 ? '' : 'es'}. Ctrl+Z undoes it.`);
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
  // The play button follows the video element.
  useEffect(() => {
    const v = mode === 'live' ? liveVideo.current : fileVideo.current;
    if (!v) return;
    const on = () => setPlaying(true), off = () => setPlaying(false);
    setPlaying(!v.paused);
    v.addEventListener('play', on); v.addEventListener('pause', off); v.addEventListener('ended', off);
    return () => { v.removeEventListener('play', on); v.removeEventListener('pause', off); v.removeEventListener('ended', off); };
  }, [mode, clip && clip.key, data && data.source.proxy_url]);
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

  // Jump somewhere and make sure the timeline shows it.
  const reveal = (s) => {
    const [a, b] = tlView;
    if (s < a || s > b) { const span = b - a; setView([s - span / 2, s + span / 2]); }
    seek(s);
  };

  // ----- issues: flagged shots and doubtful words -----
  const issues = useMemo(() => {
    if (!clip || !effectiveCut) return [];
    const list = [];
    if (focus) focus.shots.forEach((s, i) => {
      if (s.review.length) list.push({ kind: 'shot', t: frameTime(s.start_frame) + 1e-3, label: `Shot ${i + 1}`, text: s.review.map(r => r.reason || r.kind || r).join(' · ') });
    });
    ((captions && captions.words) || []).forEach(w => {
      if (lowConfidence(w, threshold) && w.start >= effectiveCut.start && w.start < effectiveCut.end)
        list.push({ kind: 'word', t: w.start + 1e-3, label: `“${w.word}”`, text: `low confidence, ${Math.round(w.probability * 100)}%`, index: w.index });
    });
    return list.sort((x, y) => x.t - y.t);
  }, [clip && clip.key, focus, captions, threshold, effectiveCut && effectiveCut.start, effectiveCut && effectiveCut.end, grid]);
  const openIssue = (x, k) => {
    if (k != null) setIssueIndex(k);
    if (x.index != null) setSelWord(x.index);
    reveal(x.t);
  };
  const stepIssue = (d) => {
    if (!issues.length) return notify('No flagged shots or doubtful words in this cut.');
    let k;
    if (d > 0) { k = issues.findIndex(x => x.t > t + 0.02); if (k < 0) k = 0; }
    else { k = -1; issues.forEach((x, j) => { if (x.t < t - 0.02) k = j; }); if (k < 0) k = issues.length - 1; }
    openIssue(issues[k], k);
  };
  const jumpMatch = (d) => {
    const loaded = (captions && captions.words) || [];
    const list = matches.filter(m => loaded.some(w => w.index === m.words[0].index));
    if (!list.length) return notify(matches.length ? 'The matches are in other parts of the source.' : 'No matches.');
    let k = d > 0 ? list.findIndex(m => m.start > t + 0.02) : -1;
    if (d < 0) list.forEach((m, j) => { if (m.start < t - 0.02) k = j; });
    if (k < 0) k = d > 0 ? 0 : list.length - 1;
    setSelWord(list[k].words[0].index);
    reveal(list[k].start + 1e-3);
  };

  // ----- keyboard -----
  const keyRef = useRef();
  keyRef.current = (e) => {
    const inField = e.target.closest && e.target.closest('input, textarea, select');
    if ((e.ctrlKey || e.metaKey) && !e.altKey) {
      const k = e.key.toLowerCase();
      if ((k === 'z' || k === 'y') && !inField) { e.preventDefault(); runHistory(k === 'y' || e.shiftKey ? 'redo' : 'undo'); }
      else if (k === 'f') { e.preventDefault(); setBinTab('transcript'); setTimeout(() => { const f = document.getElementById('find-input'); if (f) { f.focus(); f.select(); } }, 0); }
      return;
    }
    if (e.key === 'Escape') { setHelp(false); setShowHistory(false); setConfirmApply(false); return; }
    if (!clip || inField || e.altKey) return;
    const shots = focus ? focus.shots.map(s => frameTime(s.start_frame)) : [];
    const k = e.key;
    // Live steps through the source's own frames (a render shows some of them twice).
    const step = (d) => mode === 'live' && grid ? seek(grid.seconds(grid.at(t) + d) + 5e-4) : seek(t + d / FPS);
    const act = {
      ' ': playCut,
      ArrowLeft: () => e.shiftKey ? seek(t - 1) : step(-1),
      ArrowRight: () => e.shiftKey ? seek(t + 1) : step(1),
      ArrowUp: () => moveClip(-1), ArrowDown: () => moveClip(1),
      i: () => doShift('start', t - clip.start, `${clip.key}: start at the playhead`),
      o: () => doShift('end', t - clip.end, `${clip.key}: end at the playhead`),
      '[': () => { const prev = shots.filter(s => s < t - 0.02).pop(); seek(prev ?? effectiveCut.start); },
      ']': () => { const next = shots.find(s => s > t + 0.02); if (next != null) seek(next + 1e-3); },
      Home: () => seek(firstFrame(effectiveCut.start)), End: () => seek(effectiveCut.end - 1 / FPS),
      l: () => setLoop(v => !v), w: () => { setCtxLevel(v => (v + 1) % CONTEXTS.length); setViewState(null); },
      f: () => { const i = shotIndexAt(focus, frame); if (i >= 0 && framingEditable) { editShotTracked(i, {}, true, true); setTab('framing'); } },
      a: () => e.shiftKey ? approveNext() : setStatus('approved'), n: () => setStatus('needs_changes'), x: () => setStatus('rejected'),
      m: draftNote, e: () => stepIssue(e.shiftKey ? -1 : 1),
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

  const statusOf = (key) => ({ ...emptyEntry(), ...(entries[key] || {}) }).status;
  const visible = data ? data.clips.filter(c => (filter === 'all' || statusOf(c.key) === filter)
    && (!query.trim() || c.title.toLowerCase().includes(query.trim().toLowerCase()) || c.key.includes(query.trim()))) : [];
  const moveClip = (d) => {
    const list = visible.length ? visible : data.clips;
    const i = list.findIndex(c => c.key === selected);
    const next = list[clamp(i + d, 0, list.length - 1)];
    if (next) setSelected(next.key);
  };

  // ----- pending changes for Apply -----
  const pending = useMemo(() => {
    const groups = { lock: [], withdraw: [], recut: [], framing: [], comment: [], notes: [], remove: [] };
    if (!data) return { groups, captions: false, total: 0 };
    for (const c of data.clips) {
      const e = { ...emptyEntry(), ...(entries[c.key] || {}) };
      if (e.status === 'approved' && !c.locked) groups.lock.push(c);
      if (e.status !== 'approved' && c.locked) groups.withdraw.push(c);
      if (e.status === 'rejected' && !e.addressed) groups.remove.push(c);
      if (e.status === 'needs_changes' && !e.addressed) {
        if (e.focus_overrides != null) groups.framing.push(c);
        if ((e.options || []).includes('recut') && (e.start_shift || e.end_shift)) groups.recut.push(c);
        if (e.comment) groups.comment.push(c);
        if ((e.notes || []).length) groups.notes.push(c);
      }
    }
    const total = Object.values(groups).reduce((n, g) => n + g.length, 0) + (data.captions_dirty ? 1 : 0);
    return { groups, captions: !!data.captions_dirty, total };
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
  data.clips.forEach(c => counts[statusOf(c.key)]++);
  const decided = counts.approved + counts.needs_changes + counts.rejected;
  const pct = (n) => (counts.all ? n / counts.all * 100 : 0) + '%';
  const hasPreview = !!clip.preview_url;
  const previewStale = hasPreview && clip.preview && entry.updated && clip.preview.decision_updated !== entry.updated;
  const jobRunning = job && job.state === 'running';
  const shotNow = focus ? shotIndexAt(focus, frame) : -1;

  let stageView;
  if (mode === 'live') {
    stageView = data.source.proxy_url
      ? html`<${LivePreview} ctx=${ctx} videoRef=${liveVideo} onTime=${setT}
          onFrameDraw=${(tt, c, image) => { lastFrame.current = [tt, c]; overviewDraw.current && overviewDraw.current(tt, c, image); }} onDragCenter=${dragCenter} />`
      : html`<div class="viewport empty"><p>The live preview needs a low-resolution proxy that keeps the source's own frames.</p>
          ${jobRunning && (job.kind === 'proxy' || job.kind === 'prepare') ? html`<p>Creating it… ${job.progress != null ? Math.round(job.progress * 100) + '%' : ''}</p>`
            : html`<button class="btn primary" disabled=${jobRunning} onClick=${runProxy}>Create proxy</button>`}</div>`;
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
  const modeInfo = { live: html`<${Icon} n="eye" /><span>Proxy picture · approximate audio level</span>`, rendered: html`<span>The output file</span>`,
    preview: html`<span>432 × 540 with the real filters and captions</span>`, compare: html`<span>Both play in step</span>` }[mode];

  const tabDot = (k) => {
    if (k === 'cut') return (entry.start_shift || entry.end_shift) && !entry.addressed ? html`<span class="dot"></span>` : null;
    if (k === 'framing') return entry.focus_overrides != null && !entry.addressed ? html`<span class="dot"></span>` : null;
    if (k === 'review') return notes.length ? html`<span class="cnt">${notes.length}</span>` : null;
    if (k === 'captions') return data.caption_edits ? html`<span class="cnt">${data.caption_edits}</span>` : null;
    return null;
  };
  let inspector;
  if (tab === 'review') {
    inspector = html`<${ReviewPanel} ctx=${ctx} clip=${clip} entry=${entry} locked=${locked} applying=${applying} jobRunning=${jobRunning} job=${job} t=${t}
      onStatus=${setStatus} onApproveNext=${approveNext} onComment=${v => edit({ comment: v }, false)} notes=${notes} noteDraft=${noteDraft && noteDraft.key === clip.key ? noteDraft : null}
      onDraftNote=${draftNote} onSaveDraft=${saveDraftNote} onEditNote=${editNote} onDeleteNote=${deleteNote}
      issues=${issues} issueIndex=${issueIndex} onIssue=${openIssue} onPreview=${runPreview} />`;
  } else if (tab === 'cut') {
    inspector = html`<${CutPanel} ctx=${ctx} t=${t} doShift=${doShift} disabled=${locked || applying} locked=${locked} />`;
  } else if (tab === 'framing') {
    inspector = html`<${FramingPanel} ctx=${ctx} frame=${frame} editShot=${editShotTracked} setFraming=${setFraming} disabled=${locked || applying} locked=${locked}
      onSeek=${reveal}
      overviewSlot=${mode === 'live' && data.source.proxy_url ? html`<${Overview} videoRef=${liveVideo} ctx=${ctx}
        register=${fn => { overviewDraw.current = fn; if (fn && lastFrame.current) setTimeout(() => fn(...lastFrame.current), 0); }} onSetCenter=${dragCenter} />` : null} />`;
  } else {
    inspector = html`<${CaptionPanel} ctx=${ctx} t=${t} selected=${selWord} onSelect=${setSelWord} onSave=${saveCaption} onAdd=${addCaptionWords}
      onRetranscribe=${runRetranscribe} proposals=${proposals} setProposals=${setProposals} onAccept=${acceptProposals}
      editable=${!applying && !locked} busy=${jobRunning} locked=${locked} threshold=${threshold} job=${job} />`;
  }

  const hst = history.current;
  const G = pending.groups;
  const group = (list, title, icon, extra) => list.length ? html`<div class="pg"><div class="pgh"><${Icon} n=${icon} />${title}<span class="x">${list.length}</span></div>
    <div class="items">${list.map(c => html`<span class="it"><b>${c.key}</b>${c.title}${extra ? extra(c) : ''}</span>`)}</div></div>` : null;
  const shifts = (c) => { const e = { ...emptyEntry(), ...(entries[c.key] || {}) };
    return ` (${[e.start_shift ? 'start ' + signed(e.start_shift) : '', e.end_shift ? 'end ' + signed(e.end_shift) : ''].filter(Boolean).join(', ')})`; };

  return html`
  <div class="layout">
    <header class="top">
      <div class="brand"><svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true"><rect x="1" y="1" width="20" height="20" rx="5" fill="var(--accent)"/>
        <rect x="7" y="5" width="8" height="10" rx="1.5" fill="none" stroke="#fff" stroke-width="1.8"/><path d="M5 18h12" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/></svg>Clipsmith</div>
      <div class="crumb"><b>${data.source.name}</b><${Icon} n="right" /><span>${data.profile}</span></div>
      <div class="progress" title=${`${counts.approved} approved, ${counts.needs_changes} need changes, ${counts.rejected} rejected, ${counts.pending} pending`}>
        <span class="pbar"><i style=${{ width: pct(counts.approved), background: 'var(--ok)' }}></i><i style=${{ width: pct(counts.needs_changes), background: 'var(--warn)' }}></i>
          <i style=${{ width: pct(counts.rejected), background: 'var(--bad)' }}></i></span>
        <span><b>${decided}</b> of ${counts.all} decided</span></div>
      <span class="sp"></span>
      <div class="grp">
        <button class="btn icon ghost" disabled=${!hst.undo.length} onClick=${() => runHistory('undo')} title=${hst.undo.length ? `Undo: ${hst.undo[hst.undo.length - 1].label} (Ctrl+Z)` : 'Nothing to undo'} aria-label="Undo"><${Icon} n="undo" /></button>
        <button class="btn icon ghost" disabled=${!hst.redo.length} onClick=${() => runHistory('redo')} title=${hst.redo.length ? `Redo: ${hst.redo[hst.redo.length - 1].label} (Ctrl+Shift+Z)` : 'Nothing to redo'} aria-label="Redo"><${Icon} n="redo" /></button>
        <div style=${{ position: 'relative' }}>
          <button class="btn ghost ${showHistory ? 'on' : ''}" onClick=${() => setShowHistory(v => !v)} title="Edit history"><${Icon} n="history" />History</button>
          ${showHistory && html`<div class="pop" style=${{ position: 'absolute', top: '38px', right: 0 }}>
            <h4><${Icon} n="history" />Edit history<span class="grow"></span>
              <button class="btn sm" disabled=${!hst.undo.length} onClick=${() => runHistory('undo')}><${Icon} n="undo" />Undo</button>
              <button class="btn sm" disabled=${!hst.redo.length} onClick=${() => runHistory('redo')}><${Icon} n="redo" />Redo</button></h4>
            ${[...hst.redo].map(item => html`<div class="hrow undone"><${Icon} n="redo" /><span>${item.label}</span></div>`)}
            ${[...hst.undo].reverse().map(item => html`<div class="hrow"><${Icon} n="undo" /><span>${item.label}</span></div>`)}
            ${!hst.undo.length && !hst.redo.length && html`<p class="note" style=${{ padding: '4px 6px' }}>No edits in this window yet.</p>`}
            <p class="foot" style=${{ margin: '8px 6px 4px' }}>Ctrl+Z and Ctrl+Shift+Z step through decision, cut, framing, note and caption edits made in this window. Apply clears the list.</p>
          </div>`}
        </div>
      </div>
      <span class="sep"></span>
      <div class="grp">
        ${job && job.state !== 'idle' && html`<button class="btn jobpill ${job.state}" onClick=${() => setShowLog(v => !v)} title="Show the job log">
          <${Icon} n=${job.state === 'running' ? 'bolt' : job.state === 'done' ? 'check' : 'alert'} />
          ${job.kind}: ${job.state}${job.state === 'running' && job.progress != null ? ` ${Math.round(job.progress * 100)}%` : ''}
          ${job.state === 'running' && job.progress != null && html`<span class="bar"><i style=${{ width: job.progress * 100 + '%' }}></i></span>`}</button>`}
        <button class="btn ghost" onClick=${runExport} title="Copy approved clips to the ready folder"><${Icon} n="export" />Export approved</button>
        <a class="btn ghost" href=${data.gallery_url} target="_blank" title="Read-only overview of the output folder"><${Icon} n="grid" />Overview</a>
        <button class="btn icon ghost" onClick=${() => setHelp(v => !v)} title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts"><${Icon} n="keyboard" /></button>
        <button class="btn primary" disabled=${jobRunning || !pending.total} onClick=${() => setConfirmApply(true)}><${Icon} n="check" />Apply${pending.total ? ` (${pending.total})` : ''}</button>
      </div>
    </header>

    <aside class="panel bin">
      <div class="ptabs">
        <button class="ptab ${binTab === 'clips' ? 'on' : ''}" onClick=${() => setBinTab('clips')}><${Icon} n="film" />Clips <span class="cnt">${counts.all}</span></button>
        <button class="ptab ${binTab === 'transcript' ? 'on' : ''}" onClick=${() => setBinTab('transcript')}><${Icon} n="text" />Transcript</button>
      </div>
      <div class="binbody">${binTab === 'clips' ? html`
        <div class="search"><input class="field" placeholder="Search clips by title or id" value=${query} onInput=${e => setQuery(e.target.value)} /></div>
        <div class="filters">${Object.entries({ all: 'All', ...STATUS_LABELS }).map(([k, label]) => html`
          <button class="chip ${filter === k ? 'on' : ''}" onClick=${() => setFilter(k)}>${label} <b>${counts[k]}</b></button>`)}</div>
        <ul class="clips" ref=${listRef}>${visible.map(c => {
          const e = { ...emptyEntry(), ...(entries[c.key] || {}) };
          const staged = e.status === 'needs_changes' && !e.addressed && (e.focus_overrides != null || e.start_shift || e.end_shift);
          return html`<li class=${c.key === selected ? 'sel' : ''} onClick=${() => setSelected(c.key)}>
            <img src=${c.poster_url} alt="" loading="lazy" />
            <div style=${{ minWidth: 0 }}><div class="title"><span class="num">${c.key}</span>${c.title}</div>
              <div class="meta"><span class="badge st-${e.status}">${STATUS_LABELS[e.status]}</span><span class="mono">${(c.end - c.start).toFixed(1)} s</span>
                ${c.locked && html`<span class="mk-locked" title="Locked: the approved file is kept"><${Icon} n="lock" /></span>`}
                ${staged && html`<span class="mk-staged" title="Staged cut or framing edits">●</span>`}
                ${(e.notes || []).length > 0 && html`<span class="muted" title=${`${e.notes.length} note(s) for the agent`}><${Icon} n="pin" /></span>`}
                ${e.addressed && html`<span class="mk-addr" title="Addressed by the agent"><${Icon} n="check" /></span>`}</div></div></li>`;
        })}${!visible.length && html`<li class="empty-note">No clips match.</li>`}</ul>
        <div class="legend"><span><${Icon} n="lock" />locked</span><span><span class="mk-staged">●</span>staged edits</span><span><${Icon} n="pin" />notes</span>
          <span><span class="mk-addr"><${Icon} n="check" /></span>addressed</span></div>`
        : html`<${TranscriptPanel} words=${(captions && captions.words) || []} groups=${(captions && captions.groups) || []} cut=${ctx.cut} t=${t} selected=${selWord}
            onWord=${w => { setSelWord(w.index); setTab('captions'); reveal(w.start + 1e-3); }} threshold=${threshold} setThreshold=${setThreshold}
            findText=${findText} setFindText=${setFindText} replText=${replText} setReplText=${setReplText} matches=${matches}
            onReplaceAll=${replaceAll} onJump=${jumpMatch} editable=${!applying} />`}</div>
    </aside>

    <main class="panel mon">
      <div class="monhead">
        <div class="seg">
          ${[['live', 'Live', 'bolt', !!data.source.proxy_url || true], ['rendered', 'Rendered', 'film', true], ['preview', 'Preview', 'preview', hasPreview],
             ['compare', 'Compare', 'compare', !!(clip.history_url || clip.preview_url)]].map(([k, label, icon, ok], i) => html`
            <button class=${mode === k ? 'on' : ''} disabled=${!ok} onClick=${() => setMode(k)} title=${`Shortcut: ${i + 1}`}><${Icon} n=${icon} />${label}<kbd>${i + 1}</kbd></button>`)}
        </div>
        <div class="info">${modeInfo}</div>
      </div>
      <div class="stage">${stageView}</div>
      <div class="transport">
        <div class="tc">${fmt(t - ctx.cut.start)}<small>/ ${fmt(ctx.cut.end - ctx.cut.start)}</small></div>
        <div class="tbtns">
          <button class="btn icon" onClick=${() => seek(firstFrame(effectiveCut.start))} title="Start of the cut (Home)" aria-label="Start of the cut"><${Icon} n="first" /></button>
          <button class="btn icon" onClick=${() => keyRef.current({ key: '[', target: document.body, preventDefault() {} })} title="Previous camera cut ([)" aria-label="Previous camera cut"><${Icon} n="prevcut" /></button>
          <button class="btn icon" onClick=${() => keyRef.current({ key: 'ArrowLeft', target: document.body, preventDefault() {} })} title="Back one frame (←)" aria-label="Back one frame"><${Icon} n="left" /></button>
          <button class="btn icon play" onClick=${playCut} title="Play or pause the cut (Space)" aria-label="Play or pause"><${Icon} n=${playing ? 'pause' : 'play'} /></button>
          <button class="btn icon" onClick=${() => keyRef.current({ key: 'ArrowRight', target: document.body, preventDefault() {} })} title="Forward one frame (→)" aria-label="Forward one frame"><${Icon} n="right" /></button>
          <button class="btn icon" onClick=${() => keyRef.current({ key: ']', target: document.body, preventDefault() {} })} title="Next camera cut (])" aria-label="Next camera cut"><${Icon} n="nextcut" /></button>
          <button class="btn icon" onClick=${() => seek(effectiveCut.end - 1 / FPS)} title="End of the cut (End)" aria-label="End of the cut"><${Icon} n="last" /></button>
          <button class="btn icon ${loop ? 'on' : ''}" onClick=${() => setLoop(v => !v)} title="Loop the cut (L)" aria-label="Loop"><${Icon} n="loop" /></button>
        </div>
        <div class="readout"><span>Source <b>${fmt(t)}</b></span><span>Frame <b>${frame}</b>${shotNow >= 0 ? html` · Shot <b>${shotNow + 1} / ${focus.shots.length}</b>` : ''}</span></div>
      </div>
    </main>

    <aside class="panel insp">
      <div class="ptabs">${[['review', 'Review', 'review'], ['cut', 'Cut', 'cut'], ['framing', 'Framing', 'crop'], ['captions', 'Captions', 'captions']].map(([k, label, icon]) => html`
        <button class="ptab ${tab === k ? 'on' : ''}" onClick=${() => setTab(k)}><${Icon} n=${icon} />${label}${tabDot(k)}</button>`)}</div>
      <div class="ibody">${inspector}</div>
    </aside>

    <section class="panel tl">
      <div class="tlbar">
        <span class="ttl"><${Icon} n="list" />Timeline <span>${clip.key} · ${clip.title}</span></span>
        <span class="grp">Context <span class="seg">${CONTEXTS.map((c, k) => html`<button class=${ctxLevel === k && !view ? 'on' : ''}
          onClick=${() => { setCtxLevel(k); setViewState(null); }}>±${c >= 60 ? c / 60 + ' min' : c + ' s'}</button>`)}</span><kbd>W</kbd></span>
        <span class="grp"><${Icon} n="zoomout" /><input type="range" min="0" max="1000" step="1" aria-label="Timeline zoom"
          value=${Math.round(1000 * Math.log((tlView[1] - tlView[0]) / MIN_SPAN) / Math.log(Math.max(duration, MIN_SPAN + 1) / MIN_SPAN))}
          onInput=${e => { const span = MIN_SPAN * Math.pow(duration / MIN_SPAN, +e.target.value / 1000), [a, b] = tlView, at = clamp(t, a, b);
                           setView([at - span * (at - a) / (b - a), at + span * (b - at) / (b - a)]); }} /><${Icon} n="zoomin" /><span><kbd>Ctrl</kbd> + wheel</span></span>
        <span class="grp">Issues
          <button class="btn icon sm ghost" onClick=${() => stepIssue(-1)} aria-label="Previous issue" title="Previous issue (Shift+E)"><${Icon} n="left" /></button>
          <span class="mono">${issueIndex >= 0 && issueIndex < issues.length ? issueIndex + 1 : '–'} / ${issues.length}</span>
          <button class="btn icon sm ghost" onClick=${() => stepIssue(1)} aria-label="Next issue" title="Next issue (E)"><${Icon} n="right" /></button></span>
        <button class="btn sm" disabled=${locked || applying} onClick=${draftNote} title="Pin a note for the agent at the playhead (M)"><${Icon} n="pin" />Note<kbd>M</kbd></button>
        <span class="sp"></span>
        <span class="grp lgs">${[['var(--edited)', 'edited'], ['var(--missed)', 'missed speech'], ['var(--added)', 'added'], ['var(--warn)', 'low confidence'], ['var(--over)', 'override']]
          .map(([c, l]) => html`<span class="lg"><i style=${{ background: c }}></i>${l}</span>`)}</span>
      </div>
      <${Timeline} ctx=${timelineCtx} t=${t} view=${tlView} setView=${setView} onSeek=${seek}
        onEdge=${(edge, s) => { if (locked) return; tracked(`${clip.key}: ${edge} dragged`, false, () => setShift(edge, s - clip[edge], false)); }}
        onEdgeDone=${() => { save(clip.key, true); finishTracking(`${clip.key}: edge dragged`); }}
        onSelectWord=${index => { setSelWord(index); setTab('captions'); }} selectedWord=${selWord} onWordTiming=${saveCaption} threshold=${threshold}
        proposals=${proposals} notes=${notes} sourceClips=${data.clips} statusOf=${statusOf} zoomTip=${zoomTip} />
    </section>

    ${showLog && job && html`<div class="drawer">
      <div class="drawer-head"><${Icon} n="terminal" /><b>${job.kind} · ${job.state}</b><span class="grow"></span><button class="btn sm ghost" onClick=${() => setShowLog(false)}>Close</button></div>
      <pre>${(job.log || []).join('\n') || 'No output yet.'}</pre></div>`}
    ${confirmApply && html`<div class="modal" onClick=${() => setConfirmApply(false)}><div class="dialog" onClick=${e => e.stopPropagation()}>
      <div class="dh"><h3>Apply ${pending.total} change${pending.total === 1 ? '' : 's'}?</h3>
        <p>The plan is updated and changed clips are re-rendered. Approved clips stay locked. Comments and notes stay open for the agent.</p></div>
      <div class="db">
        ${group(G.lock, 'Lock approved clips', 'lock')}
        ${group(G.withdraw, 'Withdraw approval', 'restore')}
        ${group(G.recut, 'Re-cut on word boundaries', 'cut', shifts)}
        ${group(G.framing, 'Apply reviewed framing', 'crop', c => ` (${(({ ...emptyEntry(), ...(entries[c.key] || {}) }).focus_overrides || []).length} override(s))`)}
        ${group(G.comment, 'Comment for the agent', 'review')}
        ${group(G.notes, 'Timeline notes for the agent', 'pin', c => ` (${(entries[c.key].notes || []).length})`)}
        ${group(G.remove, 'Move rejected clips to rejected/', 'x')}
        ${pending.captions && html`<div class="pg"><div class="pgh"><${Icon} n="captions" />Re-render clips with corrected captions<span class="x">1</span></div>
          <div class="items"><span class="it">${data.caption_edits} caption edit(s) in this source</span></div></div>`}
      </div>
      <div class="df"><button class="btn" onClick=${() => setConfirmApply(false)}>Cancel</button>
        <button class="btn primary" onClick=${runApply}><${Icon} n="check" />Apply ${pending.total} change${pending.total === 1 ? '' : 's'}</button></div>
    </div></div>`}
    ${help && html`<div class="modal" onClick=${() => setHelp(false)}><div class="dialog" onClick=${e => e.stopPropagation()}>
      <div class="dh"><h3>Keyboard shortcuts</h3></div>
      <div class="db"><table class="keys">${[
        ['Space', 'Play or pause the cut'], ['← / →', 'One frame (Shift: one second)'], ['I / O', 'Set start / end at the playhead'],
        ['[ / ]', 'Previous / next camera cut'], ['Home / End', 'Start / end of the cut'], ['↑ / ↓', 'Previous / next clip'],
        ['F', 'Override the framing of the current shot'], ['A / N / X', 'Approve / needs changes / reject'], ['Shift+A', 'Approve and open the next pending clip'],
        ['M', 'Pin a note for the agent at the playhead'], ['E / Shift+E', 'Next / previous flagged shot or doubtful word'],
        ['1–4', 'Live / rendered / preview / compare'], ['L', 'Loop'], ['W', 'Timeline context: ±3 s, ±30 s, ±2 min, ±10 min'],
        ['Ctrl + wheel', 'Zoom the timeline around the cursor (the wheel alone scrolls it)'], ['Ctrl+Z / Ctrl+Shift+Z', 'Undo / redo'],
        ['Ctrl+F', 'Find in the transcript'], ['?', 'This list']]
        .map(([k, d]) => html`<tr><td><kbd>${k}</kbd></td><td>${d}</td></tr>`)}</table></div>
      <div class="df"><button class="btn" onClick=${() => setHelp(false)}>Close</button></div>
    </div></div>`}
    ${toast && html`<div class="toast ${toast.kind}">${toast.text}</div>`}
  </div>`;
}

render(html`<${App} />`, document.getElementById('app'));
