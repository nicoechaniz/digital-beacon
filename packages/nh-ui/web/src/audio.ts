const WORKLET_URL = '/assets/nh-renderer.worklet.js';

export class WebAudioRenderer {
  private ctx: AudioContext | null = null;
  private node: AudioWorkletNode | null = null;

  async start(): Promise<void> {
    if (this.ctx) return;
    const AC = window.AudioContext || (window as any).webkitAudioContext;
    this.ctx = new AC({ sampleRate: 48000 });
    await this.ctx.audioWorklet.addModule(WORKLET_URL);
    this.node = new AudioWorkletNode(this.ctx, 'nh-renderer', {
      outputChannelCount: [2],
    });
    this.node.connect(this.ctx.destination);
  }

  stop() {
    if (this.node) {
      this.node.disconnect();
      this.node = null;
    }
    if (this.ctx) {
      this.ctx.close();
      this.ctx = null;
    }
  }

  render(field: any) {
    if (this.node && this.node.port) {
      this.node.port.postMessage({ type: 'field', payload: field });
    }
  }

  get running(): boolean {
    return this.ctx !== null && this.ctx.state === 'running';
  }

  async resume() {
    if (this.ctx && this.ctx.state === 'suspended') {
      await this.ctx.resume();
    }
  }
}
