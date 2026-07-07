import { connectWS, sendControl, sendSensor } from './ws';
import type { WSConnection } from './ws';
import { WebAudioRenderer } from './audio';
import type { AudioStatus } from './audio';
import {
  setStatus, setRendererCaps, setRendererStatus, setAudioStatus, logStatus,
  renderPerformanceControls, updateF1Display, updateMasterDisplay, updatePartialDisplay,
  renderPresetBrowser, renderLaunchpadMirror, renderSensorPanel, renderSensorSafety,
  updateMuseFocus, updateIMUYaw, updateIMUPitch, renderRendererSelector
} from './ui';
import './style.css';

interface RuntimeState {
  baseF1: number;
  f1Offset: number;
  masterGain: number;
  partialGains: Map<number, number>;
  muted: Set<number>;
  maxPartials: number;
  currentField: any;
  renderer: 'python' | 'webaudio';
}

const state: RuntimeState = {
  baseF1: 65.0,
  f1Offset: 0.0,
  masterGain: 0.0, // safety: silent until the performer raises the master
  partialGains: new Map(),
  muted: new Set(),
  maxPartials: 32,
  currentField: null,
  renderer: 'python',
};

const launchpadState = { active: new Set<number>(), toggles: new Set<number>(), momentaries: new Set<number>() };
const audioRenderer = new WebAudioRenderer();

function getF1() {
  return state.baseF1 + state.f1Offset;
}

function sendMaster(gain: number) {
  state.masterGain = gain;
  sendControl(ws, { type: 'master', value: gain });
  updateMasterDisplay(gain);
}

function sendF1Offset(offset: number) {
  state.f1Offset = offset;
  sendControl(ws, { type: 'f1_offset', value: offset });
  updateF1Display(getF1());
}

function sendPartialGain(n: number, gain: number) {
  const effectiveGain = state.muted.has(n) ? 0 : gain;
  state.partialGains.set(n, gain);
  sendControl(ws, { type: 'partial_gain', value: { n, gain: effectiveGain } });
  updatePartialDisplay(n, effectiveGain);
}

async function loadPreset(presetId: string) {
  const res = await fetch(`/nh/v1/presets/${presetId}/load`, { method: 'POST' });
  const data = await res.json();
  if (data.ok) {
    logStatus(`Loaded preset ${presetId} (f1=${data.f1})`);
  } else {
    logStatus(`Failed to load preset: ${data.detail || 'unknown'}`);
  }
}

async function saveSnapshot() {
  if (!state.currentField) return;
  const payload = {
    version: '1',
    harmonic_field: state.currentField,
    metadata: { name: 'snapshot-' + new Date().toISOString() },
  };
  const res = await fetch('/nh/v1/presets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (data.ok) {
    logStatus(`Saved snapshot to ${data.path}`);
  } else {
    logStatus(`Failed to save snapshot: ${data.detail || 'unknown'}`);
  }
}

async function setRenderer(renderer: 'python' | 'webaudio') {
  const res = await fetch('/nh/v1/renderer', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ renderer }),
  });
  const data = await res.json();
  if (data.ok) {
    state.renderer = renderer;
    setRendererStatus(renderer, true);
    logStatus(`Renderer switched to ${renderer}`);
  } else {
    logStatus(`Failed to switch renderer: ${data.detail || 'unknown'}`);
  }
}

async function initRendererSelector() {
  try {
    const res = await fetch('/nh/v1/renderer');
    const data = await res.json();
    state.renderer = data.renderer === 'webaudio' ? 'webaudio' : 'python';
  } catch (e) {
    logStatus('Failed to fetch current renderer; defaulting to Python');
  }
  renderRendererSelector(state.renderer, {
    onChange: (renderer) => setRenderer(renderer),
  });
  setRendererStatus(state.renderer, true);
}

async function main() {
  setStatus('connecting');
  await initRendererSelector();

  ws = await connectWS({
    onOpen: () => {
      setStatus('connected');
      logStatus('WebSocket connected');
    },
    onCapabilities: (caps) => {
      setRendererCaps(caps);
      state.maxPartials = caps.max_partials || 32;
      renderPerformanceControls(state, {
        onF1Change: sendF1Offset,
        onMasterChange: sendMaster,
        onPartialGainChange: sendPartialGain,
        onMuteChange: (n, muted) => {
          if (muted) state.muted.add(n);
          else state.muted.delete(n);
          sendPartialGain(n, state.partialGains.get(n) || 1.0);
        },
      });
      // Reset master to 0 on (re)connect so audio never starts loud; the model
      // default is also 0. The performer must explicitly raise the master.
      sendMaster(0);
      renderPresetBrowser({ onLoad: loadPreset, onSave: saveSnapshot });
      renderSensorPanel({
        onSimulateMuse: (value) => {
          updateMuseFocus(value);
          sendSensor(ws, { source: 'muse', type: 'muse_focus', value });
        },
        onSimulateIMU: (yaw, pitch, _roll) => {
          updateIMUYaw(yaw);
          updateIMUPitch(pitch);
          sendSensor(ws, { source: 'imu', type: 'imu.orientation.yaw', value: yaw });
          sendSensor(ws, { source: 'imu', type: 'imu.orientation.pitch', value: pitch });
        },
        onSimulateTilt: (x, y) => {
          sendSensor(ws, { source: 'phone', type: 'phone.tilt', value: { x, y } });
        },
      });
      renderSensorSafety({
        onInfluenceChange: (value) => {
          sendControl(ws, { type: 'sensor_influence', value });
        },
        onSourceEnable: (source, enabled) => {
          sendControl(ws, { type: 'sensor_source_enable', value: { source, enabled } });
        },
      });
      renderLaunchpadMirror(launchpadState);
    },
    onField: (field) => {
      state.currentField = field;
      state.baseF1 = field.f1 - state.f1Offset;
      updateF1Display(getF1());
      if (state.renderer === 'webaudio') {
        audioRenderer.render(field);
      }
    },
    onControl: (event) => {
      const n = event.value?.n ?? 0;
      if (event.type === 'pad_on') {
        launchpadState.active.add(n);
        launchpadState.momentaries.add(n);
        renderLaunchpadMirror(launchpadState);
        setTimeout(() => {
          launchpadState.active.delete(n);
          launchpadState.momentaries.delete(n);
          renderLaunchpadMirror(launchpadState);
        }, 200);
      } else if (event.type === 'pad_toggle') {
        if (event.value?.active) {
          launchpadState.active.add(n);
          launchpadState.toggles.add(n);
        } else {
          launchpadState.active.delete(n);
          launchpadState.toggles.delete(n);
        }
        renderLaunchpadMirror(launchpadState);
      } else if (event.type === 'pad_off') {
        launchpadState.active.delete(n);
        launchpadState.momentaries.delete(n);
        renderLaunchpadMirror(launchpadState);
      }
    },
    onError: (err) => {
      setStatus('error');
      logStatus(`WS error: ${err.message || err}`);
    },
    onClose: () => {
      setStatus('disconnected');
      logStatus('WebSocket disconnected');
    },
  }, { autoReconnect: true });

  const panic = document.getElementById('panic') as HTMLButtonElement;
  panic.disabled = false;

  const audioBtn = document.getElementById('audio-toggle') as HTMLButtonElement;
  if (audioBtn) {
    audioBtn.disabled = false;
    audioBtn.addEventListener('click', async () => {
      try {
        if (audioRenderer.running) {
          audioRenderer.stop();
          audioBtn.textContent = 'Start Audio';
          logStatus('Audio stopped');
        } else {
          await audioRenderer.start();
          audioBtn.textContent = 'Stop Audio';
          logStatus('Audio started');
        }
      } catch (e: any) {
        logStatus(`Audio error: ${e}`);
      }
    });
  }

  audioRenderer.onStatusChange((status: AudioStatus) => {
    setAudioStatus(status);
    if (status.state === 'error' || status.state === 'denied') {
      logStatus(`Audio ${status.state}: ${status.message || ''}`);
    }
  });

  panic.addEventListener('click', () => {
    sendControl(ws, { type: 'panic', value: null });
    state.f1Offset = 0;
    state.masterGain = 0;
    state.partialGains.clear();
    state.muted.clear();
    // Reflect the reset in the controls so the sliders match the silent state.
    const masterSlider = document.getElementById('master-slider') as HTMLInputElement | null;
    if (masterSlider) masterSlider.value = '0';
    const f1Slider = document.getElementById('f1-slider') as HTMLInputElement | null;
    if (f1Slider) f1Slider.value = '0';
    const f1Fine = document.getElementById('f1-fine') as HTMLInputElement | null;
    if (f1Fine) f1Fine.value = '0';
    updateF1Display(state.baseF1);
    updateMasterDisplay(0);
    logStatus('Panic sent');
  });
}

let ws: WSConnection;
main();
