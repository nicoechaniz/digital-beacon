"""Pure math helpers for harmonic series."""
import math


def freq_for_harmonic(f1: float, n: int) -> float:
    """Frequency of the n-th harmonic of f1."""
    return f1 * n


def cents_difference(a: float, b: float) -> float:
    """Cents between two frequencies."""
    if a <= 0 or b <= 0:
        return 0.0
    return 1200.0 * math.log2(a / b)


def octave_reduce(freq: float, base: float = 65.0) -> float:
    """Reduce a frequency into the octave below or at base."""
    if freq <= 0 or base <= 0:
        return 0.0
    ratio = freq / base
    octave = 2 ** math.floor(math.log2(ratio)) if ratio > 0 else 1
    return freq / octave


def playable_frequency(freq: float, fmin: float = 20.0, fmax: float = 20000.0) -> float:
    """Clamp a frequency to a playable range and reject non-positive values."""
    if freq <= 0:
        return fmin
    return max(fmin, min(freq, fmax))
