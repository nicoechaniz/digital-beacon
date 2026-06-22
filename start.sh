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

# ─── 0. Pre-kill: ensure ports are free ─────────────────────────────────────
# If a previous session died badly, scsynth/sclang may still hold the ports.
# Killing them up-front prevents "address in use" failures on restart.
prekill() {
  echo ""
  echo ">>> [0/4] Pre-kill: clearing zombies on 57110/57120/9001/9002 ..."
  pkill -9 -f 'pw-jack scsynth'        2>/dev/null || true
  pkill -9 -f 'sclang.*beacon.scd'     2>/dev/null || true
  pkill -9 -f 'sclang.*digital-beacon' 2>/dev/null || true
  pkill -9 -f 'f1_bridge'               2>/dev/null || true
  pkill -9 -f 'digital_beacon.main'    2>/dev/null || true
  sleep 1.0
  # Belt-and-suspenders: explicitly free any port still bound
  local freed=0
  for port in 57110 57120 9001 9002; do
    local pids
    pids=$(ss -ulnpH 2>/dev/null | awk -v p=":$port" '$0 ~ p' | grep -oP 'pid=\K[0-9]+' | sort -u)
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
        echo "  Killed PID $pid holding port $port"
        freed=1
      fi
    done
  done
  if [[ $freed -eq 1 ]]; then
    sleep 0.5
  fi
  echo "  Pre-kill done."
}
prekill

# ─── 1. pw-jack + scsynth ────────────────────────────────────────────────────
echo ""
echo ">>> [1/4] Starting pw-jack scsynth on :57110 ..."
pw-jack scsynth -u 57110 -i 2 -o 2 > /tmp/digital-beacon-scsynth.log 2>&1 &
SCSYNTH_PID=$!
echo "  scsynth PID: $SCSYNTH_PID"

# Wait for scsynth to open its port (max 10s)
for i in {1..20}; do
  if (echo > /dev/udp/127.0.0.1/57110) 2>/dev/null; then
    echo "  scsynth port 57110 open."
    break
  fi
  # Check if scsynth died (e.g. address in use)
  if ! kill -0 $SCSYNTH_PID 2>/dev/null; then
    echo ""
    echo "ERROR: scsynth died on startup. Last log:"
    cat /tmp/digital-beacon-scsynth.log 2>/dev/null | tail -10
    echo ""
    echo "Likely cause: port 57110 still in use. Pre-kill may have failed."
    echo "Manual fix:   pkill -9 -f 'pw-jack scsynth'; sleep 2; ss -ulnp | grep 57110"
    exit 1
  fi
  sleep 0.5
  if [[ $i -eq 20 ]]; then
    echo "ERROR: scsynth did not open 57110 in 10s. Aborting."
    kill -TERM $SCSYNTH_PID 2>/dev/null || true
    exit 1
  fi
done

# Connect scsynth outputs to system playback.
# With pw-jack the JACK client name is "SuperCollider" (not "scsynth" as in
# old jackd setups). We try to connect to whichever playback sink exists:
# the R24 (capture/playback card) and the Built-in Audio fallback.
sleep 0.5
CONNECTED=0
for sink in 'R24 Analog Stereo' 'Built-in Audio Analog Stereo'; do
  if pw-jack jack_connect "SuperCollider:out_1" "${sink}:playback_FL" 2>/dev/null; then
    pw-jack jack_connect "SuperCollider:out_2" "${sink}:playback_FR" 2>/dev/null || true
    echo "  SuperCollider:out connected to ${sink}"
    CONNECTED=1
    break
  fi
done
if [[ $CONNECTED -eq 0 ]]; then
  echo "  WARNING: could not auto-connect SuperCollider to any playback sink."
  echo "  Run: pw-jack jack_connect SuperCollider:out_1 <sink>:playback_FL"
fi

# ─── 2. sclang (beacon.scd) ─────────────────────────────────────────────────
echo ""
echo ">>> [2/4] Starting sclang beacon.scd (PTY, :57120) ..."
# sclang needs a TTY or it exits. We use Python's pty module to allocate a
# pseudo-TTY and a reader thread to capture output unbuffered.
# This is more robust than script(1) + FIFO, which has buffering issues with
# long-running processes.
SCLANG_LOG=/tmp/digital-beacon-sclang.log
rm -f "$SCLANG_LOG"
./venv/bin/python3 -c "
import os, pty, select, sys, threading
log = '$SCLANG_LOG'
pid, fd = pty.fork()
if pid == 0:
    # Child: exec sclang
    os.environ['BEACON_SOURCE'] = '$BEACON_SOURCE'
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    os.execvp('sclang', ['sclang', '-u', '57120', 'beacon.scd'])
else:
    # Parent: read from pty, write to log
    def reader():
        with open(log, 'wb', buffering=0) as f:
            while True:
                try:
                    r, _, _ = select.select([fd], [], [], 0.1)
                    if not r: continue
                    data = os.read(fd, 4096)
                    if not data: break
                    f.write(data)
                except OSError:
                    break
    threading.Thread(target=reader, daemon=True).start()
    os.waitpid(pid, 0)
" </dev/null >/dev/null 2>&1 &
SCLANG_PID=$!
echo "  sclang PID: $SCLANG_PID  (log: $SCLANG_LOG)"

# ─── 3. f1_bridge ────────────────────────────────────────────────────────────
BRIDGE_PID=
if [[ $RUN_BRIDGE -eq 1 ]]; then
  echo ""
  echo ">>> [3/4] Starting f1_bridge.py ..."
  ./venv/bin/python3 f1_bridge.py > /tmp/digital-beacon-bridge.log 2>&1 &
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
  ./venv/bin/python3 -m digital_beacon.main "${EXTRA_PY_ARGS[@]}" \
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
  # SIGTERM the tracked PIDs first
  for pid in $SHAPER_PID $BRIDGE_PID $SCLANG_PID $SCSYNTH_PID; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  sleep 0.5
  # Kill anything else still holding our ports (orphans, grandchildren)
  for port in 57110 57120 9001 9002; do
    local pids
    pids=$(ss -ulnpH 2>/dev/null | awk -v p=":$port" '$0 ~ p' | grep -oP 'pid=\K[0-9]+' | sort -u)
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then
        kill -KILL "$pid" 2>/dev/null || true
      fi
    done
  done
  # Final SIGKILL on tracked PIDs
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
echo "    scsynth: tail -f /tmp/digital-beacon-scsynth.log"
echo "    sclang:  tail -f /tmp/digital-beacon-sclang.log"
echo "    bridge:  tail -f /tmp/digital-beacon-bridge.log"
echo "    shaper:  tail -f /tmp/digital-beacon-shaper.log"
echo "  Stop:    Ctrl-C"
echo "=========================================="
echo ""

# Wait for any tracked child to die. The scsynth-died check inside the boot
# loop already exited with error; once we're here, all four are alive.
wait -n $SCSYNTH_PID $SCLANG_PID ${BRIDGE_PID:-0} ${SHAPER_PID:-0} 2>/dev/null || true
EXITED=$?
echo "A child process exited (wait rc=$EXITED). Cleaning up..."
cleanup
