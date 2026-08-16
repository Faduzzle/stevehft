# Dependency and External Library README

This project is intentionally lightweight in the trading hot path.
Most imports are from the Python standard library or from this repository's
own `src/` package.

This document lists the **runtime dependencies**, **optional external
libraries**, and **live trading config/data files** required to run the system.

Dependencies also define the system's operating boundary.
The standard library supports deterministic state, risk, execution, telemetry, and dashboard code.
The SHIFT package supplies the external market and order boundary.
Optional numerical packages support feature computation but must not become hidden requirements for safety controls.

## Minimum Runtime

| Dependency | Required | Why |
|---|---:|---|
| Python 3.10+ | Yes | The codebase uses modern type syntax such as `A | B`, `dataclass(slots=True)`, and `zoneinfo.ZoneInfo`. |

### Standard Library Modules Used

No separate install is needed for these; they ship with Python:

`argparse`, `bisect`, `collections`, `csv`, `dataclasses`, `datetime`,
`enum`, `io`, `json`, `math`, `os`, `pathlib`, `queue`, `statistics`,
`sys`, `tempfile`, `threading`, `time`, `tracemalloc`, `typing`,
`unittest`, `zoneinfo`.

## Third-Party Python Libraries

| Package | Required | Used by | Purpose |
|---|---:|---|---|
| `shift` | **Yes for live trading** | `src/app/main.py`, `src/execution/shift_orders.py` | Broker/session API, market-data subscriptions, and order object construction for the SHIFT simulator/venue. |
| `numpy` | Optional | `src/strategy/mm_feature_batch.py` | Optional vectorized quote-gate computation. If NumPy is unavailable, the code automatically falls back to scalar Python logic. |

## Installation Notes

### 1) Python

Use Python 3.10 or newer:

```bash
python3 --version
```

### 2) NumPy (optional)

If you want the optional vectorized feature-batch backend:

```bash
python3 -m pip install numpy
```

The system still runs without NumPy.

### 3) SHIFT Python API (required for live broker connectivity)

The `shift` package is not declared in a local `requirements.txt` because it is
usually supplied by the SHIFT environment/vendor install rather than pulled from
this repository.

Verify that your environment can import it:

```bash
python3 - <<'PY'
import shift
print("shift import OK:", shift)
PY
```

If this import fails, live startup will fail fast with
`RuntimeError("SHIFT Python package is not installed")`.

## Required Live-Session Config and Data Files

These are not Python packages, but they are external runtime dependencies for
live SHIFT connectivity and must be present in the repository root unless you
override paths in config.

| File | Required | Why |
|---|---:|---|
| `initiator.cfg` | Yes for live trading | FIX/session configuration passed to `shift.Trader.connect(...)`. |
| `FIXT11.xml` | Yes for live trading | Transport FIX dictionary referenced by `initiator.cfg`. |
| `FIX50SP2.xml` | Yes for live trading | Application FIX dictionary referenced by `initiator.cfg`. |
| `credentials.py` or env vars | Yes for live trading | Username/password source for `run.py` and `src/app/live_smoke.py`. |

### Credentials

Preferred runtime environment variables:

```bash
export SHIFT_USERNAME="your_username"
export SHIFT_PASSWORD="your_password"
```

Fallback file format in `credentials.py`:

```python
my_username = "your_username"
my_password = "your_password"
```

## In-Repository Modules That Are Not External Dependencies

The following imports are local project modules, not packages to install:

- `src.*`
- `book_cache`, `state`, `order_state`, `logger`, `locked_trader`, `spsc`
  when imported by sibling modules inside `src/`
- `log_loader` used by scripts in `scripts/`
- `credentials` if using the local fallback credential file

## What Is **Not** Required Right Now

Some architecture/build docs mention heavier research libraries, but the current
runtime code does **not** import them:

- `pandas`
- `scipy`
- `matplotlib`
- `torch`
- `sklearn`

Do not add those unless a future research/offline analytics module truly needs
them.

## Quick Dependency Sanity Check

Run this from the repo root:

```bash
python3 - <<'PY'
import sys
print("python:", sys.version)

try:
    import numpy
    print("numpy:", numpy.__version__)
except Exception as exc:
    print("numpy unavailable (optional):", repr(exc))

try:
    import shift
    print("shift import OK")
except Exception as exc:
    print("shift unavailable (required for live trading):", repr(exc))
PY
```

## Current Dependency Philosophy

- Keep the trading loop mostly standard-library only.
- Treat `shift` as the only hard external runtime dependency for live trading.
- Keep NumPy optional and behind a fallback path.
- Avoid large dataframe/ML stacks in the hot path unless profiling proves they
  are necessary and safe.
