"""Canonical serialisation for the hash chain.

Every byte written into a hash must be reproducible on a different machine, a
different OS, and a different Python build, forever. Two rules make that true:

  1. Deterministic JSON — sorted keys, no incidental whitespace (RFC 8785 in
     spirit).
  2. No floats, ever. `0.1 + 0.2 == 0.30000000000000004`. Binary floats do not
     round-trip identically through every serialiser, so a single float in a
     payload silently breaks replay attestation for every entry after it.

Law #5: money is integer paise, probability is a 6-dp string, time is epoch ms.
"""

from __future__ import annotations

import json
from typing import Any

GENESIS = "0" * 64

_FLOAT_MSG = (
    "float in ledger payload: use int paise for money, "
    "6-dp strings for probabilities, epoch-ms ints for time"
)


def _assert_no_float(obj: Any) -> None:
    """Recursively reject floats anywhere in the structure, keys included."""
    if isinstance(obj, float):
        raise TypeError(_FLOAT_MSG)
    if isinstance(obj, dict):
        for k, v in obj.items():
            _assert_no_float(k)
            _assert_no_float(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _assert_no_float(v)


def canonical_bytes(obj: Any) -> bytes:
    """Byte-stable JSON. The input to every hash in the system."""
    _assert_no_float(obj)
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def prob_str(p: float) -> str:
    """The ONLY sanctioned float -> ledger conversion.

    Six decimal places is far finer than any decision threshold we use and is
    exactly representable as text, so it round-trips through the chain
    unchanged.
    """
    return f"{p:.6f}"


__all__ = ["GENESIS", "canonical_bytes", "prob_str"]
