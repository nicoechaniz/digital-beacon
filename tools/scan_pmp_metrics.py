#!/home/nicolas/Projects/digital-beacon/.venv/bin/python3
"""Scan PMP therapy session WAVs and compute comprehensive metrics.

Streaming / chunked only. Never loads full WAV into memory.
- Loudness via ffmpeg -af ebur128 (subprocess, parse stderr)
- VAD via webrtcvad on 10s soundfile blocks, 16kHz resample per chunk
- Clipping, DC, SNR, bandwidth, reverb via streaming accumulators
- Voiced audio capped at first 300s for bandwith/reverb only
"""
import argparse
import csv
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf
import webrtcvad
import scipy.signal as sig


def run_ffmpeg_loudness(filepath: Path):
    """Run ffmpeg ebur128 and return (integrated_lufs, lra, true_peak_dbTP) or Nones."""
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(filepath),
        "-af", "ebur128=peak=true",
        "-f", "null", "-"
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600 * 4
        )
        text = proc.stderr + "\n" + (proc.stdout or "")
        # Final summary block
        mi = re.search(
            r"Integrated loudness:\s*\n\s*I:\s*([-\d.]+)\s*LUFS",
            text, re.I | re.MULTILINE
        )
        ml = re.search(
            r"Loudness range:\s*\n\s*LRA:\s*([-\d.]+)\s*LU",
            text, re.I | re.MULTILINE
        )
        mtp = re.search(
            r"True peak:\s*\n\s*Peak:\s*([-\d.]+)\s*dBFS",
            text, re.I | re.MULTILINE
        )
        il = float(mi.group(1)) if mi else None
        lr = float(ml.group(1)) if ml else None
        tp = float(mtp.group(1)) if mtp else None
        return il, lr, tp
    except Exception as e:
        print(f"  ffmpeg ebur128 error: {e}", file=sys.stderr)
        return None, None, None


def analyze_vad_chunk(
    chunk: np.ndarray,
    sr: int,
    vad: webrtcvad.Vad,
    voiced_samples_list,
    voiced_samples_len: int,
    max_voiced_samples: int,
    voiced_energies: list,
    non_voiced_energies: list,
    max_energies: int,
    voiced_clipped_count: int,
    voiced_sample_count: int,
):
    """Process one 10s (or tail) chunk: VAD decisions, energy lists, voiced audio cap, clip counts.

    Returns updated (voiced_len, vc, tc, voiced_clipped, voiced_samp_count)
    """
    frame_ms = 30
    frame_len = int(sr * frame_ms / 1000.0)
    if frame_len <= 0 or len(chunk) == 0:
        return voiced_samples_len, 0, 0, voiced_clipped_count, voiced_sample_count

    target_sr = 16000
    chunk_f64 = np.asarray(chunk, dtype=np.float64)
    if sr != target_sr:
        num = int(round(len(chunk_f64) * target_sr / float(sr)))
        if num < 1:
            num = 1
        res_chunk = sig.resample(chunk_f64, num)
    else:
        res_chunk = chunk_f64

    int16_audio = np.clip(res_chunk * 32767.0, -32768.0, 32767.0).astype(np.int16)
    vad_frame_len = int(target_sr * frame_ms / 1000)
    num_v_frames = len(int16_audio) // vad_frame_len if vad_frame_len > 0 else 0

    n_align = min(num_v_frames, len(chunk) // frame_len)
    vc = 0
    tc = n_align

    for i in range(n_align):
        vf_start = i * vad_frame_len
        vf_end = vf_start + vad_frame_len
        frame_bytes = int16_audio[vf_start:vf_end].tobytes()
        is_voiced = vad.is_speech(frame_bytes, target_sr)

        ostart = i * frame_len
        oend = min(ostart + frame_len, len(chunk))
        seg = chunk[ostart:oend]
        if len(seg) == 0:
            continue

        e = float(np.mean(seg.astype(np.float64) ** 2))
        seg_abs_max = float(np.max(np.abs(seg)))
        seg_clipped = int(np.sum(np.abs(seg) >= 0.999))

        if is_voiced:
            vc += 1
            if len(voiced_energies) < max_energies:
                voiced_energies.append(e)
            if voiced_samples_len < max_voiced_samples:
                to_take = min(len(seg), max_voiced_samples - voiced_samples_len)
                voiced_samples_list.append(seg[:to_take].astype(np.float32).copy())
                voiced_samples_len += to_take
            voiced_clipped_count += seg_clipped
            voiced_sample_count += len(seg)
        else:
            if len(non_voiced_energies) < max_energies:
                non_voiced_energies.append(e)

    return voiced_samples_len, vc, tc, voiced_clipped_count, voiced_sample_count


def compute_metrics(filepath: Path):
    """Streaming compute of all metrics for one file. Returns dict row."""
    info = sf.info(str(filepath))
    sr = info.samplerate
    channels = info.channels
    duration = info.duration
    subtype = info.subtype

    bit_depth_map = {
        "PCM_16": 16, "PCM_24": 24, "PCM_32": 32, "PCM_U8": 8,
        "FLOAT": 32, "DOUBLE": 64, "PCM_S8": 8,
    }
    bit_depth = bit_depth_map.get(subtype, None)

    # Loudness via ffmpeg (streams internally, zero python audio mem)
    integrated_lufs, lra, true_peak = run_ffmpeg_loudness(filepath)

    # Streaming accumulators
    vad = webrtcvad.Vad(2)
    MAX_VOICED = int(300 * sr)
    MAX_ENERGIES = 10000

    voiced_samples_list = []
    voiced_len = 0
    voiced_energies = []
    non_voiced_energies = []
    voiced_fc = 0
    total_fc = 0
    voiced_clipped = 0
    voiced_samp_cnt = 0

    dc_sum = 0.0
    dc_cnt = 0
    clipped_total = 0
    total_samples = 0

    blocksize = int(sr * 10) if sr > 0 else 441000
    if blocksize < 1024:
        blocksize = 1024

    try:
        for block in sf.blocks(
            str(filepath),
            blocksize=blocksize,
            dtype="float32",
            always_2d=False,
        ):
            if block.ndim > 1:
                block = np.mean(block, axis=1).astype(np.float32, copy=False)

            n = len(block)
            if n == 0:
                continue

            # DC (running)
            dc_sum += float(np.sum(block, dtype=np.float64))
            dc_cnt += n

            # Clipping total (samples)
            clipped_total += int(np.sum(np.abs(block) >= 0.999))
            total_samples += n

            # VAD + energies + voiced cap + voiced clips
            voiced_len, vc, tc, voiced_clipped, voiced_samp_cnt = analyze_vad_chunk(
                block, sr, vad,
                voiced_samples_list, voiced_len, MAX_VOICED,
                voiced_energies, non_voiced_energies, MAX_ENERGIES,
                voiced_clipped, voiced_samp_cnt
            )
            voiced_fc += vc
            total_fc += tc

        # finalize
        dc_offset = (dc_sum / dc_cnt) if dc_cnt > 0 else 0.0
        clip_pct_total = (100.0 * clipped_total / total_samples) if total_samples > 0 else 0.0
        clip_pct_voiced = (100.0 * voiced_clipped / voiced_samp_cnt) if voiced_samp_cnt > 0 else None
        speech_ratio = (100.0 * voiced_fc / total_fc) if total_fc > 0 else 0.0

        # SNR from collected energies (subsampled)
        snr = float('nan')
        if voiced_energies and non_voiced_energies:
            noise_floor = np.percentile(non_voiced_energies, 5)
            speech_med = np.median(voiced_energies)
            if noise_floor > 0:
                snr = 10.0 * np.log10(speech_med / max(noise_floor, 1e-15))

        # Concat capped voiced (for bandwidth + reverb). This is the ONLY full-ish array (~300s max)
        voiced_audio = (
            np.concatenate(voiced_samples_list).astype(np.float32)
            if voiced_samples_list else np.array([], dtype=np.float32)
        )

        # Bandwidth: FFT on first N of voiced_audio
        bandwidth = None
        if len(voiced_audio) >= 256:
            n_fft = min(4096, 2 ** int(np.floor(np.log2(len(voiced_audio)))))
            if n_fft >= 256:
                spec = np.fft.rfft(voiced_audio[:n_fft])
                power = (np.abs(spec) ** 2).astype(np.float64)
                freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
                peak = np.max(power)
                thresh = peak * (10.0 ** (-40.0 / 10.0))
                above = power > thresh
                if np.any(above):
                    bandwidth = float(freqs[np.where(above)[0][-1]])

        # Reverb proxy C50
        c50 = None
        if len(voiced_audio) > 0:
            hop = int(sr * 0.01)
            if hop > 0:
                n = len(voiced_audio)
                num_env = (n + hop - 1) // hop
                pad = np.zeros(num_env * hop, dtype=np.float32)
                pad[:n] = voiced_audio ** 2
                env = np.mean(pad.reshape(num_env, hop), axis=1)
                if len(env) > 10:
                    early_n = max(1, int(0.05 / 0.01))
                    ee = float(np.sum(env[:early_n]))
                    le = float(np.sum(env[early_n:]))
                    if le > 0:
                        c50 = 10.0 * np.log10(ee / le)

        note = ""

        # Prefer info.frames for total_frames (metadata), but fall back to streamed
        total_f = info.frames if info.frames else total_samples

        return {
            "file": str(filepath),
            "duration": duration,
            "sample_rate": sr,
            "bit_depth": bit_depth,
            "channels": channels,
            "integrated_lufs": integrated_lufs,
            "lra": lra,
            "true_peak_dbTP": true_peak,
            "dc_offset": dc_offset,
            "clip_pct_total": clip_pct_total,
            "clip_pct_voiced": clip_pct_voiced,
            "speech_ratio": speech_ratio,
            "snr_db": snr,
            "bandwidth_hz": bandwidth,
            "reverb_proxy_c50": c50,
            "voiced_frames": voiced_fc,
            "total_frames": total_f,
            "note": note,
        }
    except Exception as e:
        print(f"  streaming error: {e}", file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description="Scan PMP WAV files and compute metrics (streaming)")
    parser.add_argument("--output-dir", default="metrics/pre", help="Output dir for CSVs")
    parser.add_argument("--files", nargs="*", help="Specific files (default: all in PMP/wav/)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)

    if args.files:
        files = [Path(f) for f in args.files]
    else:
        base = Path.home() / "Music" / "voice-analysis" / "sources" / "PMP" / "wav"
        files = list(base.rglob("*.wav"))

    files = sorted(files)

    fieldnames = [
        "file", "duration", "sample_rate", "bit_depth", "channels",
        "integrated_lufs", "lra", "true_peak_dbTP", "dc_offset",
        "clip_pct_total", "clip_pct_voiced", "speech_ratio", "snr_db",
        "bandwidth_hz", "reverb_proxy_c50", "voiced_frames", "total_frames",
        "note",
    ]

    csv_path = output_dir / "per_file.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for filepath in files:
            print(f"Processing {filepath} ...", file=sys.stderr)
            try:
                metrics = compute_metrics(filepath)
                writer.writerow(metrics)
                f.flush()
                print(f"  OK  integrated_lufs={metrics['integrated_lufs']}  speech_ratio={metrics['speech_ratio']:.1f}%", file=sys.stderr)
            except Exception as e:
                print(f"  ERROR: {e}", file=sys.stderr)
                row = {
                    "file": str(filepath),
                    "duration": None, "sample_rate": None, "bit_depth": None,
                    "channels": None, "integrated_lufs": None, "lra": None,
                    "true_peak_dbTP": None, "dc_offset": None,
                    "clip_pct_total": None, "clip_pct_voiced": None,
                    "speech_ratio": None, "snr_db": None, "bandwidth_hz": None,
                    "reverb_proxy_c50": None, "voiced_frames": None, "total_frames": None,
                    "note": "error",
                }
                writer.writerow(row)
                f.flush()

    print(f"Done. per_file: {csv_path}", file=sys.stderr)

    # Read back
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    total_files = len(rows)
    num_errors = sum(
        1 for r in rows
        if r.get("note") == "error" or r.get("duration") in (None, "", "None")
    )

    # dataset_summary.csv
    numeric_fields = [fn for fn in fieldnames if fn not in ("file", "note")]
    summary_rows = []
    for metric in numeric_fields:
        vals = []
        for r in rows:
            v = r.get(metric)
            if v not in (None, "", "None", "null"):
                try:
                    vals.append(float(v))
                except (ValueError, TypeError):
                    pass
        if vals:
            arr = np.array(vals, dtype=float)
            summary_rows.append({
                "metric": metric,
                "min": float(np.min(arr)),
                "p25": float(np.percentile(arr, 25)),
                "median": float(np.median(arr)),
                "p75": float(np.percentile(arr, 75)),
                "max": float(np.max(arr)),
                "mean": float(np.mean(arr)),
                "std": float(np.std(arr, ddof=0)),
                "count_non_null": int(len(arr)),
            })
        else:
            summary_rows.append({
                "metric": metric,
                "min": None, "p25": None, "median": None, "p75": None,
                "max": None, "mean": None, "std": None, "count_non_null": 0,
            })

    summary_path = output_dir / "dataset_summary.csv"
    sum_fields = ["metric", "min", "p25", "median", "p75", "max", "mean", "std", "count_non_null"]
    with open(summary_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=sum_fields)
        writer.writeheader()
        for srw in summary_rows:
            writer.writerow(srw)
    print(f"Summary: {summary_path}", file=sys.stderr)

    # reports/pmp_metrics_report.md
    report_path = reports_dir / "pmp_metrics_report.md"

    durations = []
    lufss = []
    lras = []
    speech_ratios = []
    snrs = []
    for r in rows:
        try:
            if r.get("duration") not in (None, "", "None"):
                durations.append(float(r["duration"]))
        except Exception:
            pass
        try:
            if r.get("integrated_lufs") not in (None, "", "None"):
                lufss.append(float(r["integrated_lufs"]))
        except Exception:
            pass
        try:
            if r.get("lra") not in (None, "", "None"):
                lras.append(float(r["lra"]))
        except Exception:
            pass
        try:
            if r.get("speech_ratio") not in (None, "", "None"):
                speech_ratios.append(float(r["speech_ratio"]))
        except Exception:
            pass
        try:
            if r.get("snr_db") not in (None, "", "None", "nan"):
                snrs.append(float(r["snr_db"]))
        except Exception:
            pass

    total_dur_h = sum(durations) / 3600.0 if durations else 0.0
    sr_set = sorted(set(int(float(r["sample_rate"])) for r in rows if r.get("sample_rate") not in (None, "", "None")))
    ch_set = sorted(set(int(float(r["channels"])) for r in rows if r.get("channels") not in (None, "", "None")))

    def fmt(x):
        if x is None:
            return "N/A"
        return f"{x:.3f}" if isinstance(x, float) else str(x)

    def stats_for(name, lst):
        if not lst:
            return "min=N/A max=N/A median=N/A"
        a = np.array(lst, dtype=float)
        return f"min={fmt(np.min(a))} max={fmt(np.max(a))} median={fmt(np.median(a))}"

    md_lines = []
    md_lines.append("# PMP Therapy WAV Metrics Report\n")
    md_lines.append(f"**Generated:** {datetime.now().isoformat()}\n")
    md_lines.append(f"**Total files scanned:** {total_files}\n")
    md_lines.append(f"**Files with errors:** {num_errors}\n")
    md_lines.append(f"**Date:** {datetime.now().date()}\n")
    md_lines.append("**Source directory:** ~/Music/voice-analysis/sources/PMP/wav/\n")
    md_lines.append("**Processing:** full-file LUFS (ffmpeg ebur128) + speech_ratio (webrtcvad); streaming only\n")
    md_lines.append(f"**Basic dataset info:**\n")
    md_lines.append(f"- Sample rates: {sr_set}\n")
    md_lines.append(f"- Channels: {ch_set}\n")
    md_lines.append(f"- Total duration: {total_dur_h:.2f} hours\n\n")

    md_lines.append("## Top-level stats (min / max / median)\n\n")
    md_lines.append(f"- duration (s): {stats_for('duration', durations)}\n")
    md_lines.append(f"- integrated_lufs (full-file LUFS): {stats_for('integrated_lufs', lufss)}\n")
    md_lines.append(f"- lra (full-file LRA): {stats_for('lra', lras)}\n")
    md_lines.append(f"- speech_ratio (%): {stats_for('speech_ratio', speech_ratios)}\n")
    md_lines.append(f"- snr_db: {stats_for('snr_db', snrs)}\n\n")

    md_lines.append("| metric | min | max | median |\n")
    md_lines.append("|--------|-----|-----|--------|\n")
    for name, lst in [
        ("duration", durations),
        ("integrated_lufs", lufss),
        ("lra", lras),
        ("speech_ratio", speech_ratios),
        ("snr_db", snrs),
    ]:
        if lst:
            a = np.array(lst, dtype=float)
            md_lines.append(f"| {name} | {fmt(np.min(a))} | {fmt(np.max(a))} | {fmt(np.median(a))} |\n")
        else:
            md_lines.append(f"| {name} | N/A | N/A | N/A |\n")
    md_lines.append("\n")

    md_lines.append("## Files with extreme values\n\n")

    def collect_extreme(key, cond, desc):
        hits = []
        for r in rows:
            try:
                val = float(r[key]) if r.get(key) not in (None, "", "None") else None
                if val is not None and cond(val):
                    hits.append((r["file"], val))
            except Exception:
                pass
        if hits:
            md_lines.append(f"### {desc}\n")
            for fpath, val in hits:
                md_lines.append(f"- `{fpath}` : {fmt(val)}\n")
            md_lines.append("\n")

    collect_extreme("integrated_lufs", lambda v: v < -50, "LUFS < -50 (very quiet)")
    collect_extreme("speech_ratio", lambda v: v < 20.0, "speech_ratio < 20%")
    collect_extreme("clip_pct_total", lambda v: v > 1.0, "clip_pct_total > 1%")
    collect_extreme("duration", lambda v: v < 60.0, "duration < 60s")

    md_lines.append("## Files with errors\n\n")
    error_files = [
        r["file"] for r in rows
        if r.get("note") == "error" or r.get("duration") in (None, "", "None")
    ]
    if error_files:
        for ef in error_files:
            md_lines.append(f"- `{ef}`\n")
    else:
        md_lines.append("None\n")

    md_lines.append("\n*Note: integrated_lufs / lra / true_peak_dbTP are full-file (ffmpeg ebur128). speech_ratio from VAD on full audio. Voiced audio analysis limited to first 300s of speech for bandwidth/reverb.*\n")

    with open(report_path, "w") as f:
        f.write("".join(md_lines))
    print(f"Report: {report_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
