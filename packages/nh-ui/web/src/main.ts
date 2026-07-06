import { connectWS, sendControl, sendSensor } from './ws';
import { WebAudioRenderer } from './audio';
import {
  setStatus, setRendererCaps, logStatus, renderPerformanceControls,
  updateF1Display, updateMasterDisplay, updatePartialDisplay, renderPresetBrowser,
  renderLaunchpadMirror, renderSensorPanel, renderSensorSafety, updateMuseFocus, updateIMUYaw, updateIMUPitch
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
}

const state: RuntimeState = {
  baseF1: 65.0,
  f1Offset: 0.0,
  masterGain: 1.0,
  partialGains: new Map(),
  muted: new Set(),
  maxPartials: 32,
  currentField: null,
};

const launchpadState = { active: new Set<number>() };
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

async function main() {
  setStatus('connecting');
  ws = await connectWS({
    onCapabilities: (caps) => {
      setStatus('connected');
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
    },
    onField: (field) => {
      state.currentField = field;
      state.baseF1 = field.f1 - state.f1Offset;
      updateF1Display(getF1());
      audioRenderer.render(field);
    },
    onControl: (event) => {
      if (event.type === 'pad_on') {
        const n = event.value?.n ?? 0;
        launchpadState.active.add(n);
        renderLaunchpadMirror(launchpadState);
        setTimeout(() => {
          launchpadState.active.delete(n);
          renderLaunchpadMirror(launchpadState);
        }, 200);
      }
    },
    onError: (err) => {
      setStatus('error');
      logStatus(`WS error: ${err.message || err}`);
    },
    onClose: () => setStatus('disconnected'),
  });

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
      } catch (e) {
        logStatus(`Audio error: ${e}`);
      }
    });
  }
  panic.addEventListener('click', () => {
    sendControl(ws, { type: 'panic', value: null });
    state.f1Offset = 0;
    state.masterGain = 1;
    state.partialGains.clear();
    state.muted.clear();
    updateF1Display(state.baseF1);
    updateMasterDisplay(1);
    logStatus('Panic sent');
  });
}

let ws: WebSocket;
main();
