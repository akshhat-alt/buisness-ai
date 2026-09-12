"""Shared SQLite connection-management base (Phase 9).

Every tenant-scoped store in this app (leads, tasks, employees, audit,
feedback, SOPs, analytics, sources, automation rules/runs, the WhatsApp
inbox, users) independently hand-rolled the exact same eight lines:
resolve the db path, create its parent directory, hold a thread lock,
open a `sqlite3.Row`-factory connection with a 30s busy timeout, and set
`PRAGMA journal_mode=WAL` once at init. Centralizing it here means (a)
twelve copies of that boilerplate become one, and (b) the day this app
needs a different backend (see ARCHITECTURE.md's "Scaling past one
instance" — Postgres is the natural next step at real multi-tenant
scale), there is exactly ONE place that changes the connection layer,
not twelve store classes each needing their own careful review.

Deliberately NOT a repository/ORM abstraction over each store's own SQL —
every store still writes its own schema and queries exactly as before,
still SQLite dialect, still direct sqlite3 cursors. Only the connection
LIFECYCLE is shared. `usage_limiter.py` deliberately does NOT use this
base — it tunes its own shorter timeout and skips the busy_timeout
pragma entirely as a deliberate hot-path choice (a stuck 30s wait on
every quota check would be worse than a fast failure there), and this
extraction must never quietly erase a real, intentional difference like
that one in the name of consistency.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


class SqliteStore:
    """Base class for a tenant-scoped (or platform-level) SQLite store.

    Subclasses call `super().__init__(db_path)` from their own
    `__init__`, then their own `_init_db()` (unchanged in shape) using
    `self._db()` and `self._apply_default_pragmas(conn)` exactly where
    they used to inline the same two lines.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @contextmanager
    def _db(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _apply_default_pragmas(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
