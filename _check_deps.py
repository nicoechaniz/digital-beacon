#!/usr/bin/env python3
"""Quick dep check."""
import sys

deps = ["numpy", "soundfile", "pyloudnorm", "webrtcvad", "scipy"]
ok = []
missing = []
for name in deps:
    try:
        __import__(name)
        ok.append(name)
    except ImportError:
        missing.append(name)

print(f"OK: {ok}")
print(f"MISSING: {missing}")
sys.exit(0 if not missing else 1)
