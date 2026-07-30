import json
import sqlite3
import threading
import time
from contextlib import closing


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY, sampled_at INTEGER NOT NULL,
  total_hashrate REAL NOT NULL, total_miners INTEGER NOT NULL, block_height INTEGER
);
CREATE INDEX IF NOT EXISTS snapshots_time ON snapshots(sampled_at);
CREATE TABLE IF NOT EXISTS candidates (
  upstream_id INTEGER PRIMARY KEY, height INTEGER NOT NULL, miner_address TEXT NOT NULL,
  worker TEXT NOT NULL, session_id TEXT NOT NULL, block_hash TEXT NOT NULL,
  detected_at TEXT, proof_valid INTEGER NOT NULL, bits TEXT,
  status TEXT NOT NULL, confirmations INTEGER, coinbase_txid TEXT,
  last_checked INTEGER NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, level TEXT NOT NULL,
  kind TEXT NOT NULL, message TEXT NOT NULL, details TEXT
);
CREATE INDEX IF NOT EXISTS events_time ON events(created_at);
"""


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with closing(self._connect()) as db:
            db.executescript(SCHEMA)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def snapshot(self, total_hashrate, total_miners, block_height):
        now = int(time.time())
        with self.lock, closing(self._connect()) as db, db:
            latest = db.execute("SELECT sampled_at FROM snapshots ORDER BY sampled_at DESC LIMIT 1").fetchone()
            if latest and now - latest[0] < 55:
                return
            db.execute(
                "INSERT INTO snapshots(sampled_at,total_hashrate,total_miners,block_height) VALUES(?,?,?,?)",
                (now, total_hashrate, total_miners, block_height),
            )
            db.execute("DELETE FROM snapshots WHERE sampled_at < ?", (now - 90 * 86400,))

    def candidate(self, item):
        with self.lock, closing(self._connect()) as db, db:
            old = db.execute("SELECT status FROM candidates WHERE upstream_id=?", (item["upstream_id"],)).fetchone()
            db.execute("""
                INSERT INTO candidates(upstream_id,height,miner_address,worker,session_id,block_hash,
                  detected_at,proof_valid,bits,status,confirmations,coinbase_txid,last_checked,error)
                VALUES(:upstream_id,:height,:miner_address,:worker,:session_id,:block_hash,
                  :detected_at,:proof_valid,:bits,:status,:confirmations,:coinbase_txid,:last_checked,:error)
                ON CONFLICT(upstream_id) DO UPDATE SET status=excluded.status,
                  confirmations=excluded.confirmations,coinbase_txid=excluded.coinbase_txid,
                  last_checked=excluded.last_checked,error=excluded.error,proof_valid=excluded.proof_valid
            """, item)
            if old is None or old[0] != item["status"]:
                self.event(
                    db, "good" if item["status"] not in ("orphaned", "invalid") else "bad",
                    "block", f'Block {item["height"]}: {item["status"]}',
                    {"hash": item["block_hash"], "worker": item["worker"]},
                )

    @staticmethod
    def event(db, level, kind, message, details=None):
        db.execute(
            "INSERT INTO events(created_at,level,kind,message,details) VALUES(?,?,?,?,?)",
            (int(time.time()), level, kind, message, json.dumps(details or {})),
        )

    def history(self, since):
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT sampled_at,total_hashrate,total_miners,block_height FROM snapshots "
                "WHERE sampled_at>=? ORDER BY sampled_at", (since,)
            )]

    def candidates(self):
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM candidates ORDER BY upstream_id DESC LIMIT 100"
            )]

    def events(self):
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100").fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["details"] = json.loads(item["details"] or "{}")
                result.append(item)
            return result
