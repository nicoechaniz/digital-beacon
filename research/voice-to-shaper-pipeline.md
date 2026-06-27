# Voice-to-Additive-Synth Pipeline Design

**Project:** digital-beacon (Shaper: 32 pure-sine harmonic voices)  
**Goal:** Map short Spanish spoken clips → F0 contour + per-harmonic gains so the Shaper produces an "ethereal harmonic ghost" (recognizable rhythm/intonation, pure sines only).  
**Constraints:** Offline analysis of 5-10 s mono WAVs; accuracy > speed; target the existing OSC control surface; no transparent resynthesis.  
**Date:** 2026-06-27

## 1. Recommended F0 Extractor: librosa.pyin (pYIN)

**Decision:** Use `librosa.pyin` (probabilistic YIN) as the primary F0 tracker.

**Rationale:**
- pYIN adds a probabilistic/HMM layer on classic YIN autocorrelation that gives both a smooth F0 and a voiced probability / decision per frame. This directly solves voiced/unvoiced segmentation (see §3).
- On clean conversational speech (quiet room, adult male/female), pYIN is highly reliable with far fewer octave errors and boundary glitches than raw YIN or basic autocorrelation.
- Lightweight (numpy/scipy under the hood), easy to integrate, widely validated for speech.
- Offline single-clip: we can afford its cost and tune parameters (fmin=50, fmax=400 for Spanish speech, frame_length scaled to sr, e.g. 4096 @48 kHz).

**Comparison notes (for the listed options):**
- **YIN (librosa.yin / torchaudio):** Solid base but raw; octave jumps and no native voicing. Requires heavy post-processing.
- **pYIN:** Best balance. Built-in voicing prob reduces failure modes at voiced/unvoiced transitions.
- **CREPE (CNN):** Highest raw pitch accuracy on many benchmarks; very robust. Overkill and slow/heavy (TF or torchcrepe) for offline accuracy-prioritized but not necessary work. Use only if pYIN shows systematic bias on target speakers.
- **HPS + peak picking:** Fast, spectral; fails often on speech due to formants, weak F0, noise. Not for primary tracker.
- **Cepstrum:** Decent for separating source/filter sometimes but less stable for continuous F0 tracking than autocorrelation variants.
- **Autocorrelation (plain):** The root of YIN; peak-picking problems (bias toward strong formants, multiple peaks) are exactly what YIN/pYIN were invented to fix.

**Parameters (starting point, 48 kHz recordings):**  
`fmin=50, fmax=400, frame_length=4096, hop_length=1024, resolution=0.1` (or librosa defaults then post-process). Use `fill_na=np.nan`; voiced frames have finite f0.

**Alternative if needed:** CREPE (via torchcrepe) for maximum contour fidelity on difficult speakers. Trade-off: install + inference cost.

## 2. Recommended Harmonic Envelope Extractor: STFT + F0-guided bin sampling + relative dB threshold

**Decision:** 
- Compute STFT (Hann window) on the same time base or resampled to match F0 frames.
- For each frame t with F0(t): evaluate magnitude at the nearest bin (or interpolated) for f = F0(t) * n, n=1..32.
- Convert to dB (ref = max of spectrum or the F0 bin itself).
- Active harmonics: those ≥ -35 dB relative to the F0 component (or per-frame strongest harmonic).
- Gains: relative magnitudes (or normalized 0-1 per frame, or absolute scaled to synth range), zeroed for inactive.

**Rationale & parameters:**
- STFT is the standard, simple, and sufficient once F0 is known. No need for joint HPS on harmonics when F0 is already high-quality.
- Multi-resolution not required for this use case (offline, narrow F0 range in speech).
- Cepstral smoothing of envelope is useful for formant modeling in full vocoders but overkill here — we only care about the 32 discrete harmonic points.

**Concrete STFT settings (48 kHz input):**
- `n_fft=4096` (or 8192 with zero-pad), `win_length=4096` (~85 ms), `hop_length=1024` (~21 ms).
- Hann window.
- Why: ~11.7 Hz bin spacing gives clean separation even for low male F0 (~80-120 Hz) up to N=20-25. 85 ms window captures enough periods for stable magnitude estimate at higher partials without excessive time-smearing for speech syllables. Hop gives ~47 fps analysis rate (easy to decimate for OSC).

**Threshold decision: -35 dB (relative to F0 bin magnitude).**
- No universal "standard" magic number; literature varies -20 dB (conservative, only strong partials) to -50 dB (includes very weak).
- -35 dB is a pragmatic starting point for quiet-room conversational speech: captures the perceptually relevant harmonics while discarding noise floor and formant sidelobes. Higher partials naturally roll off; this threshold lets N_active vary naturally (often 4-12 in voiced speech).
- Speaker dependence: breathy/pressed voices → raise to -30 dB; clear/projected → -40 dB may work. Tune by ear on 2-3 clips (compare original vs "which partials are audible when isolated").
- Implementation tip: also apply a soft floor (e.g. -60 dB absolute) and optional per-harmonic median smoothing (3-5 frames) for stable N_active.

**HPS note:** Useful historically for joint F0+strength but redundant here. STFT sampling is more direct and accurate once trustworthy F0 exists.

## 3. Recommended Voiced/Unvoiced Detector

**Decision (primary):** pYIN voiced probability (or non-NaN F0) + secondary energy gate.

- Use `librosa.pyin(..., )` — it returns probability or use the internal voiced decision logic. Threshold ~0.5-0.7 on prob.
- Confirm with short-time energy: frames below ~ -35 to -40 dBFS (or adaptive noise floor from unvoiced regions) → force unvoiced.
- Optional: high zero-crossing rate or high spectral flatness as veto.

**Synth strategy:** Silence (all gains=0) during unvoiced segments.  
Pure-sine Shaper has no good way to render fricatives/bursts without sounding artificial. Silence preserves the "harmonic ghost" aesthetic and avoids garbage. (If noise bursts desired later, that is a separate inharmonic layer.)

This keeps the pipeline simple and the output musical.

## 4. Recommended F0 Smoothing + Artifact Rejection

**Smoothing:**
1. Median filter (window 5-7 frames, ~100-150 ms) first — kills isolated octave errors and spikes.
2. Then low-pass / Savitzky-Golay (order 2-3, window corresponding to ~10-12 Hz cutoff). Or simple FIR/IIR lowpass on linear or log-F0.

**Cutoff rationale:** Speech macro-intonation (phrase/syllable contours) lives well below 10 Hz. Micro-jitter (1-5 Hz natural variation) is beautiful in real voice but produces audible "wobble" or chorusing when driving 32 locked sines. 8-12 Hz cutoff removes jitter while preserving intentional glides and steps.

**Jump / artifact rejection:**
- pYIN's built-in transition model (`max_transition_rate`) already discourages wild jumps.
- Post-process: if a frame-to-frame delta exceeds ~20-30% (or 40-60 Hz absolute, speaker-dependent) *and* does not continue smoothly in the next 1-2 frames, treat as error. Interpolate across the glitch from surrounding reliable voiced frames or hold previous good value.
- On fully unvoiced → voiced transitions, allow larger step (new phonation often starts at different F0).

Result: a smooth, musically usable contour that still feels like the speaker's intonation.

## 5. Recommended OSC Streaming Pattern

**Core pattern:** Analysis produces time-series arrays (F0(t), active_mask(t), gain_matrix[N,t]) → manual real-time or faster-than-realtime sender loop using `python_osc.udp_client.SimpleUDPClient`.

**Why not a fancy library?** None exists that turns numpy arrays into this specific control surface. Manual loop is transparent, debuggable, and gives exact control over rates and interpolation.

**Target ports/messages (from current Shaper surface):**
- Port 9002 (SHAPER_OSC_PORT) for direct: `/digital/harmonic/<N>/gain <float 0-1>`
- Beacon broadcast (9001) for lifecycle + freq: `/beacon/voice/on <vid> <freq> <gain> <note> <harmonic_n>`, `/beacon/voice/freq <vid> <new_freq>`, `/beacon/voice/off <vid>`
- Optionally `/beacon/f1 <F0>` to keep SC bands aligned.

**Recommended control flow in sender:**
- Maintain a dict harmonic_n → vid (monotonic or fixed-per-N).
- On each analysis frame (or decimated):
  - Current F0.
  - For each n=1..32:
    - If newly active: send `/beacon/voice/on` with freq = F0*n (or F0 for slot?), appropriate gain, harmonic_n=n.
    - If already active: send `/beacon/voice/freq` (for glide) + `/digital/harmonic/N/gain`.
    - If dropping: send `/beacon/voice/off`.
- Update rate: F0/freq at 30-50 Hz (smooth glide). Gains and on/off decisions at 10-20 Hz (envelope changes slowly).
- Interpolation in the loop (linear on F0 and gains between analysis frames) prevents zipper artifacts if sender runs faster than analysis hop.
- Timing: `time.sleep(dt)` or a precise loop for real-time playback feel. For "render preview" can blast faster-than-realtime.

**Activation note:** Direct gain sets do *not* flip `active=True` or set freq in the store. Use the `/beacon/voice/*` messages (or equivalent store calls if scripting in-process) to manage lifecycle and freq. Gains via `/digital/...` are fine for dynamic level once the voice slot is active.

**Alternative:** Drive entirely via repeated `/beacon/voice/on` at every update (cheap for 32 voices). Simpler state in the sender.

**Also send:** occasional `/beacon/f1` if you want the binaural beacon layers to track the same contour.

## 6. Reference Implementations / Prior Art to Study

**Primary recommendations (in priority order):**

1. **Harmonic plus Noise Model (HNM)** — Yannis Stylianou et al. (1990s-2000s, AT&T TTS work).
   - Exactly decomposes speech into a time-varying *harmonic* component (strict multiples of F0 with amplitudes/phases) + a modulated noise residual.
   - The harmonic part is almost a perfect blueprint for what we feed the Shaper. Study the analysis (F0 + harmonic amplitudes per band) and how they handle voiced/unvoiced max_frequency boundary.
   - Key papers: "Harmonic plus noise models for speech" (various ICASSP/SSW).

2. **McAulay-Quatieri (MQ) sinusoidal model** (1986 and follow-ups).
   - Foundational STFT peak-picking + sinusoidal tracking for speech analysis/synthesis.
   - Not strictly harmonic (partials can wander independently), but the analysis/synthesis loop, amplitude/frequency/phase interpolation, and handling of birth/death of partials are directly inspirational. We are a constrained special case (partials locked to F0 multiples, limited to N=32).

3. **Spectral Modeling Synthesis (SMS)** — Xavier Serra.
   - Deterministic (sinusoidal) + stochastic decomposition. The deterministic sinusoids + the overall philosophy of "explicitly modeling the tonal skeleton" is the closest artistic/technical ancestor.
   - Even though we force harmonicity and drop the residual, SMS papers on partial tracking, amplitude envelopes, and resynthesis give the right mental model.

**Others worth skimming (lower priority):**
- Kelly-Lochbaum (and LPC source-filter models) for vocal tract intuition, but not additive oscillators.
- STRAIGHT / TANDEM-STRAIGHT (Kawahara) for ultra-clean F0 + envelope extraction (gold standard in speech processing; use for validation or as oracle F0).
- Classic additive experiments (Risset, Mathews) for the "pure sine choir" aesthetic target.

**What to ignore for now:** Full vocoders, phase vocoders, neural vocoders (HiFi-GAN etc.), and anything that resynthesizes with the original excitation or formant filters. We want *only* the harmonic skeleton.

## Summary of Concrete Starting Parameters

- F0: librosa.pyin, ~85 ms frames, voicing prob.
- STFT: 4096-pt Hann @48 kHz, hop 1024.
- Harmonic threshold: -35 dB rel. to F0 component.
- Smoothing: median(5) → ~10 Hz LPF / savgol on F0.
- Unvoiced → silence.
- OSC: python-osc client, voice on/freq for pitch, /digital/harmonic/N/gain for levels, 20-50 Hz updates with interp.
- Deps to add (analysis script): librosa, soundfile (or use project recorder patterns + wave).

This design keeps the pipeline minimal, leverages the existing Shaper OSC surface, and directly serves the artistic goal of a pure-harmonic spoken "ghost."

Tune thresholds and smoothing by ear on real clips from the project. The first working version should be trivially adjustable.
