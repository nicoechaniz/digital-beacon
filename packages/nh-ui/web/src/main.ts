import { connectWS, sendControl } from './ws';
import { setStatus, setRendererCaps, logStatus } from './ui';
import './style.css';

async function main() {
  setStatus('connecting');
  const ws = await connectWS({
    onCapabilities: (caps) => {
      setStatus('connected');
      setRendererCaps(caps);
    },
    onField: (_field) => {
      // Field snapshots received here; performance controls will subscribe in M1-3.
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
    logStatus('Panic sent');
  });
}

main();
