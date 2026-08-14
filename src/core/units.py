"""Shared numeric constants used across modules.

Keep this module free of trading logic. It exists so timestamp-unit
conversions and float-comparison tolerances have one definition instead of
being copied as unnamed literals at each call site.
"""

from __future__ import annotations

NS_PER_MS: int = 1_000_000
NS_PER_S: int = 1_000_000_000

# Tolerance for treating two float prices/PnL values as equal after
# floating-point arithmetic (fills, replaces, portfolio deltas).
FLOAT_EQ_TOLERANCE: float = 1e-12
