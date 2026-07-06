/**
 * NaturalHarmony WebAudio client renderer.
 * Loads the AudioWorklet and exposes `render(field)` to send snapshots.
 */
class NHWebRenderer {
  constructor() {
    this.audioContext = null;
    this.workletNode = null;
  }

  async start(workletUrl) {
    this.audioContext = new AudioContext({ sampleRate: 48000 });
    await this.audioContext.audioWorklet.addModule(workletUrl);
    this.workletNode = new AudioWorkletNode(this.audioContext, 'nh-renderer', {
      outputChannelCount: [2],
    });
    this.workletNode.connect(this.audioContext.destination);
  }

  stop() {
    if (this.workletNode) {
      this.workletNode.disconnect();
      this.workletNode = null;
    }
    if (this.audioContext) {
      this.audioContext.close();
      this.audioContext = null;
    }
  }

  render(field) {
    if (this.workletNode) {
      this.workletNode.port.postMessage({
        type: 'field',
        payload: field,
      });
    }
  }
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { NHWebRenderer };
}
