"""Sample catalog — manages SampleSource discovery and caching (Phase 7).

Field recordings, nature samples, voice samples → cataloged with metadata,
harmonic analysis sidecars, and beacon tuning proposals.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional
import os
import json


@dataclass
class SampleEntry:
    """A catalog entry for an audio sample."""
    sample_id: str
    audio_path: str
    name: str = ""
    category: str = "field"  # field | voice | nature | music
    duration_s: float = 0.0
    format: str = ""
    channels: int = 1
    sample_rate: float = 44100.0

    # Analysis sidecar.
    analysis_path: Optional[str] = None  # path to AnalysisResult JSON

    # Beacon tuning.
    proposed_f1: Optional[float] = None
    tuned: bool = False

    # Tags.
    tags: List[str] = dc_field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "audio_path": self.audio_path,
            "name": self.name,
            "category": self.category,
            "duration_s": self.duration_s,
            "format": self.format,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "analysis_path": self.analysis_path,
            "proposed_f1": self.proposed_f1,
            "tuned": self.tuned,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SampleEntry":
        return cls(**{k: d.get(k, v) for k, v in cls.__dataclass_fields__.items()
                     if k != "tags"})  # special handling for mutable default
    # Use manual construction.


class SampleCatalog:
    """In-memory catalog of field recordings and samples.

    Usage:
        catalog = SampleCatalog()
        catalog.scan_directory("~/Music/field-recordings/wav/")
        entries = catalog.find_by_tag("frogs")
    """

    def __init__(self, root_dir: str = ""):
        self.root_dir = os.path.expanduser(root_dir) if root_dir else ""
        self.entries: Dict[str, SampleEntry] = {}

    def add(self, entry: SampleEntry) -> None:
        self.entries[entry.sample_id] = entry

    def get(self, sample_id: str) -> Optional[SampleEntry]:
        return self.entries.get(sample_id)

    def find_by_tag(self, tag: str) -> List[SampleEntry]:
        return [e for e in self.entries.values() if tag in e.tags]

    def find_by_category(self, category: str) -> List[SampleEntry]:
        return [e for e in self.entries.values() if e.category == category]

    def scan_directory(self, directory: str) -> int:
        """Discover WAV files in directory, create catalog entries."""
        import os
        d = os.path.expanduser(directory)
        count = 0
        for fname in sorted(os.listdir(d)):
            if fname.lower().endswith(".wav"):
                fpath = os.path.join(d, fname)
                sample_id = os.path.splitext(fname)[0]
                entry = SampleEntry(
                    sample_id=sample_id,
                    audio_path=fpath,
                    name=fname,
                )
                self.add(entry)
                count += 1
        return count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root_dir": self.root_dir,
            "entries": {sid: e.to_dict() for sid, e in self.entries.items()},
        }

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path: str) -> "SampleCatalog":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cat = cls(root_dir=data.get("root_dir", ""))
        for sid, ed in data.get("entries", {}).items():
            cat.add(SampleEntry(
                sample_id=ed["sample_id"],
                audio_path=ed.get("audio_path", ""),
                name=ed.get("name", ""),
                category=ed.get("category", "field"),
                duration_s=ed.get("duration_s", 0.0),
                format=ed.get("format", ""),
                channels=ed.get("channels", 1),
                sample_rate=ed.get("sample_rate", 44100.0),
                analysis_path=ed.get("analysis_path"),
                proposed_f1=ed.get("proposed_f1"),
                tuned=ed.get("tuned", False),
                tags=ed.get("tags", []),
            ))
        return cat
