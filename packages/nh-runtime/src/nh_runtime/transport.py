"""Shared transport message types and utilities."""
from __future__ import annotations

import json
from typing import Any, Dict


class TransportMessage:
    """Canonical WebSocket message wrapper."""

    def __init__(self, type_: str, payload: Dict[str, Any], timestamp: float = None):
        self.type = type_
        self.payload = payload
        self.timestamp = timestamp

    def to_dict(self) -> Dict[str, Any]:
        d = {"type": self.type, "payload": self.payload}
        if self.timestamp is not None:
            d["ts"] = self.timestamp
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TransportMessage":
        return cls(type_=d.get("type", ""), payload=d.get("payload", {}), timestamp=d.get("ts"))

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, s: str) -> "TransportMessage":
        return cls.from_dict(json.loads(s))
