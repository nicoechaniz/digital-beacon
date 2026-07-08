import {
  renderAppShell,
  renderScene,
  renderAnalysis,
  renderStatus,
  appendLog,
  bindShellHandlers,
  type SceneSnapshot,
} from './ui';
import './style.css';

const SCENE_URL = '/nh/v2/scene';
const CONTROL_URL = '/nh/v2/scene/control';
const PRESETS_URL = '/nh/v2/presets';
const ANALYSIS_URL = '/nh/v2/analysis';
const WS_URL = `ws://${window.location.host}/nh/v1/ws`;

let currentScene: SceneSnapshot | null = null;
let refreshTimer: number | null = null;
let ws: WebSocket | null = null;
let wsReconnectTimer: number | null = null;

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json() as Promise<T>;
}

async function loadScene(): Promise<void> {
  try {
    renderStatus('connecting', 'syncing scene');
    currentScene = await fetchJSON<SceneSnapshot>(SCENE_URL);
    renderScene(currentScene);
    renderStatus('connected', 'scene online');
  } catch (err) {
    renderStatus('error', 'scene unavailable');
    appendLog(`Scene fetch failed: ${err instanceof Error ? err.message : String(err)}`);
  }
}

async function sendControl(path: string, value: unknown): Promise<void> {
  await fetchJSON<{ ok: boolean }>(CONTROL_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, value }),
  });
  appendLog(`control ${path} = ${JSON.stringify(value)}`);
  await loadScene();
}

async function sendTypedControl(type: string, value: unknown): Promise<void> {
  await fetchJSON<{ ok: boolean }>(CONTROL_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type, value }),
  });
  appendLog(`control ${type} ${JSON.stringify(value)}`);
  await loadScene();
}

async function muteSource(sourceId: string, mute: boolean): Promise<void> {
  await fetchJSON<{ ok: boolean }>(`/nh/v2/scene/sources/${encodeURIComponent(sourceId)}/mute`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mute }),
  });
  appendLog(`${mute ? 'muted' : 'unmuted'} ${sourceId}`);
  await loadScene();
}

async function soloSource(sourceId: string): Promise<void> {
  await fetchJSON<{ ok: boolean }>(`/nh/v2/scene/sources/${encodeURIComponent(sourceId)}/solo`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ solo: true }),
  });
  appendLog(`solo ${sourceId}`);
  await loadScene();
}

async function loadPresets(): Promise<void> {
  try {
    const presets = await fetchJSON<any[]>(PRESETS_URL);
    const select = document.getElementById('preset-select') as HTMLSelectElement | null;
    if (!select) return;
    select.innerHTML = '<option value="">Select v2 preset…</option>';
    presets.forEach((preset) => {
      const option = document.createElement('option');
      option.value = preset.id;
      option.textContent = `${preset.name ?? preset.id} · ${preset.n_sources ?? 0} sources`;
      select.appendChild(option);
    });
    appendLog(`loaded ${presets.length} v2 presets`);
  } catch (err) {
    appendLog(`Preset list failed: ${err instanceof Error ? err.message : String(err)}`);
  }
}

async function loadAnalysis(): Promise<void> {
  try {
    const payload = await fetchJSON<{ analysis: any | null }>(ANALYSIS_URL);
    renderAnalysis(payload.analysis);
  } catch (err) {
    appendLog(`Analysis fetch failed: ${err instanceof Error ? err.message : String(err)}`);
  }
}

async function loadMockAnalysis(): Promise<void> {
  await fetchJSON<{ ok: boolean; analysis: any }>(`${ANALYSIS_URL}/mock`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      audio_path: 'field-recording-demo.wav',
      duration_s: 4.2,
      f0_track: { f0_mean: 110.0, voiced_fraction: 0.87 },
      phideus: { h_series: { concentration: 0.72, deviation: 0.08 } },
      proposed_f1: 55.0,
      sample_source: { source_id: 'field_recording_demo', kind: 'sample' },
    }),
  });
  appendLog('loaded mock analysis result');
  await loadAnalysis();
}

async function applyProposedF1(): Promise<void> {
  const result = await fetchJSON<{ ok: boolean; f1: number }>(`${ANALYSIS_URL}/apply-proposed-f1`, { method: 'POST' });
  appendLog(`applied proposed f1 = ${result.f1} Hz`);
  await Promise.all([loadScene(), loadAnalysis()]);
}

async function loadPreset(presetId: string): Promise<void> {
  try {
    await fetchJSON<{ ok: boolean }>(`${PRESETS_URL}/${encodeURIComponent(presetId)}/load`, { method: 'POST' });
    appendLog(`loaded preset ${presetId}`);
    await loadScene();
  } catch (err) {
    appendLog(`Preset load failed: ${err instanceof Error ? err.message : String(err)}`);
  }
}

async function connectWebSocket(): Promise<void> {
  if (ws !== null) return;
  try {
    ws = new WebSocket(WS_URL);
    ws.addEventListener('open', () => {
      renderStatus('connected', 'websocket live');
      appendLog('WebSocket connected');
    });
    ws.addEventListener('message', (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'control_event') {
          const payload = msg.payload || {};
          if (['pad_on', 'pad_off', 'pad_toggle', 'panic'].includes(payload.type)) {
            // Reflect external control (Launchpad, sensors) in the UI immediately.
            void loadScene();
          }
        }
      } catch (err) {
        // ignore malformed messages
      }
    });
    ws.addEventListener('close', () => {
      renderStatus('connecting', 'websocket closed, retrying');
      ws = null;
      scheduleReconnect();
    });
    ws.addEventListener('error', () => {
      renderStatus('error', 'websocket error');
      ws = null;
      scheduleReconnect();
    });
  } catch (err) {
    scheduleReconnect();
  }
}

function scheduleReconnect(): void {
  if (wsReconnectTimer !== null) return;
  wsReconnectTimer = window.setTimeout(() => {
    wsReconnectTimer = null;
    void connectWebSocket();
  }, 1500);
}

async function main(): Promise<void> {
  renderAppShell();
  bindShellHandlers({
    onRefresh: loadScene,
    onControl: sendControl,
    onTypedControl: sendTypedControl,
    onMuteSource: muteSource,
    onSoloSource: soloSource,
    onMockAnalysis: loadMockAnalysis,
    onApplyProposedF1: applyProposedF1,
    onLoadPreset: loadPreset,
  });

  await Promise.all([loadScene(), loadPresets(), loadAnalysis()]);
  void connectWebSocket();
  refreshTimer = window.setInterval(loadScene, 1500);
  window.addEventListener('beforeunload', () => {
    if (refreshTimer !== null) window.clearInterval(refreshTimer);
    if (wsReconnectTimer !== null) window.clearTimeout(wsReconnectTimer);
    if (ws !== null) ws.close();
  });
}

main().catch((err) => {
  renderStatus('error', 'boot failed');
  appendLog(`Boot failed: ${err instanceof Error ? err.message : String(err)}`);
});
