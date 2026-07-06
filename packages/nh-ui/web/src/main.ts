import { connectWS, sendControl } from './ws';
import { setStatus, setRendererCaps, logStatus, renderPerformanceControls, updateF1Display, updateMasterDisplay, updatePartialDisplay } from './ui';
import './style.css';

interface RuntimeState {
  baseF1: number;
  f1Offset: number;
  masterGain: number;
  partialGains: Map<number, number>;
  muted: Set<number>;
  maxPartials: number;
}

const state: RuntimeState = {
  baseF1: 65.0,
  f1Offset: 0.0,
  masterGain: 1.0,
  partialGains: new Map(),
  muted: new Set(),
  maxPartials: 32,
};

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
    },
    onField: (field) => {
      state.baseF1 = field.f1 - state.f1Offset;
      updateF1Display(getF1());
    },
    onError: (err) => {
      setStatus('error');
      logStatus(`WS error: ${err.message || err}`);
    },
    onClose: () => setStatus('disconnected'),
  });

  const panic = document.getElementById('panic') as HTMLButtonElement;
  panic.disabled = false;
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
