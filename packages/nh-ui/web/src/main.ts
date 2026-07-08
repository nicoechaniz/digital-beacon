import {
  renderAppShell,
  renderScene,
  renderStatus,
  appendLog,
  bindShellHandlers,
  type SceneSnapshot,
} from './ui';
import './style.css';

const SCENE_URL = '/nh/v2/scene';
const CONTROL_URL = '/nh/v2/scene/control';
const PRESETS_URL = '/nh/v2/presets';

let currentScene: SceneSnapshot | null = null;
let refreshTimer: number | null = null;

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

async function main(): Promise<void> {
  renderAppShell();
  bindShellHandlers({
    onRefresh: loadScene,
    onControl: sendControl,
    onTypedControl: sendTypedControl,
    onMuteSource: muteSource,
    onSoloSource: soloSource,
  });

  await Promise.all([loadScene(), loadPresets()]);
  refreshTimer = window.setInterval(loadScene, 1500);
  window.addEventListener('beforeunload', () => {
    if (refreshTimer !== null) window.clearInterval(refreshTimer);
  });
}

main().catch((err) => {
  renderStatus('error', 'boot failed');
  appendLog(`Boot failed: ${err instanceof Error ? err.message : String(err)}`);
});
