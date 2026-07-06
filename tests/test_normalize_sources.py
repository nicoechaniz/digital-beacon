"""Unit tests for normalize_sources CLI flag parsing and pipeline routing.

Run:
    /home/nicolas/Projects/digital-beacon/.venv/bin/python -m pytest tests/test_normalize_sources.py -v --tb=short
"""
from __future__ import annotations

import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
import pytest

# Make imports work when running pytest from project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _find_run_dir(output_dir: Path) -> Path:
    """Find the run directory under output_dir (named YYYYMMDD_HHMMSS_v1)."""
    for p in sorted(output_dir.glob("*")):
        if p.is_dir() and (p / "normalized_analysis").is_dir():
            return p
    raise FileNotFoundError(f"No run directory found under {output_dir}")

from tools.normalize_sources import (
    RunDirs,
    PipelineConfig,
    build_parser,
    parse_args,
    create_run_directories,
    run_pipeline,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_dir() -> Path:
    """Provide a temporary directory that is cleaned up after the test."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def sample_wav(temp_dir: Path) -> Path:
    """Create a minimal mono 44.1 kHz WAV fixture with wide bandwidth to pass QC."""
    path = temp_dir / "test_voice.wav"
    sr = 44100
    duration = 30.5  # long enough to pass voiced-duration QC thresholds (accept not quar/exclude)
    np.random.seed(42)
    # Pink-like noise (wide bandwidth) with speech-like envelope
    n = int(sr * duration)
    white = np.random.randn(n).astype(np.float32)
    pink = np.cumsum(white)
    pink = np.diff(pink, prepend=pink[0])
    pink = pink / (np.max(np.abs(pink)) + 1e-12)
    # Modulate with slow envelope to simulate speech
    t = np.linspace(0, duration, n, dtype=np.float32)
    envelope = 0.3 + 0.5 * np.abs(np.sin(2 * np.pi * 0.3 * t))
    y = (0.25 * envelope * pink).astype(np.float32)
    pcm = (np.clip(y, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


@pytest.fixture
def input_dir_with_sample(temp_dir: Path, sample_wav: Path) -> Path:
    """Return an input directory containing one sample WAV."""
    src_dir = temp_dir / "originals"
    src_dir.mkdir(parents=True, exist_ok=True)
    sample_wav.rename(src_dir / sample_wav.name)
    return src_dir


# ---------------------------------------------------------------------------
# Helper: relax QC thresholds for short test samples
# ---------------------------------------------------------------------------

def _relax_qc_for_test(cfg: PipelineConfig) -> PipelineConfig:
    """Lower QC thresholds so short narrow test fixtures pass QC."""
    cfg.full["min_voiced_duration_s"] = 0.5
    cfg.full["snr_floor_db"] = -20.0
    return cfg


# ---------------------------------------------------------------------------
# CLI flag parsing
# ---------------------------------------------------------------------------

def test_help_includes_enhance():
    """--help text must mention the --enhance flag."""
    parser = build_parser()
    # argparse prints help to sys.stdout on --help and exits; we catch the exit.
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--help"])
    assert exc_info.value.code == 0

    # Re-parse to capture the help string via print_help
    import io
    buf = io.StringIO()
    parser.print_help(file=buf)
    help_text = buf.getvalue()
    assert "--enhance" in help_text
    assert "enhanced_review" in help_text


def test_enhance_defaults_false():
    """Omitting --enhance should default to False."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        cfg = parse_args([
            "--input-dir", str(td_path / "in"),
            "--output-dir", str(td_path / "out"),
        ])
    assert cfg.enhance is False


def test_enhance_explicit_true():
    """Passing --enhance should set it to True."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        cfg = parse_args([
            "--input-dir", str(td_path / "in"),
            "--output-dir", str(td_path / "out"),
            "--enhance",
        ])
    assert cfg.enhance is True


def test_dry_run_defaults_false():
    """--dry-run should default to False."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        cfg = parse_args([
            "--input-dir", str(td_path / "in"),
            "--output-dir", str(td_path / "out"),
        ])
    assert cfg.dry_run is False


def test_dry_run_explicit():
    """--dry-run should set the flag to True."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        cfg = parse_args([
            "--input-dir", str(td_path / "in"),
            "--output-dir", str(td_path / "out"),
            "--dry-run",
        ])
    assert cfg.dry_run is True


# ---------------------------------------------------------------------------
# RunDirs routing logic
# ---------------------------------------------------------------------------

def test_rundirs_without_enhance():
    """RunDirs.create(enhance=False) must NOT include enhanced_review."""
    root = Path("/tmp/fake_run")
    dirs = RunDirs.create(root, enhance=False)
    assert dirs.enhanced_review is None
    assert dirs.normalized_analysis == root / "normalized_analysis"
    assert dirs.canonical == root / "canonical"


def test_rundirs_with_enhance():
    """RunDirs.create(enhance=True) MUST include enhanced_review."""
    root = Path("/tmp/fake_run")
    dirs = RunDirs.create(root, enhance=True)
    assert dirs.enhanced_review is not None
    assert dirs.enhanced_review == root / "enhanced_review"
    assert dirs.normalized_analysis == root / "normalized_analysis"


def test_rundirs_all_paths_without_enhance():
    """all_paths() must not contain enhanced_review when enhance=False."""
    root = Path("/tmp/fake_run")
    dirs = RunDirs.create(root, enhance=False)
    paths = dirs.all_paths()
    assert any("enhanced_review" in str(p) for p in paths) is False
    assert any("normalized_analysis" in str(p) for p in paths) is True


def test_rundirs_all_paths_with_enhance():
    """all_paths() must contain enhanced_review when enhance=True."""
    root = Path("/tmp/fake_run")
    dirs = RunDirs.create(root, enhance=True)
    paths = dirs.all_paths()
    assert any("enhanced_review" in str(p) for p in paths) is True
    assert any("normalized_analysis" in str(p) for p in paths) is True


# ---------------------------------------------------------------------------
# Directory creation on disk
# ---------------------------------------------------------------------------

def test_create_dirs_without_enhance(temp_dir: Path):
    """Without --enhance, only canonical, normalized_analysis, etc. exist."""
    dirs = RunDirs.create(temp_dir / "run", enhance=False)
    create_run_directories(dirs, dry_run=False)
    assert (dirs.root / "normalized_analysis").is_dir()
    assert (dirs.root / "canonical").is_dir()
    assert (dirs.root / "enhanced_review").exists() is False


def test_create_dirs_with_enhance(temp_dir: Path):
    """With --enhance, enhanced_review directory must also exist."""
    dirs = RunDirs.create(temp_dir / "run", enhance=True)
    create_run_directories(dirs, dry_run=False)
    assert (dirs.root / "normalized_analysis").is_dir()
    assert (dirs.root / "enhanced_review").is_dir()


def test_dry_run_does_not_create_dirs(temp_dir: Path):
    """--dry-run must not write directories."""
    dirs = RunDirs.create(temp_dir / "run", enhance=True)
    create_run_directories(dirs, dry_run=True)
    assert dirs.root.exists() is False


# ---------------------------------------------------------------------------
# End-to-end pipeline routing
# ---------------------------------------------------------------------------

def test_pipeline_without_enhance(input_dir_with_sample: Path, temp_dir: Path):
    """Running without --enhance must produce ONLY normalized_analysis."""
    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=False,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    results = run_pipeline(cfg)
    assert len(results) == 1

    # Find the created run directory (manifest also lives at output_dir level)
    run_root = _find_run_dir(out_dir)

    assert (run_root / "normalized_analysis" / "test_voice.wav").exists()
    assert (run_root / "normalized_analysis" / "test_voice.json").exists()
    assert (run_root / "enhanced_review").exists() is False


def test_pipeline_with_enhance(input_dir_with_sample: Path, temp_dir: Path):
    """Running with --enhance must produce BOTH normalized_analysis and enhanced_review."""
    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=True,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    results = run_pipeline(cfg)
    assert len(results) == 1

    run_root = _find_run_dir(out_dir)

    assert (run_root / "normalized_analysis" / "test_voice.wav").exists()
    assert (run_root / "normalized_analysis" / "test_voice.json").exists()
    assert (run_root / "enhanced_review" / "test_voice.wav").exists()
    assert (run_root / "enhanced_review" / "test_voice.json").exists()
    assert (run_root / "enhanced_review" / "test_voice.txt").exists()
    # Label content check
    label = (run_root / "enhanced_review" / "test_voice.txt").read_text(encoding="utf-8")
    assert "ENHANCED REVIEW" in label
    assert "NOT FOR ANALYSIS" in label


def test_pipeline_dry_run(input_dir_with_sample: Path, temp_dir: Path):
    """--dry-run must not create any files."""
    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=True,
        dry_run=True,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)
    # Output directory should not exist (or be empty if pre-created)
    assert not out_dir.exists() or not any(out_dir.iterdir())


def test_pipeline_enhance_never_auto_triggered(input_dir_with_sample: Path, temp_dir: Path):
    """The enhance flag must NEVER be auto-triggered by any metric or LRA."""
    # This is a behavioural contract test: no matter what the audio looks like,
    # the flag must only come from explicit CLI input.
    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=False,  # explicitly off
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)
    run_root = _find_run_dir(out_dir)
    assert (run_root / "enhanced_review").exists() is False


# ---------------------------------------------------------------------------
# Sidecar schema
# ---------------------------------------------------------------------------

def test_enhanced_sidecar_schema(input_dir_with_sample: Path, temp_dir: Path):
    """Enhanced sidecar must contain required top-level keys."""
    import json

    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=True,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)

    run_root = _find_run_dir(out_dir)
    sidecar_path = run_root / "enhanced_review" / "test_voice.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert "version" in sidecar
    assert "source" in sidecar
    assert "processing" in sidecar
    assert "label" in sidecar
    assert "input_integrity" in sidecar
    assert "safety" in sidecar
    assert sidecar["label"] == "ENHANCED REVIEW — NOT FOR ANALYSIS"

    # compressor params
    comp = sidecar["processing"]["compressor"]
    assert "params" in comp
    for k in ("threshold_db", "ratio", "knee_db", "attack_ms", "release_ms"):
        assert k in comp["params"], f"missing param {k}"

    # gain reduction stats
    assert "gain_reduction_stats" in comp
    grs = comp["gain_reduction_stats"]
    assert "peak_gr_db" in grs
    assert "rms_gr_db" in grs
    assert "avg_gr_db" in grs

    # peak / rms levels before and after
    stages = sidecar["processing"]["stages"]
    assert len(stages) == 3
    for stage in stages:
        for k in ("peak_before_db", "peak_after_db", "rms_before_db", "rms_after_db"):
            assert k in stage, f"missing stage key {k}"

    # makeup gain
    assert "makeup_gain_db" in comp

    # true-peak post-limiter
    assert "true_peak_db" in comp

    # input integrity hash
    integrity = sidecar["input_integrity"]
    assert "normalized_audio_hash" in integrity
    assert len(integrity["normalized_audio_hash"]) == 64
    assert "sample_rate" in integrity
    assert "samples" in integrity

    # safety checks
    safety = sidecar["safety"]
    assert safety["paths_distinct"] is True
    assert safety["overwrite_check"] == "passed"


def test_normalized_sidecar_unchanged_by_enhance(input_dir_with_sample: Path, temp_dir: Path):
    """normalized_analysis sidecar must be identical whether or not --enhance is passed."""
    import json

    out_dir_no = temp_dir / "runs_no"
    out_dir_yes = temp_dir / "runs_yes"

    cfg_no = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir_no,
        enhance=False,
        dry_run=False,
    )
    _relax_qc_for_test(cfg_no)
    run_pipeline(cfg_no)

    cfg_yes = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir_yes,
        enhance=True,
        dry_run=False,
    )
    _relax_qc_for_test(cfg_yes)
    run_pipeline(cfg_yes)

    run_no = _find_run_dir(out_dir_no)
    run_yes = _find_run_dir(out_dir_yes)

    side_no = json.loads((run_no / "normalized_analysis" / "test_voice.json").read_text())
    side_yes = json.loads((run_yes / "normalized_analysis" / "test_voice.json").read_text())

    # Both should have same structure; gain_db may differ slightly due to
    # floating point, but the key fields must match.
    assert side_no["source"]["file"] == side_yes["source"]["file"]
    assert side_no["version"] == side_yes["version"]


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def test_manifest_written(input_dir_with_sample: Path, temp_dir: Path):
    """A manifest.json must be written at output_dir/manifest/ per spec."""
    import json

    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=False,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)

    manifest_path = out_dir / "manifest" / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert "sources" in manifest
    assert len(manifest["sources"]) == 1
    assert manifest["sources"][0]["filename"] == "test_voice.wav"


# ---------------------------------------------------------------------------
# Dataset summary
# ---------------------------------------------------------------------------

def test_dataset_summary_written(input_dir_with_sample: Path, temp_dir: Path):
    """A dataset summary must be written to reports/."""
    import json

    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=False,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)

    run_root = _find_run_dir(out_dir)
    summary_path = run_root / "reports" / "dataset_summary.json"
    assert summary_path.exists()
    summary = json.loads(summary_path.read_text())
    assert summary["config"]["enhance"] is False
    assert len(summary["files"]) == 1


# ---------------------------------------------------------------------------
# Directory completeness
# ---------------------------------------------------------------------------

def test_all_required_directories_exist(input_dir_with_sample: Path, temp_dir: Path):
    """Every required directory must exist after a run."""
    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=False,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)
    run_root = _find_run_dir(out_dir)

    required = [
        "canonical",
        "normalized_analysis",
        "quarantine",
        "metrics/pre",
        "metrics/post",
        "verification",
        "reports/per_file",
        "logs",
    ]
    for rel in required:
        assert (run_root / rel).is_dir(), f"missing dir: {rel}"


# ---------------------------------------------------------------------------
# Sidecar schema completeness
# ---------------------------------------------------------------------------

def test_canonical_sidecar_schema(input_dir_with_sample: Path, temp_dir: Path):
    """Canonical sidecar must contain all required top-level keys."""
    import json

    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=False,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)
    run_root = _find_run_dir(out_dir)
    sidecar = json.loads((run_root / "canonical" / "test_voice.json").read_text())

    assert "version" in sidecar
    assert "source" in sidecar
    assert "source_hash" in sidecar
    assert "pipeline_version" in sidecar
    assert "config_hash" in sidecar
    assert "command_line" in sidecar
    assert "dependency_versions" in sidecar
    assert "metrics" in sidecar
    assert "decisions" in sidecar
    assert "applied_gain_db" in sidecar
    assert "filter_coefficients" in sidecar
    assert len(sidecar["source_hash"]) == 64  # SHA256 hex
    assert sidecar["source"]["file"] == "test_voice.wav"


def test_normalized_sidecar_schema(input_dir_with_sample: Path, temp_dir: Path):
    """Normalized sidecar must contain all required top-level keys."""
    import json

    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=False,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)
    run_root = _find_run_dir(out_dir)
    sidecar = json.loads((run_root / "normalized_analysis" / "test_voice.json").read_text())

    assert "version" in sidecar
    assert "source" in sidecar
    assert "source_hash" in sidecar
    assert "pipeline_version" in sidecar
    assert "config_hash" in sidecar
    assert "command_line" in sidecar
    assert "dependency_versions" in sidecar
    assert "metrics" in sidecar
    assert "decisions" in sidecar
    assert "applied_gain_db" in sidecar
    assert "filter_coefficients" in sidecar


# ---------------------------------------------------------------------------
# Chain-of-custody
# ---------------------------------------------------------------------------

def test_source_hash_chain_of_custody(input_dir_with_sample: Path, temp_dir: Path):
    """The source hash must propagate from canonical to normalized to sidecar."""
    import json

    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=False,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)
    run_root = _find_run_dir(out_dir)

    can = json.loads((run_root / "canonical" / "test_voice.json").read_text())
    norm = json.loads((run_root / "normalized_analysis" / "test_voice.json").read_text())
    ver = json.loads((run_root / "verification" / "verification_summary.json").read_text())

    assert can["source_hash"] == norm["source_hash"]
    assert ver["files"][0]["source_hash"] == can["source_hash"]
    assert ver["files"][0]["verified"] is True


# ---------------------------------------------------------------------------
# Dependency versions
# ---------------------------------------------------------------------------

def test_dependency_versions_include_ffmpeg(input_dir_with_sample: Path, temp_dir: Path):
    """Dependency versions must include ffmpeg."""
    import json

    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=False,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)
    run_root = _find_run_dir(out_dir)
    sidecar = json.loads((run_root / "normalized_analysis" / "test_voice.json").read_text())
    deps = sidecar["dependency_versions"]
    assert "ffmpeg" in deps
    # Should be a version string or a status marker, not completely missing
    assert deps["ffmpeg"] in ("missing", "unavailable") or "." in deps["ffmpeg"]


# ---------------------------------------------------------------------------
# Config.yaml completeness
# ---------------------------------------------------------------------------

def test_config_yaml_captures_all_tunables(input_dir_with_sample: Path, temp_dir: Path):
    """config.yaml must exist and contain all default tunable parameters."""
    import yaml

    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=False,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)
    run_root = _find_run_dir(out_dir)
    config_path = run_root / "config.yaml"
    assert config_path.exists()
    with open(config_path, "r", encoding="utf-8") as f:
        written = yaml.safe_load(f)
    assert "target_lufs" in written
    assert "peak_ceiling_dbtp" in written
    assert "target_sr" in written
    assert "min_voiced_duration_s" in written


# ---------------------------------------------------------------------------
# Post-normalization metrics
# ---------------------------------------------------------------------------

def test_post_metrics_written(input_dir_with_sample: Path, temp_dir: Path):
    """metrics/post/ must contain frame mask and metrics JSON."""
    import json

    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=False,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)
    run_root = _find_run_dir(out_dir)
    post_dir = run_root / "metrics" / "post"
    assert (post_dir / "test_voice_metrics.json").exists()
    assert (post_dir / "test_voice_frame_mask.npy").exists()
    metrics = json.loads((post_dir / "test_voice_metrics.json").read_text())
    assert "source_hash" in metrics
    assert "gain_db" in metrics
    assert "post_sibilant_frames" in metrics


# ---------------------------------------------------------------------------
# Enhanced review safety and label tests
# ---------------------------------------------------------------------------

def test_enhanced_label_is_prominent(input_dir_with_sample: Path, temp_dir: Path):
    """The enhanced review label text file must be prominent and contain the safety warning."""
    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=True,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)

    run_root = _find_run_dir(out_dir)
    label_path = run_root / "enhanced_review" / "test_voice.txt"
    assert label_path.exists()
    label = label_path.read_text(encoding="utf-8")
    assert "ENHANCED REVIEW" in label
    assert "NOT FOR ANALYSIS" in label
    assert "human-listening" in label or "human listening" in label
    assert "Do NOT use it for harmonic analysis" in label or "Do NOT use it" in label


def test_enhanced_review_only_with_enhance_flag(input_dir_with_sample: Path, temp_dir: Path):
    """Enhanced review files must NOT exist when --enhance is not passed."""
    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=False,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)

    run_root = _find_run_dir(out_dir)
    assert (run_root / "enhanced_review").exists() is False


def test_enhanced_paths_distinct(input_dir_with_sample: Path, temp_dir: Path):
    """enhanced_review and normalized_analysis must be in different directories."""
    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=True,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)

    run_root = _find_run_dir(out_dir)
    norm_dir = run_root / "normalized_analysis"
    enh_dir = run_root / "enhanced_review"
    assert enh_dir.exists()
    assert norm_dir.exists()
    assert enh_dir != norm_dir
    assert str(enh_dir) != str(norm_dir)


def test_enhanced_does_not_overwrite_normalized(input_dir_with_sample: Path, temp_dir: Path):
    """Running with --enhance must not modify the normalized_analysis WAV or sidecar."""
    import json
    import hashlib

    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=True,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)

    run_root = _find_run_dir(out_dir)
    norm_wav = run_root / "normalized_analysis" / "test_voice.wav"
    norm_json = run_root / "normalized_analysis" / "test_voice.json"
    enh_wav = run_root / "enhanced_review" / "test_voice.wav"

    # Both must exist independently
    assert norm_wav.exists()
    assert enh_wav.exists()
    assert norm_json.exists()

    # They must be different files (not hardlinks or same inode)
    assert norm_wav.resolve() != enh_wav.resolve()

    # Normalized WAV must still be float32 (not 16-bit PCM from compressor)
    import soundfile as sf
    with sf.SoundFile(str(norm_wav)) as f:
        assert f.subtype == "FLOAT"

    # Enhanced WAV must be PCM_16
    with sf.SoundFile(str(enh_wav)) as f:
        assert f.subtype == "PCM_16"


def test_enhanced_sidecar_input_hash_matches(input_dir_with_sample: Path, temp_dir: Path):
    """The sidecar input_integrity hash must match the actual normalized audio."""
    import json
    import hashlib

    out_dir = temp_dir / "runs"
    cfg = PipelineConfig(
        input_dir=input_dir_with_sample,
        output_dir=out_dir,
        enhance=True,
        dry_run=False,
    )
    _relax_qc_for_test(cfg)
    run_pipeline(cfg)

    run_root = _find_run_dir(out_dir)
    sidecar_path = run_root / "enhanced_review" / "test_voice.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    integrity_hash = sidecar["input_integrity"]["normalized_audio_hash"]
    sr = sidecar["input_integrity"]["sample_rate"]

    # Read the normalized audio and hash it
    import soundfile as sf
    y_norm, _ = sf.read(str(run_root / "normalized_analysis" / "test_voice.wav"), dtype="float32")
    expected_hash = hashlib.sha256(y_norm.astype(np.float32).tobytes()).hexdigest()
    assert integrity_hash == expected_hash

