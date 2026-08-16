# Core Module Guide

## Purpose

`src/core/` contains foundational runtime code that the rest of the trading system depends on.

This folder should remain small, stable, and free of strategy-specific behavior.

Core establishes the contracts that make the rest of the system measurable.
It owns configuration validation, session time, broker lifecycle, and bounded state transport.
Those contracts let strategy and risk code remain deterministic while dashboards and telemetry observe the same lifecycle.

## Planned Files

### `config.py`

Responsibilities:

- define typed runtime configuration
- load symbol lists, timing settings, and risk thresholds
- centralize competition parameters like rebate, fee, and flatten windows

Should contain:

- dataclasses or typed config objects
- validation for required settings
- sane defaults for paper or competition runs

Should not contain:

- API calls
- strategy logic
- dynamic market calculations

### `session.py`

Responsibilities:

- create the `shift.Trader` instance
- connect using `initiator.cfg` and credentials
- subscribe symbols
- expose clean connect, disconnect, and health-check behavior

Concurrency role:

- controlled primarily by the main thread
- should expose thread-safe status checks for other modules

Current implementation:

- `RuntimeConfig` lives in `config.py`
- `ShiftSession` wraps `shift.Trader` lifecycle, health checks, and book subscriptions
- session events are intended to be logged through telemetry

### `concurrency/spsc.py`

Responsibilities:

- provide a bounded single-producer/single-consumer handoff queue
- expose monotonic write/read sequence counters
- support nonblocking pop, blocking wait-pop, and bounded overwrite semantics

Intended use:

- market-data update transport
- strategy/execution wakeup transport
- async telemetry handoff

Important rule:

- the queue is transport only; snapshots, ledgers, and rolling feature state
  still live in their owning modules

### `session_clock.py`

Responsibilities:

- compute session phase from local trading hours
- expose elapsed-session fraction, expected trade count, minutes to close, and flatten-mode state
- keep close-window logic in one deterministic place

Why it matters:

- low-latency systems should not scatter timing logic across modules
- close handling must be deterministic

### `symbols.py`

Responsibilities:

- define tradable universe selection
- hold symbol metadata if needed
- support small configurable symbol sets for staged rollout

## Folder Rules

- keep imports light
- avoid heavy dependency chains
- keep interfaces reusable by every higher-level module

## Context For Future Work

If we need to understand how the application starts, when trading is allowed, or where core config lives, `src/core/` should be the first place to look.
