from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class ControlEvent:
    """Normalized control event."""
    source: str
    type: str
    value: Any
    timestamp: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "type": self.type,
            "value": self.value,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ControlEvent":
        return cls(
            source=d.get("source", ""),
            type=d.get("type", ""),
            value=d.get("value"),
            timestamp=d.get("timestamp"),
        )
