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


def utc_now():
    """The current UTC time as a naive datetime, the form every stored timestamp
    uses (datetime.utcnow() is deprecated)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


MAX_TARGETS = 5

PSYCH_V2_COLUMNS = ['psych_risk', 'psych_urgency', 'psych_focus']

# Every fill imported before multi-account support (2026-10) came from this one
# Wealthsimple TFSA -- confirmed from the account_id column of the original
# activity exports. Used only to backfill pre-account rows during migration.
LEGACY_ACCOUNT_ID = 'H550363K3CAD'

# Keyed on currency as well as symbol: the same ticker can be a US listing
# (USD) and a TSX listing (CAD) with completely different betas.
BETA_CACHE_DDL = """
    CREATE TABLE IF NOT EXISTS beta_cache (
        symbol TEXT NOT NULL,
        currency TEXT NOT NULL,
        benchmark TEXT NOT NULL,
        lookback_days INTEGER NOT NULL,
        beta REAL,
        error TEXT,
        computed_at TEXT NOT NULL,
        PRIMARY KEY (symbol, currency, benchmark, lookback_days)
    )
"""

DISMISSED_HOLDINGS_DDL = """
    CREATE TABLE IF NOT EXISTS {name} (
        account_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        dismissed_at TEXT NOT NULL,
        PRIMARY KEY (account_id, symbol)
    )
"""
LEGACY_ACCOUNT_TYPE = 'TFSA'

EXECUTIONS_DDL = """
    CREATE TABLE IF NOT EXISTS {name} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        name TEXT,
        timestamp TEXT NOT NULL,
        action TEXT NOT NULL,
        qty REAL NOT NULL,
        price REAL NOT NULL,
        currency TEXT NOT NULL,
        commission REAL DEFAULT 0,
        description TEXT,
        imported_at TEXT,
        UNIQUE(account_id, symbol, currency, timestamp, action, qty, price)
    )
"""


def _connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path=DB_PATH):
    with _connect(db_path) as conn:
        conn.execute(EXECUTIONS_DDL.format(name='executions'))
        # One row per brokerage account (Wealthsimple account_id). Registered
        # automatically on import; nickname is user-editable for display.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                account_id TEXT PRIMARY KEY,
                account_type TEXT,
                nickname TEXT,
                base_currency TEXT NOT NULL DEFAULT 'CAD',
                created_at TEXT NOT NULL
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
        # Manual plan<->round-trip match overrides. rt_id is a deterministic hash of
        # the execution row ids composing that round trip (see app.round_trip_id), not
        # a foreign key into a persisted table -- round trips themselves stay derived,
        # only the user's explicit override of *which* round trip a plan matches to
        # is real state that needs to survive a recompute.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plan_matches (
                plan_id TEXT PRIMARY KEY REFERENCES plans(plan_id) ON DELETE CASCADE,
                rt_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """)
        # Closed-trade locks (2026-11-09): once a plan's trade closes, its match
        # (rt_id) and its result at that moment are kept, so a later plan, import
        # or rule change can't quietly move a finished trade. See app.plan_lock_updates.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plan_locks (
                plan_id TEXT PRIMARY KEY REFERENCES plans(plan_id) ON DELETE CASCADE,
                rt_id TEXT NOT NULL,
                closed_at TEXT,
                locked_at TEXT NOT NULL,
                snapshot_json TEXT
            )
        """)
        # Manual plan<->open-LOT match overrides -- the pyramiding-support
        # equivalent of plan_matches above, but for the still-OPEN phase. Only
        # needed for the residual-ambiguity case (a lot with no
        # deterministic_plan_id: a spillover bucket, a pre-plan blended lot, or
        # a symbol whose fill history couldn't be cleanly split into per-plan
        # buckets -- see app.split_entry_fills_into_lot_buckets) since a
        # cleanly-split lot is already unambiguously tied to its plan at
        # construction time and never needs an override. lot_id is a
        # deterministic hash of the ENTRY fills composing that lot (see
        # app.round_trip_id's pattern, but entry-only so the id stays stable
        # across partial sells while the lot remains open -- unlike rt_id,
        # which also hashes exit fills and is only computed once a round trip
        # is fully closed and immutable). When a lot with an override here
        # closes into a round trip, the caller is responsible for migrating
        # the row into plan_matches and clearing it here (see
        # app._load_initial_state) -- this table only ever describes the open
        # phase, so a stale row here would otherwise silently orphan.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plan_lot_matches (
                plan_id TEXT PRIMARY KEY REFERENCES plans(plan_id) ON DELETE CASCADE,
                lot_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """)
        # Symbols the user has flagged as cash-equivalent parking positions (e.g.
        # BIL during a Risk-Off macro regime) that deliberately never get a trade
        # plan. Suppresses the "Open Positions Needing a Plan" nag and the
        # "Executed Trades Without a Plan" listing for these symbols. No effect on
        # scoring either way -- the journal is built one row per PLAN, so a
        # symbol with no plan was already excluded from every performance metric;
        # this table only quiets the UI prompts that would otherwise invite
        # logging a thesis-less plan just to clear them.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cash_equivalent_symbols (
                symbol TEXT PRIMARY KEY,
                added_at TEXT NOT NULL
            )
        """)
        # Optional dated notes on a still-open plan (e.g. "holding through
        # earnings, thesis unchanged" logged a few days in). Purely a running
        # log for the human -- not read by any scoring logic. Cascades on
        # plan delete since an orphaned note with no plan to attach to is
        # meaningless.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plan_observations (
                obs_id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
                observed_at TEXT NOT NULL,
                note TEXT NOT NULL
            )
        """)
        # Closed round trips the user has chosen to hide from "Executed Trades
        # Without a Plan" on a trade-by-trade basis (e.g. a small odd-lot
        # cleanup fill that will never get a real plan, but isn't a
        # cash-equivalent symbol either). rt_id is app.round_trip_id(...) --
        # the same deterministic hash used by plan_matches -- so the row
        # doesn't need to reference an executions/plans row directly and
        # survives re-import/recompute. This is display-only: it never
        # touches executions, plans, or the open-positions matching logic,
        # so it can't affect scoring or the Open Positions card.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dismissed_round_trips (
                rt_id TEXT PRIMARY KEY,
                dismissed_at TEXT NOT NULL
            )
        """)
        # General day-level journal entries -- market conditions, discipline,
        # macro regime reads, anything not tied to one specific trade (see
        # plan_observations above for the per-plan equivalent). obs_date is the
        # day the entry is ABOUT, separate from created_at (the day it was
        # actually typed), the same distinction plans draw between
        # planned_date and created_at.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_observations (
                obs_id INTEGER PRIMARY KEY AUTOINCREMENT,
                obs_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                note TEXT NOT NULL
            )
        """)
        # One row per week: mark-to-market balance of open positions, its
        # change from the prior week is computed on read (not stored --
        # same "derived, not persisted" principle as round trips/journal, see
        # module docstring) by diffing against the previous week_ending row.
        # user_note and llm_note are filled in independently and often at
        # different times (balance computed first, notes added after), so
        # all three columns are nullable and updated via save_weekly_review's
        # partial-field upsert, not required together.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weekly_reviews (
                week_ending TEXT PRIMARY KEY,
                balance REAL,
                user_note TEXT,
                llm_note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Computed betas (see app.compute_beta), keyed on symbol+benchmark+
        # lookback since a different benchmark or window is a genuinely
        # different number, not an update to the same one. This is the one
        # persisted, timestamped cache anywhere in TAPEs -- everything else
        # (ATR, current price) is fetched live on every call, with "staleness"
        # handled purely by a manual refresh button, no storage at all. Beta
        # needs its own cache because it costs meaningfully more than a
        # single price fetch (a full lookback window of daily history for
        # both the position AND the benchmark, per symbol) and doesn't
        # change intraday, so recomputing it on every click would be a lot of
        # redundant network calls for a number that's still correct from an
        # hour ago. beta is NULL when a computation attempt failed -- `error`
        # holds why, so a bad symbol is cached as "known to fail for now"
        # rather than retried on every single page load.
        conn.execute(BETA_CACHE_DDL)
        # Daily/weekly workflow checklists (see app.DAILY_CHECKLIST_ITEMS /
        # WEEKLY_CHECKLIST_ITEMS) -- one row per date/week, holding the
        # checked item ids as a JSON list (same "small JSON blob in one row"
        # convention as plans.targets_json) rather than one row per item,
        # since the whole checked-set is always read/written together. Item
        # wording itself lives in app.py as constants, not here -- this table
        # only ever stores which ids are checked, so editing the wording
        # later doesn't require a migration.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_checklist_state (
                obs_date TEXT PRIMARY KEY,
                checked_items_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weekly_checklist_state (
                week_ending TEXT PRIMARY KEY,
                checked_items_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Which open lots the user has checked off as "reconciled" on a given
        # day's Plan-vs-Actual Drift Report (see app.compute_plan_drift) --
        # same one-row-per-date/JSON-list-of-ids shape as daily_checklist_state
        # above, for the same reason (only the checked SET needs to persist).
        # Keyed by date, not just lot_id, since "reconciled" is inherently a
        # per-day question -- the report itself is re-run fresh each morning
        # against live price, so yesterday's checkmark shouldn't silently
        # carry over and hide today's re-check of the same still-open lot.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_drift_reconciled (
                obs_date TEXT PRIMARY KEY,
                reconciled_lot_ids_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # The pending stop orders read from a Wealthsimple screenshot for one
        # day's stop review (2026-11-05), as a JSON list of {account_id,
        # symbol, currency, qty, stop, order_type, ...}. Same one-row-per-date
        # shape as daily_drift_reconciled: it's what was placed that day.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_placed_orders (
                obs_date TEXT PRIMARY KEY,
                orders_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                all_matched INTEGER,
                summary TEXT
            )
        """)
        # Holdings-reconciliation discrepancies the user has acknowledged as
        # intentional -- same "per-item hide, display-only, never touches the
        # underlying data" pattern as dismissed_round_trips above. Keyed on
        # (account_id, symbol): a discrepancy is per symbol within one account.
        conn.execute(DISMISSED_HOLDINGS_DDL.format(name='dismissed_holdings_discrepancies'))
        # Daily to-do list (2026-11-02): one-off items, unlike the fixed
        # checklists. An unfinished item carries forward until it's done or
        # dropped, so it's one row per item rather than per date.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                added_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                closed_date TEXT,
                created_at TEXT NOT NULL
            )
        """)
        # User-edited checklist definitions (2026-10-09): name ('daily' /
        # 'weekly') -> ordered JSON list of headings and items. No row means
        # the built-in default in app.py. Checked state stays in
        # daily/weekly_checklist_state, keyed by each item's stable id.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checklist_definitions (
                name TEXT PRIMARY KEY,
                entries_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Small app-wide settings as JSON values (e.g. the cancellation reason
        # list, the stale-plan threshold). No row = the built-in default.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Coaching focus items as tracked actions (2026-10-23): each item in a
        # coaching reply's FOCUS NEXT WEEK block becomes one row, reviewed in a
        # later weekly entry as done / not_done (stays active) / dropped.
        # history_json = [{week, status, note}], one per review.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS weekly_actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_week TEXT NOT NULL,
                seq INTEGER NOT NULL,
                text TEXT NOT NULL,
                rules TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                status_note TEXT,
                reviewed_week TEXT,
                history_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        # Structured Daily Trading Journal (2026-10-23): one entry per date,
        # each template field its own column (lists as JSON). The older
        # free-text notes stay in daily_observations as legacy entries.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_journal_entries (
                obs_date TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                composite REAL, regime TEXT,
                layer_t REAL, layer_v REAL, layer_c REAL, layer_l REAL,
                layer_m REAL, layer_s REAL, layer_b REAL,
                flags_json TEXT, weekly_regime TEXT,
                delta5 REAL, delta20 REAL, delta5_manual INTEGER, delta20_manual INTEGER,
                flicker INTEGER, flicker_manual INTEGER,
                effective_limits TEXT, effective_tactics TEXT, effective_note TEXT,
                fomo_panic TEXT, fomo_extreme INTEGER,
                heat_pct REAL, heat_ceiling REAL, positions INTEGER, beta_pct REAL,
                drift_actions TEXT, execution TEXT, watchlist_json TEXT,
                psych_risk INTEGER, psych_urgency INTEGER, psych_focus INTEGER,
                lesson TEXT, market TEXT, lesson_keep TEXT
            )
        """)
        # Every saved version of a daily or weekly entry's effective regime
        # (2026-11-06), so a fill can be checked against the regime as it stood
        # at that day's open, not as later edited. A new row only when the
        # limits or tactics change. Rows seeded from entries saved before this
        # existed have saved_at = the entry's first save and edited_at = its
        # last save if it was edited (the versions in between weren't kept).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS regime_versions (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                obs_date TEXT NOT NULL,
                saved_at TEXT NOT NULL,
                limits_state TEXT,
                tactics_state TEXT,
                note TEXT,
                edited_at TEXT
            )
        """)
        # Improvement register (2026-11-07): what became of each "what to change"
        # lesson, from a trade (source 'trade', ref = plan_id) or a daily entry
        # ('day', ref = obs_date). The lesson text stays where it was written;
        # a row exists only once it's been classified or given a status.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS improvements (
                source TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                category TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                link TEXT,
                note TEXT,
                adopted_on TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, source_ref)
            )
        """)
        # Daily FX rates (Bank of Canada), e.g. pair 'USDCAD' = CAD per 1 USD.
        # A cache so the page doesn't call the Bank on every load, and a history
        # for later step-2 work (returns with/without FX).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fx_rates (
                pair TEXT NOT NULL,
                date TEXT NOT NULL,
                rate REAL NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (pair, date)
            )
        """)
        # Account balance as of each Wealthsimple holdings export (positions'
        # market value + cash), per currency, no FX. The only source of an
        # account's cash until a cash ledger exists. JSON dicts {currency: amount}.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_snapshots (
                account_id TEXT NOT NULL,
                as_of TEXT NOT NULL,
                positions_json TEXT NOT NULL,
                cash_json TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                PRIMARY KEY (account_id, as_of)
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

    # Draft/committed lifecycle (TAPEs design: a plan can be edited freely until
    # committed; the TradingView MCP overlay is only drawn once committed).
    if 'status' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN status TEXT NOT NULL DEFAULT 'committed'")
        conn.commit()
    if 'committed_at' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN committed_at TEXT")
        # Every pre-existing plan predates the draft concept -- treat it as already committed.
        conn.execute("UPDATE plans SET committed_at = created_at WHERE committed_at IS NULL AND status = 'committed'")
        conn.commit()

    # Provenance of the stop/target levels: were they the ATR-derived default
    # computed at commit time, or a deliberate manual override (e.g. the 3%
    # SSO/QLD-style rule)? NULL means "predates this field / entered manually
    # with no method recorded" -- both existing plans and today's UI, which has
    # no ATR calc yet, fall into that bucket.
    if 'target_method' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN target_method TEXT")
        conn.commit()
    if 'atr_at_commit' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN atr_at_commit REAL")
        conn.commit()
    if 'atr_multiplier' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN atr_multiplier REAL")
        conn.commit()
    # Bars the ATR was read from: '240' (4H), '1D' or '1W'. NULL = daily, the
    # only option before 2026-10-08, so existing plans need no backfill.
    if 'atr_timeframe' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN atr_timeframe TEXT")
        conn.commit()
    # Scale-in campaigns (2026-10-19): plan_type NULL/'scale_out' is the
    # original targets plan; 'scale_in' is a probe plus an add ladder
    # (adds_json: [{price, qty, stop_before}]) exiting on a trail only.
    # trail_start_add: 0 = the trail runs from the probe, k = after add k.
    if 'plan_type' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN plan_type TEXT")
        conn.commit()
    if 'adds_json' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN adds_json TEXT")
        conn.commit()
    if 'trail_start_add' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN trail_start_add INTEGER")
        conn.commit()
    # Day trades (2026-11-01, Strategy 4.5): plan_type 'day_trade' with its
    # written catalyst and named setup.
    for col in ('day_catalyst', 'day_setup'):
        if col not in cols:
            conn.execute(f"ALTER TABLE plans ADD COLUMN {col} TEXT")
            conn.commit()
    # Cancelled plans (2026-10-20): status 'cancelled' is a committed plan
    # abandoned before any fill. Kept, not deleted: the reason, a note, an
    # optional psych reading at cancel time, and the "what would have
    # happened" replay (counterfactual_json, refreshed until it's final).
    for col, kind in (('cancelled_at', 'TEXT'), ('cancel_reason', 'TEXT'), ('cancel_note', 'TEXT'),
                      ('cancel_psych_risk', 'INTEGER'), ('cancel_psych_urgency', 'INTEGER'),
                      ('cancel_psych_focus', 'INTEGER'), ('counterfactual_json', 'TEXT')):
        if col not in cols:
            conn.execute(f"ALTER TABLE plans ADD COLUMN {col} {kind}")
            conn.commit()

    # Structured context around HOW the trade was taken, for later review.
    # Two independent -2..+2 sliding scales (0 = neutral) rather than tags --
    # complacent/impatient and fear/FOMO are each a real spectrum, and a
    # numeric scale (unlike a tag) is directly usable in later scoring/
    # correlation against Plan Score or R-multiple. screenshot_path is a path
    # relative to assets/ (Dash serves that directory directly) to a
    # manually-attached chart image; lessons_learned is meant to be filled in
    # once the trade closes but isn't enforced as such at the schema level.
    if 'psych_impatience' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN psych_impatience INTEGER DEFAULT 0")
        conn.commit()
    if 'psych_fomo' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN psych_fomo INTEGER DEFAULT 0")
        conn.commit()
    if 'screenshot_path' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN screenshot_path TEXT")
        conn.commit()
    if 'lessons_learned' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN lessons_learned TEXT")
        conn.commit()
    # Keep/change lessons (2026-11-07): lessons_learned is "what to change";
    # lesson_keep is "what worked (keep)".
    if 'lesson_keep' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN lesson_keep TEXT")
        conn.commit()

    # A single plan can cover a multi-leg trade -- entry, each scale-out, a
    # deviation from plan -- so psych state belongs on individual
    # observations too, not only once on the plan itself (which still holds
    # the entry-time reading). event_type distinguishes an auto-prompted
    # "exited contrary to plan" entry (see app.py's stop_breached detection)
    # from an ordinary voluntary note; NULL means an ordinary note.
    # Trailing stop: a per-plan OPT-IN ALTERNATIVE to the ladder-only stop_after
    # model above -- defaults to disabled, so every existing plan and every new
    # plan that never touches this checkbox keeps behaving exactly as before.
    # activate_after_target is 1-based, nearest-to-entry order (matches
    # target_results' own ordering in analyze_targets_and_stop); atr_multiplier
    # is the trailing distance once active. Both mechanisms stay permanently
    # available side by side -- this is an added choice, not a migration away
    # from the existing one.
    if 'trailing_stop_enabled' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN trailing_stop_enabled INTEGER DEFAULT 0")
        conn.commit()
    if 'trailing_activate_after_target' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN trailing_activate_after_target INTEGER")
        conn.commit()
    if 'trailing_atr_multiplier' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN trailing_atr_multiplier REAL")
        conn.commit()
    # stop_method supersedes the trailing_stop_enabled switch: 'ladder' |
    # 'atr_trail' | 'ema_trail'. Backfilled from the old switch; the old
    # column is kept (and written in sync) for backward compatibility.
    if 'stop_method' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN stop_method TEXT")
        conn.execute("UPDATE plans SET stop_method = CASE WHEN trailing_stop_enabled = 1 "
                     "THEN 'atr_trail' ELSE 'ladder' END")
        conn.commit()

    # Explicit, user-set currency for this plan's own prices (entry/stop/targets) --
    # a plan predates any execution match, so there's no other authoritative source
    # for it (executions.currency only exists once a real fill is imported). Backfill
    # existing plans best-effort from the most recent execution for that same symbol
    # (if the symbol has ever actually been traded); a plan whose symbol has never
    # been executed stays NULL rather than guessing -- the app defaults new plans'
    # currency picker to 'USD' but that's a UI default, not something to silently
    # stamp onto old rows as if it were known.
    if 'currency' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN currency TEXT")
        conn.commit()
        rows = conn.execute("SELECT plan_id, symbol FROM plans WHERE currency IS NULL").fetchall()
        for row in rows:
            match = conn.execute(
                "SELECT currency FROM executions WHERE symbol = ? AND currency IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT 1", (row['symbol'],)
            ).fetchone()
            if match:
                conn.execute("UPDATE plans SET currency = ? WHERE plan_id = ?",
                             (match['currency'], row['plan_id']))
        conn.commit()

    # Psych model v2: three axes (see app.PSYCH_AXES) plus a multi-select entry
    # trigger. Deliberately NULL (no default) so rows that predate v2 read as
    # "not rated" rather than a false "neutral" -- the old two-axis readings
    # (psych_impatience/psych_fomo) mix dimensions and can't be mapped reliably,
    # so they're kept untouched as legacy history instead of converted.
    for col in PSYCH_V2_COLUMNS:
        if col not in cols:
            conn.execute(f"ALTER TABLE plans ADD COLUMN {col} INTEGER")
            conn.commit()
    if 'trade_triggers_json' not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN trade_triggers_json TEXT")
        conn.commit()

    obs_cols = {row['name'] for row in conn.execute("PRAGMA table_info(plan_observations)").fetchall()}
    if 'psych_impatience' not in obs_cols:
        conn.execute("ALTER TABLE plan_observations ADD COLUMN psych_impatience INTEGER DEFAULT 0")
        conn.commit()
    if 'psych_fomo' not in obs_cols:
        conn.execute("ALTER TABLE plan_observations ADD COLUMN psych_fomo INTEGER DEFAULT 0")
        conn.commit()
    if 'event_type' not in obs_cols:
        conn.execute("ALTER TABLE plan_observations ADD COLUMN event_type TEXT")
        conn.commit()
    for col in PSYCH_V2_COLUMNS:
        if col not in obs_cols:
            conn.execute(f"ALTER TABLE plan_observations ADD COLUMN {col} INTEGER")
            conn.commit()

    _migrate_accounts(conn)

    # Per-account tracking start date (2026-10-07): unplanned round trips that
    # closed before it are archived (hidden), never deleted. NULL = no archive.
    # Weekly balance currency (2026-10-08): from then on the balance is saved
    # in CAD (balance_currency='CAD', per-currency detail as JSON). Older rows
    # stay NULL = summed across currencies as-is, so a week-over-week change
    # is only computed between rows on the same basis.
    wr_cols = {r['name'] for r in conn.execute("PRAGMA table_info(weekly_reviews)").fetchall()}
    if wr_cols and 'balance_currency' not in wr_cols:
        conn.execute("ALTER TABLE weekly_reviews ADD COLUMN balance_currency TEXT")
        conn.execute("ALTER TABLE weekly_reviews ADD COLUMN balance_detail_json TEXT")
        conn.commit()

    # Weekly template (2026-10-23): the structured weekly entry's fields live on
    # the week's weekly_reviews row; lists as JSON. entry_saved_at marks a row
    # whose template has been saved (so the form loads it instead of pre-fills).
    wr_cols = {r['name'] for r in conn.execute("PRAGMA table_info(weekly_reviews)").fetchall()}
    for col, typ in WEEKLY_ENTRY_COLUMNS:
        if wr_cols and col not in wr_cols:
            conn.execute(f"ALTER TABLE weekly_reviews ADD COLUMN {col} {typ}")
    conn.commit()

    acct_cols = {r['name'] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if 'tracking_start_date' not in acct_cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN tracking_start_date TEXT")
        conn.commit()

    # Stop review outcome (2026-11-05): all_matched drives the daily checklist
    # item that ticks itself (see app.STOP_REVIEW_AUTO); summary is one line.
    po_cols = {r['name'] for r in conn.execute("PRAGMA table_info(daily_placed_orders)").fetchall()}
    for col, typ in (('all_matched', 'INTEGER'), ('summary', 'TEXT')):
        if po_cols and col not in po_cols:
            conn.execute(f"ALTER TABLE daily_placed_orders ADD COLUMN {col} {typ}")
    conn.commit()
    dj_cols = {r['name'] for r in conn.execute("PRAGMA table_info(daily_journal_entries)").fetchall()}
    if dj_cols and 'lesson_keep' not in dj_cols:
        conn.execute("ALTER TABLE daily_journal_entries ADD COLUMN lesson_keep TEXT")
        conn.commit()
    # Improvement register links (2026-11-07): what a lesson became is a list of
    # {kind: practice | strategy | action | tapes, ref}. The old free-text link
    # moves into the note so nothing is lost.
    imp_cols = {r['name'] for r in conn.execute("PRAGMA table_info(improvements)").fetchall()}
    if imp_cols and 'became_json' not in imp_cols:
        conn.execute("ALTER TABLE improvements ADD COLUMN became_json TEXT DEFAULT '[]'")
        for r in conn.execute("SELECT source, source_ref, link, note FROM improvements "
                              "WHERE link IS NOT NULL AND link != ''").fetchall():
            note = f"Became: {r['link']}" + (f" -- {r['note']}" if r['note'] else "")
            conn.execute("UPDATE improvements SET note = ?, link = NULL WHERE source = ? AND source_ref = ?",
                         (note, r['source'], r['source_ref']))
        conn.commit()
    _seed_regime_versions(conn)


def _seed_regime_versions(conn):
    """One version for each daily/weekly entry that has none (entries saved
    before regime_versions existed). Every later save records its own."""
    tables = {r['name'] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {'regime_versions', 'daily_journal_entries', 'weekly_reviews'} <= tables:
        return
    have = {(r['source'], r['obs_date']) for r in
            conn.execute("SELECT DISTINCT source, obs_date FROM regime_versions")}
    for r in conn.execute("SELECT obs_date, created_at, updated_at, effective_limits, effective_tactics, "
                          "effective_note FROM daily_journal_entries").fetchall():
        if ('daily', r['obs_date']) not in have and r['effective_limits']:
            edited = r['updated_at'] if r['updated_at'] and r['updated_at'] != r['created_at'] else None
            conn.execute("INSERT INTO regime_versions (source, obs_date, saved_at, limits_state, tactics_state, "
                         "note, edited_at) VALUES ('daily', ?, ?, ?, ?, ?, ?)",
                         (r['obs_date'], r['created_at'], r['effective_limits'], r['effective_tactics'],
                          r['effective_note'], edited))
    wr_cols = {r['name'] for r in conn.execute("PRAGMA table_info(weekly_reviews)").fetchall()}
    if 'posture_limits' in wr_cols:
        for r in conn.execute("SELECT week_ending, entry_saved_at, posture_limits, posture_tactics, posture_note "
                              "FROM weekly_reviews WHERE entry_saved_at IS NOT NULL").fetchall():
            if ('weekly', r['week_ending']) not in have and r['posture_limits']:
                conn.execute("INSERT INTO regime_versions (source, obs_date, saved_at, limits_state, "
                             "tactics_state, note) VALUES ('weekly', ?, ?, ?, ?, ?)",
                             (r['week_ending'], r['entry_saved_at'], r['posture_limits'], r['posture_tactics'],
                              r['posture_note']))
    conn.commit()


def _record_regime_version(conn, source, obs_date, limits_state, tactics_state, note, saved_at):
    """Add a version when the limits or tactics differ from the latest one."""
    if not limits_state:
        return
    last = conn.execute("SELECT limits_state, tactics_state FROM regime_versions WHERE source = ? AND obs_date = ? "
                        "ORDER BY saved_at DESC, version_id DESC LIMIT 1", (source, obs_date)).fetchone()
    if last and (last['limits_state'], last['tactics_state']) == (limits_state, tactics_state):
        return
    conn.execute("INSERT INTO regime_versions (source, obs_date, saved_at, limits_state, tactics_state, note) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (source, obs_date, saved_at, limits_state, tactics_state, note))


def fetch_regime_versions(db_path=DB_PATH) -> list:
    """Every saved version of a daily/weekly effective regime, oldest first."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM regime_versions ORDER BY saved_at, version_id").fetchall()
    return [dict(r) for r in rows]


def _clean_became(became):
    out = []
    for b in became or []:
        kind, ref = (b or {}).get('kind'), str((b or {}).get('ref') or '').strip()
        if kind in ('practice', 'strategy', 'action', 'tapes') and ref and {'kind': kind, 'ref': ref} not in out:
            out.append({'kind': kind, 'ref': ref})
    return out


def _improvement_dict(row):
    d = dict(row)
    try:
        d['became'] = _clean_became(json.loads(d.pop('became_json', None) or '[]'))
    except (TypeError, ValueError):
        d['became'] = []
    return d


def fetch_improvements(db_path=DB_PATH) -> list:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM improvements ORDER BY updated_at").fetchall()
    return [_improvement_dict(r) for r in rows]


def save_improvement(source: str, source_ref: str, category=None, status='new', became=None, note=None,
                     adopted_on=None, db_path=DB_PATH) -> dict:
    """Insert or replace one lesson's classification. became: what it turned into, a list
    of {kind: practice | strategy | action | tapes, ref}. adopted_on is kept from the
    existing row unless given, and set to today the first time the status is adopted
    or verified."""
    now = utc_now().isoformat(timespec='seconds')
    with _connect(db_path) as conn:
        old = conn.execute("SELECT adopted_on FROM improvements WHERE source = ? AND source_ref = ?",
                           (source, source_ref)).fetchone()
        adopted_on = adopted_on or (old['adopted_on'] if old else None)
        if status in ('adopted', 'verified') and not adopted_on:
            adopted_on = now[:10]
        conn.execute("INSERT OR REPLACE INTO improvements (source, source_ref, category, status, link, note, "
                     "adopted_on, updated_at, became_json) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                     (source, source_ref, category or None, status or 'new', note or None,
                      adopted_on, now, json.dumps(_clean_became(became))))
        conn.commit()
        row = conn.execute("SELECT * FROM improvements WHERE source = ? AND source_ref = ?",
                           (source, source_ref)).fetchone()
    return _improvement_dict(row)


def fetch_fx_history(pair: str, db_path=DB_PATH) -> list:
    """[{date, rate}] for a pair, oldest first."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT date, rate FROM fx_rates WHERE pair = ? ORDER BY date", (pair,)).fetchall()
    return [dict(r) for r in rows]


def _backup_before_migration(conn, label):
    """Online copy of the whole database next to it, before a migration that
    rebuilds tables. Skipped for in-memory databases. Returns the backup path."""
    db_file = conn.execute("PRAGMA database_list").fetchone()['file']
    if not db_file:
        return None
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = f"{os.path.splitext(db_file)[0]}.backup-{label}-{stamp}.db"
    conn.commit()
    with sqlite3.connect(backup_path) as dst:
        conn.backup(dst)
    return backup_path


def _migrate_accounts(conn):
    """Multi-account support (2026-10). One-time, behind an automatic backup:
    - executions gains account_id (pre-account rows are the legacy TFSA) and a
      UNIQUE key that includes account_id and currency. The table is rebuilt
      because SQLite can't alter a UNIQUE constraint, and every existing `id`
      is kept: round_trip_id / lot_id are hashes of execution ids, so
      plan_matches / plan_lot_matches depend on them.
    - plans gains account_id (backfilled to the legacy TFSA).
    - dismissed_holdings_discrepancies is re-keyed (account_id, symbol).
    - beta_cache is re-keyed with currency (a cache: dropped and recreated)."""
    exec_cols = {r['name'] for r in conn.execute("PRAGMA table_info(executions)").fetchall()}
    if 'account_id' not in exec_cols:
        _backup_before_migration(conn, 'pre-accounts')
        old_seq = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'executions'").fetchone()
        conn.execute("BEGIN")
        try:
            conn.execute(EXECUTIONS_DDL.format(name='executions_new'))
            conn.execute("""
                INSERT INTO executions_new (id, account_id, symbol, name, timestamp, action, qty, price,
                                            currency, commission, description, imported_at)
                SELECT id, ?, symbol, name, timestamp, action, qty, price,
                       COALESCE(currency, 'CAD'), commission, description, imported_at
                FROM executions
            """, (LEGACY_ACCOUNT_ID,))
            conn.execute("DROP TABLE executions")
            conn.execute("ALTER TABLE executions_new RENAME TO executions")
            if old_seq is not None:
                conn.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'executions'",
                             (old_seq['seq'],))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    has_history = conn.execute("SELECT EXISTS(SELECT 1 FROM executions) OR "
                               "EXISTS(SELECT 1 FROM plans)").fetchone()[0]
    if has_history:
        conn.execute("INSERT OR IGNORE INTO accounts (account_id, account_type, nickname, created_at) "
                     "VALUES (?, ?, ?, ?)",
                     (LEGACY_ACCOUNT_ID, LEGACY_ACCOUNT_TYPE, LEGACY_ACCOUNT_TYPE,
                      utc_now().isoformat()))
        conn.commit()

    plan_cols = {r['name'] for r in conn.execute("PRAGMA table_info(plans)").fetchall()}
    if 'account_id' not in plan_cols:
        conn.execute("ALTER TABLE plans ADD COLUMN account_id TEXT")
        conn.execute("UPDATE plans SET account_id = ? WHERE account_id IS NULL", (LEGACY_ACCOUNT_ID,))
        conn.commit()

    dis_cols = {r['name'] for r in conn.execute(
        "PRAGMA table_info(dismissed_holdings_discrepancies)").fetchall()}
    if 'account_id' not in dis_cols:
        conn.execute(DISMISSED_HOLDINGS_DDL.format(name='dismissed_holdings_new'))
        conn.execute("INSERT INTO dismissed_holdings_new (account_id, symbol, dismissed_at) "
                     "SELECT ?, symbol, dismissed_at FROM dismissed_holdings_discrepancies",
                     (LEGACY_ACCOUNT_ID,))
        conn.execute("DROP TABLE dismissed_holdings_discrepancies")
        conn.execute("ALTER TABLE dismissed_holdings_new RENAME TO dismissed_holdings_discrepancies")
        conn.commit()

    beta_cols = {r['name'] for r in conn.execute("PRAGMA table_info(beta_cache)").fetchall()}
    if 'currency' not in beta_cols:
        conn.execute("DROP TABLE beta_cache")
        conn.execute(BETA_CACHE_DDL)
        conn.commit()


# ------------------------------------------------------------
# Accounts
# ------------------------------------------------------------

def upsert_account(account_id: str, account_type: str = None, db_path=DB_PATH):
    """Register an account seen on import (no-op if known). A new account's
    nickname defaults to its type, e.g. 'RRSP'; two of the same type get the
    last 4 characters of the id appended so they stay distinguishable."""
    with _connect(db_path) as conn:
        if conn.execute("SELECT 1 FROM accounts WHERE account_id = ?", (account_id,)).fetchone():
            return
        nickname = account_type or account_id
        if account_type and conn.execute("SELECT 1 FROM accounts WHERE account_type = ?",
                                         (account_type,)).fetchone():
            nickname = f"{account_type} …{account_id[-4:]}"
        conn.execute("INSERT INTO accounts (account_id, account_type, nickname, created_at) "
                     "VALUES (?, ?, ?, ?)",
                     (account_id, account_type, nickname, utc_now().isoformat()))
        conn.commit()


def fetch_accounts(db_path=DB_PATH) -> list:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT account_id, account_type, nickname, base_currency, tracking_start_date "
                            "FROM accounts ORDER BY created_at, account_id").fetchall()
    return [dict(r) for r in rows]


def fetch_checklist_definition(name: str, db_path=DB_PATH):
    """The saved entries for a checklist, or None if it's never been edited."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT entries_json FROM checklist_definitions WHERE name = ?", (name,)).fetchone()
    return json.loads(row['entries_json']) if row else None


def save_checklist_definition(name: str, entries: list, db_path=DB_PATH):
    with _connect(db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO checklist_definitions (name, entries_json, updated_at) "
                     "VALUES (?, ?, ?)", (name, json.dumps(entries), utc_now().isoformat()))
        conn.commit()


def get_setting(key: str, default=None, db_path=DB_PATH):
    """An app setting's saved value, or `default` if it's never been set."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT value_json FROM app_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row['value_json']) if row else default


def set_setting(key: str, value, db_path=DB_PATH):
    with _connect(db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value_json, updated_at) VALUES (?, ?, ?)",
                     (key, json.dumps(value), utc_now().isoformat()))
        conn.commit()


def reset_checklist_definition(name: str, db_path=DB_PATH):
    """Back to the built-in default (the saved definition is removed)."""
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM checklist_definitions WHERE name = ?", (name,))
        conn.commit()


def save_fx_rate(pair: str, date: str, rate: float, db_path=DB_PATH):
    with _connect(db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO fx_rates (pair, date, rate, fetched_at) VALUES (?, ?, ?, ?)",
                     (pair, date, float(rate), utc_now().isoformat()))
        conn.commit()


def fetch_latest_fx_rate(pair: str, db_path=DB_PATH):
    """The newest stored rate for a pair: {date, rate, fetched_at}, or None."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT date, rate, fetched_at FROM fx_rates WHERE pair = ? "
                           "ORDER BY date DESC, fetched_at DESC LIMIT 1", (pair,)).fetchone()
    return dict(row) if row else None


def save_account_snapshot(account_id: str, as_of: str, positions: dict, cash: dict, db_path=DB_PATH):
    """Record one account's balance from a holdings export. Re-uploading the
    same export (same as_of) replaces it rather than duplicating."""
    with _connect(db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO account_snapshots "
                     "(account_id, as_of, positions_json, cash_json, imported_at) VALUES (?, ?, ?, ?, ?)",
                     (account_id, as_of, json.dumps(positions), json.dumps(cash),
                      utc_now().isoformat()))
        conn.commit()


def fetch_latest_account_snapshots(db_path=DB_PATH) -> dict:
    """{account_id: {as_of, positions, cash, imported_at}} -- the newest
    snapshot per account (by as_of)."""
    with _connect(db_path) as conn:
        rows = conn.execute("""
            SELECT s.* FROM account_snapshots s
            JOIN (SELECT account_id, MAX(as_of) AS as_of FROM account_snapshots GROUP BY account_id) m
              ON s.account_id = m.account_id AND s.as_of = m.as_of
        """).fetchall()
    return {r['account_id']: {'as_of': r['as_of'], 'positions': json.loads(r['positions_json']),
                              'cash': json.loads(r['cash_json']), 'imported_at': r['imported_at']}
            for r in rows}


def set_account_tracking_start(account_id: str, start_date: str = None, db_path=DB_PATH) -> bool:
    """Set (YYYY-MM-DD) or clear (None/'') an account's tracking start date."""
    with _connect(db_path) as conn:
        cur = conn.execute("UPDATE accounts SET tracking_start_date = ? WHERE account_id = ?",
                           ((start_date or '')[:10] or None, account_id))
        conn.commit()
        return cur.rowcount > 0


def set_account_nickname(account_id: str, nickname: str, db_path=DB_PATH) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("UPDATE accounts SET nickname = ? WHERE account_id = ?",
                           ((nickname or '').strip() or account_id, account_id))
        conn.commit()
        return cur.rowcount > 0


# ------------------------------------------------------------
# Executions: Create (bulk insert w/ dedup), Read, Delete-all
# ------------------------------------------------------------

def insert_executions(df: pd.DataFrame, db_path=DB_PATH, account_id: str = None) -> int:
    """Insert new fills, skipping ones already present (by unique key, which
    includes the account and currency). Each row's own `account_id` column
    wins; `account_id` is the fallback for parsers without one (generic CSVs).
    Raises ValueError if a row ends up with no account. Returns the number of
    rows actually inserted."""
    if df is None or df.empty:
        return 0
    if df.columns.duplicated().any():
        # A duplicate-named column makes df['col'] return a Series instead of a
        # scalar per row, which sqlite3 can't bind ("Error binding parameter 1:
        # type 'Series' is not supported"). Callers should already dedupe at parse
        # time; this is a defensive backstop, not the primary fix.
        df = df.loc[:, ~df.columns.duplicated()]
    now = utc_now().isoformat()
    rows = []
    for _, r in df.iterrows():
        acct = r.get('account_id') if 'account_id' in df.columns else None
        acct = acct if isinstance(acct, str) and acct.strip() else account_id
        if not acct:
            raise ValueError("Every execution needs an account -- pick one for this import.")
        rows.append((acct.strip(), r['symbol'], r.get('name', r['symbol']), str(r['timestamp']), r['action'],
                     float(r['qty']), float(r['price']), r.get('currency') or 'CAD',
                     float(r.get('commission', 0) or 0), r.get('description', ''), now))
    with _connect(db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
        conn.executemany("""
            INSERT OR IGNORE INTO executions
                (account_id, symbol, name, timestamp, action, qty, price, currency, commission,
                 description, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
               'entry_price', 'stop_loss', 'targets_json', 'qty', 'reason', 'notes',
               'status', 'committed_at', 'target_method', 'atr_at_commit', 'atr_multiplier',
               'atr_timeframe', 'trailing_stop_enabled', 'trailing_activate_after_target', 'trailing_atr_multiplier',
               'stop_method', 'account_id', 'currency', 'psych_impatience', 'psych_fomo',
               'psych_risk', 'psych_urgency', 'psych_focus', 'trade_triggers_json',
               'screenshot_path', 'lessons_learned', 'lesson_keep', 'plan_type', 'adds_json', 'trail_start_add',
               'day_catalyst', 'day_setup']


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
            'r_multiple': float(t['r_multiple']) if t.get('r_multiple') not in (None, '') else None,
        })
    return clean


def _clean_adds(adds) -> list:
    """Keep only well-formed add rows (a trigger price), capped at MAX_TARGETS."""
    clean = []
    for a in (adds or [])[:MAX_TARGETS]:
        if a is None or a.get('price') in (None, ''):
            continue
        clean.append({
            'price': float(a['price']),
            'qty': float(a['qty']) if a.get('qty') not in (None, '') else None,
            'stop_before': float(a['stop_before']) if a.get('stop_before') not in (None, '') else None,
        })
    return clean


def _clean_triggers(triggers) -> list:
    return [str(t) for t in (triggers or []) if t]


def _row_to_plan(row: dict) -> dict:
    plan = dict(row)
    raw = plan.pop('targets_json', None) or '[]'
    try:
        plan['targets'] = json.loads(raw)
    except (TypeError, ValueError):
        plan['targets'] = []
    raw_adds = plan.pop('adds_json', None)
    try:
        plan['adds'] = json.loads(raw_adds) if raw_adds else []
    except (TypeError, ValueError):
        plan['adds'] = []
    raw_cf = plan.pop('counterfactual_json', None)
    try:
        plan['counterfactual'] = json.loads(raw_cf) if raw_cf else None
    except (TypeError, ValueError):
        plan['counterfactual'] = None
    raw_triggers = plan.pop('trade_triggers_json', None)
    try:
        plan['trade_triggers'] = json.loads(raw_triggers) if raw_triggers else []
    except (TypeError, ValueError):
        plan['trade_triggers'] = []
    return plan


def insert_plan(plan: dict, db_path=DB_PATH):
    plan = dict(plan)
    plan.setdefault('updated_at', plan.get('created_at'))
    plan['targets_json'] = json.dumps(_clean_targets(plan.get('targets')))
    plan['trade_triggers_json'] = json.dumps(_clean_triggers(plan.get('trade_triggers')))
    plan['adds_json'] = json.dumps(_clean_adds(plan.get('adds')))
    plan.setdefault('psych_impatience', 0)
    plan.setdefault('psych_fomo', 0)
    plan.setdefault('stop_method', 'atr_trail' if plan.get('trailing_stop_enabled') else 'ladder')
    plan['trailing_stop_enabled'] = 0 if plan['stop_method'] == 'ladder' else 1
    plan.setdefault('currency', 'USD')
    # The app has no draft-staging UI yet -- clicking "Add Trade Plan" finalizes it
    # immediately, so default straight to 'committed' unless the caller says otherwise.
    plan.setdefault('status', 'committed')
    if plan['status'] == 'committed':
        plan.setdefault('committed_at', plan.get('created_at'))
    with _connect(db_path) as conn:
        conn.execute("""
            INSERT INTO plans (plan_id, symbol, direction, planned_date, created_at, updated_at,
                                entry_price, stop_loss, targets_json, qty, reason, notes,
                                status, committed_at, target_method, atr_at_commit, atr_multiplier,
                                plan_type, adds_json, trail_start_add,
                                atr_timeframe, trailing_stop_enabled, trailing_activate_after_target,
                                trailing_atr_multiplier, stop_method, account_id, currency,
                                psych_impatience, psych_fomo,
                                psych_risk, psych_urgency, psych_focus, trade_triggers_json,
                                screenshot_path, lessons_learned, lesson_keep, day_catalyst, day_setup)
            VALUES (:plan_id, :symbol, :direction, :planned_date, :created_at, :updated_at,
                    :entry_price, :stop_loss, :targets_json, :qty, :reason, :notes,
                    :status, :committed_at, :target_method, :atr_at_commit, :atr_multiplier,
                    :plan_type, :adds_json, :trail_start_add,
                    :atr_timeframe, :trailing_stop_enabled, :trailing_activate_after_target,
                    :trailing_atr_multiplier, :stop_method, :account_id, :currency,
                    :psych_impatience, :psych_fomo,
                    :psych_risk, :psych_urgency, :psych_focus, :trade_triggers_json,
                    :screenshot_path, :lessons_learned, :lesson_keep, :day_catalyst, :day_setup)
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
    if 'trade_triggers' in fields:
        fields['trade_triggers_json'] = json.dumps(_clean_triggers(fields.pop('trade_triggers')))
    if 'adds' in fields:
        fields['adds_json'] = json.dumps(_clean_adds(fields.pop('adds')))
    if 'counterfactual' in fields:
        cf = fields.pop('counterfactual')
        fields['counterfactual_json'] = json.dumps(cf) if cf is not None else None
    fields['updated_at'] = utc_now().isoformat()
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


# ------------------------------------------------------------
# Plan <-> round-trip manual match overrides
#
# The automatic matcher (app.match_plans_to_roundtrips) re-runs its greedy
# nearest-date guess on every recompute; these rows are the user's explicit
# correction when that guess is wrong, and take priority over it. rt_id is
# app.round_trip_id(...) -- deterministic given the same set of execution
# fills, so the link survives a re-import/recompute.
# ------------------------------------------------------------

def set_plan_match(plan_id: str, rt_id: str, db_path=DB_PATH):
    """Pin a plan to a specific round trip, replacing any existing match on
    either side (a plan matches at most one round trip and vice versa)."""
    now = utc_now().isoformat()
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM plan_matches WHERE plan_id = ? OR rt_id = ?", (plan_id, rt_id))
        conn.execute("INSERT INTO plan_matches (plan_id, rt_id, created_at) VALUES (?, ?, ?)",
                     (plan_id, rt_id, now))
        conn.commit()


def clear_plan_match(plan_id: str, db_path=DB_PATH) -> bool:
    """Remove a manual match override, letting the plan fall back to auto-matching."""
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM plan_matches WHERE plan_id = ?", (plan_id,))
        conn.commit()
        return cur.rowcount > 0


def save_plan_lock(plan_id: str, rt_id: str, closed_at=None, snapshot=None, db_path=DB_PATH):
    """Lock a closed trade's match and its result (replaces any earlier lock)."""
    with _connect(db_path) as conn:
        conn.execute("INSERT OR REPLACE INTO plan_locks (plan_id, rt_id, closed_at, locked_at, snapshot_json) "
                     "VALUES (?, ?, ?, ?, ?)",
                     (plan_id, rt_id, closed_at, utc_now().isoformat(), json.dumps(snapshot or {})))
        conn.commit()


def delete_plan_lock(plan_id: str, db_path=DB_PATH) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM plan_locks WHERE plan_id = ?", (plan_id,))
        conn.commit()
        return cur.rowcount > 0


def fetch_plan_locks(db_path=DB_PATH) -> dict:
    """{plan_id: {rt_id, closed_at, locked_at, snapshot}}."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM plan_locks").fetchall()
    return {r['plan_id']: {'rt_id': r['rt_id'], 'closed_at': r['closed_at'], 'locked_at': r['locked_at'],
                           'snapshot': json.loads(r['snapshot_json'] or '{}')} for r in rows}


def fetch_plan_matches(db_path=DB_PATH) -> dict:
    """Returns {plan_id: rt_id} for all manually-pinned matches."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT plan_id, rt_id FROM plan_matches").fetchall()
    return {r['plan_id']: r['rt_id'] for r in rows}


def set_plan_lot_match(plan_id: str, lot_id: str, db_path=DB_PATH):
    """Pin a plan to a specific open lot, replacing any existing match on
    either side (a plan matches at most one lot and vice versa) -- exact
    mirror of set_plan_match for the open-lot phase (see plan_lot_matches
    table comment in init_db)."""
    now = utc_now().isoformat()
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM plan_lot_matches WHERE plan_id = ? OR lot_id = ?", (plan_id, lot_id))
        conn.execute("INSERT INTO plan_lot_matches (plan_id, lot_id, created_at) VALUES (?, ?, ?)",
                     (plan_id, lot_id, now))
        conn.commit()


def clear_plan_lot_match(plan_id: str, db_path=DB_PATH) -> bool:
    """Remove a manual lot-match override, letting the plan fall back to auto-matching."""
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM plan_lot_matches WHERE plan_id = ?", (plan_id,))
        conn.commit()
        return cur.rowcount > 0


def fetch_plan_lot_matches(db_path=DB_PATH) -> dict:
    """Returns {plan_id: lot_id} for all manually-pinned open-lot matches."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT plan_id, lot_id FROM plan_lot_matches").fetchall()
    return {r['plan_id']: r['lot_id'] for r in rows}


def counts(db_path=DB_PATH) -> dict:
    with _connect(db_path) as conn:
        n_plans = conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        n_exec = conn.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
    return {'plans': n_plans, 'executions': n_exec}


# ------------------------------------------------------------
# Cash-equivalent symbol exemptions (e.g. BIL parking positions --
# see the cash_equivalent_symbols table comment in init_db)
# ------------------------------------------------------------

def add_cash_equivalent_symbol(symbol: str, db_path=DB_PATH):
    symbol = symbol.strip().upper()
    now = utc_now().isoformat()
    with _connect(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO cash_equivalent_symbols (symbol, added_at) VALUES (?, ?)",
                     (symbol, now))
        conn.commit()


def remove_cash_equivalent_symbol(symbol: str, db_path=DB_PATH) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM cash_equivalent_symbols WHERE symbol = ?", (symbol.strip().upper(),))
        conn.commit()
        return cur.rowcount > 0


def fetch_cash_equivalent_symbols(db_path=DB_PATH) -> list:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT symbol FROM cash_equivalent_symbols ORDER BY symbol").fetchall()
    return [r['symbol'] for r in rows]


# ------------------------------------------------------------
# Plan observations: optional dated notes on a plan, e.g. logged daily
# while a position is open (see plan_observations table comment in init_db)
# ------------------------------------------------------------

OBSERVATION_COLUMNS = ("obs_id, plan_id, observed_at, note, psych_impatience, psych_fomo, "
                       "psych_risk, psych_urgency, psych_focus, event_type")


def add_plan_observation(plan_id: str, note: str, psych_risk: int = 0, psych_urgency: int = 0,
                          psych_focus: int = 0, event_type: str = None, db_path=DB_PATH) -> dict:
    """event_type='deviation' marks an entry logged in response to the
    auto-detected "exited contrary to plan" prompt (see app.py's
    stop_breached); None is an ordinary voluntary note. Writes only the v2
    psych axes -- the legacy psych_impatience/psych_fomo columns keep their
    schema default and are only read for pre-v2 history."""
    now = utc_now().isoformat()
    note = (note or '').strip()
    values = {'psych_risk': psych_risk or 0, 'psych_urgency': psych_urgency or 0,
              'psych_focus': psych_focus or 0}
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO plan_observations (plan_id, observed_at, note, psych_risk, psych_urgency, "
            "psych_focus, event_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (plan_id, now, note, values['psych_risk'], values['psych_urgency'],
             values['psych_focus'], event_type))
        conn.commit()
        return {'obs_id': cur.lastrowid, 'plan_id': plan_id, 'observed_at': now, 'note': note,
                'psych_impatience': 0, 'psych_fomo': 0, **values, 'event_type': event_type}


def fetch_plan_observations(plan_id: str, db_path=DB_PATH) -> list:
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {OBSERVATION_COLUMNS} "
            "FROM plan_observations WHERE plan_id = ? ORDER BY observed_at DESC", (plan_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def fetch_deviation_logged_plan_ids(db_path=DB_PATH) -> set:
    """Plan ids that already have a 'deviation' observation logged, so the
    auto-detected "exited contrary to plan" prompt (app.py's render_journal)
    stops nagging once answered."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT plan_id FROM plan_observations WHERE event_type = 'deviation'"
        ).fetchall()
    return {r['plan_id'] for r in rows}


def delete_plan_observation(obs_id: int, db_path=DB_PATH) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM plan_observations WHERE obs_id = ?", (obs_id,))
        conn.commit()
        return cur.rowcount > 0


# ------------------------------------------------------------
# Dismissed round trips: per-trade hide from "Executed Trades Without a
# Plan" (see dismissed_round_trips table comment in init_db)
# ------------------------------------------------------------

def dismiss_round_trip(rt_id: str, db_path=DB_PATH):
    now = utc_now().isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO dismissed_round_trips (rt_id, dismissed_at) VALUES (?, ?)",
            (rt_id, now))
        conn.commit()


def undismiss_round_trip(rt_id: str, db_path=DB_PATH) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM dismissed_round_trips WHERE rt_id = ?", (rt_id,))
        conn.commit()
        return cur.rowcount > 0


def fetch_dismissed_round_trips(db_path=DB_PATH) -> list:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT rt_id FROM dismissed_round_trips").fetchall()
    return [r['rt_id'] for r in rows]


# ------------------------------------------------------------
# Daily (general, not-trade-specific) journal entries -- see
# daily_observations table comment in init_db
# ------------------------------------------------------------

def add_daily_observation(note: str, obs_date: str = None, db_path=DB_PATH) -> dict:
    now = utc_now().isoformat()
    note = (note or '').strip()
    obs_date = obs_date or now[:10]
    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO daily_observations (obs_date, created_at, note) VALUES (?, ?, ?)",
            (obs_date, now, note))
        conn.commit()
        return {'obs_id': cur.lastrowid, 'obs_date': obs_date, 'created_at': now, 'note': note}


DJ_JSON_FIELDS = {'flags_json': 'flags', 'watchlist_json': 'watchlist'}
DJ_COLUMNS = ['obs_date', 'created_at', 'updated_at', 'composite', 'regime',
              'layer_t', 'layer_v', 'layer_c', 'layer_l', 'layer_m', 'layer_s', 'layer_b',
              'flags_json', 'weekly_regime', 'delta5', 'delta20', 'delta5_manual', 'delta20_manual',
              'flicker', 'flicker_manual', 'effective_limits', 'effective_tactics', 'effective_note',
              'fomo_panic', 'fomo_extreme', 'heat_pct', 'heat_ceiling', 'positions', 'beta_pct',
              'drift_actions', 'execution', 'watchlist_json',
              'psych_risk', 'psych_urgency', 'psych_focus', 'lesson', 'market', 'lesson_keep']


def _dj_row(row: dict) -> dict:
    entry = dict(row)
    for col, key in DJ_JSON_FIELDS.items():
        raw = entry.pop(col, None)
        try:
            entry[key] = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            entry[key] = []
    return entry


def fetch_daily_journal_entries(db_path=DB_PATH) -> list:
    """Structured daily journal entries, newest date first ('flags' and
    'watchlist' decoded from JSON)."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM daily_journal_entries ORDER BY obs_date DESC").fetchall()
    return [_dj_row(dict(r)) for r in rows]


def save_daily_journal_entry(entry: dict, db_path=DB_PATH) -> dict:
    """Insert or replace the entry for entry['obs_date']. created_at is kept
    from an existing entry for that date (so a later edit doesn't hide when
    it was first written); updated_at is now. Returns the stored entry."""
    entry = dict(entry)
    now = utc_now().isoformat(timespec='seconds')
    with _connect(db_path) as conn:
        old = conn.execute("SELECT created_at FROM daily_journal_entries WHERE obs_date = ?",
                           (entry['obs_date'],)).fetchone()
        entry['created_at'] = old['created_at'] if old else (entry.get('created_at') or now)
        entry['updated_at'] = now
        for col, key in DJ_JSON_FIELDS.items():
            entry[col] = json.dumps(entry.pop(key, None) or [])
        cols = ', '.join(DJ_COLUMNS)
        conn.execute(f"INSERT OR REPLACE INTO daily_journal_entries ({cols}) VALUES "
                     f"({', '.join(':' + c for c in DJ_COLUMNS)})", {c: entry.get(c) for c in DJ_COLUMNS})
        _record_regime_version(conn, 'daily', entry['obs_date'], entry.get('effective_limits'),
                               entry.get('effective_tactics'), entry.get('effective_note'), now)
        conn.commit()
        row = conn.execute("SELECT * FROM daily_journal_entries WHERE obs_date = ?",
                           (entry['obs_date'],)).fetchone()
    return _dj_row(dict(row))


def delete_daily_journal_entry(obs_date: str, db_path=DB_PATH) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM daily_journal_entries WHERE obs_date = ?", (obs_date,))
        conn.execute("DELETE FROM regime_versions WHERE source = 'daily' AND obs_date = ?", (obs_date,))
        conn.commit()
        return cur.rowcount > 0


def fetch_daily_observations(db_path=DB_PATH) -> list:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT obs_id, obs_date, created_at, note FROM daily_observations "
            "ORDER BY obs_date DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_daily_observation(obs_id: int, db_path=DB_PATH) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM daily_observations WHERE obs_id = ?", (obs_id,))
        conn.commit()
        return cur.rowcount > 0


def fetch_all_observations(db_path=DB_PATH) -> list:
    """Every plan observation across every plan, for trend analysis (see
    app.compute_weekly_trend_context) -- fetch_plan_observations is scoped to
    one plan at a time, which is right for the Notes/Observations modal but
    wrong here where the whole history needs scanning at once."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT {OBSERVATION_COLUMNS} "
            "FROM plan_observations ORDER BY observed_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------
# Weekly reviews: one row per week (mark-to-market balance + two freeform
# notes), see weekly_reviews table comment in init_db
# ------------------------------------------------------------

def save_weekly_review(week_ending: str, db_path=DB_PATH, **fields) -> dict:
    """Create the week's row if it doesn't exist yet, else update only the
    given fields -- balance, user_note, llm_note are each typically filled in
    at different times (balance computed first, notes added after), so this
    never requires all three at once. Returns the resulting full row."""
    now = utc_now().isoformat()
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM weekly_reviews WHERE week_ending = ?", (week_ending,)
        ).fetchone()
        if existing:
            if fields:
                set_clause = ', '.join(f"{k} = :{k}" for k in fields)
                params = dict(fields, week_ending=week_ending, updated_at=now)
                conn.execute(
                    f"UPDATE weekly_reviews SET {set_clause}, updated_at = :updated_at "
                    "WHERE week_ending = :week_ending", params)
        else:
            params = dict(fields, week_ending=week_ending, created_at=now, updated_at=now)
            cols = ', '.join(params.keys())
            placeholders = ', '.join(f':{k}' for k in params.keys())
            conn.execute(f"INSERT INTO weekly_reviews ({cols}) VALUES ({placeholders})", params)
        row = conn.execute(
            "SELECT * FROM weekly_reviews WHERE week_ending = ?", (week_ending,)
        ).fetchone()
        if 'posture_limits' in fields and row['entry_saved_at']:
            _record_regime_version(conn, 'weekly', week_ending, row['posture_limits'], row['posture_tactics'],
                                   row['posture_note'], now)
        conn.commit()
    return dict(row)


WEEKLY_ENTRY_COLUMNS = [
    ('entry_saved_at', 'TEXT'),
    ('weekly_regime', 'TEXT'), ('weekly_composite', 'REAL'),
    ('daily_regime', 'TEXT'), ('daily_composite', 'REAL'),
    ('delta20', 'REAL'), ('delta20_manual', 'INTEGER'), ('flags_json', 'TEXT'),
    ('posture_limits', 'TEXT'), ('posture_tactics', 'TEXT'), ('posture_note', 'TEXT'),
    ('heat_pct', 'REAL'), ('positions', 'INTEGER'), ('beta_pct', 'REAL'),
    ('largest_theme', 'TEXT'), ('largest_theme_pct', 'REAL'),
    ('results_json', 'TEXT'), ('lesson_decisions_json', 'TEXT'),
    ('graduations', 'TEXT'), ('watchlist_json', 'TEXT'),
    ('strategy_changes', 'TEXT'), ('market', 'TEXT'),
]
WEEKLY_JSON_FIELDS = {'flags_json': 'flags', 'results_json': 'results',
                      'lesson_decisions_json': 'lesson_decisions', 'watchlist_json': 'watchlist'}


def weekly_entry_fields(entry: dict) -> dict:
    """The weekly template's fields as weekly_reviews columns (lists/dicts
    JSON-encoded), ready for save_weekly_review(**fields)."""
    out = {}
    for col, _ in WEEKLY_ENTRY_COLUMNS:
        key = WEEKLY_JSON_FIELDS.get(col, col)
        if key in entry:
            out[col] = json.dumps(entry[key]) if col in WEEKLY_JSON_FIELDS else entry[key]
    return out


def decode_weekly_entry(row: dict) -> dict:
    """A weekly_reviews row with the template's JSON columns decoded."""
    row = dict(row)
    for col, key in WEEKLY_JSON_FIELDS.items():
        raw = row.pop(col, None)
        empty = {} if key == 'results' else []
        try:
            row[key] = json.loads(raw) if raw else empty
        except (TypeError, ValueError):
            row[key] = empty
    return row


def _action_row(r) -> dict:
    a = dict(r)
    try:
        a['history'] = json.loads(a.pop('history_json') or '[]')
    except (TypeError, ValueError):
        a['history'] = []
    return a


def fetch_weekly_actions(db_path=DB_PATH) -> list:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM weekly_actions ORDER BY source_week, seq").fetchall()
    return [_action_row(r) for r in rows]


def replace_focus_actions(source_week: str, items: list, db_path=DB_PATH) -> list:
    """Store a coaching reply's focus items as actions for source_week.
    Re-saving the reply keeps an action whose text is unchanged (so its id
    stays stable), drops not-yet-reviewed ones no longer in the reply, keeps
    every reviewed one, and adds the new items. Returns the week's actions."""
    now = utc_now().isoformat(timespec='seconds')
    wanted = {it['text'].strip().lower() for it in items}
    with _connect(db_path) as conn:
        for r in conn.execute("SELECT action_id, text FROM weekly_actions WHERE source_week = ? "
                              "AND reviewed_week IS NULL", (source_week,)).fetchall():
            if r['text'].strip().lower() not in wanted:
                conn.execute("DELETE FROM weekly_actions WHERE action_id = ?", (r['action_id'],))
        kept = {r['text'].strip().lower() for r in conn.execute(
            "SELECT text FROM weekly_actions WHERE source_week = ?", (source_week,)).fetchall()}
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM weekly_actions WHERE source_week = ?",
                           (source_week,)).fetchone()[0]
        for it in items:
            if it['text'].strip().lower() in kept:
                continue
            seq += 1
            conn.execute("INSERT INTO weekly_actions (source_week, seq, text, rules, status, history_json, "
                         "created_at, updated_at) VALUES (?, ?, ?, ?, 'open', '[]', ?, ?)",
                         (source_week, seq, it['text'].strip(), it.get('rules'), now, now))
        conn.commit()
        rows = conn.execute("SELECT * FROM weekly_actions WHERE source_week = ? ORDER BY seq",
                            (source_week,)).fetchall()
    return [_action_row(r) for r in rows]


def review_weekly_action(action_id: int, week: str, status: str, note: str = None, db_path=DB_PATH) -> dict:
    """Record a review of an action in `week`: status done / not_done /
    dropped (not_done keeps it active, so it comes up again next week).
    Re-reviewing in the same week replaces that week's history entry."""
    now = utc_now().isoformat(timespec='seconds')
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM weekly_actions WHERE action_id = ?", (action_id,)).fetchone()
        if row is None:
            raise ValueError(f"no action {action_id}")
        history = [h for h in _action_row(row)['history'] if h.get('week') != week]
        history.append({'week': week, 'status': status, 'note': note or None})
        history.sort(key=lambda h: h['week'])
        conn.execute("UPDATE weekly_actions SET status = ?, status_note = ?, reviewed_week = ?, "
                     "history_json = ?, updated_at = ? WHERE action_id = ?",
                     (status, note or None, week, json.dumps(history), now, action_id))
        conn.commit()
        row = conn.execute("SELECT * FROM weekly_actions WHERE action_id = ?", (action_id,)).fetchone()
    return _action_row(row)


def fetch_weekly_reviews(db_path=DB_PATH) -> list:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM weekly_reviews ORDER BY week_ending DESC").fetchall()
    return [dict(r) for r in rows]


def delete_weekly_review(week_ending: str, db_path=DB_PATH) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM weekly_reviews WHERE week_ending = ?", (week_ending,))
        conn.commit()
        return cur.rowcount > 0


# ------------------------------------------------------------
# Beta cache: computed betas, keyed on symbol+benchmark+lookback (see
# beta_cache table comment in init_db)
# ------------------------------------------------------------

def upsert_beta_cache(symbol: str, benchmark: str, lookback_days: int, beta, error=None,
                      currency: str = '', db_path=DB_PATH):
    """Cache one instrument's computed beta (beta=None, error=<message> if the
    computation failed) against a specific benchmark+lookback combination.
    currency distinguishes a US listing from a TSX listing of the same ticker."""
    now = utc_now().isoformat()
    currency = currency or ''
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO beta_cache (symbol, currency, benchmark, lookback_days, beta, error, "
            "computed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (symbol, currency, benchmark, lookback_days, beta, error, now))
        conn.commit()


def fetch_beta_cache(symbol: str, benchmark: str, lookback_days: int, currency: str = '', db_path=DB_PATH):
    """One cached beta entry, or None if this instrument+benchmark+lookback has
    never been computed. Returns {'beta', 'error', 'computed_at'}."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT beta, error, computed_at FROM beta_cache "
            "WHERE symbol = ? AND currency = ? AND benchmark = ? AND lookback_days = ?",
            (symbol, currency or '', benchmark, lookback_days)).fetchone()
    return dict(row) if row else None


def update_daily_observation(obs_id: int, note: str = None, obs_date: str = None, db_path=DB_PATH) -> bool:
    """Correct an entry logged in error (wrong date, or typed in haste) --
    note and/or obs_date; pass only what changed."""
    fields = {}
    if note is not None:
        fields['note'] = note.strip()
    if obs_date is not None:
        fields['obs_date'] = obs_date
    if not fields:
        return False
    set_clause = ', '.join(f"{k} = :{k}" for k in fields)
    fields['obs_id'] = obs_id
    with _connect(db_path) as conn:
        cur = conn.execute(f"UPDATE daily_observations SET {set_clause} WHERE obs_id = :obs_id", fields)
        conn.commit()
        return cur.rowcount > 0


# ------------------------------------------------------------
# Daily/weekly workflow checklists (see daily_checklist_state /
# weekly_checklist_state table comments in init_db). Both tables share the
# same shape (one JSON-list-of-checked-item-ids row per key), so the two
# pairs of functions below are intentionally near-identical rather than a
# shared helper parameterized by table name -- there's no third checklist on
# the horizon to justify that abstraction yet.
# ------------------------------------------------------------

def fetch_daily_checklist_state(obs_date: str, db_path=DB_PATH) -> list:
    """Checked item ids for one date, or [] if nothing's been checked yet."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT checked_items_json FROM daily_checklist_state WHERE obs_date = ?",
            (obs_date,)).fetchone()
    return json.loads(row['checked_items_json']) if row else []


def save_daily_checklist_state(obs_date: str, checked_items: list, db_path=DB_PATH):
    now = utc_now().isoformat()
    checked_items_json = json.dumps(sorted(set(checked_items)))
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM daily_checklist_state WHERE obs_date = ?", (obs_date,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE daily_checklist_state SET checked_items_json = ?, updated_at = ? "
                "WHERE obs_date = ?", (checked_items_json, now, obs_date))
        else:
            conn.execute(
                "INSERT INTO daily_checklist_state (obs_date, checked_items_json, updated_at) "
                "VALUES (?, ?, ?)", (obs_date, checked_items_json, now))
        conn.commit()


def fetch_checklist_state_dates(name: str, limit: int = 30, db_path=DB_PATH) -> list:
    """Dates (daily) or week_endings (weekly) with saved ticks, newest first."""
    table, col = (('daily_checklist_state', 'obs_date') if name == 'daily'
                  else ('weekly_checklist_state', 'week_ending'))
    with _connect(db_path) as conn:
        rows = conn.execute(f"SELECT {col} AS d FROM {table} WHERE checked_items_json != '[]' "
                            f"ORDER BY {col} DESC LIMIT ?", (limit,)).fetchall()
    return [r['d'] for r in rows]


def fetch_weekly_checklist_state(week_ending: str, db_path=DB_PATH) -> list:
    """Checked item ids for one week_ending, or [] if nothing's been checked yet."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT checked_items_json FROM weekly_checklist_state WHERE week_ending = ?",
            (week_ending,)).fetchone()
    return json.loads(row['checked_items_json']) if row else []


def save_weekly_checklist_state(week_ending: str, checked_items: list, db_path=DB_PATH):
    now = utc_now().isoformat()
    checked_items_json = json.dumps(sorted(set(checked_items)))
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM weekly_checklist_state WHERE week_ending = ?", (week_ending,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE weekly_checklist_state SET checked_items_json = ?, updated_at = ? "
                "WHERE week_ending = ?", (checked_items_json, now, week_ending))
        else:
            conn.execute(
                "INSERT INTO weekly_checklist_state (week_ending, checked_items_json, updated_at) "
                "VALUES (?, ?, ?)", (week_ending, checked_items_json, now))
        conn.commit()


def fetch_daily_drift_reconciled(obs_date: str, db_path=DB_PATH) -> list:
    """Reconciled lot ids for one date's Drift Report, or [] if nothing's
    been checked off yet (see daily_drift_reconciled table comment in init_db)."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT reconciled_lot_ids_json FROM daily_drift_reconciled WHERE obs_date = ?",
            (obs_date,)).fetchone()
    return json.loads(row['reconciled_lot_ids_json']) if row else []


def save_daily_drift_reconciled(obs_date: str, lot_ids: list, db_path=DB_PATH):
    now = utc_now().isoformat()
    lot_ids_json = json.dumps(sorted(set(lot_ids)))
    with _connect(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM daily_drift_reconciled WHERE obs_date = ?", (obs_date,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE daily_drift_reconciled SET reconciled_lot_ids_json = ?, updated_at = ? "
                "WHERE obs_date = ?", (lot_ids_json, now, obs_date))
        else:
            conn.execute(
                "INSERT INTO daily_drift_reconciled (obs_date, reconciled_lot_ids_json, updated_at) "
                "VALUES (?, ?, ?)", (obs_date, lot_ids_json, now))
        conn.commit()


def fetch_daily_placed_orders(obs_date: str, db_path=DB_PATH) -> list:
    """The pending orders saved by that day's stop review, or [] if none."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT orders_json FROM daily_placed_orders WHERE obs_date = ?",
                           (obs_date,)).fetchone()
    return json.loads(row['orders_json']) if row else []


def save_daily_placed_orders(obs_date: str, orders: list, all_matched: bool = None, summary: str = None,
                             db_path=DB_PATH):
    with _connect(db_path) as conn:
        conn.execute("INSERT INTO daily_placed_orders (obs_date, orders_json, updated_at, all_matched, summary) "
                     "VALUES (?, ?, ?, ?, ?) ON CONFLICT(obs_date) DO UPDATE SET orders_json = excluded.orders_json, "
                     "updated_at = excluded.updated_at, all_matched = excluded.all_matched, "
                     "summary = excluded.summary",
                     (obs_date, json.dumps(orders or []), utc_now().isoformat(),
                      None if all_matched is None else int(bool(all_matched)), summary))
        conn.commit()


def fetch_stop_review(obs_date: str, db_path=DB_PATH):
    """That day's stop review outcome: {checked_at (UTC), all_matched, summary,
    orders}, or None if no review was saved."""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM daily_placed_orders WHERE obs_date = ?", (obs_date,)).fetchone()
    if not row:
        return None
    return {'checked_at': row['updated_at'], 'all_matched': None if row['all_matched'] is None
            else bool(row['all_matched']), 'summary': row['summary'], 'orders': json.loads(row['orders_json'])}


def fetch_stop_reviews(db_path=DB_PATH) -> list:
    """Every saved stop review, oldest first: [{obs_date, checked_at (UTC), orders}]."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT obs_date, updated_at, orders_json FROM daily_placed_orders "
                            "ORDER BY updated_at").fetchall()
    return [{'obs_date': r['obs_date'], 'checked_at': r['updated_at'], 'orders': json.loads(r['orders_json'])}
            for r in rows]


# ------------------------------------------------------------
# Daily to-do list (see todos table comment in init_db)
# ------------------------------------------------------------

def fetch_todos(db_path=DB_PATH) -> list:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM todos ORDER BY added_date, id").fetchall()
    return [dict(row) for row in rows]


def add_todo(text: str, added_date: str, db_path=DB_PATH) -> int:
    with _connect(db_path) as conn:
        cur = conn.execute("INSERT INTO todos (text, added_date, created_at) VALUES (?, ?, ?)",
                           (text, added_date, utc_now().isoformat()))
        conn.commit()
        return cur.lastrowid


def set_todo_status(todo_id: int, status: str, closed_date: str = None, db_path=DB_PATH):
    """status is 'open', 'done' or 'dropped'; closed_date is None for 'open'."""
    with _connect(db_path) as conn:
        conn.execute("UPDATE todos SET status = ?, closed_date = ? WHERE id = ?", (status, closed_date, todo_id))
        conn.commit()


# ------------------------------------------------------------
# Dismissed holdings-reconciliation discrepancies (see
# dismissed_holdings_discrepancies table comment in init_db)
# ------------------------------------------------------------

def dismiss_holdings_discrepancy(account_id: str, symbol: str, db_path=DB_PATH):
    now = utc_now().isoformat()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO dismissed_holdings_discrepancies (account_id, symbol, dismissed_at) "
            "VALUES (?, ?, ?)", (account_id, symbol, now))
        conn.commit()


def undismiss_holdings_discrepancy(account_id: str, symbol: str, db_path=DB_PATH) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM dismissed_holdings_discrepancies WHERE account_id = ? AND symbol = ?",
                           (account_id, symbol))
        conn.commit()
        return cur.rowcount > 0


def fetch_dismissed_holdings_discrepancies(db_path=DB_PATH) -> list:
    """List of 'ACCOUNT|SYMBOL' keys (strings, so they round-trip through a
    JSON Dash store)."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT account_id, symbol FROM dismissed_holdings_discrepancies").fetchall()
    return [f"{r['account_id']}|{r['symbol']}" for r in rows]
