import sqlite3
from contextlib import closing


class PoolDatabase:
    """Read-only adapter that tolerates TypeORM table-name changes."""

    def __init__(self, path):
        self.path = path

    def _connect(self):
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=2)

    @staticmethod
    def _table_with_columns(connection, required):
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (name,) in tables:
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')}
            if required.issubset(columns):
                return name
        return None

    def blocks(self):
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            table = self._table_with_columns(
                db, {"id", "height", "minerAddress", "worker", "sessionId", "blockData"}
            )
            if not table:
                return []
            rows = db.execute(
                f'SELECT id,height,minerAddress,worker,sessionId,blockData,createdAt '
                f'FROM "{table}" WHERE deletedAt IS NULL ORDER BY id DESC'
            ).fetchall()
            return [dict(row) for row in rows]

    def workers(self):
        with closing(self._connect()) as db:
            db.row_factory = sqlite3.Row
            table = self._table_with_columns(
                db, {"address", "clientName", "sessionId", "bestDifficulty", "hashRate"}
            )
            if not table:
                return []
            rows = db.execute(
                f'SELECT address,clientName,sessionId,userAgent,startTime,bestDifficulty,hashRate,updatedAt '
                f'FROM "{table}" WHERE deletedAt IS NULL ORDER BY updatedAt DESC'
            ).fetchall()
            return [dict(row) for row in rows]
