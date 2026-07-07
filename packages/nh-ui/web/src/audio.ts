const WORKLET_URL = '/assets/nh-renderer.worklet.js';

export interface AudioStatus {
  state: 'idle' | 'starting' | 'running' | 'suspended' | 'error' | 'denied';
  message?: string;
}

export class WebAudioRenderer {
  private ctx: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;
  private statusListeners: Set<(status: AudioStatus) => void> = new Set();
  private _status: AudioStatus = { state: 'idle' };

  private _setStatus(status: AudioStatus) {
    this._status = status;
    this.statusListeners.forEach((cb) => cb(status));
  }

  onStatusChange(callback: (status: AudioStatus) => void): () => void {
    this.statusListeners.add(callback);
    callback(this._status);
    return () => this.statusListeners.delete(callback);
  }

  async start(): Promise<void> {
    if (this.ctx) {
      await this.resume();
      return;
    }
    this._setStatus({ state: 'starting' });
    try {
      const AC = window.AudioContext || (window as any).webkitAudioContext;
      this.ctx = new AC({ sampleRate: 48000 });
      await this.ctx.audioWorklet.addModule(WORKLET_URL);
      this.node = new AudioWorkletNode(this.ctx, 'nh-renderer', {
        outputChannelCount: [2],
      });
      this.node.connect(this.ctx.destination);

      if (this.ctx.state === 'suspended') {
        await this.ctx.resume();
      }
      this._setStatus({ state: 'running' });
    } catch (e: any) {
      const isPermissionError =
        e?.name === 'NotAllowedError' ||
        e?.message?.toLowerCase().includes('permission') ||
        e?.message?.toLowerCase().includes('user gesture');
      this.cleanup();
      this._setStatus({
        state: isPermissionError ? 'denied' : 'error',
        message: isPermissionError
          ? 'Audio permission denied. Click again or allow audio in site settings.'
          : e?.message || 'Failed to start audio',
      });
      throw e;
    }
  }

  stop() {
    this.cleanup();
    this._setStatus({ state: 'idle' });
  }

  private cleanup() {
    if (this.node) {
      try {
        this.node.disconnect();
      } catch {}
      this.node = null;
    }
    if (this.ctx) {
      try {
        this.ctx.close();
      } catch {}
      this.ctx = null;
    }
  }

  render(field: any) {
    if (this.node && this.node.port) {
      this.node.port.postMessage({ type: 'field', payload: field });
    }
    // If the context exists but is suspended (e.g. after a policy block), try to
    // resume on any user-driven render call (slider move, preset load, etc.).
    if (this.ctx && this.ctx.state === 'suspended') {
      this.ctx.resume().catch(() => {});
    }
  }

  get running(): boolean {
    return this.ctx !== null && this.ctx.state === 'running';
  }

  get status(): AudioStatus {
    return this._status;
  }

  async resume() {
    if (this.ctx && this.ctx.state === 'suspended') {
      try {
        await this.ctx.resume();
        this._setStatus({ state: this.ctx.state as any });
      } catch (e: any) {
        this._setStatus({ state: 'error', message: e?.message });
      }
    }
  }
}
