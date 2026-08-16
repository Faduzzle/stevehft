# Test Strategy Guide

## Purpose

`tests/` contains fast automated checks for the new architecture.

These tests should focus on deterministic logic, not on the deprecated `Old model/` behavior.

The test system protects both trading behavior and system observability.
Tests must verify that models produce explainable targets, controls block unsafe actions, ledgers converge toward broker state, and metrics expose performance regressions.

## What To Test First

### State Tests

- best-price updates
- spread and microprice calculations
- stale-data detection

### Execution Tests

- intent-to-order translation
- duplicate-order suppression
- cancel state transitions
- reconciliation against mocked order responses

### Risk Tests

- long and short inventory caps
- reserved buying-power checks
- flatten-mode restrictions
- close-window risk responses

### Strategy Tests

- passive quote generation
- inventory skewing
- spread filters
- pacing response when behind `200 trades/day`
- deterministic replay through `src/app/replay.py`

### Telemetry Tests

- fee estimate calculations
- rebate estimate calculations
- trade count tracking

### Pre-Sim Profiling

Use `scripts/profile_fake_load.py` to measure control-cycle latency and peak memory
against a deterministic fake trader before running the real SHIFT simulator.

Example:

```bash
python3 scripts/profile_fake_load.py --symbols AAPL XOM --cycles 500 --update-interval-ms 20
```

## Testing Philosophy

- keep tests small and isolated
- mock SHIFT interactions at boundaries
- verify economic logic, not just syntax
- prioritize behavior that protects capital and prevents operational mistakes

## Context For Future Work

Before trusting a live trading change, there should be a focused test in this tree that explains the intended behavior.
