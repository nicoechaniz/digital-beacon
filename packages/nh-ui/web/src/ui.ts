export function setStatus(state: 'connected' | 'connecting' | 'disconnected' | 'error') {
  const el = document.getElementById('connection-status')!;
  el.className = state;
  el.textContent = state.charAt(0).toUpperCase() + state.slice(1);
}

export function setRendererCaps(caps: any) {
  const el = document.getElementById('renderer-caps')!;
  const lines = Object.entries(caps).map(([k, v]) => `${k}: ${v}`);
  el.innerHTML = '<pre>' + lines.join('\n') + '</pre>';
}

export function logStatus(message: string) {
  const el = document.getElementById('status-log')!;
  const line = document.createElement('div');
  line.textContent = message;
  el.prepend(line);
}

export function updateF1Display(f1: number) {
  const el = document.getElementById('f1-display') as HTMLSpanElement | null;
  if (el) el.textContent = f1.toFixed(2) + ' Hz';
}

export function updateMasterDisplay(gain: number) {
  const el = document.getElementById('master-display') as HTMLSpanElement | null;
  if (el) el.textContent = gain.toFixed(2);
}

export function updatePartialDisplay(n: number, gain: number) {
  const el = document.getElementById(`partial-gain-display-${n}`) as HTMLSpanElement | null;
  if (el) el.textContent = gain.toFixed(2);
}

interface PerformanceHandlers {
  onF1Change: (offset: number) => void;
  onMasterChange: (gain: number) => void;
  onPartialGainChange: (n: number, gain: number) => void;
  onMuteChange: (n: number, muted: boolean) => void;
}

export function renderPerformanceControls(
  state: { baseF1: number; masterGain: number; partialGains: Map<number, number>; muted: Set<number>; maxPartials: number },
  handlers: PerformanceHandlers
) {
  const container = document.getElementById('performance-controls')!;
  container.innerHTML = '';

  const f1Section = document.createElement('div');
  f1Section.className = 'control-group';
  f1Section.innerHTML = `
    <label>f1 <span id="f1-display">${state.baseF1.toFixed(2)} Hz</span></label>
    <input type="range" id="f1-slider" min="-20" max="20" step="0.1" value="0" />
    <input type="number" id="f1-fine" min="-20" max="20" step="0.01" value="0" />
  `;
  container.appendChild(f1Section);

  const f1Slider = f1Section.querySelector('#f1-slider') as HTMLInputElement;
  const f1Fine = f1Section.querySelector('#f1-fine') as HTMLInputElement;
  const updateF1 = (offset: number) => {
    f1Slider.value = String(offset);
    f1Fine.value = String(offset);
    handlers.onF1Change(offset);
  };
  f1Slider.addEventListener('input', () => updateF1(parseFloat(f1Slider.value)));
  f1Fine.addEventListener('change', () => updateF1(parseFloat(f1Fine.value)));

  const masterSection = document.createElement('div');
  masterSection.className = 'control-group';
  masterSection.innerHTML = `
    <label>Master <span id="master-display">${state.masterGain.toFixed(2)}</span></label>
    <input type="range" id="master-slider" min="0" max="2" step="0.01" value="${state.masterGain}" />
  `;
  container.appendChild(masterSection);
  const masterSlider = masterSection.querySelector('#master-slider') as HTMLInputElement;
  masterSlider.addEventListener('input', () => handlers.onMasterChange(parseFloat(masterSlider.value)));

  const partialsSection = document.createElement('div');
  partialsSection.className = 'partials-grid';
  partialsSection.innerHTML = '<h3>Partials</h3>';
  const grid = document.createElement('div');
  grid.className = 'grid';

  const maxN = Math.min(state.maxPartials, 16);
  for (let n = 1; n <= maxN; n++) {
    const gain = state.partialGains.get(n) || 1.0;
    const muted = state.muted.has(n);
    const row = document.createElement('div');
    row.className = 'partial-row';
    row.innerHTML = `
      <span class="partial-label">${n}</span>
      <input type="range" class="partial-slider" data-n="${n}" min="0" max="2" step="0.01" value="${gain}" ${muted ? 'disabled' : ''} />
      <span class="partial-gain" id="partial-gain-display-${n}">${muted ? 'MUTE' : gain.toFixed(2)}</span>
      <button class="mute-btn" data-n="${n}">${muted ? 'Unmute' : 'Mute'}</button>
    `;
    grid.appendChild(row);
  }
  partialsSection.appendChild(grid);
  container.appendChild(partialsSection);

  grid.querySelectorAll('.partial-slider').forEach((slider) => {
    slider.addEventListener('input', (e) => {
      const target = e.target as HTMLInputElement;
      const n = parseInt(target.dataset.n!);
      const gain = parseFloat(target.value);
      handlers.onPartialGainChange(n, gain);
    });
  });

  grid.querySelectorAll('.mute-btn').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      const target = e.target as HTMLButtonElement;
      const n = parseInt(target.dataset.n!);
      const wasMuted = state.muted.has(n);
      const muted = !wasMuted;
      handlers.onMuteChange(n, muted);
      target.textContent = muted ? 'Unmute' : 'Mute';
      const slider = grid.querySelector(`.partial-slider[data-n="${n}"]`) as HTMLInputElement;
      slider.disabled = muted;
      updatePartialDisplay(n, muted ? 0 : (state.partialGains.get(n) || 1.0));
    });
  });
}

interface PresetBrowserHandlers {
  onLoad: (presetId: string) => void;
  onSave: () => void;
}

export async function renderPresetBrowser(handlers: PresetBrowserHandlers) {
  const container = document.getElementById('preset-browser')!;
  container.innerHTML = '<h2>Presets</h2>';

  const controls = document.createElement('div');
  controls.className = 'preset-controls';
  const loadBtn = document.createElement('button');
  loadBtn.textContent = 'Load selected';
  const saveBtn = document.createElement('button');
  saveBtn.textContent = 'Save snapshot';
  controls.appendChild(loadBtn);
  controls.appendChild(saveBtn);
  container.appendChild(controls);

  const select = document.createElement('select');
  select.id = 'preset-select';
  select.innerHTML = '<option value="">-- select preset --</option>';
  try {
    const res = await fetch('/nh/v1/presets');
    const presets = await res.json();
    presets.forEach((p: any) => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = `${p.name} (${p.n_partials} partials, f1=${p.f1.toFixed(1)})`;
      select.appendChild(opt);
    });
  } catch (e) {
    logStatus('Failed to load preset list');
  }
  container.appendChild(select);

  loadBtn.addEventListener('click', () => {
    const id = (select as HTMLSelectElement).value;
    if (id) handlers.onLoad(id);
  });
  saveBtn.addEventListener('click', () => handlers.onSave());
}
