from __future__ import annotations

import threading
from typing import Any


class LockedTraderProxy:
    """Thread-safe proxy around the SHIFT C++ trader object.

    The trader is accessed from two threads:
    - main thread: OrderRouter (submit_order, submit_cancellation) and
      Reconciler (is_connected, get_waiting_list, get_executed_orders,
      get_portfolio_items, get_portfolio_summary)
    - market-data thread: BookCache.refresh_symbol calls
      get_best_price, get_order_book, get_last_price, get_last_size

    A single RLock serialises all cross-thread calls so the C++ object is
    never accessed concurrently.  RLock (re-entrant) is used so that any
    internal calls within the same thread remain deadlock-free.
    """

    __slots__ = ("_trader", "_lock")

    def __init__(self, trader: Any) -> None:
        self._trader = trader
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # OrderRouter / Reconciler interface (main thread)
    # ------------------------------------------------------------------ #

    def submit_order(self, order: Any) -> None:
        with self._lock:
            self._trader.submit_order(order)

    def submit_cancellation(self, order: Any) -> None:
        with self._lock:
            self._trader.submit_cancellation(order)

    def is_connected(self) -> bool:
        with self._lock:
            return self._trader.is_connected()

    def get_waiting_list(self) -> list:
        with self._lock:
            return self._trader.get_waiting_list()

    def get_executed_orders(self, order_id: str) -> list:
        with self._lock:
            return self._trader.get_executed_orders(order_id)

    def get_portfolio_items(self) -> dict:
        with self._lock:
            return self._trader.get_portfolio_items()

    def get_portfolio_summary(self) -> Any:
        with self._lock:
            return self._trader.get_portfolio_summary()

    # ------------------------------------------------------------------ #
    # BookCache / MarketDataLoop interface (market-data thread)
    # ------------------------------------------------------------------ #

    def get_best_price(self, symbol: str) -> Any:
        with self._lock:
            return self._trader.get_best_price(symbol)

    def get_order_book(self, symbol: str, book_type: Any, max_level: int = 99) -> list:
        with self._lock:
            return self._trader.get_order_book(symbol, book_type, max_level)

    def get_last_price(self, symbol: str) -> float:
        with self._lock:
            return self._trader.get_last_price(symbol)

    def get_last_size(self, symbol: str) -> int:
        with self._lock:
            return self._trader.get_last_size(symbol)

    # ------------------------------------------------------------------ #
    # Fallback for any additional methods on the underlying trader
    # ------------------------------------------------------------------ #

    def __getattr__(self, name: str) -> Any:
        with self._lock:
            return getattr(self._trader, name)
