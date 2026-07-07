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

export function setRendererStatus(state: 'python' | 'webaudio', active: boolean) {
  const el = document.getElementById('renderer-status') as HTMLSpanElement | null;
  if (!el) return;
  el.className = active ? 'status-ok' : 'status-warn';
  el.textContent = `${state === 'python' ? 'Python (sounddevice)' : 'WebAudio (browser)'} ${active ? 'active' : 'inactive'}`;
}

export function setAudioStatus(status: { state: string; message?: string }) {
  const el = document.getElementById('audio-status') as HTMLSpanElement | null;
  if (!el) return;
  const label = status.state.charAt(0).toUpperCase() + status.state.slice(1);
  el.className = `audio-${status.state}`;
  el.textContent = label;
  if (status.message) {
    el.title = status.message;
  }
}

interface RendererSelectorHandlers {
  onChange: (renderer: 'python' | 'webaudio') => void;
}

export function renderRendererSelector(
  current: 'python' | 'webaudio',
  handlers: RendererSelectorHandlers
) {
  const container = document.getElementById('renderer-section')!;
  container.innerHTML = '<h2>Renderer</h2>';

  const row = document.createElement('div');
  row.className = 'renderer-row';
  row.innerHTML = `
    <select id="renderer-select">
      <option value="webaudio">WebAudio (browser)</option>
      <option value="python">Python (sounddevice)</option>
    </select>
    <span id="renderer-status" class="status-warn">Unknown</span>
  `;
  container.appendChild(row);

  const caps = document.createElement('div');
  caps.id = 'renderer-caps';
  container.appendChild(caps);

  const help = document.createElement('div');
  help.className = 'help-box';
  help.innerHTML = `
    <p><strong>Python (sounddevice):</strong> audio goes to the server's sound card.</p>
    <p><strong>WebAudio (browser):</strong> audio goes to your browser's output. You must click <em>Start Audio</em> once with your mouse to unlock the browser audio context.</p>
  `;
  container.appendChild(help);

  const select = row.querySelector('#renderer-select') as HTMLSelectElement;
  select.value = current;
  select.addEventListener('change', () => {
    const value = select.value as 'python' | 'webaudio';
    handlers.onChange(value);
  });
}

export function logStatus(message: string) {
  const el = document.getElementById('status-log')!;
  const line = document.createElement('div');
  const time = new Date().toLocaleTimeString();
  line.textContent = `[${time}] ${message}`;
  el.prepend(line);
}

export function initTabs() {
  const buttons = document.querySelectorAll('.tab-btn');
  const panels = document.querySelectorAll('.tab-panel');
  buttons.forEach((btn) => {
    btn.addEventListener('click', () => {
      const tab = (btn as HTMLElement).dataset.tab;
      buttons.forEach((b) => b.classList.remove('active'));
      panels.forEach((p) => p.classList.remove('active'));
      btn.classList.add('active');
      const panel = document.getElementById(tab!);
      if (panel) panel.classList.add('active');
    });
  });
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
  onSpatialRotationChange?: (deg: number) => void;
  onResidualMixChange?: (mix: number) => void;
}

export function renderPerformanceControls(
  state: { baseF1: number; masterGain: number; partialGains: Map<number, number>; muted: Set<number>; maxPartials: number; spatialRotation?: number; residualMix?: number },
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

  const spatialSection = document.createElement('div');
  spatialSection.className = 'control-group';
  spatialSection.innerHTML = `
    <label>Spatial <span id="spatial-rotation-display">${(state.spatialRotation || 0).toFixed(0)}°</span></label>
    <input type="range" id="spatial-rotation" min="0" max="360" step="1" value="${state.spatialRotation || 0}" />
  `;
  container.appendChild(spatialSection);
  const spatialSlider = spatialSection.querySelector('#spatial-rotation') as HTMLInputElement;
  spatialSlider.addEventListener('input', () => {
    const val = parseFloat(spatialSlider.value);
    const display = document.getElementById('spatial-rotation-display') as HTMLSpanElement;
    if (display) display.textContent = val.toFixed(0) + '°';
    if (handlers.onSpatialRotationChange) handlers.onSpatialRotationChange(val);
  });

  const residualSection = document.createElement('div');
  residualSection.className = 'control-group';
  residualSection.innerHTML = `
    <label>Residual <span id="residual-mix-display">${(state.residualMix ?? 1).toFixed(2)}</span></label>
    <input type="range" id="residual-mix" min="0" max="2" step="0.01" value="${state.residualMix ?? 1}" />
  `;
  container.appendChild(residualSection);
  const residualSlider = residualSection.querySelector('#residual-mix') as HTMLInputElement;
  residualSlider.addEventListener('input', () => {
    const val = parseFloat(residualSlider.value);
    const display = document.getElementById('residual-mix-display') as HTMLSpanElement;
    if (display) display.textContent = val.toFixed(2);
    if (handlers.onResidualMixChange) handlers.onResidualMixChange(val);
  });

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

interface LaunchpadState {
  active: Set<number>;
  toggles: Set<number>;
  momentaries: Set<number>;
}

export function renderLaunchpadMirror(state: LaunchpadState) {
  let container = document.getElementById('launchpad-mirror');
  if (!container) return;
  let grid = container.querySelector('.launchpad-grid');
  if (!grid) {
    grid = document.createElement('div');
    grid.className = 'launchpad-grid';
    for (let row = 0; row < 8; row++) {
      for (let col = 0; col < 8; col++) {
        const n = row * 8 + col + 1;  // 1-based to match adapter n and pad events
        const btn = document.createElement('div');
        btn.className = 'launchpad-pad';
        btn.dataset.n = String(n);
        btn.textContent = String(n);
        grid.appendChild(btn);
      }
    }
    container.appendChild(grid);
  }
  container.querySelectorAll('.launchpad-pad').forEach((pad) => {
    const el = pad as HTMLElement;
    const n = parseInt(el.dataset.n!);
    const isToggle = state.toggles && state.toggles.has(n);
    const isMom = state.momentaries && state.momentaries.has(n);
    const isActive = state.active.has(n) || isToggle || isMom;
    el.classList.toggle('active', isActive);
    el.classList.toggle('toggle', isToggle);
    el.classList.toggle('momentary', isMom);
  });
}

interface SensorPanelHandlers {
  onSimulateMuse: (value: number) => void;
  onSimulateIMU: (yaw: number, pitch: number, roll: number) => void;
  onSimulateTilt: (x: number, y: number) => void;
}

export function renderSensorPanel(handlers: SensorPanelHandlers) {
  const container = document.getElementById('sensor-panel')!;
  container.innerHTML = '<h2>Sensors</h2>';

  const museSection = document.createElement('div');
  museSection.className = 'sensor-group';
  museSection.innerHTML = `
    <label>Muse focus <span id="muse-focus-display">0.00</span></label>
    <input type="range" id="muse-focus" min="0" max="1" step="0.01" value="0" />
    <button id="muse-send">Send Muse</button>
  `;
  container.appendChild(museSection);
  museSection.querySelector('#muse-send')!.addEventListener('click', () => {
    const val = parseFloat((museSection.querySelector('#muse-focus') as HTMLInputElement).value);
    handlers.onSimulateMuse(val);
  });

  const imuSection = document.createElement('div');
  imuSection.className = 'sensor-group';
  imuSection.innerHTML = `
    <label>IMU yaw <span id="imu-yaw-display">0°</span></label>
    <input type="range" id="imu-yaw" min="-180" max="180" step="1" value="0" />
    <label>Pitch <span id="imu-pitch-display">0°</span></label>
    <input type="range" id="imu-pitch" min="-90" max="90" step="1" value="0" />
    <button id="imu-send">Send IMU</button>
  `;
  container.appendChild(imuSection);
  imuSection.querySelector('#imu-send')!.addEventListener('click', () => {
    const yaw = parseFloat((imuSection.querySelector('#imu-yaw') as HTMLInputElement).value);
    const pitch = parseFloat((imuSection.querySelector('#imu-pitch') as HTMLInputElement).value);
    handlers.onSimulateIMU(yaw, pitch, 0);
  });

  const tiltSection = document.createElement('div');
  tiltSection.className = 'sensor-group';
  tiltSection.innerHTML = `
    <label>Phone tilt</label>
    <div class="tilt-pad" id="tilt-pad"></div>
    <button id="tilt-send">Send tilt</button>
  `;
  container.appendChild(tiltSection);
  let tiltX = 0, tiltY = 0;
  tiltSection.querySelector('#tilt-pad')!.addEventListener('click', (e) => {
    const rect = (e.target as HTMLElement).getBoundingClientRect();
    tiltX = ((e as MouseEvent).clientX - rect.left) / rect.width * 2 - 1;
    tiltY = ((e as MouseEvent).clientY - rect.top) / rect.height * 2 - 1;
  });
  tiltSection.querySelector('#tilt-send')!.addEventListener('click', () => {
    handlers.onSimulateTilt(tiltX, tiltY);
  });
}

export function updateMuseFocus(value: number) {
  const el = document.getElementById('muse-focus-display') as HTMLSpanElement | null;
  if (el) el.textContent = value.toFixed(2);
}

export function updateIMUYaw(yaw: number) {
  const el = document.getElementById('imu-yaw-display') as HTMLSpanElement | null;
  if (el) el.textContent = yaw.toFixed(0) + '°';
}

export function updateIMUPitch(pitch: number) {
  const el = document.getElementById('imu-pitch-display') as HTMLSpanElement | null;
  if (el) el.textContent = pitch.toFixed(0) + '°';
}

interface UploadHandlers {
  onPresetUpload: (file: File) => void;
  onWavAnalyze: (file: File) => void;
}

export function renderUploadPanels(handlers: UploadHandlers) {
  const container = document.getElementById('preset-upload')!;
  container.innerHTML = '<h3>Upload preset</h3>';

  const presetInput = document.createElement('input');
  presetInput.type = 'file';
  presetInput.accept = '.json';
  const presetBtn = document.createElement('button');
  presetBtn.textContent = 'Upload preset';
  presetBtn.disabled = true;
  presetInput.addEventListener('change', () => {
    presetBtn.disabled = !presetInput.files?.length;
  });
  presetBtn.addEventListener('click', () => {
    if (presetInput.files?.[0]) handlers.onPresetUpload(presetInput.files[0]);
  });
  container.appendChild(presetInput);
  container.appendChild(presetBtn);

  const analysis = document.getElementById('analysis-panel')!;
  analysis.innerHTML = '<h3>Analyze WAV</h3>';
  const wavInput = document.createElement('input');
  wavInput.type = 'file';
  wavInput.accept = 'audio/wav,.wav';
  const wavBtn = document.createElement('button');
  wavBtn.textContent = 'Analyze';
  wavBtn.disabled = true;
  wavInput.addEventListener('change', () => {
    wavBtn.disabled = !wavInput.files?.length;
  });
  wavBtn.addEventListener('click', () => {
    if (wavInput.files?.[0]) handlers.onWavAnalyze(wavInput.files[0]);
  });
  analysis.appendChild(wavInput);
  analysis.appendChild(wavBtn);

  const results = document.createElement('div');
  results.id = 'analysis-results';
  results.className = 'analysis-results';
  results.innerHTML = '<p>No analysis yet.</p>';
  analysis.appendChild(results);
}

export function setAnalysisResults(result: any) {
  const el = document.getElementById('analysis-results') as HTMLDivElement | null;
  if (!el) return;
  if (!result.ok) {
    el.innerHTML = `<p class="error">Analysis failed: ${result.detail || 'unknown'}</p>`;
    return;
  }
  const f1 = result.f1 ? `${result.f1.toFixed(2)} Hz` : 'N/A';
  el.innerHTML = `
    <ul>
      <li><strong>f1:</strong> ${f1}</li>
      <li><strong>sr:</strong> ${result.sr || 'N/A'}</li>
      <li><strong>duration:</strong> ${result.duration ? result.duration.toFixed(2) + 's' : 'N/A'}</li>
      <li><strong>voiced frames:</strong> ${result.voiced_frames ?? 'N/A'} / ${result.total_frames ?? 'N/A'}</li>
    </ul>
  `;
}

export function renderFieldSummary(field: any) {
  let el = document.getElementById('field-summary') as HTMLDivElement | null;
  if (!el) {
    const performance = document.getElementById('performance')!;
    el = document.createElement('div');
    el.id = 'field-summary';
    el.className = 'field-summary';
    performance.insertBefore(el, performance.firstChild);
  }
  const partials = field?.partials || {};
  const active = Object.values(partials).filter((p: any) => (p.gain || 0) > 0).length;
  el.innerHTML = `
    <span><strong>f1:</strong> ${(field?.f1 || 0).toFixed(2)} Hz</span>
    <span><strong>partials:</strong> ${active} / ${Object.keys(partials).length}</span>
  `;
}

interface SensorSafetyHandlers {
  onInfluenceChange: (value: number) => void;
  onSourceEnable: (source: string, enabled: boolean) => void;
}

export function renderSensorSafety(handlers: SensorSafetyHandlers) {
  const container = document.getElementById('sensor-safety')!;
  container.innerHTML = '<h2>Sensor Safety</h2>';

  const influenceSection = document.createElement('div');
  influenceSection.className = 'control-group';
  influenceSection.innerHTML = `
    <label>Influence <span id="sensor-influence-display">100%</span></label>
    <input type="range" id="sensor-influence" min="0" max="1" step="0.01" value="1" />
  `;
  container.appendChild(influenceSection);
  const influenceSlider = influenceSection.querySelector('#sensor-influence') as HTMLInputElement;
  influenceSlider.addEventListener('input', () => {
    const val = parseFloat(influenceSlider.value);
    const display = document.getElementById('sensor-influence-display') as HTMLSpanElement;
    display.textContent = Math.round(val * 100) + '%';
    handlers.onInfluenceChange(val);
  });

  const sources = ['muse', 'imu', 'phone'];
  sources.forEach((source) => {
    const row = document.createElement('div');
    row.className = 'sensor-source-row';
    row.innerHTML = `
      <label>${source}</label>
      <input type="checkbox" class="sensor-source-toggle" data-source="${source}" checked />
    `;
    container.appendChild(row);
  });

  container.querySelectorAll('.sensor-source-toggle').forEach((toggle) => {
    toggle.addEventListener('change', (e) => {
      const target = e.target as HTMLInputElement;
      handlers.onSourceEnable(target.dataset.source!, target.checked);
    });
  });
}
