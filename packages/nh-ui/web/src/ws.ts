const WS_PATH = '/nh/v1/ws';

export interface WSHandlers {
  onCapabilities?: (caps: any) => void;
  onField?: (field: any) => void;
  onControl?: (event: any) => void;
  onError?: (err: any) => void;
  onClose?: () => void;
}

export function connectWS(handlers: WSHandlers): Promise<WebSocket> {
  return new Promise((resolve, _reject) => {
    const ws = new WebSocket(`ws://${location.host}${WS_PATH}`);

    ws.onopen = () => {
      resolve(ws);
    };

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'renderer_capabilities') {
          handlers.onCapabilities?.(msg.payload);
        } else if (msg.type === 'base_field') {
          handlers.onField?.(msg.payload);
        } else if (msg.type === 'control_event') {
          handlers.onControl?.(msg.payload);
        } else if (msg.type === 'error') {
          handlers.onError?.(msg.payload);
        }
      } catch (err) {
        handlers.onError?.(err);
      }
    };

    ws.onclose = () => {
      handlers.onClose?.();
    };

    ws.onerror = (err) => {
      handlers.onError?.(err);
    };
  });
}

export function sendControl(ws: WebSocket, event: { type: string; value: any }) {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'control_event', payload: event }));
  }
}

export function sendSensor(ws: WebSocket, event: { source: string; type: string; value: any }) {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'sensor_event', payload: event }));
  }
}
