const WS_PATH = '/nh/v1/ws';
const RECONNECT_DELAY_MS = 500;
const MAX_RECONNECT_DELAY_MS = 5000;

export interface WSHandlers {
  onCapabilities?: (caps: any) => void;
  onField?: (field: any) => void;
  onControl?: (event: any) => void;
  onError?: (err: any) => void;
  onClose?: () => void;
  onOpen?: () => void;
}

export interface WSConnection {
  ws: WebSocket;
  readyState: number;
  send: (data: string) => void;
}

function isWebSocketOpen(ws: WebSocket | null): boolean {
  return ws !== null && ws.readyState === WebSocket.OPEN;
}

export function connectWS(handlers: WSHandlers, options?: { autoReconnect?: boolean }): Promise<WSConnection> {
  const autoReconnect = options?.autoReconnect !== false;
  let ws: WebSocket | null = null;
  let reconnectDelay = RECONNECT_DELAY_MS;
  let reconnectTimer: number | null = null;
  let manuallyClosed = false;

  const connect = (): Promise<WSConnection> => {
    return new Promise((resolve) => {
      manuallyClosed = false;
      ws = new WebSocket(`ws://${location.host}${WS_PATH}`);

      ws.onopen = () => {
        reconnectDelay = RECONNECT_DELAY_MS;
        handlers.onOpen?.();
        resolve({
          ws: ws!,
          get readyState() {
            return ws?.readyState ?? WebSocket.CLOSED;
          },
          send(data: string) {
            if (isWebSocketOpen(ws)) {
              ws!.send(data);
            }
          },
        });
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
          } else if (msg.type === 'pong') {
            // Keep-alive response
          }
        } catch (err) {
          handlers.onError?.(err);
        }
      };

      ws.onclose = () => {
        handlers.onClose?.();
        if (autoReconnect && !manuallyClosed) {
          if (reconnectTimer) window.clearTimeout(reconnectTimer);
          reconnectTimer = window.setTimeout(() => {
            reconnectTimer = null;
            connect();
          }, reconnectDelay);
          reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_DELAY_MS);
        }
      };

      ws.onerror = (err) => {
        handlers.onError?.(err);
      };
    });
  };

  return connect();
}

export function sendControl(ws: WSConnection, event: { type: string; value: any }) {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'control_event', payload: event }));
  }
}

export function sendSensor(ws: WSConnection, event: { source: string; type: string; value: any }) {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'sensor_event', payload: event }));
  }
}

export function sendPing(ws: WSConnection) {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'ping', payload: {} }));
  }
}
