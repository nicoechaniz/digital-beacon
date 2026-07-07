/*
 * NaturalHarmony Web Audio renderer: AudioWorklet processor.
 * Receives a harmonic field snapshot via MessagePort and renders
 * additive sine partials to the output buffer.
 */
class NHRendererProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.field = { f1: 65.0, partials: [] };
    this.phase = 0.0;
    this.port.onmessage = (event) => {
      if (event.data && event.data.type === 'field') {
        this.field = event.data.payload;
      }
    };
  }

  process(inputs, outputs, parameters) {
    const output = outputs[0];
    const frames = output[0].length;
    const sr = sampleRate;
    const f1 = this.field.f1 || 65.0;

    const left = new Float32Array(frames);
    const right = new Float32Array(frames);

    const partials = Array.isArray(this.field.partials)
      ? this.field.partials
      : Object.values(this.field.partials || {});
    for (const partial of partials) {
      if (partial.gain <= 0) continue;
      const freq = f1 * partial.n;
      const pan = partial.pan || 0.0;
      const phaseDeg = partial.phase || 0.0;
      const phaseOffset = (phaseDeg * Math.PI) / 180.0;
      const leftGain = 0.5 * (1.0 - pan);
      const rightGain = 0.5 * (1.0 + pan);

      for (let i = 0; i < frames; i++) {
        const t = (currentFrame + i) / sr;
        const sample = partial.gain * Math.sin(2.0 * Math.PI * freq * t + phaseOffset + this.phase);
        left[i] += leftGain * sample;
        right[i] += rightGain * sample;
      }
    }

    this.phase += 2.0 * Math.PI * f1 * frames / sr;
    this.phase %= 2.0 * Math.PI;

    const outL = output[0];
    const outR = output[1] || outL;
    for (let i = 0; i < frames; i++) {
      let l = left[i];
      let r = right[i];
      if (Math.abs(l) > 1.0) l = Math.tanh(l);
      if (Math.abs(r) > 1.0) r = Math.tanh(r);
      outL[i] = l;
      outR[i] = r;
    }
    return true;
  }
}

registerProcessor('nh-renderer', NHRendererProcessor);
