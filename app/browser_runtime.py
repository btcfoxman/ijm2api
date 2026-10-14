"""Start managed profiles on first use and share them across account operations."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .browser import BrowserError, browser_alive, start_managed_browser, stop_managed_browser


class BrowserRuntime:
    def __init__(self, store: Any, settings: Callable[[], dict[str, Any]]) -> None:
        self.store = store
        self.settings = settings
        self._guard = threading.RLock()
        self._locks: dict[int, threading.RLock] = {}
        self._leases: dict[int, int] = {}

    def _lock(self, account_id: int) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(account_id, threading.RLock())

    @staticmethod
    def managed(account: dict[str, Any]) -> bool:
        port = int(account.get("cdp_port") or 0)
        return bool(account.get("managed_profile")) or port in {0, 19300 + int(account["id"])}

    def acquire(self, account: dict[str, Any], *, force_managed: bool = False
                ) -> tuple[dict[str, Any], bool]:
        """Return an account with a live CDP port and whether we own its lease."""
        account_id = int(account["id"])
        if not force_managed and not self.managed(account):
            return account, False
        with self._lock(account_id):
            current = self.store.account(account_id, private=True)
            if not current or current["status"] == "logging_in":
                raise BrowserError("account is in online browser login")
            current_port = int(current.get("cdp_port") or 0)
            if (current.get("managed_profile") and current_port != 19300 + account_id
                    and current_port and browser_alive(current_port)):
                self._leases[account_id] = self._leases.get(account_id, 0) + 1
                return current, True
            settings = self.settings()
            port = start_managed_browser(current,
                chrome=str(settings["chrome_executable"]),
                profiles=Path(str(settings["chrome_user_data_root"])))
            if int(current.get("cdp_port") or 0) != port:
                self.store.update_account(account_id, {"cdp_port": port})
            self._leases[account_id] = self._leases.get(account_id, 0) + 1
            return {**current, "cdp_port": port}, True

    def release(self, account: dict[str, Any], owned: bool) -> None:
        if not owned:
            return
        account_id = int(account["id"])
        with self._lock(account_id):
            count = self._leases.get(account_id, 0)
            if count <= 1:
                self._leases.pop(account_id, None)
            else:
                self._leases[account_id] = count - 1

    @contextmanager
    def lease(self, account: dict[str, Any], *, force_managed: bool = False):
        current, owned = self.acquire(account, force_managed=force_managed)
        try:
            yield current
        finally:
            self.release(current, owned)

    def close_idle(self, account_id: int) -> None:
        with self._lock(account_id):
            self.assert_idle(account_id)
            stop_managed_browser(account_id)

    def assert_idle(self, account_id: int) -> None:
        with self._lock(account_id):
            if self._leases.get(account_id, 0):
                raise BrowserError("account browser is in use by another operation")

    def close_all_idle(self) -> None:
        with self._guard:
            account_ids = list(self._locks)
        for account_id in account_ids:
            try:
                self.close_idle(account_id)
            except BrowserError:
                continue
