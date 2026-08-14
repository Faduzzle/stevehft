from __future__ import annotations

import json
import queue
import sys
import threading
import time
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, TextIO


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _to_jsonable(getattr(value, f.name))
            for f in fields(value)
        }
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "value"):
        return getattr(value, "value")
    return value


@dataclass(slots=True)
class LogEvent:
    kind: str
    ts_ns: int
    payload: Dict[str, Any] = field(default_factory=dict)


class EventLoggerLike(Protocol):
    def log(self, kind: str, **payload: Any) -> None: ...


class NullEventLogger:
    def log(self, kind: str, **payload: Any) -> None:
        del kind
        del payload


class JsonlEventLogger:
    def __init__(
        self,
        path: str | Path,
        *,
        flush_every: int = 64,
        max_queue_size: int = 50_000,
        mirror_stream: TextIO | None = None,
    ) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._flush_every = max(1, flush_every)
        self._queue: queue.Queue[Optional[LogEvent]] = queue.Queue(maxsize=max_queue_size)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="event-logger", daemon=True)
        self._dropped_events = 0
        self._started = False
        self._lifecycle_lock = threading.Lock()
        self._mirror_stream = mirror_stream

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            if self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="event-logger",
                daemon=True,
            )
            self._started = True
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return
            self._stop.set()
            if self._thread.is_alive():
                try:
                    self._queue.put(None, timeout=timeout)
                except queue.Full:
                    self._dropped_events += 1
            self._thread.join(timeout=timeout)
            self._started = False

    def log(self, kind: str, **payload: Any) -> None:
        event = LogEvent(
            kind=kind,
            ts_ns=time.monotonic_ns(),
            payload=_to_jsonable(dict(payload)),
        )
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._dropped_events += 1

    @property
    def dropped_events(self) -> int:
        return self._dropped_events

    def _run(self) -> None:
        pending: list[str] = []
        with self._path.open("a", encoding="utf-8") as fh:
            while not self._stop.is_set():
                item = self._queue.get()
                if item is None:
                    break
                line = self._serialize_event(item)
                pending.append(line)
                self._write_mirror_line(line)
                if len(pending) >= self._flush_every:
                    fh.write("\n".join(pending) + "\n")
                    fh.flush()
                    pending.clear()

            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    line = self._serialize_event(item)
                    pending.append(line)
                    self._write_mirror_line(line)

            if pending:
                fh.write("\n".join(pending) + "\n")
                fh.flush()

    @staticmethod
    def _serialize_event(item: LogEvent) -> str:
        return json.dumps(
            {
                "kind": item.kind,
                "ts_ns": item.ts_ns,
                "payload": item.payload,
            },
            separators=(",", ":"),
        )

    def _write_mirror_line(self, line: str) -> None:
        if self._mirror_stream is None:
            return
        stream = self._mirror_stream or sys.stdout
        stream.write(line + "\n")
        stream.flush()
