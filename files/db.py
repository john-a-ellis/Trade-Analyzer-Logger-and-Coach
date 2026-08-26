# db.py
"""
SQLite persistence layer for the Trade Journal app.

Two tables:
- executions: raw imported fills (from CSV), deduplicated on
  (symbol, timestamp, action, qty, price).
- plans: user-entered trade plans (full CRUD).

Round trips and the journal are derived data (computed from executions +
plans at request time) and are not persisted separately -- they're cheap to
recompute and this avoids keeping a derived table in sync.

Every function opens and closes its own connection (safe for Dash's
multi-threaded dev server; this app's data volumes are small so the
overhead is negligible).
"""

import os
import json
import sqlite3
import datetime as dt
import pandas as pd

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trade_journal.db')

MAX_TARGETS = 5


def _connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path=DB_PATH):
    with _connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                name TEXT,
                timestamp TEXT NOT NULL,
                action TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL NOT NULL,
                currency TEXT,
                commission REAL DEFAULT 0,
                description TEXT,
                imported_at TEXT,
                UNIQUE(symbol, timestamp, action, qty, price)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                plan_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                planned_date TEXT,
                created_at TEXT,
                updated_at TEXT,
                entry_price REAL NOT NULL,
                stop_loss REAL,
                take_profit REAL,
                qty REAL NOT NULL,
                reason TEXT,
                notes TEXT
            )
        """)
        conn.commit()
        _migrate(conn)


def _migrate(conn):
    """Add the targets_json column if it's missing (older DB files), and
    fold any legacy single take_profit value into a one-target list so
    existing plans keep working under the new multi-target model."""
    cols = {row['name'] for row in conn.execute("PRAGMA table_info(plans)").fetchall()}
    if 'targets_json' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN targets_json TEXT DEFAULT '[]'")
        conn.commit()

    # Backfill: legacy rows that have an old take_profit but no targets yet.
    rows = conn.execute(
        "SELECT plan_id, take_profit, targets_json FROM plans "
        "WHERE take_profit IS NOT NULL AND (targets_json IS NULL OR targets_json = '' OR targets_json = '[]')"
    ).fetchall()
    for row in rows:
        targets = [{'price': row['take_profit'], 'qty_pct': 100, 'stop_after': None}]
        conn.execute("UPDATE plans SET targets_json = ? WHERE plan_id = ?",
                     (json.dumps(targets), row['plan_id']))
    if rows:
        conn.commit()


# ------------------------------------------------------------
# Executions: Create (bulk insert w/ dedup), Read, Delete-all
# ------------------------------------------------------------

def insert_executions(df: pd.DataFrame, db_path=DB_PATH) -> int:
    """Insert new fills, skipping ones already present (by unique key).
    Returns the number of rows actually inserted."""
    if df is None or df.empty:
        return 0
    now = dt.datetime.utcnow().isoformat()
    rows = [
        (r['symbol'], r.get('name', r['symbol']), str(r['timestamp']), r['action'],
         float(r['qty']), float(r['price']), r.get('currency', 'CAD'),
         float(r.get('commission', 0) or 0), r.get('description', ''), now)
        for _, r in df.iterrows()
    ]
    with _connect(db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        conn.executemany("""
            INSERT OR IGNORE INTO executions
                (symbol, name, timestamp, action, qty, price, currency, commission, description, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
    return after - before


def fetch_executions(db_path=DB_PATH) -> pd.DataFrame:
    with _connect(db_path) as conn:
        df = pd.read_sql_query("SELECT * FROM executions ORDER BY timestamp", conn)
    if df.empty:
        return df
    df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True, errors='coerce')
    return df


def delete_all_executions(db_path=DB_PATH) -> int:
    with _connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        conn.execute("DELETE FROM executions")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='executions'")
        conn.commit()
    return n


def delete_executions_by_ids(ids, db_path=DB_PATH) -> int:
    if not ids:
        return 0
    with _connect(db_path) as conn:
        q_marks = ','.join('?' * len(ids))
        cur = conn.execute(f"DELETE FROM executions WHERE id IN ({q_marks})", list(ids))
        conn.commit()
        return cur.rowcount


# ------------------------------------------------------------
# Plans: full CRUD
# ------------------------------------------------------------

# Columns actually written by the app going forward. `take_profit` is kept
# in the schema for backward compatibility (old DB files) but is no longer
# populated -- it's superseded by `targets` (a list of up to MAX_TARGETS
# {price, qty_pct, stop_after} dicts, stored as JSON in targets_json).
PLAN_FIELDS = ['plan_id', 'symbol', 'direction', 'planned_date', 'created_at', 'updated_at',
               'entry_price', 'stop_loss', 'targets_json', 'qty', 'reason', 'notes']


def _clean_targets(targets) -> list:
    """Keep only well-formed target rows, capped at MAX_TARGETS."""
    clean = []
    for t in (targets or [])[:MAX_TARGETS]:
        if t is None or t.get('price') in (None, ''):
            continue
        clean.append({
            'price': float(t['price']),
            'qty_pct': float(t['qty_pct']) if t.get('qty_pct') not in (None, '') else None,
            'stop_after': float(t['stop_after']) if t.get('stop_after') not in (None, '') else None,
        })
    return clean


def _row_to_plan(row: dict) -> dict:
    plan = dict(row)
    raw = plan.pop('targets_json', None) or '[]'
    try:
        plan['targets'] = json.loads(raw)
    except (TypeError, ValueError):
        plan['targets'] = []
    return plan


def insert_plan(plan: dict, db_path=DB_PATH):
    plan = dict(plan)
    plan.setdefault('updated_at', plan.get('created_at'))
    plan['targets_json'] = json.dumps(_clean_targets(plan.get('targets')))
    with _connect(db_path) as conn:
        conn.execute("""
            INSERT INTO plans (plan_id, symbol, direction, planned_date, created_at, updated_at,
                                entry_price, stop_loss, targets_json, qty, reason, notes)
            VALUES (:plan_id, :symbol, :direction, :planned_date, :created_at, :updated_at,
                    :entry_price, :stop_loss, :targets_json, :qty, :reason, :notes)
        """, {k: plan.get(k) for k in PLAN_FIELDS})
        conn.commit()


def fetch_plans(db_path=DB_PATH) -> list:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM plans ORDER BY created_at").fetchall()
    return [_row_to_plan(dict(r)) for r in rows]


def update_plan(plan_id: str, fields: dict, db_path=DB_PATH) -> bool:
    """Update the given fields of a plan. Returns True if a row was updated."""
    if not fields:
        return False
    fields = dict(fields)
    if 'targets' in fields:
        fields['targets_json'] = json.dumps(_clean_targets(fields.pop('targets')))
    fields['updated_at'] = dt.datetime.utcnow().isoformat()
    set_clause = ', '.join(f"{k} = :{k}" for k in fields)
    fields['plan_id'] = plan_id
    with _connect(db_path) as conn:
        cur = conn.execute(f"UPDATE plans SET {set_clause} WHERE plan_id = :plan_id", fields)
        conn.commit()
        return cur.rowcount > 0


def delete_plan(plan_id: str, db_path=DB_PATH) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
        conn.commit()
        return cur.rowcount > 0


def delete_all_plans(db_path=DB_PATH) -> int:
    with _connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        conn.execute("DELETE FROM plans")
        conn.commit()
    return n


def counts(db_path=DB_PATH) -> dict:
    with _connect(db_path) as conn:
        n_plans = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        n_exec = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
    return {'plans': n_plans, 'executions': n_exec}
