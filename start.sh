#!/usr/bin/env bash
# start.sh — digital-beacon launcher
#
# Starts (in order):
#   1. pw-jack scsynth           (audio server, 57110)
#   2. sclang beacon.scd         (synth engine, 57120)
#   3. f1_bridge.py              (forwards f1 -> vsrate)
#   4. digital_beacon main       (Shaper: MIDI + OSC + audio)
#
# Usage:
#   ./start.sh --file             (WAV source — same as beacon-spatial)
#   ./start.sh --live             (SoundIn(0) — R24 CH1)
#   ./start.sh --no-shaper        (beacon only)
#   ./start.sh --no-bridge        (beacon + shaper, f1 stays fixed)
#
# Hard stop: Ctrl-C cleans up all child PIDs.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Default flags
USE_FILE=0
RUN_SHAPER=1
RUN_BRIDGE=1
EXTRA_PY_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --file)        USE_FILE=1; shift ;;
    --live)        USE_FILE=0; shift ;;
    --no-shaper)   RUN_SHAPER=0; shift ;;
    --no-bridge)   RUN_BRIDGE=0; shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)  echo "Unknown arg: $1"; exit 1 ;;
  esac
done

# Verify the audio source file exists when --file
AUDIO_FILE="/home/nicolas/Projects/beacon-spatial/harmonic_beacon_2026_05_13_session.wav"
if [[ $USE_FILE -eq 1 && ! -f "$AUDIO_FILE" ]]; then
  echo "ERROR: audio source not found: $AUDIO_FILE"
  echo "Use --live to use SoundIn(0) instead."
  exit 1
fi

# Source = file or live
if [[ $USE_FILE -eq 1 ]]; then
  export BEACON_SOURCE=file
  echo "Source: FILE — $(basename "$AUDIO_FILE")"
else
  export BEACON_SOURCE=live
  echo "Source: LIVE — SoundIn.ar(0) [R24 CH1]"
fi

# ─── 1. pw-jack + scsynth ────────────────────────────────────────────────────
echo ""
echo ">>> [1/4] Starting pw-jack scsynth on :57110 ..."
pw-jack scsynth -u 57110 -i 2 -o 2 &
SCSYNTH_PID=$!
echo "  scsynth PID: $SCSYNTH_PID"

# Wait for scsynth to open its port (max 10s)
for i in {1..20}; do
  if (echo > /dev/udp/127.0.0.1/57110) 2>/dev/null; then
    echo "  scsynth port 57110 open."
    break
  fi
  sleep 0.5
  if [[ $i -eq 20 ]]; then
    echo "ERROR: scsynth did not open 57110 in 10s. Aborting."
    kill -TERM $SCSYNTH_PID 2>/dev/null || true
    exit 1
  fi
done

# Connect scsynth outputs to system playback
sleep 0.3
pw-jack jack_connect scsynth:output_1 system:playback_1 2>/dev/null || true
pw-jack jack_connect scsynth:output_2 system:playback_2 2>/dev/null || true

# ─── 2. sclang (beacon.scd) ─────────────────────────────────────────────────
echo ""
echo ">>> [2/4] Starting sclang beacon.scd (TTY wrapper, :57120) ..."
# script(1) wrapper is required — sclang exits immediately without a TTY
script -q -c "QT_QPA_PLATFORM=offscreen sclang -u 57120 beacon.scd" /tmp/digital-beacon-sclang.log &
SCLANG_PID=$!
echo "  sclang PID: $SCLANG_PID  (log: /tmp/digital-beacon-sclang.log)"

# ─── 3. f1_bridge ────────────────────────────────────────────────────────────
BRIDGE_PID=
if [[ $RUN_BRIDGE -eq 1 ]]; then
  echo ""
  echo ">>> [3/4] Starting f1_bridge.py ..."
  python3 f1_bridge.py > /tmp/digital-beacon-bridge.log 2>&1 &
  BRIDGE_PID=$!
  echo "  bridge PID: $BRIDGE_PID  (log: /tmp/digital-beacon-bridge.log)"
else
  echo ""
  echo ">>> [3/4] Skipping f1_bridge (--no-bridge)"
fi

# ─── 4. digital_beacon Shaper ────────────────────────────────────────────────
SHAPER_PID=
if [[ $RUN_SHAPER -eq 1 ]]; then
  echo ""
  echo ">>> [4/4] Starting digital_beacon Shaper (MIDI + OSC + audio) ..."
  python3 -m digital_beacon.main "${EXTRA_PY_ARGS[@]}" \
    > /tmp/digital-beacon-shaper.log 2>&1 &
  SHAPER_PID=$!
  echo "  shaper PID: $SHAPER_PID  (log: /tmp/digital-beacon-shaper.log)"
else
  echo ""
  echo ">>> [4/4] Skipping shaper (--no-shaper)"
fi

# ─── Trap for clean shutdown ─────────────────────────────────────────────────
cleanup() {
  echo ""
  echo "=== Shutting down digital-beacon ==="
  for pid in $SHAPER_PID $BRIDGE_PID $SCLANG_PID $SCSYNTH_PID; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  sleep 0.5
  for pid in $SHAPER_PID $BRIDGE_PID $SCLANG_PID $SCSYNTH_PID; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
  echo "Done."
  exit 0
}
trap cleanup INT TERM

echo ""
echo "=========================================="
echo "  digital-beacon running"
echo "  PIDs: scsynth=$SCSYNTH_PID sclang=$SCLANG_PID bridge=$BRIDGE_PID shaper=$SHAPER_PID"
echo "  Logs:"
echo "    sclang:  tail -f /tmp/digital-beacon-sclang.log"
echo "    bridge:  tail -f /tmp/digital-beacon-bridge.log"
echo "    shaper:  tail -f /tmp/digital-beacon-shaper.log"
echo "  Stop:    Ctrl-C"
echo "=========================================="
echo ""

# Wait for any child to die
wait -n $SCSYNTH_PID $SCLANG_PID ${BRIDGE_PID:-0} ${SHAPER_PID:-0} 2>/dev/null || true
echo "A child process exited. Cleaning up..."
cleanup
