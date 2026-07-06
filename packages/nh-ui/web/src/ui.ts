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
