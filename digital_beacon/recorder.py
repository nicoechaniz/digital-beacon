"""Recording manager for digital-beacon.

Captures the final mixed audio (SC binaural + Shaper sines) by recording
the monitor of the active PipeWire sink. This is the analogue of
beacon-spatial's `Server.record` — but it captures BOTH engines in a
single WAV (the natural mixdown that reaches the user's headphones).

Strategy
--------
We don't intercept SC or Shaper audio individually. Both engines already
land in the user's PipeWire default sink via:
  - SC binaural  -> pw-jack scsynth  -> PipeWire
  - Shaper sines -> sounddevice       -> PipeWire
The default sink's `.monitor` exposes exactly that mix. Recording the
monitor captures the same signal the user hears.

We use `pw-record` (PipeWire's CLI) in a subprocess. It's a stable
public tool, doesn't require extra Python deps, and writes WAV
headered correctly out of the box.

Threading
---------
Recording runs in a subprocess; the manager exposes start/stop/status
methods that are thread-safe (a single lock guards the subprocess and
its metadata). The FastAPI dashboard calls these from request threads.
"""

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Path layout matches beacon-spatial: ~/Music/beacon/
RECORD_DIR = Path(os.path.expanduser("~/Music/beacon"))
RECORD_DIR.mkdir(parents=True, exist_ok=True)

# Sensible default: the PipeWire sink with the monitor we want to record.
# The user can override via RECORD_TARGET env var. We resolve the
# default sink dynamically via pw-cli so this works on any system.
DEFAULT_RECORD_TARGET = os.environ.get(
    "RECORD_TARGET", "auto"
)  # "auto" -> resolve at start()


class Recorder:
    """Thread-safe manager for a single active recording session."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._path: Optional[Path] = None
        self._started_at: Optional[float] = None
        self._target: Optional[str] = None
        self._last_error: Optional[str] = None
        self._total_recordings: int = 0

    # ─── Public API ───────────────────────────────────────────────────────

    def status(self) -> dict:
        """Snapshot of the recorder state — safe to call from any thread."""
        with self._lock:
            running = self._is_alive()
            return {
                "running": running,
                "recording": running,
                "path": str(self._path) if self._path else None,
                "target": self._target,
                "elapsed_s": (time.time() - self._started_at) if (running and self._started_at) else 0.0,
                "last_error": self._last_error,
                "total_recordings": self._total_recordings,
                "record_dir": str(RECORD_DIR),
            }

    def start(self, name: Optional[str] = None) -> dict:
        """Start recording. Returns a status dict.

        `name` is a free-form label (becomes part of the filename). If
        omitted, we use "session" with a timestamp.
        """
        with self._lock:
            if self._is_alive():
                return {"ok": False, "error": "Already recording",
                        "path": str(self._path) if self._path else None}

            target = self._resolve_target()
            if not target:
                self._last_error = "No PipeWire sink with a monitor found"
                return {"ok": False, "error": self._last_error}

            label = self._safe_name(name or "session")
            ts = time.strftime("%Y%m%d-%H%M%S")
            path = RECORD_DIR / f"{label}_{ts}.wav"

            cmd = [
                "pw-record",
                "--target", target,
                "--rate", "48000",
                "--channels", "2",
                "--format", "s16",
                str(path),
            ]
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    # Don't inherit stdin — pw-record is interactive on stdin
                    stdin=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                self._last_error = "pw-record not found in PATH"
                log.error(self._last_error)
                return {"ok": False, "error": self._last_error}
            except Exception as exc:
                self._last_error = f"Failed to start pw-record: {exc}"
                log.exception(self._last_error)
                return {"ok": False, "error": self._last_error}

            # Give pw-record a beat to fail loudly (bad target, etc.)
            time.sleep(0.15)
            if self._proc.poll() is not None:
                _, err = self._proc.communicate()
                err_text = (err or b"").decode(errors="replace").strip()
                self._last_error = f"pw-record exited early: {err_text or 'no stderr'}"
                log.error(self._last_error)
                self._proc = None
                return {"ok": False, "error": self._last_error}

            self._path = path
            self._target = target
            self._started_at = time.time()
            self._last_error = None
            log.info("Recording started: %s (target=%s)", path, target)
            return {
                "ok": True,
                "path": str(path),
                "target": target,
                "started_at": self._started_at,
            }

    def stop(self) -> dict:
        """Stop recording. Returns the saved path + duration."""
        with self._lock:
            if not self._is_alive():
                # Even if the proc died on its own, try to recover the path
                return {
                    "ok": True,
                    "already_stopped": True,
                    "path": str(self._path) if self._path else None,
                    "elapsed_s": (time.time() - self._started_at) if self._started_at else 0.0,
                }
            proc = self._proc
            assert proc is not None  # _is_alive() guarantees this
            path = self._path
            started = self._started_at
            # pw-record exits cleanly on SIGINT
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    log.warning("pw-record didn't exit on SIGTERM, killing")
                    proc.kill()
                    proc.wait(timeout=2.0)
            except Exception as exc:
                log.warning("Error stopping pw-record: %s", exc)

            elapsed = (time.time() - started) if started else 0.0
            size_bytes = path.stat().st_size if path and path.exists() else 0
            self._proc = None
            self._target = None
            self._started_at = None
            self._total_recordings += 1
            log.info("Recording stopped: %s (%.1fs, %d bytes)",
                     path, elapsed, size_bytes)
            return {
                "ok": True,
                "path": str(path) if path else None,
                "elapsed_s": elapsed,
                "size_bytes": size_bytes,
            }

    def toggle(self, name: Optional[str] = None) -> dict:
        """Convenience: start if idle, stop if recording."""
        with self._lock:
            if self._is_alive():
                return self.stop()
        return self.start(name)

    # ─── Internal helpers ─────────────────────────────────────────────────

    def _is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @staticmethod
    def _safe_name(label: str) -> str:
        s = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_")
        return s or "session"

    @staticmethod
    def _resolve_target() -> Optional[str]:
        """Pick a PipeWire sink monitor target.

        Strategy:
        1. If RECORD_TARGET env var is set explicitly, use it as-is.
        2. Otherwise, find the user's default sink via wpctl (preferred —
           matches what the user actually hears).
        3. Fallback: any sink matching "Jabra" / "R24" / "Built-in" by
           name (these are the most common output devices we know of).
        4. Last resort: scan pw-dump looking for any Audio/Sink node.
        """
        env_target = os.environ.get("RECORD_TARGET", "").strip()
        if env_target and env_target != "auto":
            return env_target

        # ── Try wpctl status (the wireplumber CLI, present everywhere) ──
        try:
            out = subprocess.check_output(
                ["wpctl", "status"], text=True, timeout=3.0
            )
            return Recorder._wpctl_pick_sink(out)
        except (subprocess.SubprocessError, FileNotFoundError) as exc:
            log.debug("wpctl failed: %s — falling back to pw-dump", exc)

        # ── Fallback: pw-dump, looking at info.props.media.class ──
        try:
            out = subprocess.check_output(
                ["pw-dump"], text=True, timeout=3.0
            )
            import json
            data = json.loads(out)
            return Recorder._pwdump_pick_sink(data)
        except (subprocess.SubprocessError, FileNotFoundError, ValueError) as exc:
            log.warning("pw-dump fallback failed: %s", exc)

        return None

    @staticmethod
    def _wpctl_pick_sink(out: str) -> Optional[str]:
        """Parse wpctl status output for the default sink.

        wpctl marks the default sink with `*` prefix in the Sinks
        section. We look for the first `*` line in the Sinks section,
        extract the numeric ID, and resolve the node name via
        `wpctl inspect <id>`.
        """
        in_sinks = False
        default_id = None
        # wpctl uses box-drawing chars (│ U+2502) as tree borders.
        # Those are NOT stripped by str.strip() so we nuke them too.
        BORDER_CHARS = "\u2502\u251c\u2500\u2514\u2502 "
        for line in out.splitlines():
            stripped = line.strip().strip(BORDER_CHARS).strip()
            if "Sinks:" in stripped:
                in_sinks = True
                continue
            if in_sinks:
                if "Sources:" in stripped:
                    break
                if stripped.startswith("*"):
                    # `*   80. R24 Analog Stereo`
                    try:
                        default_id = int(stripped.lstrip("*").strip().split(".")[0].strip())
                        break
                    except (ValueError, IndexError):
                        continue
        if default_id is None:
            return None
        # Resolve ID → node name
        try:
            ins = subprocess.check_output(
                ["wpctl", "inspect", str(default_id)], text=True, timeout=3.0
            )
        except subprocess.SubprocessError:
            return None
        for line in ins.splitlines():
            line_stripped = line.strip().lstrip("*").strip()
            if line_stripped.startswith("node.name"):
                # node.name = "alsa_output.usb-..."
                name = line_stripped.split("=", 1)[1].strip().strip('"')
                return f"{name}.monitor"
        return None

    @staticmethod
    def _pwdump_pick_sink(data: list) -> Optional[str]:
        """Fallback: scan pw-dump JSON for any Audio/Sink node."""
        import json
        candidates = []
        for n in data:
            if n.get("type") != "PipeWire:Interface/Node" and n.get("type") != "PipeWire:Interface:Node":
                continue
            info_props = n.get("info", {}).get("props", {})
            mc = info_props.get("media.class", "")
            if mc == "Audio/Sink":
                name = info_props.get("node.name", "")
                desc = info_props.get("node.description", "")
                candidates.append((desc, name))
        if not candidates:
            return None
        # Prefer known names in priority order
        priority_substrings = ["r24", "jabra", "usb", "hdmi", "built-in", "analog"]
        for sub in priority_substrings:
            for desc, name in candidates:
                if sub in desc.lower() or sub in name.lower():
                    return f"{name}.monitor"
        return f"{candidates[0][1]}.monitor"