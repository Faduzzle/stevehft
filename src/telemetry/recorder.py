from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .logger import EventLoggerLike, JsonlEventLogger, NullEventLogger


@dataclass(slots=True)
class SessionTelemetry:
    session_dir: Path
    event_path: Path
    event_logger: EventLoggerLike

    def start(self) -> None:
        if isinstance(self.event_logger, JsonlEventLogger):
            self.event_logger.start()

    def stop(self) -> None:
        if isinstance(self.event_logger, JsonlEventLogger):
            self.event_logger.stop()


def build_session_telemetry(
    session_dir: str | Path,
    *,
    flush_every: int = 64,
    max_queue_size: int = 50_000,
    mirror_stream: TextIO | None = None,
) -> SessionTelemetry:
    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_event_log(session_path)
    event_path = next_event_log_path(session_path)
    _refresh_latest_event_link(session_path, event_path)
    logger = JsonlEventLogger(
        event_path,
        flush_every=flush_every,
        max_queue_size=max_queue_size,
        mirror_stream=mirror_stream,
    )
    return SessionTelemetry(
        session_dir=session_path,
        event_path=event_path,
        event_logger=logger,
    )


def build_null_telemetry(session_dir: str | Path = ".") -> SessionTelemetry:
    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)
    return SessionTelemetry(
        session_dir=session_path,
        event_path=session_path / "events.jsonl",
        event_logger=NullEventLogger(),
    )


def next_event_log_path(session_dir: str | Path) -> Path:
    session_path = Path(session_dir)
    max_index = 0
    for path in session_path.glob("events_*.jsonl"):
        stem = path.stem
        _, _, suffix = stem.partition("_")
        if not suffix.isdigit():
            continue
        max_index = max(max_index, int(suffix))
    if max_index == 0 and (session_path / "events.jsonl").exists():
        max_index = 1
    return session_path / f"events_{max_index + 1:04d}.jsonl"


def _refresh_latest_event_link(session_dir: Path, event_path: Path) -> None:
    latest_path = session_dir / "events.jsonl"
    if latest_path.exists() or latest_path.is_symlink():
        latest_path.unlink()
    try:
        latest_path.symlink_to(event_path.name)
    except OSError:
        latest_path.write_text(
            f"{event_path.name}\n",
            encoding="utf-8",
        )


def _migrate_legacy_event_log(session_dir: Path) -> None:
    legacy_path = session_dir / "events.jsonl"
    if legacy_path.is_symlink() or not legacy_path.exists():
        return
    if any(session_dir.glob("events_*.jsonl")):
        return
    if legacy_path.stat().st_size <= 0:
        legacy_path.unlink()
        return
    legacy_path.rename(session_dir / "events_0001.jsonl")
