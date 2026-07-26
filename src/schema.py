"""schema.py — TYPED relational tables for collected raw data.

One table per data domain, with explicit typed columns and a real timestamp — NOT
a generic JSON blob. Every row carries the moment the data was GENERATED (its own
timestamp column) and `ingested_at` (when we collected it). Lists (insider
transactions, analyst revisions, option bars) are ONE ROW PER RECORD, not JSON.

This is the source of truth for S1 raw data. Consumers query real columns; the
dashboard inspector shows the schema.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from core import DEFAULT_DB                                      # noqa: E402

# domain -> (timestamp column, [other columns]). ticker/name is the entity key.
SCHEMA = {
    "bars": ("date", ["ticker", "open", "high", "low", "close", "volume"]),
    "quotes": ("quote_ts", ["ticker", "price", "session"]),
    "macro": ("date", ["name", "value"]),
    "short_interest": ("settlement_date", ["ticker", "shares_short", "pct_float",
                                           "days_to_cover", "change_pct"]),
    "options_implied": ("snap_date", ["ticker", "underlying", "expiry", "atm_call",
                                      "atm_put", "straddle_pct", "implied_move"]),
    "earnings_reports": ("report_date", ["ticker", "eps_estimate", "eps_reported",
                                         "surprise_pct", "revenue", "net_income",
                                         "revenue_year_ago"]),
    "earnings_calendar": ("snap_date", ["ticker", "next_earnings_ts"]),
    "insider_transactions": ("txn_date", ["ticker", "value", "shares", "position",
                                          "insider", "is_sale"]),
    "analyst_revisions": ("revision_date", ["ticker", "firm", "action",
                                            "from_grade", "to_grade"]),
    "fundamentals": ("publish_date", ["ticker", "period_end", "revenue", "net_income",
                                      "total_equity", "gross_profit", "total_assets",
                                      "free_cash_flow", "trailing_eps",
                                      "shares_outstanding"]),
}

# text vs real typing (everything else defaults REAL if it looks numeric, else TEXT)
_TEXT = {"ticker", "name", "session", "expiry", "position", "insider", "firm",
         "action", "from_grade", "to_grade", "next_earnings_ts", "period_end",
         "date", "quote_ts", "settlement_date", "snap_date", "report_date",
         "txn_date", "revision_date", "publish_date"}
# per-table PRIMARY KEY (entity + timestamp, plus disambiguators for lists)
_PK = {
    "bars": ["ticker", "date"], "quotes": ["ticker", "quote_ts"],
    "macro": ["name", "date"], "short_interest": ["ticker", "settlement_date"],
    "options_implied": ["ticker", "snap_date"],
    "earnings_reports": ["ticker", "report_date"],
    "earnings_calendar": ["ticker", "snap_date"],
    "insider_transactions": ["ticker", "txn_date", "insider", "value"],
    "analyst_revisions": ["ticker", "revision_date", "firm", "action"],
    "fundamentals": ["ticker", "publish_date"],
}


def _coltype(col):
    return "TEXT" if col in _TEXT else "REAL"


def _ddl(table):
    ts, cols = SCHEMA[table]
    allcols = list(dict.fromkeys(cols + [ts]))          # entity+data+timestamp
    defs = [f"{c} {_coltype(c)}" for c in allcols] + ["ingested_at TEXT NOT NULL"]
    pk = ", ".join(_PK[table])
    return f"CREATE TABLE IF NOT EXISTS {table} (\n  " + ",\n  ".join(defs) + \
           f",\n  PRIMARY KEY ({pk})\n);"


class TypedStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.c = sqlite3.connect(Path(db_path))
        self.c.execute("PRAGMA busy_timeout=5000")
        for t in SCHEMA:
            self.c.execute(_ddl(t))
        self.c.commit()

    def _now(self):
        return datetime.now(timezone.utc).isoformat()

    def put(self, table: str, row: dict):
        """Insert/replace one typed row (ingested_at added automatically)."""
        self.put_many(table, [row])

    def put_many(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        ts_col, cols = SCHEMA[table]
        allcols = list(dict.fromkeys(cols + [ts_col])) + ["ingested_at"]
        now = self._now()
        vals = [tuple(r.get(c) for c in allcols[:-1]) + (now,) for r in rows]
        ph = ",".join("?" * len(allcols))
        self.c.executemany(
            f"INSERT OR REPLACE INTO {table} ({','.join(allcols)}) VALUES ({ph})", vals)
        self.c.commit()
        return len(rows)

    # ── read (for the inspector / consumers) ──────────────────────────────────
    def columns(self, table: str) -> list[str]:
        return [r[1] for r in self.c.execute(f"PRAGMA table_info({table})")]

    def rows(self, table: str, ticker: str | None = None, limit: int = 100) -> dict:
        ts_col = SCHEMA[table][0]
        ent = "name" if table == "macro" else "ticker"
        where, args = "", []
        if ticker:
            where = f"WHERE {ent}=?"; args = [ticker]
        cols = self.columns(table)
        q = f"SELECT {','.join(cols)} FROM {table} {where} ORDER BY {ts_col} DESC LIMIT ?"
        data = [dict(zip(cols, r)) for r in self.c.execute(q, args + [limit])]
        return {"table": table, "ts_col": ts_col, "columns": cols, "rows": data}

    def count(self, table: str) -> int:
        return self.c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def coverage(self, table: str) -> dict:
        ts_col = SCHEMA[table][0]
        ent = "name" if table == "macro" else "ticker"
        n, ents, lo, hi, last = self.c.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT {ent}), MIN({ts_col}), MAX({ts_col}), "
            f"MAX(ingested_at) FROM {table}").fetchone()
        return {"table": table, "rows": n, "entities": ents,
                "first": lo, "last": hi, "last_ingested": last}
