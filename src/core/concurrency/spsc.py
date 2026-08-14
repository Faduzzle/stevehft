from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Generic, Optional, TypeVar


T = TypeVar("T")


@dataclass(slots=True)
class SpscRingBuffer(Generic[T]):
    """Bounded single-producer/single-consumer ring buffer with sequence counters.

    This is a transport primitive, not an authoritative state store.

    Intended usage:

    - one producer thread calls `try_push(...)` or `push_overwrite_oldest(...)`
    - one consumer thread calls `try_pop(...)` or `wait_pop(...)`
    - consumer-side logic tracks `write_seq/read_seq` to avoid recomputing when idle

    The implementation uses a tiny condition-protected critical section instead
    of a spin-heavy lock-free loop because this repo is Python-first and we want
    deterministic behavior before chasing lower-level queue optimizations.
    """

    capacity: int
    _buffer: list[Optional[T]] = field(init=False, repr=False)
    _head: int = field(default=0, init=False, repr=False)
    _tail: int = field(default=0, init=False, repr=False)
    _size: int = field(default=0, init=False, repr=False)
    _write_seq: int = field(default=0, init=False, repr=False)
    _read_seq: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(init=False, repr=False)
    _not_empty: threading.Condition = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        self._buffer = [None] * self.capacity
        self._head = 0
        self._tail = 0
        self._size = 0
        self._write_seq = 0
        self._read_seq = 0
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)

    @property
    def write_seq(self) -> int:
        with self._lock:
            return self._write_seq

    @property
    def read_seq(self) -> int:
        with self._lock:
            return self._read_seq

    @property
    def size(self) -> int:
        with self._lock:
            return self._size

    def try_push(self, item: T) -> bool:
        with self._not_empty:
            if self._size >= self.capacity:
                return False
            self._push_unlocked(item)
            self._not_empty.notify()
            return True

    def push_overwrite_oldest(self, item: T) -> bool:
        with self._not_empty:
            overwritten = self._size >= self.capacity
            if overwritten:
                self._buffer[self._head] = None
                self._head = (self._head + 1) % self.capacity
                self._size -= 1
                self._read_seq += 1
            self._push_unlocked(item)
            self._not_empty.notify()
            return overwritten

    def try_pop(self) -> Optional[T]:
        with self._lock:
            if self._size <= 0:
                return None
            return self._pop_unlocked()

    def wait_pop(self, timeout_s: float | None = None) -> Optional[T]:
        deadline_ns = None
        if timeout_s is not None:
            deadline_ns = time.monotonic_ns() + max(timeout_s, 0.0) * 1_000_000_000

        with self._not_empty:
            while self._size <= 0:
                if timeout_s is None:
                    self._not_empty.wait()
                    continue

                remaining_s = max(
                    (deadline_ns - time.monotonic_ns()) / 1_000_000_000.0,
                    0.0,
                )
                if remaining_s <= 0.0:
                    return None
                self._not_empty.wait(remaining_s)

            return self._pop_unlocked()

    def drain(self, *, max_items: int | None = None) -> list[T]:
        items: list[T] = []
        with self._lock:
            while self._size > 0 and (max_items is None or len(items) < max_items):
                item = self._pop_unlocked()
                if item is not None:
                    items.append(item)
        return items

    def _push_unlocked(self, item: T) -> None:
        self._buffer[self._tail] = item
        self._tail = (self._tail + 1) % self.capacity
        self._size += 1
        self._write_seq += 1

    def _pop_unlocked(self) -> Optional[T]:
        item = self._buffer[self._head]
        self._buffer[self._head] = None
        self._head = (self._head + 1) % self.capacity
        self._size -= 1
        self._read_seq += 1
        return item
