"""Plot temporal stability of proposed f1 and spectral metrics for field recordings.

Reads temporal_stability.json and emits one PNG per file plus a summary grid.

Usage:
    .venv/bin/python tools/plot_temporal_stability.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def plot_file(ax, data):
    windows = data["windows"]
    times = [(w["start_s"] + w["end_s"]) / 2 for w in windows]
    f1s = [w["proposed_f1_hz"] for w in windows if w["proposed_f1_hz"] is not None]
    times_f1 = [t for t, w in zip(times, windows) if w["proposed_f1_hz"] is not None]

    ax.plot(times_f1, f1s, "o-", color="#58a6ff", lw=1.2, markersize=3, label="proposed f1")
    stats = data.get("stats", {})
    if stats:
        mean = stats.get("mean_hz")
        if mean is not None:
            ax.axhline(mean, color="white", linestyle="--", lw=1, alpha=0.6, label=f"mean {mean:.1f} Hz")

    ax.set_xlabel("Time (s)")
    ax.set_ylabel("F1 (Hz)")
    ax.set_title(f"{data['label']} — {data['duration_s']:.1f}s")
    ax.set_ylim(20, 120)
    ax.legend(loc="upper right", fontsize=7)
    ax.grid(True, alpha=0.2)


def main():
    ap = argparse.ArgumentParser(description="Plot temporal stability analysis")
    ap.add_argument("--in-json", default=str(Path.home() / "Music" / "field-recordings" / "analysis" / "temporal_stability.json"))
    ap.add_argument("--out-dir", default=str(Path.home() / "Music" / "field-recordings" / "analysis" / "stability"))
    args = ap.parse_args()

    data = json.loads(Path(args.in_json).read_text())
    files = data["files"]
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Individual plots
    for f in files:
        fig, ax = plt.subplots(figsize=(10, 4))
        plot_file(ax, f)
        fig.tight_layout()
        out = out_dir / f"{f['label']}_stability.png"
        fig.savefig(out, dpi=120, facecolor="#0e1116")
        plt.close(fig)

    # Summary grid
    n = len(files)
    cols = 3
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(15, 4 * rows), facecolor="#0e1116")
    axes = np.atleast_1d(axes).reshape(-1)
    i = 0
    for i, f in enumerate(files):
        plot_file(axes[i], f)
    for j in range(i + 1, len(axes)):
        axes[j].axis("off")
    fig.tight_layout()
    grid_out = out_dir / "summary_grid.png"
    fig.savefig(grid_out, dpi=120, facecolor="#0e1116")
    plt.close(fig)

    print(f"Saved plots to {out_dir}")
    print(f"Grid: {grid_out}")


if __name__ == "__main__":
    main()
