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

  const paint = useCallback((t) => {
    const c = live.current, v = videoRef.current, sz = sizeRef.current;
    if (!v || !sz.ds) return;
    const frame = frameOf(t, c.clip.start);
    const center = centerAt(frame, c.focus, c.overrides);
    const x = clamp(center * sz.sw - sz.w / 2, 0, sz.sw - sz.w);
    v.style.transform = `translate(${-x}px, ${-(sz.sh - c.settings.height * sz.ds) / 2}px)`;
    // Captions are evaluated at the render's frame times (the cut's 30 fps grid).
    const ct = frameOf(t, c.cut.start) / FPS + 1e-6;
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
    onFrameDraw && onFrameDraw(t, center);
  }, []);

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    let handle, raf, last = 0;
    const tick = (now, meta) => {
      const t = meta ? meta.mediaTime : v.currentTime;
      paint(t);
      if (now - last > 80) { last = now; onTime(t); }
      handle = v.requestVideoFrameCallback ? v.requestVideoFrameCallback(tick) : null;
    };
    if (v.requestVideoFrameCallback) handle = v.requestVideoFrameCallback(tick);
    const loop = () => { const t = v.currentTime; paint(t); onTime(t); raf = requestAnimationFrame(loop); };
    if (!v.requestVideoFrameCallback) raf = requestAnimationFrame(loop);
    const seeked = () => { paint(v.currentTime); onTime(v.currentTime); };
    v.addEventListener('seeked', seeked);
    v.addEventListener('loadeddata', seeked);
    return () => {
      if (handle && v.cancelVideoFrameCallback) v.cancelVideoFrameCallback(handle);
      cancelAnimationFrame(raf);
      v.removeEventListener('seeked', seeked);
      v.removeEventListener('loadeddata', seeked);
    };
  }, [ctx.proxyUrl]);

  // Repaint when edits change the crop or captions while paused.
  useEffect(() => { const v = videoRef.current; if (v) paint(v.currentTime); });

  const drag = useRef(null);
  const down = (e) => {
    if (!onDragCenter || !ctx.framingEditable) return;
    const v = videoRef.current;
    const frame = frameOf(v.currentTime, ctx.clip.start);
    drag.current = { x: e.clientX, center: centerAt(frame, ctx.focus, ctx.overrides) };
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
      <video ref=${videoRef} src=${ctx.proxyUrl} preload="auto" playsinline
             style=${{ width: sw + 'px', height: sh + 'px' }}></video>
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
  drawRef.current = (t, center) => {
    const canvas = canvasRef.current, v = videoRef.current;
    if (!canvas || !v || !v.videoWidth) return;
    const g = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    g.drawImage(v, 0, 0, W, H);
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
  useEffect(() => register((t, c) => drawRef.current(t, c)), []);
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

function Timeline({ ctx, t, onSeek, onEdge, onEdgeDone, wide, onSelectWord }) {
  const ref = useRef();
  const { w } = useSize(ref);
  const { clip, cut, snapped, focus, words, duration } = ctx;
  const pad = wide ? ctx.limits.context : 3;
  const a = Math.max(0, Math.min(clip.start, cut.start) - pad);
  const b = Math.min(duration, Math.max(clip.end, cut.end) + pad);
  const X = (s) => (s - a) / (b - a) * w;
  const T = (x) => a + clamp(x, 0, w) / w * (b - a);
  const drag = useRef(null);

  const down = (e, kind) => {
    e.stopPropagation();
    drag.current = kind;
    ref.current.setPointerCapture(e.pointerId);
    if (kind === 'seek') onSeek(T(e.clientX - ref.current.getBoundingClientRect().left));
  };
  const move = (e) => {
    if (!drag.current) return;
    const s = T(e.clientX - ref.current.getBoundingClientRect().left);
    if (drag.current === 'seek') onSeek(s);
    else onEdge(drag.current, s);
  };
  const up = () => { if (drag.current && drag.current !== 'seek') onEdgeDone(); drag.current = null; };

  const step = (b - a) > 90 ? 10 : (b - a) > 30 ? 5 : 1;
  const ticks = [];
  for (let s = Math.ceil(a / step) * step; s <= b; s += step) ticks.push(s);
  const shotRows = focus ? focus.shots.map((s, i) => {
    const s0 = clip.start + s.start_frame / FPS, s1 = clip.start + s.end_frame / FPS;
    const over = ctx.overrides.some(o => o.start_frame < s.end_frame && o.end_frame > s.start_frame);
    return html`<div class="shot ${i % 2 ? 'odd' : ''} ${over ? 'over' : ''} ${s.review.length ? 'flag' : ''}"
      title=${`Shot ${i + 1}: frames ${s.start_frame}–${s.end_frame - 1}${s.review.length ? ' · review: ' + s.review.map(r => r.reason || r.kind || r).join(', ') : ''}`}
      style=${{ left: X(s0) + 'px', width: Math.max(1, X(s1) - X(s0)) + 'px' }}
      onPointerDown=${e => { e.stopPropagation(); onSeek(s0 + 1e-3); }}>${X(s1) - X(s0) > 22 ? i + 1 : ''}</div>`;
  }) : null;
  const wordRows = (words || []).filter(wd => wd.end >= a && wd.start <= b).map(wd => html`
    <div class="word ${wd.word !== wd.original ? 'edited' : ''}" title=${wd.word !== wd.original ? `${wd.original} → ${wd.word || '(removed)'}` : wd.word}
      style=${{ left: X(wd.start) + 'px', width: Math.max(2, X(wd.end) - X(wd.start)) + 'px' }}
      onPointerDown=${e => { e.stopPropagation(); onSeek(wd.start + 1e-3); onSelectWord(wd.index); }}>
      ${X(wd.end) - X(wd.start) > 26 ? wd.word : ''}</div>`);
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

function Section({ title, children, extra }) {
  return html`<section class="panel"><h3>${title}${extra && html`<span class="extra">${extra}</span>`}</h3>${children}</section>`;
}

function CutPanel({ ctx, t, setShift, nudgeShift, disabled }) {
  const { clip, entry, snapped } = ctx;
  const row = (edge, label, key) => {
    const base = clip[edge], shift = entry[edge + '_shift'] || 0;
    const nudge = (d) => nudgeShift(edge, d);
    return html`<div class="edge">
      <div class="edge-head"><b>${label}</b> <span class="mono">${fmt(base + shift)}</span> <span class="muted">${signed(shift)}</span></div>
      <div class="buttons">
        <button disabled=${disabled} onClick=${() => nudge(-0.5)}>−0.5 s</button>
        <button disabled=${disabled} onClick=${() => nudge(-1 / FPS)}>−1 f</button>
        <button disabled=${disabled} onClick=${() => nudge(1 / FPS)}>+1 f</button>
        <button disabled=${disabled} onClick=${() => nudge(0.5)}>+0.5 s</button>
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

function CaptionPanel({ ctx, t, selected, onSave, lockedNote }) {
  const words = (ctx.words || []).filter(w => (selected != null ? Math.abs(w.index - selected) <= 6 : w.end >= t - 1.5 && w.start <= t + 3));
  const [drafts, setDrafts] = useState({});
  const commit = (w, value) => {
    if (value == null || value === w.word) return;
    onSave(w.index, value).then(() => setDrafts(d => { const n = { ...d }; delete n[w.index]; return n; }));
  };
  return html`<${Section} title="Captions" extra=${ctx.captionEdits ? `${ctx.captionEdits} fixes in this source` : ''}>
    <div class="words-edit">
      ${words.map(w => html`<label class="wordbox ${w.word !== w.original ? 'edited' : ''} ${w.index === selected ? 'sel' : ''} ${w.start <= t && t < w.end ? 'now' : ''}"
          title=${`${fmt(w.start)} · original: ${w.original}`}>
        <input value=${drafts[w.index] ?? w.word} size=${Math.max(3, (drafts[w.index] ?? w.word).length + 1)}
          onInput=${e => setDrafts(d => ({ ...d, [w.index]: e.target.value }))}
          onKeyDown=${e => { if (e.key === 'Enter') { e.preventDefault(); commit(w, e.target.value); e.target.blur(); } if (e.key === 'Escape') { e.target.value = w.word; setDrafts(d => ({ ...d, [w.index]: undefined })); e.target.blur(); } }}
          onBlur=${e => commit(w, e.target.value)} />
        ${w.word !== w.original && html`<button class="revert" title="Restore “${w.original}”" onClick=${() => onSave(w.index, w.original)}>↺</button>`}
      </label>`)}
      ${!words.length && html`<p class="note muted">No words near the playhead.</p>`}
    </div>
    <p class="note muted">Press Enter to save a word; an empty word removes it. Fixes apply to every clip of this source on the next Apply.${lockedNote}</p>
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
  const [wide, setWide] = useState(false);
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
      request(`clips/${encodeURIComponent(clip.key)}/captions?start=${capStart}&end=${capEnd}`)
        .then(c => { setCaptions(c); setGroupsVersion(v => v + 1); }).catch(e => notify('Captions: ' + e.message, 'bad'));
    }, 150);
    return () => clearTimeout(timer);
  }, [clip && clip.key, capStart, capEnd, data && data.caption_edits]);

  // When the selected clip changes, park the playhead at its start.
  useEffect(() => {
    if (!clip) return;
    setSelWord(null);
    setTimeout(() => seek(clip.start), 0);
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

  const setShift = (edge, value, immediate = true) => {
    const max = data.limits.max_shift;
    value = clamp(Math.round(value * 1000) / 1000, -max, max);
    const now = latest();
    const other = edge === 'start' ? clip.end + (now.end_shift || 0) : clip.start + (now.start_shift || 0);
    const pos = clip[edge] + value;
    if (edge === 'start' && other - pos < 3) value = other - 3 - clip.start;
    if (edge === 'end' && pos - other < 3) value = other + 3 - clip.end;
    if (edge === 'start' && clip.start + value < 0) value = -clip.start;
    if (edge === 'end' && clip.end + value > data.source.duration) value = data.source.duration - clip.end;
    stage('recut', { [edge + '_shift']: Math.round(value * 1000) / 1000 }, immediate);
  };

  const nudgeShift = (edge, d) => setShift(edge, Math.round(((latest()[edge + '_shift'] || 0) + d) * 1000) / 1000);

  const overrides = clip ? overridesFor(clip, entry) : [];
  const frame = clip ? frameOf(t, clip.start) : 0;

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

  const saveCaption = async (index, to) => {
    try {
      await request('captions', { index, to });
      const d = await request('state');
      setData(d);
      notify('Caption word saved.', 'ok');
    } catch (e) { notify('Caption not saved: ' + e.message, 'bad'); }
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
    if (mode === 'live' && (t < effectiveCut.start - 0.05 || t >= effectiveCut.end - 0.05)) seek(effectiveCut.start);
    v.play();
    leftVideo.current && leftVideo.current.play();
  };
  // Stop or loop at the end of the cut in the live view.
  useEffect(() => {
    if (mode !== 'live' || !effectiveCut) return;
    const v = liveVideo.current;
    if (!v || v.paused) return;
    if (t >= effectiveCut.end) { if (loop) seek(effectiveCut.start); else v.pause(); }
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
    const shots = focus ? focus.shots.map(s => clip.start + s.start_frame / FPS) : [];
    const k = e.key;
    const act = {
      ' ': playCut,
      ArrowLeft: () => seek(t - (e.shiftKey ? 1 : 1 / FPS)),
      ArrowRight: () => seek(t + (e.shiftKey ? 1 : 1 / FPS)),
      ArrowUp: () => moveClip(-1), ArrowDown: () => moveClip(1),
      i: () => setShift('start', t - clip.start), o: () => setShift('end', t - clip.end),
      '[': () => { const prev = shots.filter(s => s < t - 0.02).pop(); seek(prev ?? effectiveCut.start); },
      ']': () => { const next = shots.find(s => s > t + 0.02); if (next != null) seek(next + 1e-3); },
      Home: () => seek(effectiveCut.start), End: () => seek(effectiveCut.end - 1 / FPS),
      l: () => setLoop(v => !v), w: () => setWide(v => !v),
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
          onFrameDraw=${(tt, c) => { lastFrame.current = [tt, c]; overviewDraw.current && overviewDraw.current(tt, c); }} onDragCenter=${dragCenter} />`
      : html`<div class="viewport empty"><p>The live preview needs a low-resolution proxy of the source.</p>
          ${jobRunning && job.kind === 'proxy' ? html`<p>Creating it… ${job.progress != null ? Math.round(job.progress * 100) + '%' : ''}</p>`
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
        <label class="check"><input type="checkbox" checked=${wide} onChange=${e => setWide(e.target.checked)} />Context (W)</label>
      </div>
    </main>

    <div class="tl">
      <${Timeline} ctx=${timelineCtx} t=${t} wide=${wide} onSeek=${seek}
        onEdge=${(edge, s) => setShift(edge, s - clip[edge], false)} onEdgeDone=${() => save(clip.key, true)}
        onSelectWord=${setSelWord} />
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
      <${CaptionPanel} ctx=${ctx} t=${t} selected=${selWord} onSave=${saveCaption} lockedNote=${lockedClipNote} />
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
        ['1–4', 'Live / rendered / preview / compare'], ['L', 'Loop'], ['W', 'Wide timeline context'], ['?', 'This help']]
        .map(([k, d]) => html`<tr><td><kbd>${k}</kbd></td><td>${d}</td></tr>`)}</table>
    </div></div>`}
    ${toast && html`<div class="toast ${toast.kind}">${toast.text}</div>`}
  </div>`;
}

render(html`<${App} />`, document.getElementById('app'));
