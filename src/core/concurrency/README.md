# Core Concurrency Guide

## Purpose

`src/core/concurrency/` contains reusable transport and synchronization
primitives that are shared across market data, strategy, execution, and
telemetry.

This folder should stay strategy-agnostic.

Concurrency is part of the performance model.
The design separates current authoritative state from event transport so the system can measure latency, detect dropped updates, and preserve ownership.
The queue improves handoff timing, but it must never become a hidden source of trading truth.

## `spsc.py`

`SpscRingBuffer` is the default one-producer/one-consumer handoff queue.

Use it for:

- market-data thread -> strategy/execution thread update notifications
- strategy/execution thread -> telemetry handoff
- replay reader -> offline feature worker

Do not use it as:

- the authoritative order or portfolio ledger
- a growing historical store
- a replacement for bounded rolling feature buffers

## Design Rules

- queue payloads should be compact deltas or wakeup markers
- current-state snapshots should live outside the queue
- producer and consumer should each have exactly one owning thread
- if a consumer falls behind, prefer bounded overwrite/coalescing over
  unbounded memory growth

## Why This Is Not Fully Lock-Free

The first implementation uses a small condition-protected critical section.

That is deliberate:

- correctness and deterministic shutdown are more important than clever queue
  micro-optimizations at this stage
- Python's GIL and object semantics make “true lock-free” queues less
  straightforward than in C/C++
- once profiling shows this queue is the bottleneck, we can replace the
  implementation behind the same interface
