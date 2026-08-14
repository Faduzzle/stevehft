"""Shared loader for JSONL event logs.

Every analysis script imports from here so parsing logic stays in one place.

Supports versioned event logs (events_0001.jsonl, events_0002.jsonl, etc.)
and the events.jsonl symlink convention used by SessionTelemetry.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


@dataclass(slots=True)
class Event:
    kind: str
    ts_ns: int
    payload: dict[str, Any]

    @property
    def ts_s(self) -> float:
        return self.ts_ns / 1_000_000_000.0

    @property
    def ts_ms(self) -> float:
        return self.ts_ns / 1_000_000.0

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


def iter_events(path: str | Path) -> Iterator[Event]:
    with open(path, encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[warn] skip malformed line {line_no}: {exc}", file=sys.stderr)
                continue
            yield Event(
                kind=obj.get("kind", ""),
                ts_ns=obj.get("ts_ns", 0),
                payload=obj.get("payload", {}),
            )


def iter_events_multi(paths: list[Path]) -> Iterator[Event]:
    """Iterate events across multiple log files, sorted by timestamp."""
    all_events: list[Event] = []
    for p in paths:
        all_events.extend(iter_events(p))
    all_events.sort(key=lambda ev: ev.ts_ns)
    yield from all_events


def load_events(path: str | Path) -> list[Event]:
    return list(iter_events(path))


def find_all_event_logs(session_dir: str | Path) -> list[Path]:
    """Find all versioned event log files in a session directory, sorted by name."""
    d = Path(session_dir)
    logs = sorted(d.glob("events_*.jsonl"), key=lambda p: p.name)
    if not logs:
        plain = d / "events.jsonl"
        if plain.exists() and not plain.is_symlink():
            return [plain]
    return logs


@dataclass(slots=True)
class EventIndex:
    """Pre-grouped index over a session log for fast queries."""

    events: list[Event]
    by_kind: dict[str, list[Event]] = field(default_factory=dict)
    first_ts_ns: int = 0
    last_ts_ns: int = 0

    @classmethod
    def build(cls, path: str | Path) -> EventIndex:
        events = load_events(path)
        by_kind: dict[str, list[Event]] = {}
        for ev in events:
            by_kind.setdefault(ev.kind, []).append(ev)
        return cls(
            events=events,
            by_kind=by_kind,
            first_ts_ns=events[0].ts_ns if events else 0,
            last_ts_ns=events[-1].ts_ns if events else 0,
        )

    @classmethod
    def build_multi(cls, paths: list[Path]) -> EventIndex:
        """Build index from multiple versioned log files."""
        events = list(iter_events_multi(paths))
        by_kind: dict[str, list[Event]] = {}
        for ev in events:
            by_kind.setdefault(ev.kind, []).append(ev)
        return cls(
            events=events,
            by_kind=by_kind,
            first_ts_ns=events[0].ts_ns if events else 0,
            last_ts_ns=events[-1].ts_ns if events else 0,
        )

    @property
    def duration_s(self) -> float:
        return (self.last_ts_ns - self.first_ts_ns) / 1_000_000_000.0

    def kinds(self) -> list[str]:
        return sorted(self.by_kind.keys())

    def of(self, kind: str) -> list[Event]:
        return self.by_kind.get(kind, [])

    def count(self, kind: str) -> int:
        return len(self.of(kind))

    def relative_s(self, ev: Event) -> float:
        return (ev.ts_ns - self.first_ts_ns) / 1_000_000_000.0


def resolve_log_path(argv: list[str] | None = None) -> Path:
    """Resolve a single log file path from CLI args or default location.

    Accepts a file path or a session directory (picks events.jsonl symlink).
    """
    args = argv if argv is not None else sys.argv[1:]
    if args:
        p = Path(args[0])
        if p.is_dir():
            symlink = p / "events.jsonl"
            if symlink.exists():
                return symlink
            logs = find_all_event_logs(p)
            if logs:
                return logs[-1]  # latest
            print(f"No event logs found in {p}", file=sys.stderr)
            sys.exit(1)
        return p
    default = Path("runs/live_smoke/events.jsonl")
    if default.exists():
        return default
    print("Usage: <script> <path-to-events.jsonl | session-dir>", file=sys.stderr)
    sys.exit(1)


def resolve_log_paths(argv: list[str] | None = None, *, all_versions: bool = False) -> list[Path]:
    """Resolve log file path(s). With all_versions=True, returns all versioned logs."""
    args = argv if argv is not None else sys.argv[1:]
    if args:
        p = Path(args[0])
        if p.is_dir() and all_versions:
            logs = find_all_event_logs(p)
            if logs:
                return logs
        return [resolve_log_path(argv)]
    return [resolve_log_path(argv)]