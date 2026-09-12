"""Tests for the shared SqliteStore base (Phase 9) — the connection
lifecycle every tenant-scoped store now inherits instead of hand-rolling.
"""

from __future__ import annotations

from business_ai.storage import SqliteStore


class _DummyStore(SqliteStore):
    def __init__(self, db_path) -> None:
        super().__init__(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with self._lock, self._db() as conn:
            self._apply_default_pragmas(conn)
            conn.execute("CREATE TABLE IF NOT EXISTS widgets (id TEXT PRIMARY KEY)")
            conn.commit()

    def add(self, widget_id: str) -> None:
        with self._lock, self._db() as conn:
            conn.execute("INSERT INTO widgets (id) VALUES (?)", (widget_id,))
            conn.commit()

    def list_all(self) -> list[str]:
        with self._lock, self._db() as conn:
            return [row["id"] for row in conn.execute("SELECT id FROM widgets").fetchall()]


def test_creates_parent_directory(tmp_path):
    nested = tmp_path / "nested" / "dir" / "widgets.db"
    store = _DummyStore(nested)
    assert nested.parent.is_dir()
    assert store.db_path == nested.resolve()


def test_read_write_round_trip(tmp_path):
    store = _DummyStore(tmp_path / "widgets.db")
    store.add("w1")
    store.add("w2")
    assert sorted(store.list_all()) == ["w1", "w2"]


def test_row_factory_returns_row_objects(tmp_path):
    store = _DummyStore(tmp_path / "widgets.db")
    store.add("w1")
    with store._db() as conn:
        row = conn.execute("SELECT id FROM widgets WHERE id = ?", ("w1",)).fetchone()
        assert row["id"] == "w1"  # sqlite3.Row supports name-based access


def test_persists_across_store_instances(tmp_path):
    db_path = tmp_path / "widgets.db"
    _DummyStore(db_path).add("persisted")
    reopened = _DummyStore(db_path)
    assert reopened.list_all() == ["persisted"]
