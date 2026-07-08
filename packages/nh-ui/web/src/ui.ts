export interface SceneSnapshot {
  scene?: Record<string, unknown>;
  sources: Record<string, any>;
  processing_chain?: {
    processors?: any[];
    routing?: Record<string, string[]>;
  };
  lfos?: Record<string, any>;
  modulation_routes?: Record<string, any>;
  transport?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
}

export interface AnalysisSnapshot {
  audio_path?: string;
  duration_s?: number;
  f0_track?: { f0_mean?: number; voiced_fraction?: number };
  phideus?: { h_series?: Record<string, unknown>; v4_linear?: Record<string, unknown>; v4_log?: Record<string, unknown> };
  proposed_f1?: number;
  sample_source?: Record<string, unknown>;
}

interface ShellHandlers {
  onRefresh: () => void | Promise<void>;
  onControl: (path: string, value: unknown) => void | Promise<void>;
  onTypedControl: (type: string, value: unknown) => void | Promise<void>;
  onMuteSource: (sourceId: string, mute: boolean) => void | Promise<void>;
  onSoloSource: (sourceId: string) => void | Promise<void>;
  onMockAnalysis: () => void | Promise<void>;
  onApplyProposedF1: () => void | Promise<void>;
}

type StatusKind = 'connected' | 'connecting' | 'disconnected' | 'error';

export function renderAppShell(): void {
  const app = document.getElementById('app-shell');
  if (!app) return;
  app.className = 'app-shell';
  app.innerHTML = `
    <header class="topbar">
      <div class="brand">
        <span class="sigil">NH</span>
        <div>
          <h1>NaturalHarmony v2</h1>
          <p>Scene instrument · Beacon + Shaper + field recordings</p>
        </div>
      </div>
      <div id="preset-bar" class="preset-bar">
        <select id="preset-select" aria-label="Preset select">
          <option value="">Loading v2 presets…</option>
        </select>
        <button id="refresh-button" type="button">Refresh</button>
        <button id="panic-button" type="button" class="danger">Panic</button>
        <span id="connection-status" class="status disconnected">disconnected</span>
      </div>
    </header>

    <main class="workspace-grid">
      <section id="sources-panel" class="panel sources-panel">
        <div class="panel-heading"><h2>Sources</h2><span id="source-count">0</span></div>
        <div id="sources-list" class="card-list"></div>
      </section>

      <section id="shaper-panel" class="panel shaper-panel">
        <div class="panel-heading"><h2>Shaper / Launchpad</h2><span id="active-voices">0 voices</span></div>
        <div id="launchpad-grid" class="launchpad-grid" aria-label="Launchpad harmonic pad grid"></div>
        <div id="voice-list" class="voice-list"></div>
      </section>

      <section id="spatial-panel" class="panel spatial-panel">
        <div class="panel-heading"><h2>Spatial Field</h2><span>32 bands</span></div>
        <div id="spatial-radar" class="spatial-radar"><canvas id="spatial-canvas" width="320" height="240"></canvas></div>
        <div id="spatial-band-list" class="spatial-band-list"></div>
      </section>

      <section id="processing-panel" class="panel processing-panel">
        <div class="panel-heading"><h2>Processing</h2><span id="processor-count">0</span></div>
        <div id="processor-list" class="card-list empty-note">No processors in scene yet.</div>
      </section>

      <section id="lfo-panel" class="panel lfo-panel">
        <div class="panel-heading"><h2>LFO / Modulation</h2><span id="route-count">0</span></div>
        <div id="lfo-list" class="card-list empty-note">No modulation routes yet.</div>
      </section>

      <section id="analysis-panel" class="panel analysis-panel">
        <div class="panel-heading"><h2>Analysis / Field Recordings</h2><span>upload → analyze → source</span></div>
        <div class="analysis-actions">
          <button id="mock-analysis-button" type="button">Load mock analysis</button>
          <button id="apply-proposed-f1-button" type="button">Apply proposed f1</button>
        </div>
        <div class="analysis-grid">
          <div id="analysis-f0" class="metric-card"><b>F0</b><span>waiting for analysis</span></div>
          <div id="analysis-phideus" class="metric-card"><b>Phideus</b><span>waiting for descriptors</span></div>
          <div id="analysis-proposed-f1" class="metric-card"><b>Proposed f1</b><span>waiting for recording</span></div>
        </div>
        <div id="samples-panel" class="samples-panel empty-note">Sample workflow will appear here.</div>
      </section>

      <section id="event-log" class="panel event-log">
        <div class="panel-heading"><h2>Event Log</h2><span>live</span></div>
        <div id="event-log-lines"></div>
      </section>
    </main>
  `;
  renderLaunchpadGrid();
}

export function renderStatus(kind: StatusKind, label: string): void {
  const el = document.getElementById('connection-status');
  if (!el) return;
  el.className = `status ${kind}`;
  el.textContent = label;
}

export function appendLog(message: string): void {
  const log = document.getElementById('event-log-lines');
  if (!log) return;
  const line = document.createElement('div');
  line.className = 'log-line';
  line.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
  log.prepend(line);
  while (log.children.length > 80) log.removeChild(log.lastChild!);
}

export function bindShellHandlers(handlers: ShellHandlers): void {
  document.getElementById('refresh-button')?.addEventListener('click', () => handlers.onRefresh());
  document.getElementById('panic-button')?.addEventListener('click', () => handlers.onTypedControl('panic', true));
  document.getElementById('mock-analysis-button')?.addEventListener('click', () => handlers.onMockAnalysis());
  document.getElementById('apply-proposed-f1-button')?.addEventListener('click', () => handlers.onApplyProposedF1());
  document.getElementById('launchpad-grid')?.addEventListener('click', (ev) => {
    const target = ev.target as HTMLElement;
    const pad = target.closest<HTMLButtonElement>('.pad-button');
    if (!pad) return;
    const n = Number(pad.dataset.n);
    handlers.onControl(`sources.shaper.voice_${n}_toggle`, 1.0);
  });
  document.getElementById('sources-panel')?.addEventListener('click', (ev) => {
    const target = ev.target as HTMLElement;
    const sourceId = target.dataset.sourceId;
    if (!sourceId) return;
    if (target.matches('[data-action="mute"]')) handlers.onMuteSource(sourceId, true);
    if (target.matches('[data-action="unmute"]')) handlers.onMuteSource(sourceId, false);
    if (target.matches('[data-action="solo"]')) handlers.onSoloSource(sourceId);
  });
  document.getElementById('sources-panel')?.addEventListener('input', (ev) => {
    const target = ev.target as HTMLInputElement;
    const path = target.dataset.path;
    if (!path) return;
    handlers.onControl(path, Number(target.value));
  });
  document.getElementById('spatial-panel')?.addEventListener('input', (ev) => {
    const target = ev.target as HTMLInputElement;
    const path = target.dataset.path;
    if (!path) return;
    const value = target.type === 'checkbox' ? target.checked : Number(target.value);
    handlers.onControl(path, value);
  });
}

export function renderScene(scene: SceneSnapshot): void {
  renderSources(scene);
  renderShaper(scene);
  renderSpatial(scene);
  renderProcessing(scene);
  renderModulation(scene);
}

export function renderAnalysis(analysis: AnalysisSnapshot | null): void {
  const f0 = document.getElementById('analysis-f0');
  const phideus = document.getElementById('analysis-phideus');
  const proposed = document.getElementById('analysis-proposed-f1');
  const samples = document.getElementById('samples-panel');
  if (!analysis) {
    if (f0) f0.innerHTML = '<b>F0</b><span>waiting for analysis</span>';
    if (phideus) phideus.innerHTML = '<b>Phideus</b><span>waiting for descriptors</span>';
    if (proposed) proposed.innerHTML = '<b>Proposed f1</b><span>waiting for recording</span>';
    if (samples) samples.textContent = 'Sample workflow will appear here.';
    return;
  }
  const f0Mean = analysis.f0_track?.f0_mean;
  const voiced = analysis.f0_track?.voiced_fraction;
  const hSeries = analysis.phideus?.h_series || {};
  const concentration = (hSeries as any).concentration;
  if (f0) f0.innerHTML = `<b>F0</b><span>${formatNumber(f0Mean)} Hz · voiced ${formatNumber((voiced ?? 0) * 100)}%</span>`;
  if (phideus) phideus.innerHTML = `<b>Phideus</b><span>H concentration ${formatNumber(concentration)}</span>`;
  if (proposed) proposed.innerHTML = `<b>Proposed f1</b><span>${formatNumber(analysis.proposed_f1)} Hz</span>`;
  if (samples) {
    samples.classList.remove('empty-note');
    samples.innerHTML = `
      <article class="mini-card" id="analysis-sample-card">
        <b>${escapeHtml(analysis.audio_path || 'field recording')}</b>
        <span>${formatNumber(analysis.duration_s)} s · proposed source ${escapeHtml((analysis.sample_source as any)?.source_id || 'sample')}</span>
      </article>
    `;
  }
}

function renderSources(scene: SceneSnapshot): void {
  const list = document.getElementById('sources-list');
  const count = document.getElementById('source-count');
  if (!list) return;
  const sources = scene.sources || {};
  const entries = Object.entries(sources);
  if (count) count.textContent = String(entries.length);
  list.innerHTML = entries.map(([id, src]) => sourceCard(id, src)).join('');
}

function sourceCard(id: string, source: any): string {
  const kind = source.kind || source.type || id;
  const runtime = source.runtime || {};
  const f1 = source.f1 ?? source.model?.f1;
  const modelGain = source.master_gain ?? source.gain ?? 1;
  const runtimeGain = runtime.gain_offset ?? 1;
  const effectiveGain = modelGain * runtimeGain;
  const gainPath = `sources.${id}.gain`;
  const bands = source.bands ? Object.keys(source.bands).length : 0;
  const activeVoices = runtime.voice_count ?? Object.keys(runtime.active_voices || {}).length;
  const effectiveF1 = runtime.effective_f1 ?? f1;
  const muted = runtimeGain === 0;
  const stableId = `source-card-${id}`;
  return `
    <article id="${escapeHtml(stableId)}" class="source-card kind-${escapeHtml(kind)} ${muted ? 'muted' : ''}">
      <div class="source-title"><b>${escapeHtml(id)}</b><span>${escapeHtml(kind)}</span></div>
      <dl>
        ${f1 !== undefined ? `<div><dt>f1</dt><dd>${formatNumber(f1)} Hz</dd></div>` : ''}
        ${effectiveF1 !== undefined && effectiveF1 !== f1 ? `<div><dt>effective f1</dt><dd>${formatNumber(effectiveF1)} Hz</dd></div>` : ''}
        <div><dt>model gain</dt><dd>${formatNumber(modelGain)}</dd></div>
        <div><dt>runtime gain</dt><dd>${formatNumber(runtimeGain)}</dd></div>
        <div><dt>effective gain</dt><dd>${formatNumber(effectiveGain)}</dd></div>
        ${bands ? `<div><dt>bands</dt><dd>${bands}</dd></div>` : ''}
        ${activeVoices ? `<div><dt>voices</dt><dd>${activeVoices}</dd></div>` : ''}
      </dl>
      <label class="inline-control">runtime gain
        <input id="source-gain-${escapeHtml(id)}" type="range" min="0" max="1.5" step="0.01" value="${runtimeGain}" data-path="${escapeHtml(gainPath)}" />
      </label>
      <div class="source-controls">
        <button type="button" data-action="mute" data-source-id="${escapeHtml(id)}">Mute</button>
        <button type="button" data-action="unmute" data-source-id="${escapeHtml(id)}">Unmute</button>
        <button type="button" data-action="solo" data-source-id="${escapeHtml(id)}">Solo</button>
      </div>
      ${id === 'beacon' ? `
        <label class="inline-control">f1 offset
          <input type="range" min="-24" max="24" step="0.1" value="${source.f1_offset ?? runtime.f1_offset ?? 0}" data-path="sources.beacon.f1_offset" />
        </label>` : ''}
    </article>
  `;
}

function renderShaper(scene: SceneSnapshot): void {
  const shaper = scene.sources?.shaper || Object.values(scene.sources || {}).find((s: any) => s.kind === 'shaper');
  const runtime = shaper?.runtime || {};
  const active = runtime.active_voices || {};
  const activeKeys = Object.keys(active);
  const activeEl = document.getElementById('active-voices');
  const voiceList = document.getElementById('voice-list');
  if (activeEl) activeEl.textContent = `${activeKeys.length} voices`;
  document.querySelectorAll('.pad-button').forEach((el) => {
    const n = (el as HTMLElement).dataset.n || '';
    el.classList.toggle('active', activeKeys.includes(n));
  });
  if (voiceList) {
    voiceList.innerHTML = activeKeys.length
      ? activeKeys.map((n) => `<span class="voice-pill">H${escapeHtml(n)}</span>`).join('')
      : '<span class="empty-note">No active shaper voices.</span>';
  }
}

function renderLaunchpadGrid(): void {
  const grid = document.getElementById('launchpad-grid');
  if (!grid) return;
  grid.innerHTML = '';
  for (let n = 1; n <= 64; n += 1) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pad-button';
    btn.dataset.n = String(n);
    btn.title = `Toggle Shaper harmonic ${n}`;
    btn.textContent = String(n);
    grid.appendChild(btn);
  }
}

function renderSpatial(scene: SceneSnapshot): void {
  const beacon = scene.sources?.beacon || Object.values(scene.sources || {}).find((s: any) => s.kind === 'beacon');
  const bands = beacon?.bands || {};
  const list = document.getElementById('spatial-band-list');
  if (list) {
    const entries = Object.entries(bands).slice(0, 32);
    list.innerHTML = entries.length
      ? entries.map(([n, band]: [string, any]) => `
        <div class="spatial-row" data-band="${escapeHtml(n)}">
          <span class="band-label">H${escapeHtml(n)}</span>
          <label>az
            <input class="spatial-input" type="number" step="1" min="0" max="360" value="${band.az ?? 0}" data-path="sources.beacon.bands.${escapeHtml(n)}.az" />
          </label>
          <label>dist
            <input class="spatial-input" type="number" step="0.01" min="0" max="4" value="${band.dist ?? 1}" data-path="sources.beacon.bands.${escapeHtml(n)}.dist" />
          </label>
          <label>q
            <input class="spatial-input" type="number" step="0.01" min="0.01" max="8" value="${band.q ?? 0.5}" data-path="sources.beacon.bands.${escapeHtml(n)}.q" />
          </label>
          <label class="band-on">on
            <input class="spatial-on" type="checkbox" ${band.on === false ? '' : 'checked'} data-path="sources.beacon.bands.${escapeHtml(n)}.on" />
          </label>
        </div>
      `).join('')
      : '<span class="empty-note">No beacon bands in scene.</span>';
  }
  drawRadar(bands);
}

function renderProcessing(scene: SceneSnapshot): void {
  const processors = scene.processing_chain?.processors || [];
  const count = document.getElementById('processor-count');
  const list = document.getElementById('processor-list');
  if (count) count.textContent = String(processors.length);
  if (!list) return;
  list.classList.toggle('empty-note', processors.length === 0);
  list.innerHTML = processors.length
    ? processors.map((p: any) => `<article class="mini-card"><b>${escapeHtml(p.processor_id || p.id || 'processor')}</b><span>${escapeHtml(p.kind || p.type || '')}</span></article>`).join('')
    : 'No processors in scene yet.';
}

function renderModulation(scene: SceneSnapshot): void {
  const routes = scene.modulation_routes || {};
  const lfos = scene.lfos || {};
  const count = document.getElementById('route-count');
  const list = document.getElementById('lfo-list');
  const n = Object.keys(routes).length + Object.keys(lfos).length;
  if (count) count.textContent = String(n);
  if (!list) return;
  list.classList.toggle('empty-note', n === 0);
  list.innerHTML = n
    ? [
      ...Object.entries(lfos).map(([id, lfo]: [string, any]) => `<article class="mini-card"><b>${escapeHtml(id)}</b><span>${escapeHtml(lfo.waveform || 'lfo')}</span></article>`),
      ...Object.entries(routes).map(([id, route]: [string, any]) => `<article class="mini-card"><b>${escapeHtml(id)}</b><span>${escapeHtml(route.target_path || '')}</span></article>`),
    ].join('')
    : 'No modulation routes yet.';
}

function drawRadar(bands: Record<string, any>): void {
  const canvas = document.getElementById('spatial-canvas') as HTMLCanvasElement | null;
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const w = canvas.width;
  const h = canvas.height;
  const cx = w / 2;
  const cy = h / 2;
  const radius = Math.min(w, h) * 0.38;
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = '#2f3b52';
  ctx.lineWidth = 1;
  for (const r of [0.33, 0.66, 1]) {
    ctx.beginPath();
    ctx.arc(cx, cy, radius * r, 0, Math.PI * 2);
    ctx.stroke();
  }
  Object.entries(bands || {}).slice(0, 32).forEach(([n, band]: [string, any]) => {
    if (band.on === false) return;
    const az = ((Number(band.az ?? 0) - 90) * Math.PI) / 180;
    const dist = Math.max(0.1, Math.min(1.2, Number(band.dist ?? 1)));
    const x = cx + Math.cos(az) * radius * Math.min(dist, 1);
    const y = cy + Math.sin(az) * radius * Math.min(dist, 1);
    ctx.fillStyle = '#58a6ff';
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#c9d1d9';
    ctx.font = '10px system-ui';
    ctx.fillText(n, x + 6, y + 3);
  });
}

function formatNumber(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return n.toFixed(Math.abs(n) >= 100 ? 0 : 2);
}

function escapeHtml(value: unknown): string {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

