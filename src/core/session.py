from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Protocol

from src.telemetry.logger import EventLoggerLike


class TraderLike(Protocol):
    def connect(self, cfg_file: str, password: str) -> bool: ...

    def disconnect(self) -> bool: ...

    def is_connected(self) -> bool: ...

    def sub_order_book(self, symbol: str) -> None: ...

    def sub_all_order_book(self) -> None: ...

    def unsub_order_book(self, symbol: str) -> None: ...

    def get_subscribed_order_book_list(self) -> list[str]: ...

    def get_stock_list(self) -> list[str]: ...

    def get_last_trade_time(self) -> datetime: ...


class ThreadSafeTraderProxy:
    def __init__(self, trader: TraderLike, lock: threading.RLock | None = None) -> None:
        self._trader = trader
        self._lock = lock or threading.RLock()

    def __getattr__(self, name: str):
        attr = getattr(self._trader, name)
        if not callable(attr):
            return attr

        def locked_call(*args, **kwargs):
            with self._lock:
                return attr(*args, **kwargs)

        return locked_call


@dataclass(slots=True)
class SessionStatus:
    connected: bool = False
    subscribed_symbols: tuple[str, ...] = ()
    requested_symbols: tuple[str, ...] = ()
    subscribe_all_books: bool = False
    last_connect_ok: bool = False
    last_disconnect_ok: bool = False


@dataclass(slots=True)
class ShiftSession:
    trader: TraderLike
    username: str
    initiator_cfg: Path
    event_logger: Optional[EventLoggerLike] = None
    status: SessionStatus = field(default_factory=SessionStatus)

    def connect(self, password: str) -> bool:
        ok = bool(self.trader.connect(str(self.initiator_cfg), password))
        self.status.connected = ok and bool(self.trader.is_connected())
        self.status.last_connect_ok = ok
        if self.event_logger is not None:
            self.event_logger.log(
                "session_connect",
                username=self.username,
                initiator_cfg=str(self.initiator_cfg),
                ok=ok,
                connected=self.status.connected,
            )
        return self.status.connected

    def disconnect(self) -> bool:
        for symbol in self.status.subscribed_symbols:
            try:
                self.trader.unsub_order_book(symbol)
            except Exception as exc:
                if self.event_logger is not None:
                    self.event_logger.log(
                        "session_unsubscribe_failed",
                        username=self.username,
                        symbol=symbol,
                        error=repr(exc),
                    )
                continue
        ok = bool(self.trader.disconnect())
        self.status.connected = False
        self.status.subscribed_symbols = ()
        self.status.last_disconnect_ok = ok
        if self.event_logger is not None:
            self.event_logger.log(
                "session_disconnect",
                username=self.username,
                ok=ok,
            )
        return ok

    def subscribe_symbols(
        self,
        symbols: Iterable[str],
        *,
        subscribe_all_books: bool = False,
    ) -> tuple[str, ...]:
        requested = tuple(
            symbol.strip().upper()
            for symbol in symbols
            if symbol.strip()
        )
        self.status.requested_symbols = requested
        self.status.subscribe_all_books = subscribe_all_books
        if subscribe_all_books:
            self.trader.sub_all_order_book()
            try:
                subscribed = tuple(self.trader.get_subscribed_order_book_list())
            except Exception as exc:
                if self.event_logger is not None:
                    self.event_logger.log(
                        "session_list_subscribed_books_failed",
                        username=self.username,
                        error=repr(exc),
                    )
                subscribed = requested
        else:
            subscribed_list: list[str] = []
            for symbol in requested:
                self.trader.sub_order_book(symbol)
                subscribed_list.append(symbol)
            subscribed = tuple(subscribed_list)

        self.status.subscribed_symbols = subscribed
        if self.event_logger is not None:
            self.event_logger.log(
                "session_subscribe_books",
                subscribe_all_books=subscribe_all_books,
                symbols=subscribed,
            )
        return subscribed

    def discover_symbols(self) -> tuple[str, ...]:
        symbols = tuple(
            symbol.strip().upper()
            for symbol in self.trader.get_stock_list()
            if symbol.strip()
        )
        if self.event_logger is not None:
            self.event_logger.log(
                "session_discover_symbols",
                symbols=symbols,
                symbol_count=len(symbols),
            )
        return symbols

    def get_last_trade_time(self) -> datetime:
        try:
            trade_time = self.trader.get_last_trade_time()
        except Exception as exc:
            if self.event_logger is not None:
                self.event_logger.log(
                    "session_last_trade_time_failed",
                    username=self.username,
                    error=repr(exc),
                )
            raise
        if not isinstance(trade_time, datetime):
            raise ValueError(
                "trader.get_last_trade_time() did not return a datetime"
            )
        return trade_time

    def health_check(self) -> bool:
        try:
            connected = bool(self.trader.is_connected())
        except Exception as exc:
            connected = False
            if self.event_logger is not None:
                self.event_logger.log(
                    "session_health_check_failed",
                    username=self.username,
                    error=repr(exc),
                )
        self.status.connected = connected
        return connected

    def ensure_connected_and_subscribed(self, password: str) -> bool:
        if not self.health_check():
            if self.event_logger is not None:
                self.event_logger.log(
                    "session_reconnect_attempt",
                    username=self.username,
                    requested_symbols=self.status.requested_symbols,
                    subscribe_all_books=self.status.subscribe_all_books,
                )
            try:
                reconnect_ok = self.connect(password)
            except Exception as exc:
                if self.event_logger is not None:
                    self.event_logger.log(
                        "session_reconnect_failed",
                        username=self.username,
                        error=repr(exc),
                    )
                return False
            if not reconnect_ok:
                return False
            self.status.subscribed_symbols = ()

        return self._repair_subscriptions() and self.status.connected

    def _repair_subscriptions(self) -> bool:
        requested = self.status.requested_symbols
        if self.status.subscribe_all_books:
            if self.status.subscribed_symbols:
                return True
            self.subscribe_symbols(requested, subscribe_all_books=True)
            return bool(self.status.subscribed_symbols)
        if not requested:
            return True

        try:
            current = tuple(
                symbol.strip().upper()
                for symbol in self.trader.get_subscribed_order_book_list()
                if symbol.strip()
            )
        except Exception as exc:
            current = self.status.subscribed_symbols
            if self.event_logger is not None:
                self.event_logger.log(
                    "session_list_subscribed_books_failed",
                    username=self.username,
                    error=repr(exc),
                )

        current_set = set(current)
        missing = tuple(symbol for symbol in requested if symbol not in current_set)
        if not missing:
            self.status.subscribed_symbols = current
            return True

        resubscribed: list[str] = list(current)
        for symbol in missing:
            try:
                self.trader.sub_order_book(symbol)
            except Exception as exc:
                if self.event_logger is not None:
                    self.event_logger.log(
                        "session_resubscribe_failed",
                        username=self.username,
                        symbol=symbol,
                        error=repr(exc),
                    )
                continue
            resubscribed.append(symbol)
        self.status.subscribed_symbols = tuple(dict.fromkeys(resubscribed))
        if self.event_logger is not None:
            self.event_logger.log(
                "session_resubscribe_books",
                symbols=missing,
                subscribed_symbols=self.status.subscribed_symbols,
            )
        subscribed_set = set(self.status.subscribed_symbols)
        return all(symbol in subscribed_set for symbol in requested)


def build_shift_session(
    trader: TraderLike,
    *,
    username: str,
    initiator_cfg: str | Path,
    event_logger: Optional[EventLoggerLike] = None,
) -> ShiftSession:
    return ShiftSession(
        trader=trader,
        username=username,
        initiator_cfg=Path(initiator_cfg),
        event_logger=event_logger,
    )
