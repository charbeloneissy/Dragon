import json
import os
import sqlite3
import threading
import time


class Ledger:
    def __init__(self, path=None):
        self.path = path or os.getenv("LEDGER_PATH", "/tmp/dragon_ledger.sqlite3")
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            path TEXT NOT NULL,
            start_usdt TEXT NOT NULL,
            final_usdt TEXT,
            realized_pnl_usdt TEXT,
            status TEXT NOT NULL,
            error TEXT,
            payload TEXT
        )""")
        self._db.commit()

    def record(self, path, start_usdt, result=None, error=None):
        status = "FILLED" if result and result.get("finished") else "ERROR"
        final = result.get("final_usdt") if result else None
        pnl = result.get("realized_pnl_usdt") if result else None
        with self._lock:
            self._db.execute(
                "INSERT INTO executions(ts,path,start_usdt,final_usdt,realized_pnl_usdt,status,error,payload) VALUES(?,?,?,?,?,?,?,?)",
                (time.time(), json.dumps(list(path)), str(start_usdt), final, pnl, status, str(error) if error else None, json.dumps(result, default=str) if result else None),
            )
            self._db.commit()

    def summary(self):
        with self._lock:
            row = self._db.execute("SELECT COUNT(*), COALESCE(SUM(CAST(realized_pnl_usdt AS REAL)),0) FROM executions WHERE status='FILLED'").fetchone()
            last = self._db.execute("SELECT ts,path,realized_pnl_usdt,status,error FROM executions ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "filled": int(row[0]),
            "realized_pnl_usdt": float(row[1]),
            "last": ({"ts": last[0], "path": json.loads(last[1]), "pnl_usdt": last[2], "status": last[3], "error": last[4]} if last else None),
        }
