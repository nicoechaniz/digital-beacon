"""HTTP server for the interactive Voice → Shaper UI.

Single-file stdlib server (no Flask/uvicorn). Binds to 127.0.0.1:8770 so it
does not collide with:
  - :8765  existing voice-analysis static file server (`python3 -m http.server`)
  - :8080  digital_beacon/api.py dashboard
  - :9001/:9002 OSC bridges

Endpoints
---------
  GET  /                  → inline HTML/JS UI (placeholder; filled by frontend task)
  GET  /samples           → JSON list of discovered voice samples (id, label, stats)
  GET  /orig/<id>         → original WAV audio for the given sample
  POST /render            → STUB: returns 501 with JSON {error, detail}
                            until the render handler is wired (depends on the
                            synth_pure refactor + VoiceCache)
  GET  /viz/<path>        → static file server for existing PNGs under
                            ~/Music/voice-analysis/viz/
  GET  /health            → JSON health check

Sample discovery mirrors `tools/build_voice_compare_v3.py`: glob `*.wav`
under `~/Music/voice-analysis/` and skip derivative suffixes (_synth,
_orig, _mono, _clean, _filt, _0-9s, _side_by_side).

Usage
-----
    python tools/voice_shaper_server.py [--port 8770] [--host 127.0.0.1] [--voice-dir PATH]

    # curl checks
    curl -s http://127.0.0.1:8770/samples | python3 -m json.tool
    curl -s -X POST http://127.0.0.1:8770/render \
        -H 'Content-Type: application/json' \
        -d '{"sample_id":"nico_voz_sample_02"}' -i

Ctrl-C triggers a graceful shutdown (server.shutdown() is called from a
worker thread after a SIGINT handler parks the serve loop).
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import re
import signal
import sys
import threading
import time
import wave
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Discovery + paths
# ---------------------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8770
DEFAULT_VOICE_DIR = Path.home() / "Music" / "voice-analysis"

# Mirror tools/build_voice_compare_v3.py:SKIP_SUFFIXES so the server picks
# up exactly the same set of "original" recordings as the dashboard builder.
SKIP_SUFFIXES = ("_synth", "_orig", "_mono", "_clean", "_filt", "_0-9s", "_side_by_side")

# Strict id charset for /orig/<id> and /samples[<id>]. Path traversal and
# extension smuggling both fail to match.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

log = logging.getLogger("voice_shaper_server")


def discover_samples(voice_dir: Path) -> list[dict[str, Any]]:
    """Return [{id, label, path, size_bytes, duration_s}] for usable samples.

    `id` and `label` are derived from the WAV stem (file name without
    extension). Size and duration are best-effort — duration is None when
    the file cannot be probed.
    """
    if not voice_dir.is_dir():
        log.warning("voice dir does not exist: %s", voice_dir)
        return []

    out: list[dict[str, Any]] = []
    for wav_path in sorted(voice_dir.glob("*.wav")):
        stem = wav_path.stem
        if any(stem.endswith(suf) for suf in SKIP_SUFFIXES):
            log.debug("skip derivative: %s", wav_path.name)
            continue
        try:
            size = wav_path.stat().st_size
        except OSError as exc:
            log.warning("stat failed for %s: %s", wav_path, exc)
            continue

        duration_s = _probe_duration_seconds(wav_path)

        out.append(
            {
                "id": stem,
                "label": stem.replace("_", " "),
                "filename": wav_path.name,
                "path": str(wav_path),
                "size_bytes": size,
                "duration_s": duration_s,
            }
        )

    log.info("discovered %d usable voice samples in %s", len(out), voice_dir)
    return out


def _probe_duration_seconds(wav_path: Path) -> Optional[float]:
    """Best-effort duration probe without pulling in soundfile/librosa.

    Reads the WAV header. Returns None on any parse error so a corrupt
    file does not break /samples.
    """
    try:
        with wave.open(str(wav_path), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate()
            if rate <= 0:
                return None
            return round(frames / float(rate), 3)
    except (wave.Error, OSError, EOFError) as exc:
        log.debug("duration probe failed for %s: %s", wav_path, exc)
        return None


# ---------------------------------------------------------------------------
# Inline UI placeholder (replaced by the frontend task)
# ---------------------------------------------------------------------------

PLACEHOLDER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Voice → Shaper</title>
<style>
  :root { color-scheme: dark; }
  body {
    margin: 0; min-height: 100vh;
    background: #0e1116; color: #c9d1d9;
    font: 14px/1.45 -apple-system, "Segoe UI", system-ui, sans-serif;
    display: flex; align-items: center; justify-content: center;
  }
  .card {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 24px 28px; max-width: 520px;
  }
  h1 { margin: 0 0 8px; font-size: 18px; color: #58a6ff; }
  p  { margin: 6px 0; color: #8b949e; }
  code { background: #0d1117; padding: 1px 6px; border-radius: 4px; color: #c9d1d9; }
  ul { margin: 12px 0 0; padding-left: 18px; }
  li { margin: 4px 0; }
  .pill {
    display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px;
    background: #1f6feb33; color: #58a6ff; margin-left: 8px;
  }
</style>
</head>
<body>
  <div class="card">
    <h1>Voice → Shaper <span class="pill">UI placeholder</span></h1>
    <p>The server skeleton is up. The interactive harmonic mixer will be
       wired into this page by the frontend task.</p>
    <p>Try the data endpoints while the UI is being built:</p>
    <ul>
      <li><code>GET /samples</code> — list of available voice recordings</li>
      <li><code>GET /orig/&lt;id&gt;</code> — original WAV audio</li>
      <li><code>POST /render</code> — synthesised WAV (currently 501 stub)</li>
      <li><code>GET /viz/&lt;path&gt;</code> — existing dashboard PNGs</li>
    </ul>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class VoiceShaperHandler(BaseHTTPRequestHandler):
    """Routing layer on top of BaseHTTPRequestHandler.

    Routing is dispatched in `do_*` based on (method, path). Path patterns:
        GET  /                 → inline HTML
        GET  /samples          → JSON list
        GET  /orig/<id>        → WAV bytes
        POST /render           → JSON body, returns 501 stub for now
        GET  /viz/<path>       → file under <voice_dir>/viz/
        GET  /health           → JSON health check (handy for scripting)
    """

    server_version = "VoiceShaper/0.1"

    # Per-request state set by the server before serve_forever starts.
    voice_dir: Path  # injected by the factory below
    viz_dir: Path    # injected by the factory below

    # ------------------------------------------------------------------ helpers

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        # Route stdlib access logs through our logger so the format is
        # consistent with the rest of the project.
        log.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str,
                    extra_headers: Optional[dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> tuple[Optional[dict], Optional[str]]:
        """Return (parsed_dict, error_message). error_message is non-None on failure."""
        length = self.headers.get("Content-Length")
        if length is None:
            return None, "missing Content-Length header"
        try:
            n = int(length)
        except ValueError:
            return None, f"invalid Content-Length: {length!r}"
        if n < 0 or n > 4 * 1024 * 1024:  # 4 MiB hard cap on render bodies
            return None, f"Content-Length out of range: {n}"
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, f"invalid JSON: {exc}"
        if not isinstance(data, dict):
            return None, "JSON body must be an object"
        return data, None

    # --------------------------------------------------------------- GET routes

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        path = self.path
        started = time.monotonic()

        try:
            if path == "/" or path == "/index.html":
                self._send_bytes(HTTPStatus.OK, PLACEHOLDER_HTML.encode("utf-8"),
                                 "text/html; charset=utf-8",
                                 extra_headers={"Cache-Control": "no-store"})
            elif path == "/samples":
                samples = discover_samples(self.voice_dir)
                self._send_json(HTTPStatus.OK, {
                    "count": len(samples),
                    "voice_dir": str(self.voice_dir),
                    "samples": samples,
                })
            elif path.startswith("/orig/"):
                sample_id = path[len("/orig/"):].split("?", 1)[0]
                self._handle_get_orig(sample_id)
            elif path.startswith("/viz/"):
                rel = path[len("/viz/"):].split("?", 1)[0]
                self._handle_get_viz(rel)
            elif path == "/health":
                self._send_json(HTTPStatus.OK, {
                    "ok": True,
                    "voice_dir": str(self.voice_dir),
                    "viz_dir": str(self.viz_dir),
                })
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {
                    "error": "not_found",
                    "method": "GET",
                    "path": path,
                })
        finally:
            log.info("GET %s -> %.0fms", path, (time.monotonic() - started) * 1000.0)

    # --------------------------------------------------------------- POST routes

    def do_POST(self) -> None:  # noqa: N802
        path = self.path
        started = time.monotonic()
        try:
            if path == "/render":
                self._handle_post_render()
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {
                    "error": "not_found",
                    "method": "POST",
                    "path": path,
                })
        finally:
            log.info("POST %s -> %.0fms", path, (time.monotonic() - started) * 1000.0)

    # ---------------------------------------------------------------- handlers

    def _handle_get_orig(self, sample_id: str) -> None:
        if not SAFE_ID_RE.match(sample_id):
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_sample_id",
                "sample_id": sample_id,
            })
            return
        wav_path = self.voice_dir / f"{sample_id}.wav"
        if not wav_path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {
                "error": "sample_not_found",
                "sample_id": sample_id,
                "voice_dir": str(self.voice_dir),
            })
            return
        try:
            data = wav_path.read_bytes()
        except OSError as exc:
            log.error("read failed for %s: %s", wav_path, exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "read_failed",
                "sample_id": sample_id,
            })
            return
        self._send_bytes(HTTPStatus.OK, data, "audio/wav",
                         extra_headers={"Cache-Control": "no-store"})

    def _handle_get_viz(self, rel: str) -> None:
        # Strict allow-list: alnum + dot + dash + underscore + slash for
        # subpaths. No `..`, no leading `/`, no backslashes.
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_viz_path",
                "path": rel,
            })
            return
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", rel):
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_viz_path",
                "path": rel,
            })
            return
        target = (self.viz_dir / rel).resolve()
        viz_root = self.viz_dir.resolve()
        try:
            target.relative_to(viz_root)  # raises ValueError if escape
        except ValueError:
            self._send_json(HTTPStatus.FORBIDDEN, {
                "error": "viz_path_escape",
                "path": rel,
            })
            return
        if not target.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {
                "error": "viz_not_found",
                "path": rel,
            })
            return
        ctype, _ = mimetypes.guess_type(target.name)
        try:
            data = target.read_bytes()
        except OSError as exc:
            log.error("read failed for %s: %s", target, exc)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "read_failed",
                "path": rel,
            })
            return
        self._send_bytes(HTTPStatus.OK, data, ctype or "application/octet-stream",
                         extra_headers={"Cache-Control": "public, max-age=60"})

    def _handle_post_render(self) -> None:
        """Stub. Returns 501 until the render handler is wired.

        This deliberately parses the body (so we can validate the wiring
        path) but does not run any synthesis. The render handler is
        pending the synth_pure refactor (prepare_analysis +
        synthesize_prepared) and the VoiceCache layer.
        """
        body, err = self._read_json_body()
        if err is not None or body is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "bad_request",
                "detail": err or "empty body",
            })
            return

        sample_id = body.get("sample_id")
        if not isinstance(sample_id, str) or not SAFE_ID_RE.match(sample_id):
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "error": "missing_or_invalid_sample_id",
                "hint": "supply {\"sample_id\": \"<alnum/dash/underscore>\"}",
            })
            return

        wav_path = self.voice_dir / f"{sample_id}.wav"
        if not wav_path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {
                "error": "sample_not_found",
                "sample_id": sample_id,
                "voice_dir": str(self.voice_dir),
            })
            return

        # Stub response — wired once the render handler lands.
        self._send_json(HTTPStatus.NOT_IMPLEMENTED, {
            "error": "not_implemented",
            "endpoint": "POST /render",
            "detail": (
                "render handler is pending the synth_pure refactor and "
                "VoiceCache layer. The skeleton accepts JSON, validates "
                "sample_id, and confirms the file exists."
            ),
            "received": {k: body.get(k) for k in (
                "sample_id", "gain_curve", "spectral_tilt_db", "thresh_db",
                "noise_floor_db", "max_voices", "per_harmonic_gains", "wave_shapes",
            ) if k in body},
            "sample_path": str(wav_path),
        })


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def make_handler(voice_dir: Path, viz_dir: Path):
    """Closure factory that injects paths into the handler class.

    BaseHTTPRequestHandler instances are created by the HTTPServer per
    request; class attributes are the simplest way to share config without
    globals.
    """

    class _Bound(VoiceShaperHandler):
        pass

    _Bound.voice_dir = voice_dir
    _Bound.viz_dir = viz_dir
    return _Bound


def install_signal_handlers(server: ThreadingHTTPServer) -> None:
    """Wire SIGINT/SIGTERM to a graceful shutdown.

    HTTPServer.shutdown() must be called from a thread other than the one
    blocked in serve_forever(), so the signal handler schedules shutdown on
    a dedicated thread.
    """
    shutdown_started = threading.Event()

    def _trigger_shutdown(signum, _frame):
        if shutdown_started.is_set():
            log.warning("second signal %d received; ignoring", signum)
            return
        shutdown_started.set()
        log.info("received signal %d, shutting down...", signum)
        # server.shutdown() returns immediately and unblocks serve_forever()
        threading.Thread(target=server.shutdown, name="vs-shutdown", daemon=True).start()

    signal.signal(signal.SIGINT, _trigger_shutdown)
    signal.signal(signal.SIGTERM, _trigger_shutdown)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Voice → Shaper HTTP server (interactive mixer skeleton).",
    )
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"bind host (default: {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"bind port (default: {DEFAULT_PORT})")
    p.add_argument("--voice-dir", type=Path, default=DEFAULT_VOICE_DIR,
                   help=f"voice samples directory (default: {DEFAULT_VOICE_DIR})")
    p.add_argument("--log-level", default="INFO",
                   choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                   help="logging level (default: INFO)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    voice_dir: Path = args.voice_dir.expanduser().resolve()
    viz_dir: Path = (voice_dir / "viz").resolve()

    if not voice_dir.is_dir():
        log.error("voice dir does not exist: %s", voice_dir)
        return 2

    handler_cls = make_handler(voice_dir, viz_dir)
    # ThreadingHTTPServer so /render (when wired) does not block /samples etc.
    server = ThreadingHTTPServer((args.host, args.port), handler_cls)
    server.daemon_threads = True

    install_signal_handlers(server)

    log.info("voice_shaper_server listening on http://%s:%d", args.host, args.port)
    log.info("  voice_dir: %s", voice_dir)
    log.info("  viz_dir:   %s", viz_dir)
    log.info("Ctrl-C to stop.")

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        log.info("server closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
