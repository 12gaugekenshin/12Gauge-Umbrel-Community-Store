import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from app.pool_db import PoolDatabase


class PoolDatabaseTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite")
        os.close(handle)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE renamed_blocks(
                  id INTEGER, height INTEGER, minerAddress TEXT, worker TEXT, sessionId TEXT,
                  blockData TEXT, createdAt TEXT, updatedAt TEXT, deletedAt TEXT
                );
                CREATE TABLE renamed_clients(
                  address TEXT, clientName TEXT, sessionId TEXT, userAgent TEXT, startTime TEXT,
                  bestDifficulty REAL, hashRate REAL, updatedAt TEXT, deletedAt TEXT
                );
                INSERT INTO renamed_blocks VALUES(7,900000,'bc1qtest','garage','abc12345','00','now','now',NULL);
                INSERT INTO renamed_clients VALUES('bc1qtest','garage','abc12345','Bitaxe','now',1234,500000000000,datetime('now'),NULL);
                INSERT INTO renamed_clients VALUES('bc1qtest','stale','old12345','Bitaxe','old',100,100000000000,'2000-01-01 00:00:00',NULL);
            """)

    def tearDown(self):
        os.unlink(self.path)

    def test_schema_discovery_survives_table_renames(self):
        pool = PoolDatabase(self.path)
        self.assertEqual(pool.blocks()[0]["height"], 900000)
        self.assertEqual(pool.workers()[0]["clientName"], "garage")

    def test_active_workers_exclude_stale_sessions(self):
        workers = PoolDatabase(self.path).workers(active_within_seconds=150)
        self.assertEqual([worker["clientName"] for worker in workers], ["garage"])


if __name__ == "__main__":
    unittest.main()
