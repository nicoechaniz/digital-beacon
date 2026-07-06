from typing import Any, Dict


class MappingGraph:
    """Declarative binding from control/sensor event types to model parameters."""

    def __init__(self, mappings: Dict[str, Dict[str, Any]] = None):
        self.mappings = mappings or {}

    def add(self, event_type: str, param: str, scale: float = 1.0, offset: float = 0.0,
            smooth_s: float = 0.0, n: int = None) -> None:
        self.mappings[event_type] = {
            "param": param,
            "scale": scale,
            "offset": offset,
            "smooth_s": smooth_s,
            "n": n,
        }

    def get(self, event_type: str) -> Dict[str, Any]:
        return self.mappings.get(event_type)

    def to_dict(self) -> Dict[str, Dict[str, Any]]:
        return self.mappings

    @classmethod
    def from_dict(cls, d: Dict[str, Dict[str, Any]]) -> "MappingGraph":
        return cls(mappings=d)

    def apply(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Return a scaled control event dict for the model, or None if no mapping."""
        cfg = self.mappings.get(event.get("type"))
        if cfg is None:
            return None
        raw = float(event.get("value", 0.0))
        scaled = raw * cfg.get("scale", 1.0) + cfg.get("offset", 0.0)
        return {
            "type": cfg["param"],
            "value": scaled if cfg.get("n") is None else {"n": cfg["n"], "gain": scaled},
        }
