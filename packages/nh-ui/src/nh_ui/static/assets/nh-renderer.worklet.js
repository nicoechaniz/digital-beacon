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
    const activePartials = partials.filter(p => p.gain > 0);
    // Peak-safe normalization (mirrors the Python renderer): the worst-case
    // coherent peak of a sum of sines is the sum of their amplitudes. Dividing
    // by that sum when it exceeds unity guarantees the output never saturates,
    // even for a high-gain preset with the master raised.
    const totalGain = activePartials.reduce((sum, p) => sum + p.gain, 0);
    const norm = 1.0 / Math.max(1.0, totalGain);
    for (const partial of activePartials) {
      const freq = f1 * partial.n;
      const pan = partial.pan || 0.0;
      const phaseDeg = partial.phase || 0.0;
      const phaseOffset = (phaseDeg * Math.PI) / 180.0;
      const leftGain = 0.5 * (1.0 - pan);
      const rightGain = 0.5 * (1.0 + pan);

      for (let i = 0; i < frames; i++) {
        const t = (currentFrame + i) / sr;
        const sample = norm * partial.gain * Math.sin(2.0 * Math.PI * freq * t + phaseOffset + this.phase);
        left[i] += leftGain * sample;
        right[i] += rightGain * sample;
      }
    }

    this.phase += 2.0 * Math.PI * f1 * frames / sr;
    this.phase %= 2.0 * Math.PI;

    const outL = output[0];
    const outR = output[1] || outL;
    for (let i = 0; i < frames; i++) {
      // Defensive hard limit; the peak-safe normalization already bounds this.
      outL[i] = Math.max(-1.0, Math.min(1.0, left[i]));
      outR[i] = Math.max(-1.0, Math.min(1.0, right[i]));
    }
    return true;
  }
}

registerProcessor('nh-renderer', NHRendererProcessor);
