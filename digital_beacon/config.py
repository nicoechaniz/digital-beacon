"""Configuration for digital-beacon (Shaper side + f1 bridge defaults)."""

# ─── Audio (Shaper) ──────────────────────────────────────────────────────────
AUDIO_SAMPLE_RATE = 44100
AUDIO_BLOCK_SIZE = 256
AUDIO_DEVICE = None       # None = system default

# ─── f1 / harmonic band setup ────────────────────────────────────────────────
DEFAULT_F1 = 40.0        # base frequency in Hz (NOT fixed — see F1_MOD_POINTS)
N_BANDS = 32              # 32 BPF in beacon.scd (band N = f1 * N)

# 12-point discrete f1 modulation slot — NOT IMPLEMENTED YET.
# Will be populated with 12 fixed f1 values for users to step through.
# Each entry is a frequency in Hz. Empty = f1 stays at DEFAULT_F1.
# Example: F1_MOD_POINTS = [32.0, 36.0, 40.0, 44.0, 48.0, 52.0, 56.0, 60.0,
#                            65.0, 70.0, 80.0, 96.0]
F1_MOD_POINTS = []

# Range bounds for safety clamping on /beacon/f1
F1_MIN = 20.0
F1_MAX = 200.0

# ─── OSC ─────────────────────────────────────────────────────────────────────
# Beacon broadcast port (NaturalHarmony harmonic_beacon broadcasts /beacon/*
# here — Shaper co-listens via SO_REUSEPORT, same as the original)
BEACON_BROADCAST_PORT = 9001

# Shaper direct OSC control (for /digital/* commands)
SHAPER_OSC_PORT = 9002

# sclang port — where f1_bridge forwards /beacon/vsource and where external
# control can drive the SC engine
SCLANG_OSC_PORT = 57120
SCLANG_HOST = "127.0.0.1"

OSC_HOST = "0.0.0.0"      # bind for listeners

# ─── Beacon-spatial legacy port (older /beacon/* messages) ──────────────────
# digital-beacon ALSO listens on the legacy 9001 broadcast port.
# The old schema (13 bands) is not consumed — only /beacon/voice/* and
# /beacon/f1 are. See osc_receiver.py for the exact routing.

# ─── Voice limits ────────────────────────────────────────────────────────────
# Shaper polyphony: maximum simultaneous active voices (sines)
# Larger than the original 5-voice limit because we have 32 band targets
# and we want the user to be able to layer freely during the demo.
MAX_VOICES = 32

# Default gain/pan/phase for a fresh voice
DEFAULT_VOICE_GAIN = 0.6
DEFAULT_VOICE_PAN = 0.0
DEFAULT_VOICE_PHASE_DEG = 0.0

# Envelope defaults (applied per-voice in the audio callback)
DEFAULT_VOICE_ATTACK_S = 0.01   # ramp-up time in seconds
DEFAULT_VOICE_RELEASE_S = 0.15  # ramp-down time in seconds

# Timbre: waveshaper drive (0=pure sine, 1=rich harmonics — didgeridoo/vocal)
DEFAULT_VOICE_SHAPE = 0.0

# ─── Side-chain (beacon → shaper envelope following) ────────────────────────
# sidechain_amount: -1=ducking, 0=off, +1=follow
# beacon_level is updated via OSC /beacon/level from SC engine
DEFAULT_SIDECHAIN_AMOUNT = 0.0

# ─── LFO (synced to beacon strumming) ───────────────────────────────────────
DEFAULT_LFO_RATE_DIVISOR = 1    # strum period ÷ N (1=every strum)
DEFAULT_LFO_WAVEFORM = "sine"   # sine | triangle | saw | square | samplehold
DEFAULT_LFO_AMOUNT = 0.0        # 0..1 global amount
DEFAULT_LFO_GAIN = 0.0          # per-voice LFO → gain mod
DEFAULT_LFO_PAN = 0.0
DEFAULT_LFO_PHASE = 0.0

# Strum detection: how many recent strums to average for period estimate
STRUM_WINDOW = 8
DEFAULT_STRUM_PERIOD_S = 0.5    # fallback when no strums detected yet

# ─── Mix ─────────────────────────────────────────────────────────────────────
# Shaper master gain (0..1). Beacon master is controlled via /beacon/master OSC.
DEFAULT_SHAPER_MASTER = 0.8
DEFAULT_BEACON_MASTER = 0.9

# ─── Minilab3 (auxiliary MIDI controller) ────────────────────────────────────
MINILAB_PORT_PATTERN = "Minilab"
MINILAB_PANIC_PAD = 39

# ─── Launchpad Mini (primary) ────────────────────────────────────────────────
# Pad-mode defaults: 8x8 grid, pad N = harmonic N (1..64)
LAUNCHPAD_PORT_PATTERN = "Launchpad"
LAUNCHPAD_PADS_X = 8
LAUNCHPAD_PADS_Y = 8

# Split mode: bottom 4 rows (32 pads) momentary, top 4 rows (32 pads) toggle
# CC104 from NaturalHarmony toggles this. Default ON for digital-beacon demo.
SPLIT_MODE_ENABLED_BY_DEFAULT = True
SPLIT_MODE_TOGGLE_CC = 104

# Launchpad Mini Programmer-mode feedback colors (velocity = color)
PAD_FEEDBACK_COLOR_ON = 60          # Green High
PAD_FEEDBACK_COLOR_TOGGLE_ON = 21   # Orange

# ─── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
