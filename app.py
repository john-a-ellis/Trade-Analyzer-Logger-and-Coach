# app.py
"""
TAPEs -- Trading Analysis, Performance and Evaluation System ("reading the tape")
----------------------------------------------------------------------------------
A Plotly Dash app for logging stock trade plans (entry, stop-loss, take-profit,
reasoning) and automatically matching them against actual executions imported
from a Wealthsimple activity export, so you can see how well you stuck to the
plan and how profitable each trade actually was.

Key ideas:
- Executions (individual fills from the CSV) are grouped into "round trips" per
  symbol using FIFO lot matching: a round trip starts at the first BUY after a
  flat position and ends when the position returns to flat via one or more
  SELLs. This correctly handles partial fills and scaling in/out.
- Trade Plans are entered manually (symbol, direction, entry, stop, target,
  qty, reason, notes). Stop-loss, take-profit, reason and notes are optional --
  only symbol, direction, entry price and qty are required.
- Plans are auto-matched to the closest-in-time round trip for the same
  symbol/direction. Executed trades with no matching plan show up as
  "Trades without a plan" -- you can log a plan retroactively with one click.
- Scoring compares what actually happened to what was planned:
    * Actual $ P&L and % return (always available)
    * R-multiple (P&L relative to planned risk), if a stop was set
    * Plan Score = actual P&L / planned reward * 100, if a take-profit was set
    * Execution notes: whether the exit breached the stop or reached the
      target, and how far the actual entry deviated from the planned entry.
"""

import io
import os
import re
import json
import time
import uuid
import glob
import base64
import hashlib
import urllib.request
import db
import datetime as dt

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, dcc, html, dash_table, Input, Output, State, ALL, callback_context, no_update
from dash.mcp import mcp_enabled, configure_mcp_server
import dash_bootstrap_components as dbc
from dash.exceptions import PreventUpdate

# ==============================================================
# 1. CSV parsing
# ==============================================================

# Wealthsimple has exported two different shapes of this column at different
# times: a single 'effective_at' timestamp, or separate 'effective_date' +
# 'effective_time' columns. Both are accepted; REQUIRED_WS_COLS covers
# everything else that's the same in both.
REQUIRED_WS_COLS = {
    'settlement_date', 'activity_type', 'activity_sub_type',
    'direction', 'symbol', 'name', 'currency', 'quantity', 'unit_price', 'commission'
}


def _missing_ws_cols(columns) -> set:
    """REQUIRED_WS_COLS, plus a check that at least one 'when did this happen'
    column shape is present ('effective_at', or 'effective_date' with
    'effective_time')."""
    columns = set(columns)
    missing = REQUIRED_WS_COLS - columns
    has_effective_at = 'effective_at' in columns
    has_split_effective = {'effective_date', 'effective_time'}.issubset(columns)
    if not has_effective_at and not has_split_effective:
        missing.add('effective_at (or effective_date + effective_time)')
    return missing


def decode_upload(contents: str) -> str:
    """Strip the dcc.Upload data-URL header and return raw text."""
    import base64
    _, content_string = contents.split(',', 1)
    decoded = base64.b64decode(content_string)
    return decoded.decode('utf-8', errors='replace')


EXECUTION_COLUMNS = ['account_id', 'account_type', 'symbol', 'name', 'timestamp', 'action', 'qty',
                     'price', 'currency', 'commission', 'description']


def _clean_str_col(df, col):
    """A string column with blanks/NaN as None (missing column -> all None)."""
    if col not in df.columns:
        return pd.Series([None] * len(df), index=df.index, dtype=object)
    s = df[col].astype(object).where(df[col].notna(), None)
    return s.map(lambda v: (str(v).strip() or None) if v is not None else None)


def parse_wealthsimple_csv(contents: str) -> pd.DataFrame:
    """
    Parse a Wealthsimple activity export and return one row per executed fill.

    Returns columns: account_id, account_type, symbol, name, timestamp, action
    (BUY/SELL), qty, price, currency, commission, description. One export can
    cover several accounts; every row keeps its own account_id.

    Note: uses `activity_sub_type` (BUY/SELL) rather than `direction`, since
    Wealthsimple exports `direction` as "LONG" for both opening and closing
    fills -- it does not indicate buy vs. sell.
    """
    text = decode_upload(contents)
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip().lower() for c in df.columns]
    # Two raw headers that only differ in case/whitespace (e.g. "Symbol" and
    # "symbol ") collapse to the same name above -- keep just the first so a
    # later `trades['symbol']` lookup returns a scalar per row, not a Series
    # (which SQLite can't bind: "Error binding parameter 1: type 'Series' is
    # not supported").
    df = df.loc[:, ~df.columns.duplicated()]

    missing = _missing_ws_cols(df.columns)
    if missing:
        raise ValueError(f"CSV is missing expected Wealthsimple columns: {sorted(missing)}")

    trades = df[df['activity_type'] == 'Trade'].copy()
    if trades.empty:
        return pd.DataFrame(columns=EXECUTION_COLUMNS)

    if 'effective_at' in trades.columns:
        trades['timestamp'] = pd.to_datetime(trades['effective_at'], errors='coerce', utc=True)
    else:
        # Newer export shape: date and time of day in separate columns.
        trades['timestamp'] = pd.to_datetime(
            trades['effective_date'].astype(str) + ' ' + trades['effective_time'].astype(str),
            errors='coerce', utc=True,
        )
    fallback = pd.to_datetime(trades['settlement_date'], errors='coerce', utc=True)
    trades['timestamp'] = trades['timestamp'].fillna(fallback)

    trades['action'] = trades['activity_sub_type'].astype(str).str.upper().str.strip()
    trades = trades[trades['action'].isin(['BUY', 'SELL'])].copy()

    trades['qty'] = pd.to_numeric(trades['quantity'], errors='coerce').abs()
    trades['price'] = pd.to_numeric(trades['unit_price'], errors='coerce')
    trades['commission'] = pd.to_numeric(trades['commission'], errors='coerce').fillna(0)
    trades['symbol'] = trades['symbol'].astype(str).str.strip()
    trades['name'] = trades.get('name', trades['symbol']).fillna(trades['symbol'])
    trades['currency'] = trades.get('currency', 'CAD').fillna('CAD').astype(str).str.strip().str.upper()
    trades['description'] = trades.get('description', '')
    trades['account_id'] = _clean_str_col(trades, 'account_id')
    trades['account_type'] = _clean_str_col(trades, 'account_type')

    trades = trades.dropna(subset=['timestamp', 'price', 'qty', 'symbol'])
    trades = trades[trades['qty'] > 0].sort_values('timestamp')

    return trades[EXECUTION_COLUMNS].reset_index(drop=True)


def parse_generic_csv(contents: str) -> pd.DataFrame:
    """Best-effort fallback parser for non-Wealthsimple CSVs."""
    text = decode_upload(contents)
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip().lower() for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]  # see parse_wealthsimple_csv for why

    mapping = {
        'symbol': ['symbol', 'ticker', 'description'],
        'timestamp': ['time', 'date', 'effective', 'settlement'],
        'action': ['action', 'direction', 'side', 'activity_sub_type'],
        'price': ['price', 'unit'],
        'qty': ['qty', 'quantity', 'shares'],
    }
    # Exact column-name matches first, substring/fuzzy guessing only for whatever's
    # left -- otherwise a fuzzy candidate (e.g. 'description', a fallback for
    # 'symbol') can steal a rename ahead of an exact 'symbol' column that comes
    # later in the header, producing two 'symbol' columns after rename() (which
    # breaks every `row['symbol']` lookup downstream: it returns a Series, not a
    # scalar, and sqlite3 can't bind that -- this is exactly how a real
    # Wealthsimple export with a 'description' column before 'symbol' crashed).
    rename = {}
    claimed = set()
    for req in mapping:
        if req in df.columns:
            claimed.add(req)
    for req, candidates in mapping.items():
        if req in claimed:
            continue
        for col in df.columns:
            if col in claimed:
                continue
            if any(c in col for c in candidates):
                rename[col] = req
                claimed.add(col)
                break
    df = df.rename(columns=rename)

    required = ['symbol', 'timestamp', 'action', 'price', 'qty']
    missing = [r for r in required if r not in df.columns]
    if missing:
        raise ValueError(f"CSV missing required columns after mapping: {missing}")

    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce', utc=True)
    df['price'] = pd.to_numeric(df['price'], errors='coerce')
    df['qty'] = pd.to_numeric(df['qty'], errors='coerce').abs()
    df['action'] = df['action'].astype(str).str.upper().str.strip()
    df['action'] = df['action'].replace({'LONG': 'BUY', 'SHORT': 'SELL'})
    df['currency'] = df.get('currency', 'CAD')
    df['currency'] = df['currency'].fillna('CAD').astype(str).str.strip().str.upper() \
        if isinstance(df['currency'], pd.Series) else 'CAD'
    df['commission'] = pd.to_numeric(df.get('commission', 0), errors='coerce').fillna(0)
    df['name'] = df.get('name', df['symbol'])
    df['description'] = df.get('description', '')
    # Usually absent -- import_csv then stamps the account chosen in the UI.
    df['account_id'] = _clean_str_col(df, 'account_id')
    df['account_type'] = _clean_str_col(df, 'account_type')

    df = df.dropna(subset=['timestamp', 'price', 'qty', 'symbol'])
    df = df[df['action'].isin(['BUY', 'SELL']) & (df['qty'] > 0)].sort_values('timestamp')
    return df[EXECUTION_COLUMNS].reset_index(drop=True)


def detect_csv_format(contents: str) -> str:
    try:
        text = decode_upload(contents)
    except Exception:
        return 'generic'
    lines = [l for l in text.split('\n') if l.strip()]
    if not lines:
        return 'generic'
    header = lines[0].lower()
    ws_keywords = ['settlement_date', 'activity_type', 'activity_sub_type']
    has_time_col = 'effective_at' in header or ('effective_date' in header and 'effective_time' in header)
    return 'wealthsimple' if has_time_col and all(k in header for k in ws_keywords) else 'generic'


# ==============================================================
# 1a. Holdings-reconciliation CSV (a Wealthsimple *holdings* export -- current
# positions as of today -- not the *activity* export parse_wealthsimple_csv
# handles above, which is historical fills). Used only by the Weekly
# Reconciliation card to cross-check TAPEs's own open-position records
# against the real account, since Wealthsimple has no API and TAPEs can
# silently drift out of sync (see Data Freshness / the Three-System
# Architecture note in CLAUDE.md).
# ==============================================================

HOLDINGS_QTY_TOLERANCE = 1.0  # shares -- swallows dividend-reinvestment/rounding noise (e.g. a
                              # fractional-share BIL drift), not a real quantity mismatch

REQUIRED_HOLDINGS_COLS = {'account number', 'symbol', 'quantity', 'security type'}


def _read_holdings_csv(contents):
    """A Wealthsimple holdings export as (DataFrame with lower-cased columns,
    as_of ISO timestamp or None). The rows end at the first blank line; after
    it comes an '"As of 2026-10-03 13:27 GMT-04:00"' footer with a different
    column count, which pandas can't parse as data."""
    text = decode_upload(contents)
    lines = text.split('\n')
    blank_at = next((i for i, l in enumerate(lines) if not l.strip()), len(lines))
    df = pd.read_csv(io.StringIO('\n'.join(lines[:blank_at])), dtype=str)
    df.columns = [c.strip().lower() for c in df.columns]
    df = df.loc[:, ~df.columns.duplicated()]  # see parse_wealthsimple_csv for why
    as_of = None
    for line in lines[blank_at:]:
        m = re.search(r'As of (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})(?: GMT([+-]\d{2}:\d{2}))?', line)
        if m:
            as_of = f"{m.group(1)}T{m.group(2)}:00{m.group(3) or ''}"
            break
    return df, as_of


def parse_holdings_snapshot(contents) -> dict:
    """Per-account balance from a holdings export: {account_id: {as_of,
    positions: {currency: market value}, cash: {currency: amount}}}. Kept per
    currency -- no FX conversion. as_of comes from the export's own footer
    (falling back to now), since that's when the balance was true. Returns {}
    for an export without market values (nothing to snapshot)."""
    df, as_of = _read_holdings_csv(contents)
    needed = {'account number', 'security type', 'market value', 'market value currency'}
    if needed - set(df.columns):
        return {}
    as_of = as_of or dt.datetime.now().astimezone().isoformat(timespec='seconds')
    out = {}
    for r in df.to_dict('records'):
        acct = (r.get('account number') or '').strip()
        value = pd.to_numeric(r.get('market value'), errors='coerce')
        if not acct or pd.isna(value):
            continue
        ccy = (r.get('market value currency') or '').strip().upper() or '?'
        snap = out.setdefault(acct, {'as_of': as_of, 'positions': {}, 'cash': {}})
        bucket = snap['cash'] if str(r.get('security type')).upper() == 'CURRENCY' else snap['positions']
        bucket[ccy] = round(bucket.get(ccy, 0.0) + float(value), 2)
    return out


def parse_wealthsimple_holdings_csv(contents: str) -> pd.DataFrame:
    """Parse a Wealthsimple *holdings* export (current positions snapshot, one
    row per symbol per account) into columns: account_id, account_type,
    symbol, currency, qty. CURRENCY rows (raw cash, e.g. a "USD" row) are
    dropped -- TAPEs doesn't track cash yet, only positions.

    `Account Number` is the same id the activity export calls account_id, so
    each holding lines up with TAPEs's own (account, symbol, currency)
    positions; two accounts of the same type (the two LIRAs) stay distinct.

    The real export has a trailing blank line followed by an "As of <timestamp>"
    footer line with a different column count than the header -- pandas'
    C parser errors on that ragged row, so it's trimmed off before parsing
    rather than caught via an exception handler."""
    df, _ = _read_holdings_csv(contents)
    missing = REQUIRED_HOLDINGS_COLS - set(df.columns)
    if missing:
        raise ValueError(f"CSV is missing expected holdings columns: {sorted(missing)}")

    df = df[df['security type'].astype(str).str.upper() != 'CURRENCY'].copy()
    df['symbol'] = df['symbol'].astype(str).str.strip().str.upper()
    df['qty'] = pd.to_numeric(df['quantity'], errors='coerce')
    # groupby drops None keys, so blanks become ''.
    df['account_id'] = _clean_str_col(df, 'account number').fillna('')
    df['account_type'] = _clean_str_col(df, 'account type').fillna('')
    df['currency'] = _clean_str_col(df, 'market price currency').fillna('').str.upper()
    df = df.dropna(subset=['symbol', 'qty'])
    return df.groupby(['account_id', 'account_type', 'symbol', 'currency'], as_index=False)['qty'].sum()


def holdings_key(account_id, symbol):
    """Dismissal key for one holding: 'ACCT|SYM' (store dicts/ids need strings)."""
    return f"{account_id or ''}|{symbol}"


def compute_holdings_discrepancies(holdings_df, open_positions, cash_equiv_symbols, dismissed_keys,
                                   accounts=None):
    """Cross-check a parsed Wealthsimple holdings snapshot against TAPEs's own
    open positions, per account. Returns a list of {key, account_id, symbol,
    kind, detail} dicts, one per discrepancy, excluding anything whose
    holdings_key is in dismissed_keys (see db.dismissed_holdings_discrepancies
    -- an acknowledged, intentional gap, not something to re-flag every week).
    kind is one of:
      'untracked_in_tapes'       -- Wealthsimple has it, TAPEs has no record
      'untracked_in_wealthsimple' -- TAPEs thinks it's open, Wealthsimple doesn't show it
      'qty_mismatch'             -- both have it, quantities differ beyond HOLDINGS_QTY_TOLERANCE
    Cash-equivalent symbols are still cross-checked (a real quantity mismatch
    matters there too) -- only CURRENCY/cash itself is out of scope, handled
    upstream in parse_wealthsimple_holdings_csv.

    Keyed by (account, symbol, currency): the same ticker in two accounts, or
    listed in both USD and CAD, is two holdings. TAPEs-side quantities sum
    qty_open across lots (two pyramided lots would otherwise overwrite each
    other); Wealthsimple's export has no lot concept, so that level is right.
    Only accounts that appear in the export are checked on the TAPEs side, so
    exporting one account doesn't flag every other account's positions."""
    def k(acct, sym, ccy):
        return (_scalar(acct) or '', sym, (_scalar(ccy) or '').upper())

    ws_qty = {}
    if holdings_df is not None and not holdings_df.empty:
        for r in holdings_df.to_dict('records'):
            key = k(r.get('account_id'), r['symbol'], r.get('currency'))
            ws_qty[key] = ws_qty.get(key, 0.0) + float(r['qty'])
    exported_accounts = {a for a, _, _ in ws_qty}

    tapes_qty = {}
    for p in (open_positions or []):
        key = k(p.get('account_id'), p['symbol'], p.get('currency'))
        if key[0] in exported_accounts:
            tapes_qty[key] = tapes_qty.get(key, 0.0) + p['qty_open']

    dismissed = set(dismissed_keys or [])
    many = len(exported_accounts) > 1

    def entry(key, kind, detail):
        acct, sym, ccy = key
        where = f"{account_name(accounts, acct)}: " if many else ''
        return {'key': holdings_key(acct, sym), 'account_id': acct, 'symbol': sym, 'currency': ccy,
                'kind': kind, 'detail': where + detail}

    discrepancies = []
    for key, qty in ws_qty.items():
        if holdings_key(key[0], key[1]) in dismissed:
            continue
        if key not in tapes_qty:
            discrepancies.append(entry(key, 'untracked_in_tapes',
                f"Wealthsimple shows {qty:g} shares ({key[2]}); TAPEs has no open position on record."))
        elif abs(qty - tapes_qty[key]) > HOLDINGS_QTY_TOLERANCE:
            discrepancies.append(entry(key, 'qty_mismatch',
                f"Wealthsimple {qty:g} vs. TAPEs {tapes_qty[key]:g} (off by {abs(qty - tapes_qty[key]):g})."))

    for key, qty in tapes_qty.items():
        if holdings_key(key[0], key[1]) in dismissed or key in ws_qty:
            continue
        discrepancies.append(entry(key, 'untracked_in_wealthsimple',
            f"TAPEs shows an open position ({qty:g} shares, {key[2]}) that Wealthsimple's export "
            f"doesn't have -- likely already closed and not yet re-imported (see \"Import Executions\" below)."))

    return discrepancies


# ==============================================================
# 1b. ATR
#
# Two sources, tried in order:
#   1. Live TradingView Desktop, read directly over the Chrome DevTools
#      Protocol (CDP) debug port (127.0.0.1:9222) that TradingView Desktop
#      exposes -- the same port the tradingview-mcp server connects to. CDP
#      supports multiple independent debugger connections to one target, so
#      this doesn't conflict with a Claude Code session already attached via
#      MCP; it also means no MCP client/Node process is needed here, just a
#      raw websocket call. Before reading bars, this actively brings the chart
#      into a known-good state -- corrects the symbol if it doesn't match the
#      plan, and forces the 1D timeframe -- rather than just reading whatever
#      happens to be on screen. This does mutate the user's live TradingView
#      Desktop session; that's a deliberate choice made after testing showed a
#      passive read-and-warn design was worse (silently used the wrong
#      instrument's ATR too often). Switching the *saved layout* was tried too
#      but dropped -- TradingView has a documented bug where switching between
#      custom/saved layouts via the API doesn't reliably apply, confirmed by
#      hands-on testing here. The user manages the layout manually; symbol and
#      timeframe correction both tested reliable and stay automatic.
#   2. Yahoo Finance (yfinance), as a proxy when TradingView Desktop isn't
#      reachable -- always daily bars, regardless of the plan's intended
#      timeframe, so it's a coarser approximation than a live TradingView read.
# Either way, the fetched value always lands in an editable field so the real
# TradingView reading can override it before committing.
# ==============================================================

ATR_PERIOD = 14

# Remembered view state across sessions (Dash's built-in persistence, stored
# in the browser's localStorage per component id). Applied to layout/view
# controls and the Add Plan form's method choices only -- never to per-trade
# data entry, date pickers (they default to today on purpose), or anything a
# callback populates (Dash drops a remembered value when a callback writes the
# same prop). Bump VIEW_PERSISTENCE_KEY to reset everyone's saved view.
VIEW_PERSISTENCE_KEY = 'v1'
PERSIST = {'persistence': VIEW_PERSISTENCE_KEY, 'persistence_type': 'local'}

# Long lists (2026-10-20): show recent entries first, the rest a click away.
DAILY_OBS_PAGE_SIZE = 10
DAILY_OBS_PERIODS = [('7', 'Last 7 days'), ('30', 'Last 30 days'), ('90', 'Last 90 days'), ('all', 'All')]
JOURNAL_PAGE_SIZE = 15
JOURNAL_TABS = [('active', 'Active'), ('closed', 'Closed'), ('cancelled', 'Cancelled'), ('all', 'All')]
JOURNAL_TAB_STATUSES = {'active': {'Open (in progress)', 'Planned (no execution matched yet)'},
                        'closed': {'Closed'}, 'cancelled': {'Cancelled'}}
JOURNAL_PERIODS = [('30', 'Last 30 days'), ('90', 'Last 90 days'), ('ytd', 'This year'), ('all', 'All time')]
JOURNAL_SORTS = [('recent', 'Most recent first'), ('score', 'Plan Score')]
# A list taller than this scrolls inside its own panel, header pinned, so the
# rest of the page stays put however many entries there are.
SCROLL_PANEL = {'maxHeight': '640px', 'overflowY': 'auto', 'overflowX': 'hidden'}
STICKY_HEADER = {'position': 'sticky', 'top': 0, 'zIndex': 2, 'background': 'var(--bs-body-bg, #fff)',
                 'paddingTop': '0.25rem', 'paddingBottom': '0.25rem', 'borderBottom': '1px solid #dee2e6'}

# Header "Account view" value meaning every account combined.
ALL_ACCOUNTS = '__all__'

# Stop methods (plans.stop_method). 'ladder' = each target's stop_after only;
# the two trailing methods layer a daily-reset trail on top (never lowering
# the ladder stop). Wealthsimple has no trailing orders, so a "trailing" stop
# here is a level recomputed after each session's close and set by hand
# before the next session -- see compute_trailing_series.
STOP_METHODS = [('ladder', 'Ladder (stop_after per target)'),
                ('atr_trail', 'ATR(14) trail'),
                ('ema_trail', 'EMA(21) trail')]
TRAIL_BADGES = {'atr_trail': 'ATR trail', 'ema_trail': 'EMA(21) trail'}
EMA_TRAIL_PERIOD = 21
EMA_TRAIL_BUFFER_ATR = 0.25
MARKET_CLOSE_TZ = 'America/New_York'
MARKET_CLOSE_HOUR = 16
TRAIL_HISTORY_WARMUP_DAYS = 150
TV_CDP_HOST = '127.0.0.1'
TV_CDP_PORT = 9222


# ATR timeframe per plan (2026-10-08). Values are TradingView resolution
# strings, so the chart can be switched to them directly. Stored on the plan
# (plans.atr_timeframe; NULL means daily, the only option before this).
ATR_TIMEFRAMES = [('240', '4H'), ('1D', '1D'), ('1W', '1W')]
# Day trades (Strategy 4.5) plan on 5- or 15-minute bars; no other plan type may.
INTRADAY_TIMEFRAMES = [('5', '5m'), ('15', '15m')]
ATR_TIMEFRAME_LABELS = dict(INTRADAY_TIMEFRAMES + ATR_TIMEFRAMES)
DEFAULT_ATR_TIMEFRAME = '1D'
# US and TSX regular session in New York time. TradingView's 4H bars for these
# listings start at the open: 09:30-13:30, then 13:30-16:00 (a short bar).
SESSION_OPEN = (9, 30)
FOUR_HOUR_SPLIT = (13, 30)


def atr_timeframe_of(record):
    """A plan's ATR timeframe; plans from before the choice existed are daily."""
    tf = _scalar(record.get('atr_timeframe')) if record is not None else None
    return tf if tf in ATR_TIMEFRAME_LABELS else DEFAULT_ATR_TIMEFRAME


def trail_label(record):
    """Short stop-method label naming the bars it trails on, e.g. 'ATR trail
    (1W)' or 'EMA(21) trail' (always daily); None for a ladder plan."""
    method = stop_method_of(record)
    if method == 'atr_trail':
        return f"ATR trail ({ATR_TIMEFRAME_LABELS[atr_timeframe_of(record)]})"
    return TRAIL_BADGES.get(method)


def resample_to_weekly(daily):
    """Weekly OHLC bars (week ending Friday) from daily bars, indexed by the
    week's LAST trading day -- the day that bar completes. Built from daily
    bars rather than Yahoo's own weekly series so the weekly ATR uses exactly
    the daily data everything else does."""
    d = daily.copy()
    d.index = pd.DatetimeIndex(d.index).tz_localize(None) if getattr(d.index, 'tz', None) else d.index
    week = d.index.to_period('W-FRI')
    grouped = d.groupby(week)
    out = pd.DataFrame({'Open': grouped['Open'].first(), 'High': grouped['High'].max(),
                        'Low': grouped['Low'].min(), 'Close': grouped['Close'].last()})
    out.index = grouped.apply(lambda g: g.index.max())
    return out


def resample_to_four_hour(hourly):
    """Session-aligned 4H bars from Yahoo's 60-minute bars (stamped 09:30,
    10:30 ... 15:30 New York): 09:30-13:30 and 13:30-16:00 each day, matching
    TradingView's 4H bars for US/TSX listings. Indexed by the bar's start, in
    New York time. Bars outside the regular session are ignored."""
    h = hourly.copy()
    idx = pd.DatetimeIndex(h.index)
    idx = idx.tz_localize('UTC') if idx.tz is None else idx
    h.index = idx.tz_convert(MARKET_CLOSE_TZ)
    minutes = h.index.hour * 60 + h.index.minute
    open_m, split_m = SESSION_OPEN[0] * 60 + SESSION_OPEN[1], FOUR_HOUR_SPLIT[0] * 60 + FOUR_HOUR_SPLIT[1]
    h = h[(minutes >= open_m) & (minutes < MARKET_CLOSE_HOUR * 60)]
    minutes = h.index.hour * 60 + h.index.minute
    starts = [ts.normalize() + pd.Timedelta(minutes=(open_m if m < split_m else split_m))
              for ts, m in zip(h.index, minutes)]
    grouped = h.groupby(pd.DatetimeIndex(starts))
    return pd.DataFrame({'Open': grouped['Open'].first(), 'High': grouped['High'].max(),
                         'Low': grouped['Low'].min(), 'Close': grouped['Close'].last()})


def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Wilder's-smoothing ATR (matches Pine Script's ta.atr default smoothing)."""
    high, low, close = df['High'], df['Low'], df['Close']
    prev_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False).mean()


def _tv_cdp_find_chart_target():
    """Find the CDP target (browser tab) for the open TradingView chart."""
    url = f"http://{TV_CDP_HOST}:{TV_CDP_PORT}/json/list"
    with urllib.request.urlopen(url, timeout=3) as resp:
        targets = json.loads(resp.read())
    for t in targets:
        if t.get('type') == 'page' and 'tradingview.com/chart' in t.get('url', ''):
            return t
    for t in targets:
        if t.get('type') == 'page' and 'tradingview' in t.get('url', '').lower():
            return t
    return None


def _tv_cdp_evaluate(ws_url: str, expression: str, timeout: int = 10):
    """Send one Runtime.evaluate command over a CDP websocket and return its value."""
    import websocket  # imported lazily -- only needed when this is actually called
    # suppress_origin: Electron's CDP debug port rejects WebSocket handshakes that
    # carry an Origin header unless it's been allowlisted via --remote-allow-origins
    # (TradingView Desktop wasn't launched with that flag). Sending no Origin header
    # at all -- what Node's chrome-remote-interface does -- passes the check.
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({
            'id': 1,
            'method': 'Runtime.evaluate',
            'params': {'expression': expression, 'returnByValue': True, 'awaitPromise': True},
        }))
        while True:
            msg = json.loads(ws.recv())
            if msg.get('id') != 1:
                continue  # ignore unrelated CDP events sharing this socket
            if 'error' in msg:
                raise RuntimeError(msg['error'].get('message', 'CDP error'))
            result = msg.get('result', {})
            if result.get('exceptionDetails'):
                detail = result['exceptionDetails']
                raise RuntimeError(detail.get('exception', {}).get('description') or detail.get('text', 'JS evaluation error'))
            return result.get('result', {}).get('value')
    finally:
        ws.close()


def _tv_poll_until(fn, predicate, timeout=8, interval=0.4):
    """Call fn() repeatedly (swallowing exceptions) until predicate(value) is
    true or timeout elapses. Returns the last value seen either way -- callers
    check the returned state themselves rather than treating a timeout as fatal,
    since the chart may still be usably close to ready."""
    deadline = time.monotonic() + timeout
    value = None
    while True:
        try:
            value = fn()
        except Exception:
            value = None
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            return value
        time.sleep(interval)


# Mirrors tradingview-mcp's own known-good API paths (src/connection.js,
# src/core/chart.js, src/core/ui.js) -- window.TradingViewApi
# ._activeChartWidgetWV.value() for symbol()/resolution()/setSymbol()/
# setResolution(), ..._chartWidget.model().mainSeries().bars() for OHLC bars,
# and getSavedCharts()/loadChartFromServer() for switching saved layouts.
_TV_BARS_JS = """
(function() {
    var w = window.TradingViewApi._activeChartWidgetWV.value();
    var bars = w._chartWidget.model().mainSeries().bars();
    if (!bars || typeof bars.lastIndex !== 'function') return null;
    var result = [];
    var end = bars.lastIndex();
    var start = Math.max(bars.firstIndex(), end - 200 + 1);
    for (var i = start; i <= end; i++) {
        var v = bars.valueAt(i);
        if (v) result.push({high: v[2], low: v[3], close: v[4]});
    }
    return {bars: result, symbol: w.symbol(), resolution: w.resolution()};
})()
"""

_TV_GET_SYMBOL_RESOLUTION_JS = """
(function() {
    var chart = window.TradingViewApi._activeChartWidgetWV.value();
    return { symbol: chart.symbol(), resolution: chart.resolution() };
})()
"""

# symbol()/resolution() flip the instant setSymbol()/setResolution() is called,
# but the underlying bar *data* keeps loading from TradingView's feed for a bit
# after that -- reading bars right after the metadata matches can still return
# the previous symbol's stale data. This combines symbol+resolution+the last
# bar's index/close into one signature string; polling it until it stops
# changing across consecutive reads (_tv_wait_for_stable_bars, below) means the
# feed has actually caught up, not just the getter.
_TV_BAR_SIGNATURE_JS = """
(function() {
    var w = window.TradingViewApi._activeChartWidgetWV.value();
    var bars = w._chartWidget.model().mainSeries().bars();
    if (!bars || typeof bars.lastIndex !== 'function') return null;
    var end = bars.lastIndex();
    var v = bars.valueAt(end);
    return { sig: w.symbol() + '|' + w.resolution() + '|' + end + '|' + (v ? v[4] : 'null') + '|' + bars.size() };
})()
"""


def _tv_wait_for_stable_bars(ws_url: str, stable_reads=3, interval=0.4, timeout=10):
    """Poll the bar signature until it's identical across `stable_reads`
    consecutive polls -- i.e. the feed has settled after a setSymbol/
    setResolution call, not just that the API getters updated. Returns True if
    it settled, False on timeout (caller proceeds anyway; a stale read is
    better than hanging forever, and the caller can always retry)."""
    deadline = time.monotonic() + timeout
    last_sig = object()  # sentinel that can't equal any real signature
    stable = 0
    while True:
        data = _tv_cdp_evaluate(ws_url, _TV_BAR_SIGNATURE_JS)
        sig = (data or {}).get('sig')
        if sig is not None and sig == last_sig:
            stable += 1
            if stable >= stable_reads:
                return True
        else:
            stable = 0
        last_sig = sig
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def _tv_cdp_set_symbol(ws_url: str, symbol: str):
    js = """
    (function() {
        var chart = window.TradingViewApi._activeChartWidgetWV.value();
        chart.setSymbol(__SYMBOL__, {});
        return true;
    })()
    """.replace('__SYMBOL__', json.dumps(symbol))
    _tv_cdp_evaluate(ws_url, js)


def _tv_cdp_set_resolution(ws_url: str, resolution: str):
    js = """
    (function() {
        var chart = window.TradingViewApi._activeChartWidgetWV.value();
        chart.setResolution(__RES__, {});
        return true;
    })()
    """.replace('__RES__', json.dumps(resolution))
    _tv_cdp_evaluate(ws_url, js)


def _same_resolution(a, b):
    """TradingView reports daily/weekly as '1D'/'1W' but accepts 'D'/'W' too."""
    norm = {'D': '1D', 'W': '1W'}
    return norm.get(str(a or ''), str(a or '')) == norm.get(str(b or ''), str(b or ''))


def fetch_atr_from_tradingview(symbol: str, period: int = ATR_PERIOD, currency: str = None,
                               timeframe: str = DEFAULT_ATR_TIMEFRAME):
    """Bring the live TradingView Desktop chart into a known-good state -- the
    plan's instrument (the TSX listing for a CAD plan) and the plan's ATR
    timeframe (4H / 1D / 1W, see ATR_TIMEFRAMES) --
    actively correcting whichever doesn't already match, then read OHLC bars
    and compute ATR from them.

    Returns (atr_value, final_symbol, final_resolution, changes) where
    `changes` is a list of human-readable strings describing what TAPEs had
    to correct (empty if the chart was already in the right state).

    Raises RuntimeError if TradingView Desktop isn't reachable or the chart
    never settles.
    """
    target = _tv_cdp_find_chart_target()
    if not target:
        raise RuntimeError(f"No TradingView chart tab found via CDP ({TV_CDP_HOST}:{TV_CDP_PORT}). "
                            f"Is TradingView Desktop open with a chart?")
    ws_url = target['webSocketDebuggerUrl']
    changes = []

    # No layout switching here: TradingView has a documented bug where switching
    # between custom/saved layouts via the API doesn't reliably apply, confirmed
    # by hands-on testing against the live app. The user manages the layout
    # manually; TAPEs only corrects symbol and timeframe, both of which were
    # confirmed working.

    state = _tv_poll_until(
        lambda: _tv_cdp_evaluate(ws_url, _TV_GET_SYMBOL_RESOLUTION_JS),
        lambda v: v is not None,
        timeout=10, interval=0.5,
    )
    if not state:
        raise RuntimeError("Could not read the TradingView chart's current symbol/resolution.")

    # 1. Symbol -- correct if it isn't the plan's instrument. Currency picks
    # the listing: a CAD plan needs the TSX listing, not the US one with the
    # same ticker (and vice versa), so the exchange is part of the match.
    changed_anything = False
    tv_symbol = f"TSX:{_bare_symbol(symbol)}" if (currency or '').upper() == 'CAD' else symbol

    def _right_listing(v):
        return v and _tv_symbol_matches(v.get('symbol'), symbol, currency)

    if not _right_listing(state):
        old_symbol = state.get('symbol')
        _tv_cdp_set_symbol(ws_url, tv_symbol)
        state = _tv_poll_until(
            lambda: _tv_cdp_evaluate(ws_url, _TV_GET_SYMBOL_RESOLUTION_JS),
            _right_listing, timeout=8, interval=0.4,
        ) or state
        if not _right_listing(state):
            raise RuntimeError(f"TradingView chart is on {state.get('symbol')}, not the "
                               f"{currency or ''} listing of {symbol}")
        changes.append(f"Changed TradingView chart symbol from {old_symbol} to {state.get('symbol')}")
        changed_anything = True

    # 2. Timeframe -- correct if it isn't the plan's ATR timeframe.
    if not _same_resolution(state.get('resolution'), timeframe):
        old_res = state.get('resolution')
        _tv_cdp_set_resolution(ws_url, timeframe)
        state = _tv_poll_until(
            lambda: _tv_cdp_evaluate(ws_url, _TV_GET_SYMBOL_RESOLUTION_JS),
            lambda v: v and _same_resolution(v.get('resolution'), timeframe),
            timeout=8, interval=0.4,
        ) or state
        if not _same_resolution(state.get('resolution'), timeframe):
            raise RuntimeError(f"TradingView chart stayed on {state.get('resolution')}, not "
                               f"{ATR_TIMEFRAME_LABELS.get(timeframe, timeframe)}")
        changes.append(f"Changed TradingView timeframe from {old_res} to "
                       f"{ATR_TIMEFRAME_LABELS.get(timeframe, timeframe)}")
        changed_anything = True

    # The symbol()/resolution() getters above confirm the API registered the
    # change; they do NOT confirm the bar *data* has caught up (issue: fetching
    # right after used to occasionally return the previous symbol's bars, fixed
    # by waiting for the feed to settle here before reading anything).
    if changed_anything:
        _tv_wait_for_stable_bars(ws_url)

    # 3. Read bars and compute ATR off the now-corrected chart.
    data = _tv_cdp_evaluate(ws_url, _TV_BARS_JS)
    if not data or not data.get('bars'):
        raise RuntimeError("Could not read bars from the TradingView chart -- it may still be loading.")

    df = pd.DataFrame(data['bars']).rename(columns={'high': 'High', 'low': 'Low', 'close': 'Close'})
    atr_series = compute_atr(df, period).dropna()
    if atr_series.empty:
        raise RuntimeError("Not enough bars on the TradingView chart to compute ATR.")
    return float(atr_series.iloc[-1]), data.get('symbol'), data.get('resolution'), changes


def yahoo_candidates(symbol: str, currency: str = None) -> list:
    """Yahoo tickers to try, in order, for an instrument. Currency decides the
    listing: the same ticker can be US-listed (USD) and TSX-listed (CAD), and
    trying the bare ticker first silently fetched the US price for a CAD
    position. USD -> [SYM]; CAD -> TSX, then Cboe Canada (NEO, where CDRs
    trade), then TSX Venture; unknown currency -> the old bare-then-.TO order.
    A symbol that already carries a Yahoo suffix is used as-is."""
    sym = symbol.strip().upper()
    if '.' in sym and sym.rsplit('.', 1)[-1] in ('TO', 'V', 'NE', 'CN'):
        return [sym]
    currency = (currency or '').upper()
    if currency == 'USD':
        return [sym]
    if currency == 'CAD':
        return [f"{sym}.TO", f"{sym}.NE", f"{sym}.V"]
    return [sym, f"{sym}.TO"]


def _yahoo_bars(ticker, timeframe):
    """Yahoo bars for one ATR timeframe. Daily is Yahoo's own series; weekly is
    built from daily (resample_to_weekly); 4H is built from 60-minute bars,
    session-aligned (resample_to_four_hour). The 4H build was checked against
    TradingView's own 4H bars on 2026-10-08 (TSX:CHPS, 119 bars, every bar
    start matched; ATR(14) 1.0026 vs 0.9986), so it's a fair fallback."""
    import yfinance as yf  # imported lazily -- only needed when this is actually called
    if timeframe in dict(INTRADAY_TIMEFRAMES):
        # Regular-session 5m/15m bars, used as they are (day trades only).
        df = yf.download(ticker, period='5d', interval=f'{timeframe}m', progress=False, auto_adjust=False)
    elif timeframe == '240':
        df = yf.download(ticker, period='60d', interval='60m', progress=False, auto_adjust=False)
    else:
        df = yf.download(ticker, period='2y' if timeframe == '1W' else '3mo', interval='1d',
                         progress=False, auto_adjust=False)
    if df is None or df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=['High', 'Low', 'Close'])
    if timeframe == '240':
        return resample_to_four_hour(df)
    if timeframe == '1W':
        return resample_to_weekly(df)
    return df


def fetch_atr_from_yfinance(symbol: str, period: int = ATR_PERIOD, currency: str = None,
                            timeframe: str = DEFAULT_ATR_TIMEFRAME):
    """Fetch recent bars from Yahoo Finance at the plan's ATR timeframe and
    return (atr_value, ticker_used). The listing tried depends on currency --
    see yahoo_candidates. Raises ValueError if no usable data is found."""
    candidates = yahoo_candidates(symbol, currency)

    for ticker in candidates:
        try:
            df = _yahoo_bars(ticker, timeframe)
        except Exception:
            continue
        if df is None or df.empty or len(df) < period + 1:
            continue
        atr_series = compute_atr(df, period).dropna()
        if not atr_series.empty:
            return float(atr_series.iloc[-1]), ticker

    tried = f" (tried {', '.join(candidates)})" if len(candidates) > 1 else ""
    raise ValueError(f"No usable {ATR_TIMEFRAME_LABELS.get(timeframe, timeframe)} price data "
                     f"found for '{symbol}'{tried}")


def _bare_symbol(sym):
    """Strip an exchange prefix (e.g. 'BATS:XLF' -> 'XLF') for comparison."""
    return (sym or '').split(':')[-1].strip().upper()


CANADIAN_TV_EXCHANGES = {'TSX', 'TSXV', 'NEO', 'CSE'}


def _tv_exchange(chart_symbol):
    """The exchange part of a TradingView symbol, without a data-feed suffix.
    Without a real-time subscription TradingView reports a delayed feed, e.g.
    'TSX_DLY:CHPS' -- that's still the TSX listing. (Found 2026-10-08: the
    suffix made a correct TSX chart fail the CAD check, and pass the USD one.)"""
    if ':' not in (chart_symbol or ''):
        return ''
    return chart_symbol.split(':')[0].upper().split('_')[0]


def _tv_symbol_matches(chart_symbol, symbol, currency=None):
    """Is the TradingView chart on this instrument? The ticker must match, and
    for a known currency the exchange must be on the right side of the border
    (TSX/TSXV/NEO/CSE for CAD, anything else for USD)."""
    if _bare_symbol(chart_symbol) != _bare_symbol(symbol):
        return False
    exchange = _tv_exchange(chart_symbol)
    currency = (currency or '').upper()
    if currency == 'CAD':
        return exchange in CANADIAN_TV_EXCHANGES
    if currency == 'USD':
        return exchange not in CANADIAN_TV_EXCHANGES
    return True


def fetch_atr(symbol: str, period: int = ATR_PERIOD, currency: str = None,
              timeframe: str = DEFAULT_ATR_TIMEFRAME):
    """Try live TradingView Desktop first (actively correcting layout/symbol/
    timeframe as needed), falling back to the Yahoo Finance proxy if
    TradingView Desktop isn't reachable or can't be put on the right listing.
    Returns (atr_value, source_note) where source_note describes where the
    value came from and what, if anything, TAPEs had to change on the
    TradingView chart to get it (for display, not parsing). currency picks the
    listing (US vs TSX) on both sources; timeframe picks the bars (4H/1D/1W)."""
    timeframe = timeframe if timeframe in ATR_TIMEFRAME_LABELS else DEFAULT_ATR_TIMEFRAME
    try:
        atr_value, tv_symbol, tv_resolution, changes = fetch_atr_from_tradingview(
            symbol, period, currency, timeframe)
        note = f"TradingView Desktop, {tv_symbol} @ {tv_resolution}"
        if changes:
            note += " -- " + "; ".join(changes)
        return atr_value, note
    except Exception as tv_err:
        try:
            atr_value, ticker_used = fetch_atr_from_yfinance(symbol, period, currency, timeframe)
            return atr_value, (f"Yahoo Finance proxy ({ticker_used}, {ATR_TIMEFRAME_LABELS[timeframe]}) "
                               f"-- TradingView Desktop unavailable: {tv_err}")
        except Exception as yf_err:
            raise RuntimeError(f"TradingView Desktop: {tv_err}. Yahoo Finance fallback also failed: {yf_err}")


def fetch_current_price(symbol: str, currency: str = None):
    """Latest daily close for a symbol, via Yahoo Finance only -- deliberately
    NOT the TradingView-Desktop-first path fetch_atr uses. That path actively
    hijacks the live chart (changes its symbol/timeframe) for a single,
    deliberate action tied to committing one plan; the weekly balance instead
    needs a price for every open position in one pass, and cycling the user's
    live chart through 5-10 symbols back to back would be disruptive for no
    real benefit -- Yahoo's daily close is precise enough for a week-over-week
    balance trend. currency picks the listing (see yahoo_candidates). Returns
    (price, ticker_used). Raises ValueError if no usable data is found."""
    import yfinance as yf  # imported lazily -- only needed when this is actually called

    candidates = yahoo_candidates(symbol, currency)

    for ticker in candidates:
        try:
            df = yf.download(ticker, period='5d', interval='1d', progress=False, auto_adjust=False)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        # The most recent row can be NaN (e.g. an in-progress/incomplete
        # session) without yfinance raising -- fall back to the last
        # non-NaN close instead of silently returning NaN, which would
        # otherwise poison the whole portfolio sum in
        # compute_mark_to_market_balance (NaN + anything = NaN) while
        # looking like a normal, successful fetch.
        closes = df['Close'].dropna()
        if closes.empty:
            continue
        return float(closes.iloc[-1]), ticker

    tried = f" (tried {', '.join(candidates)})" if len(candidates) > 1 else ""
    raise ValueError(f"No usable price data found for '{symbol}'{tried}")


def _completed_daily_bars(df, now=None):
    """Drop today's bar unless the US session has closed (16:00 New York).
    Stops are reset once per day after the close, from completed bars only --
    an in-progress bar must never move a stop."""
    now = now if now is not None else pd.Timestamp.now(tz=MARKET_CLOSE_TZ)
    last_complete = now.normalize() if now.hour >= MARKET_CLOSE_HOUR else now.normalize() - pd.Timedelta(days=1)
    dates = pd.to_datetime(df.index).tz_localize(None).normalize() if getattr(df.index, 'tz', None) is None \
        else pd.to_datetime(df.index).tz_convert(None).normalize()
    return df[dates <= last_complete.tz_localize(None)]


def fetch_daily_history(symbol: str, since_date, warmup_days: int = TRAIL_HISTORY_WARMUP_DAYS,
                        currency: str = None):
    """Completed daily OHLC bars from `warmup_days` before since_date to now
    (the warm-up lets EMA(21)/ATR(14) settle before the trail activates).
    Yahoo Finance only, like fetch_current_price -- NOT the TradingView-first
    path fetch_atr uses: the Drift Report runs this for several lots in one
    pass, and cycling the live chart through each would be disruptive for no
    accuracy gain at daily resolution. currency picks the listing (see
    yahoo_candidates). Raises ValueError if nothing usable."""
    import yfinance as yf  # imported lazily -- only needed when this is actually called

    candidates = yahoo_candidates(symbol, currency)
    start = (pd.to_datetime(since_date).tz_localize(None) if pd.to_datetime(since_date).tzinfo is None
             else pd.to_datetime(since_date).tz_convert(None)) - pd.Timedelta(days=warmup_days)
    for ticker in candidates:
        try:
            df = yf.download(ticker, start=start.strftime('%Y-%m-%d'), interval='1d',
                             progress=False, auto_adjust=False)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = _completed_daily_bars(df.dropna(subset=['High', 'Low', 'Close']))
        if len(df) > ATR_PERIOD:
            return df

    tried = f" (tried {', '.join(candidates)})" if len(candidates) > 1 else ""
    raise ValueError(f"No usable daily history for '{symbol}'{tried}")


def _day_index(index):
    dates = pd.to_datetime(index)
    if getattr(dates, 'tz', None) is not None:
        dates = dates.tz_localize(None)
    return dates.normalize()


def timeframe_atr_by_day(daily, timeframe, four_hour=None):
    """ATR(14) at the plan's timeframe, as one value per daily bar: the value
    known at that day's close, from COMPLETED bars only. This is the distance
    an ATR trail sits below its anchor.

    - '1D': the daily ATR.
    - '1W': ATR of weekly bars built from the (completed) daily bars. A week
      counts once it's finished -- its last bar is a Friday, or a later day
      exists (a holiday Friday) -- so the in-progress week never moves a stop.
    - '240': ATR of session-aligned 4H bars (four_hour, from
      resample_to_four_hour); each day takes the value at its last 4H bar.
      Days after the last completed daily bar are ignored.
    Returns a Series indexed like _day_index(daily.index), forward-filled."""
    days = _day_index(daily.index)
    if timeframe == '1W':
        weekly = resample_to_weekly(daily)
        if not weekly.empty and pd.Timestamp(weekly.index[-1]).weekday() != 4:
            weekly = weekly.iloc[:-1]                   # the current week isn't over
        atr = compute_atr(weekly, ATR_PERIOD) if len(weekly) else pd.Series(dtype=float)
        atr.index = _day_index(atr.index)
    elif timeframe == '240':
        if four_hour is None or four_hour.empty:
            raise ValueError("No 4H bars to compute a 4H ATR from")
        atr = compute_atr(four_hour, ATR_PERIOD)
        atr.index = _day_index(atr.index)
        atr = atr.groupby(level=0).last()
        atr = atr[atr.index <= days.max()]
    else:
        return pd.Series(compute_atr(daily, ATR_PERIOD).to_numpy(dtype=float), index=days)
    return atr.reindex(atr.index.union(days)).sort_index().ffill().reindex(days)


def fetch_four_hour_history(symbol: str, since_date, warmup_days: int = 30, currency: str = None):
    """Session-aligned 4H bars (resample_to_four_hour) from warmup_days before
    since_date, via Yahoo 60-minute bars (Yahoo keeps ~730 days of those).
    Yahoo-only, for the same reason as fetch_daily_history."""
    import yfinance as yf  # imported lazily -- only needed when this is actually called
    start = pd.to_datetime(since_date)
    start = (start.tz_convert(None) if start.tzinfo is not None else start) - pd.Timedelta(days=warmup_days)
    start = max(start, pd.Timestamp.now() - pd.Timedelta(days=720))
    for ticker in yahoo_candidates(symbol, currency):
        try:
            df = yf.download(ticker, start=start.strftime('%Y-%m-%d'), interval='60m',
                             progress=False, auto_adjust=False)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        bars = resample_to_four_hour(df.dropna(subset=['High', 'Low', 'Close']))
        if len(bars) > ATR_PERIOD:
            return bars
    raise ValueError(f"No usable 4H history for '{symbol}'")


def compute_trailing_series(bars, direction, method, activation_date, entry_price,
                            atr_multiplier=None, trail_atr=None, ema_gate='entry'):
    """Daily trailing-stop level for an activated trail, from COMPLETED daily
    bars only. Returns a Series indexed by bar date; the value on date D is
    the stop to set at D's close, i.e. the stop in effect for the NEXT session.
    NaN until the first eligible day. Pure: no network.

    - 'atr_trail': highest high of completed bars since activation -
      atr_multiplier x ATR(14) (today's in-progress bar is never included).
      The anchor is always the DAILY high; trail_atr (from
      timeframe_atr_by_day) swaps in the plan's own timeframe ATR for the
      distance -- a weekly plan trails 2 x weekly ATR below the high.
    - 'ema_trail': EMA(21) - EMA_TRAIL_BUFFER_ATR x ATR(14), but only on days
      when EMA(21) itself is above entry_price -- or, with ema_gate='close'
      (scale-in campaigns, which start below a falling EMA), on days the
      close is above EMA(21). Either way the stop can't land above the
      market at the start of a reversal.
    Both only ratchet in the trade's favor (running max for longs, running
    min for shorts) -- a recomputed level never lowers the stop. Shorts mirror
    everything. Only days on/after activation_date count."""
    sign = 1 if direction == 'BUY' else -1
    dates = pd.to_datetime(bars.index).normalize()
    if getattr(dates, 'tz', None) is not None:
        dates = dates.tz_convert(None)
    close = pd.Series(bars['Close'].to_numpy(dtype=float), index=dates)
    atr = pd.Series(compute_atr(bars, ATR_PERIOD).to_numpy(dtype=float), index=dates)
    active = dates >= pd.Timestamp(pd.to_datetime(activation_date).date())

    if method == 'atr_trail':
        # Chandelier-style: anchored on the highest HIGH (lowest LOW for
        # shorts) of completed bars since activation -- deliberately not
        # closes, which would loosen ATR stops already placed under the
        # original high-based rule.
        side = bars['High'] if sign == 1 else bars['Low']
        extreme = pd.Series(side.to_numpy(dtype=float), index=dates).where(active)
        extreme = extreme.cummax() if sign == 1 else extreme.cummin()
        distance_atr = atr if trail_atr is None else pd.Series(
            trail_atr.reindex(dates).to_numpy(dtype=float), index=dates)
        candidate = extreme - sign * float(atr_multiplier) * distance_atr
    elif method == 'ema_trail':
        ema = close.ewm(span=EMA_TRAIL_PERIOD, adjust=False).mean()
        if ema_gate == 'close':
            eligible = (close > ema) if sign == 1 else (close < ema)
        else:
            eligible = (ema > float(entry_price)) if sign == 1 else (ema < float(entry_price))
        candidate = (ema - sign * EMA_TRAIL_BUFFER_ATR * atr).where(eligible)
    else:
        raise ValueError(f"Unknown trailing method {method!r}")

    candidate = candidate.where(active)
    running = candidate.cummax() if sign == 1 else candidate.cummin()
    return running.ffill()


def trail_level_before(series, when=None):
    """The trailing stop in effect during the session on `when`'s date: the
    last level set at a close BEFORE that date (stops are reset after the
    close, effective the next session). when=None -> the latest level, i.e.
    what to set for the next session. None if no eligible level yet."""
    if series is None or series.empty:
        return None
    if when is not None:
        when_ts = pd.to_datetime(when)
        when_ts = when_ts.tz_convert(None) if when_ts.tzinfo is not None else when_ts
        series = series[series.index < when_ts.normalize()]
    series = series.dropna()
    return float(series.iloc[-1]) if not series.empty else None


def compute_mark_to_market_balance(open_positions):
    """Sum of (qty_open * current price) across every open position. Returns
    (total, priced_symbols, errors) -- a symbol whose price fetch fails is
    excluded from the total (not silently treated as worth $0) and reported
    in `errors` so a stale/wrong balance is never presented as complete.

    priced_symbols holds only price/ticker -- both genuinely symbol-level
    (a fresh quote is the same regardless of how many lots of that symbol
    are held), unlike qty_open, which is NOT included here since it's
    per-position: two pyramided lots of the same symbol have their own
    qty_open, and every caller already has its own position record in scope
    to read that from directly. A symbol already priced by an earlier lot in
    this same call is reused rather than re-fetched.

    Keyed by price_key ('SYM|CUR'), not bare symbol: the currency picks the
    listing that gets priced (see yahoo_candidates), so the same ticker held
    in USD and in CAD is two prices. The total spans every account."""
    total = 0.0
    priced_symbols = {}
    errors = {}
    for pos in (open_positions or []):
        symbol = pos['symbol']
        key = price_key(pos)
        try:
            qty_open = pos['qty_open']
            if key in priced_symbols:
                price = priced_symbols[key]['price']
            else:
                price, ticker = fetch_current_price(symbol, _scalar(pos.get('currency')))
                # Belt-and-suspenders: a NaN price must not silently poison
                # the whole sum -- NaN + anything = NaN, which would make ONE
                # bad symbol wreck the entire portfolio total while still
                # looking like a normal, successful computation.
                if pd.isna(price):
                    raise ValueError(f"Got a non-numeric price for {symbol}")
                priced_symbols[key] = {'price': price, 'ticker': ticker}
            if pd.isna(qty_open):
                raise ValueError(f"Got a non-numeric quantity for {symbol}")
            total += qty_open * price
        except Exception as e:
            errors[key] = str(e)
    return total, priced_symbols, errors


def stage_prices(openpos, total, priced, errors, fx=None):
    """The staged-prices store: prices plus the FX rates fetched alongside
    them, so every consumer of the prices also has the rates they were valued
    at. 'total' stays the old mixed-currency sum for compatibility; use
    staged_portfolio_value for anything shown or compared."""
    by_currency, _ = value_by_currency(openpos, priced)
    return {'total': total, 'priced_symbols': priced, 'errors': errors,
            'by_currency': by_currency, 'fx': fx if fx is not None else get_fx_rates()}


def staged_portfolio_value(staged):
    """(value, fx_used, by_currency) for a staged-prices store. With a rate for
    every currency held, value is in the base currency and fx_used is the
    rates; otherwise value is the old mixed-currency sum and fx_used is None,
    so callers can say it's unconverted."""
    staged = staged or {}
    by_currency = staged.get('by_currency') or {}
    fx_used = usable_fx(staged.get('fx'), by_currency) if by_currency else None
    if fx_used:
        return to_base(by_currency, fx_used), fx_used, by_currency
    return staged.get('total') or 0.0, None, by_currency


def usable_fx(fx, currencies):
    """fx if it has a rate for every currency in `currencies`, else None --
    the signal to callers to show per-currency figures only rather than a
    combined total that's partly unconverted."""
    if not fx:
        return None
    return fx if all(fx_rate(fx, c) is not None for c in currencies) else None


def _in_base(amount, record, fx):
    """amount (in the record's own currency) in the base currency when fx is
    given; unchanged without fx (the old mixed-currency behaviour)."""
    if not fx:
        return amount
    return amount * fx_rate(fx, _scalar(record.get('currency')))


def position_currencies(open_positions):
    return sorted({_scalar(p.get('currency')) or '?' for p in (open_positions or [])})


def account_values(open_positions, priced_symbols, fx=None):
    """{account_id: mark-to-market of that account's priced positions}, in the
    base currency when fx is given (see usable_fx). Cash isn't tracked yet, so
    this understates an account that holds cash (and overstates its risk %) --
    callers label it accordingly."""
    values = {}
    for pos in (open_positions or []):
        key = price_key(pos)
        if key in (priced_symbols or {}):
            acct = _scalar(pos.get('account_id'))
            values[acct] = values.get(acct, 0.0) + _in_base(
                priced_symbols[key]['price'] * pos['qty_open'], pos, fx)
    return values


def risk_by_account(open_positions, per_lot_risk, fx=None):
    """{account_id: dollars at risk}, summed from compute_portfolio_risk's
    per-lot figures (each in its position's own currency), converted to the
    base currency when fx is given -- information only; caps are portfolio-wide."""
    out = {}
    for pos in (open_positions or []):
        lot = pos.get('lot_id')
        if lot in (per_lot_risk or {}):
            acct = _scalar(pos.get('account_id'))
            out[acct] = out.get(acct, 0.0) + _in_base(per_lot_risk[lot], pos, fx)
    return out


# Portfolio-risk alert thresholds (% of portfolio value at risk across open,
# non-cash-equivalent positions) -- see compute_portfolio_risk.
PORTFOLIO_RISK_CAUTION_PCT = 5.0
PORTFOLIO_RISK_WARNING_PCT = 6.0

# Pre-trade risk check (informational only, never blocks Save Draft/Commit) --
# flags a single new plan whose own risk (full position stopped out at the
# initial stop) alone exceeds this % of portfolio value. See
# compute_pretrade_risk_check.
PER_TRADE_RISK_CAP_PCT = 2.0

# Stop gap allowance (2026-10-24). A stop can be jumped: an overnight gap opens
# past it and the stop order fills at the open. So the risk TAPEs counts for
# planning -- the plan's max loss and R:R, the pre-trade check, campaign peak
# risk and portfolio heat -- is the stop distance PLUS this many ATRs per
# share (the plan's atr_at_commit). Set from the user's own stop-outs: the 6
# trades that went to their stop lost 1.24R on average (USO, ARKW and CHPS
# gapped 0.78-1.51 ATR through), and 0.5 ATR on the standard 2.5 x ATR daily
# stop is 0.2R. Editable in Data Management (setting 'stop_gap_allowance_atr').
# Closed-trade R is NOT affected: it stays measured against the planned stop,
# so a gap still shows as a loss beyond -1R. Without an ATR on record the
# allowance is the same share of the stop distance it would be on a
# STANDARD_STOP_ATR stop.
DEFAULT_STOP_GAP_ATR = 0.5
STANDARD_STOP_ATR = 2.5
MAX_STOP_GAP_ATR = 3.0


def load_gap_allowance_atr():
    saved = db.get_setting('stop_gap_allowance_atr')
    try:
        return min(MAX_STOP_GAP_ATR, max(0.0, float(saved))) if saved not in (None, '') else DEFAULT_STOP_GAP_ATR
    except (TypeError, ValueError):
        return DEFAULT_STOP_GAP_ATR


def gap_per_share(atr, entry, stop, k=None):
    """The gap allowance per share: k x ATR, or, with no ATR on record,
    k / STANDARD_STOP_ATR of the stop distance. 0 when k is 0 or there's no stop."""
    k = load_gap_allowance_atr() if k is None else float(k)
    if not k:
        return 0.0
    try:
        a = float(atr) if atr not in (None, '') else None
    except (TypeError, ValueError):
        a = None
    if a is not None and not np.isnan(a) and a > 0:
        return k * a
    try:
        e, st = float(entry), float(stop)
    except (TypeError, ValueError):
        return 0.0
    if np.isnan(e) or np.isnan(st):
        return 0.0
    return k / STANDARD_STOP_ATR * abs(e - st)

# Stop-exit slippage tolerance (closed trades only -- see classify_stop_exits).
# A stop that triggers and fills a fraction past its level is a normal
# stop-out, not a breach. An exit counts as a breach only when it fills past
# the stop in effect by more than STOP_SLIPPAGE_ATR_MULT x atr_at_commit; with
# no ATR on record, the tolerance falls back to STOP_SLIPPAGE_PCT_FALLBACK
# percent of the stop price. The live drift report's stop_breached_live
# deliberately has NO tolerance -- it's a real-time alert.
STOP_SLIPPAGE_ATR_MULT = 0.1
STOP_SLIPPAGE_PCT_FALLBACK = 0.1


def compute_current_r_multiple(direction, entry_price, stop_loss, current_price):
    """Live/unrealized R-multiple: how many multiples of the ORIGINAL planned
    risk (entry to initial stop) a position has moved in your favor (or
    against you) at the current price. Same convention as the closed-trade
    r_multiple in build_journal_row (actual_pnl_per_share / risk_per_share) --
    just using the current price in place of a realized exit. Returns None if
    there's no stop on record (nothing to measure risk against) or the stop
    is on the wrong side of entry (not a real protective stop)."""
    if stop_loss in (None, '') or entry_price in (None, ''):
        return None
    sign = 1 if direction == 'BUY' else -1
    risk_per_share = sign * (entry_price - stop_loss)
    if risk_per_share <= 0 or pd.isna(risk_per_share):
        return None
    pnl_per_share = sign * (current_price - entry_price)
    return pnl_per_share / risk_per_share


def compute_portfolio_risk(open_positions, plan_stops, cash_equiv_symbols, priced_symbols, portfolio_value,
                           fx=None, gap_by_lot=None):
    """Total dollar risk across open, non-cash-equivalent positions, as a % of
    the portfolio's mark-to-market value (cash-equivalent parking positions
    like BIL are excluded from the numerator -- they carry no directional
    risk -- but the denominator is still the FULL portfolio value, since
    that's what "% of portfolio" means).

    Per position, risk is (current_price - stop) * qty_open for a long,
    clamped at 0 -- risk here means what could still be lost going forward
    from the current price, not a loss that's already happened (a position
    already through its stop contributes 0 additional downside, not a
    negative number). A position with NO stop on record has its FULL current
    market value treated as at risk: an unprotected position's downside is
    unbounded in principle, and silently excluding it from the total would
    understate risk, which defeats the point of a risk warning.

    plan_stops is keyed by lot_id, not symbol -- two lots of the same symbol
    (pyramiding) can have genuinely different stops, and priced_symbols
    (keyed by symbol, from compute_mark_to_market_balance) only ever holds
    ONE entry per symbol regardless of how many lots share it, so qty_open
    is read from each position's own record, not from priced_symbols (only
    `price`, a genuinely symbol-level quantity, is safe to share from there).

    With fx (see usable_fx), total_risk is in the base currency and
    portfolio_value must be too -- the only way a % of a USD+CAD portfolio is
    right. per_lot_risk always stays in each position's own currency.

    Returns (total_risk_dollars, total_risk_pct, per_lot_risk_dollars) --
    per_lot_risk_dollars only covers positions that were actually priced
    (see compute_mark_to_market_balance's `errors`), same "don't silently
    treat a failed fetch as zero" principle as the balance total.

    gap_by_lot ({lot_id: allowance per share}, see gap_allowance_by_lot) adds
    the stop gap allowance to each protected lot's risk."""
    cash_equiv = set(cash_equiv_symbols or [])
    total_risk = 0.0
    per_lot_risk = {}
    for pos in (open_positions or []):
        symbol = pos['symbol']
        pkey = price_key(pos)
        if symbol in cash_equiv or pkey not in priced_symbols:
            continue
        lot_key = pos.get('lot_id', symbol)
        current_price = priced_symbols[pkey]['price']
        qty_open = pos['qty_open']
        stop_loss = plan_stops.get(lot_key)
        if stop_loss in (None, ''):
            risk_dollars = current_price * qty_open  # unprotected: full value at risk
        else:
            sign = 1 if pos.get('direction', 'BUY') == 'BUY' else -1
            risk_per_share = max(0.0, sign * (current_price - stop_loss)) + (gap_by_lot or {}).get(lot_key, 0.0)
            risk_dollars = risk_per_share * qty_open
        per_lot_risk[lot_key] = risk_dollars
        total_risk += _in_base(risk_dollars, pos, fx)
    total_risk_pct = (total_risk / portfolio_value * 100) if portfolio_value else None
    return total_risk, total_risk_pct, per_lot_risk


# ==============================================================
# Beta-weighted net market exposure -- a different question from portfolio
# heat above. Heat answers "how much could I lose if every stop got hit."
# This answers whether positions that look diversified by ticker are
# actually one large correlated bet on the same market direction (several
# leveraged or single-theme index/sector ETFs can move almost identically in
# a risk-off move despite different tickers and "different sectors").
#
# Deviation from established codebase convention, flagged per the request
# that raised this feature: there is no caching mechanism anywhere else in
# TAPEs -- ATR and current-price fetches are always live, with "staleness"
# handled purely by a manual refresh button and no persistence at all (see
# fetch_atr, fetch_current_price, and the Refresh Live Prices / Compute
# Balance buttons). Beta gets a genuine persisted, timestamped cache
# (db.beta_cache) instead, because it costs meaningfully more than a single
# price fetch (a full lookback window of daily history for BOTH the
# position and the benchmark, per symbol) and doesn't change intraday, so
# recomputing it on every click would be a lot of redundant network calls
# for a number that's still correct from an hour ago.
# ==============================================================

BETA_BENCHMARK_SYMBOL = 'SPY'
BETA_LOOKBACK_DAYS = 60
BETA_CACHE_MAX_AGE_HOURS = 24  # "refresh at most once a day per symbol"
BETA_FETCH_THROTTLE_SECONDS = 0.5  # pause between symbols in a batch recompute, live fetches only


def price_key(record_or_symbol, currency=None):
    """String key for a priced instrument -- 'SYM|CUR'. Prices are per
    instrument, not per bare ticker: XYZ on the TSX and XYZ in the US are
    different prices. String (not tuple) so it survives a JSON Dash store."""
    if isinstance(record_or_symbol, str):
        return f"{record_or_symbol}|{(currency or '').upper()}"
    rec = record_or_symbol
    return f"{rec.get('symbol')}|{(_scalar(rec.get('currency')) or '').upper()}"


# ---- FX: one base currency for combined totals (2026-10-08) ----
# Every account's base currency is CAD (accounts.base_currency). Per-currency
# figures need no rate; a combined total, and any % of the whole portfolio
# (heat, the per-trade cap, beta exposure), converts each currency to CAD at
# the Bank of Canada's daily rate. Positions only -- cash per currency still
# comes from the holdings export until a cash ledger exists.
BASE_CURRENCY = 'CAD'
FX_FOREIGN_CURRENCIES = ['USD']
FX_CACHE_MAX_AGE_HOURS = 12
BOC_VALET_URL = "https://www.bankofcanada.ca/valet/observations/FX{ccy}CAD/json?recent=1"


def _fetch_boc_rate(ccy):
    """(date, rate) for 1 unit of ccy in CAD, from the Bank of Canada's Valet
    API (the daily average rate, published once per business day)."""
    with urllib.request.urlopen(BOC_VALET_URL.format(ccy=ccy), timeout=6) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    obs = data['observations'][-1]
    return obs['d'], float(obs[f'FX{ccy}CAD']['v'])


def get_fx_rates(force_refresh=False, now=None, fetch=None, db_path=None):
    """The rates for converting into BASE_CURRENCY, as a JSON-safe dict for a
    Dash store: {'base': 'CAD', 'rates': {'CAD': 1.0, 'USD': 1.424},
    'as_of': {'USD': '2026-10-08'}, 'source': ..., 'errors': {...}}.

    Uses the database cache (db.fx_rates) when it was fetched within
    FX_CACHE_MAX_AGE_HOURS, so the Bank is called at most a couple of times a
    day. A failed fetch falls back to the newest cached rate, whatever its age
    (as_of says how old). A currency with no rate at all is left out of
    `rates` and named in `errors` -- callers then can't convert it, and say
    so rather than guessing."""
    fetch = fetch or _fetch_boc_rate
    kw = {'db_path': db_path} if db_path else {}
    now = now or db.utc_now()
    out = {'base': BASE_CURRENCY, 'rates': {BASE_CURRENCY: 1.0}, 'as_of': {},
           'source': 'Bank of Canada daily rate', 'errors': {}}
    for ccy in FX_FOREIGN_CURRENCIES:
        pair = f"{ccy}{BASE_CURRENCY}"
        cached = db.fetch_latest_fx_rate(pair, **kw)
        fresh = cached and (now - dt.datetime.fromisoformat(cached['fetched_at'])) < \
            dt.timedelta(hours=FX_CACHE_MAX_AGE_HOURS)
        if cached and fresh and not force_refresh:
            out['rates'][ccy], out['as_of'][ccy] = cached['rate'], cached['date']
            continue
        try:
            date, rate = fetch(ccy)
            db.save_fx_rate(pair, date, rate, **kw)
            out['rates'][ccy], out['as_of'][ccy] = rate, date
        except Exception as e:
            if cached:
                out['rates'][ccy], out['as_of'][ccy] = cached['rate'], cached['date']
                out['errors'][ccy] = f"couldn't refresh ({e}); using the {cached['date']} rate"
            else:
                out['errors'][ccy] = f"no rate available ({e})"
    return out


def fx_rate(fx, ccy):
    """CAD per 1 unit of ccy, or None when unknown (no fx, or no rate)."""
    if not fx:
        return None
    ccy = (ccy or '').upper()
    return (fx.get('rates') or {}).get(ccy)


def to_base(amounts, fx):
    """{currency: amount} -> one total in the base currency, or None if any
    currency in it has no rate (a partial total would be quietly wrong)."""
    total = 0.0
    for ccy, amt in (amounts or {}).items():
        rate = fx_rate(fx, ccy)
        if rate is None:
            return None
        total += amt * rate
    return total


def value_by_currency(open_positions, priced_symbols, account_id=None):
    """{currency: market value} of open positions at the staged prices,
    optionally one account only. Positions only -- cash isn't tracked. A
    position without a price is left out (second value) rather than counted
    as zero."""
    values, unpriced = {}, []
    for pos in (open_positions or []):
        if account_id is not None and _scalar(pos.get('account_id')) != account_id:
            continue
        key = price_key(pos)
        if key not in (priced_symbols or {}):
            unpriced.append(pos['symbol'])
            continue
        ccy = _scalar(pos.get('currency')) or '?'
        values[ccy] = values.get(ccy, 0.0) + priced_symbols[key]['price'] * pos['qty_open']
    return values, unpriced


def fmt_fx_note(fx, currencies=None):
    """'USD/CAD 1.4240 (Bank of Canada, 2026-10-08)' for the rates actually used."""
    if not fx:
        return ""
    parts = [f"{c}/{fx.get('base', BASE_CURRENCY)} {fx['rates'][c]:.4f} ({fx['as_of'].get(c, '?')})"
             for c in sorted(currencies or fx.get('rates', {})) if c != fx.get('base') and c in fx.get('rates', {})]
    return (", ".join(parts) + " -- Bank of Canada daily rate") if parts else ""


def compute_beta(symbol, benchmark=BETA_BENCHMARK_SYMBOL, lookback_days=BETA_LOOKBACK_DAYS, currency=None):
    """Beta of `symbol` against `benchmark`, computed directly from realized
    daily returns over the trailing `lookback_days` trading days:
    covariance(position returns, benchmark returns) / variance(benchmark
    returns). Deliberately NOT yfinance's precomputed `info['beta']` field --
    that's a fixed 5-year monthly regression with inconsistent coverage,
    especially for smaller or leveraged ETFs. Computing directly from price
    history keeps the window controllable and coverage consistent across the
    whole book, and naturally captures a leveraged fund's REAL trailing
    relationship (including the volatility decay leveraged funds experience
    over anything longer than a single day) rather than assuming its nominal
    leverage ratio (e.g. a "2x" fund is deliberately never special-cased to
    beta=2.0).

    Raises ValueError if either symbol's price history is unusable -- callers
    must catch this and record it rather than let one bad symbol crash the
    whole portfolio calculation (see compute_portfolio_beta_exposure)."""
    import yfinance as yf  # imported lazily -- only needed when this is actually called

    def _fetch_closes(sym, ccy):
        # Same candidate/fallback/MultiIndex-flattening convention as
        # fetch_atr_from_yfinance and fetch_current_price. period='6mo' is a
        # deliberately generous buffer over the ~60-trading-day requirement
        # (roughly 125 trading days) so occasional missing/holiday rows never
        # leave too little history, without being wasteful. No lookback_days
        # truncation here -- that has to happen AFTER aligning with the
        # benchmark's dates below, not before (truncating each series to "the
        # last N rows" independently is exactly what caused the bug this
        # function used to have -- see the comment below).
        for ticker in yahoo_candidates(sym, ccy):
            try:
                df = yf.download(ticker, period='6mo', interval='1d', progress=False, auto_adjust=False)
            except Exception:
                continue
            if df is None or df.empty:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            closes = df['Close'].dropna()
            if len(closes) >= 2:
                return closes
        return None

    pos_closes = _fetch_closes(symbol, currency)
    if pos_closes is None:
        raise ValueError(f"Not enough price history for '{symbol}'")
    bench_closes = _fetch_closes(benchmark, 'USD')
    if bench_closes is None:
        raise ValueError(f"Not enough price history for benchmark '{benchmark}'")

    # Align on the shared date index, NOT position. A single missing bar on
    # one side but not the other -- confirmed to actually happen (SSO was
    # missing an 2026-08-28 bar that SPY had, within the trailing 60 days --
    # not a rare edge case) -- would otherwise silently pair every
    # subsequent day against the wrong calendar date, corrupting the
    # covariance for the whole tail of the window rather than just that one
    # day. Aligning the CLOSES (not the returns) on shared dates means a
    # missing day merges into one multi-day return instead of mis-pairing
    # everything after it: still the same calendar period on both sides,
    # just occasionally a 2-day-equivalent return instead of two 1-day ones.
    aligned = pd.concat(
        [pos_closes.rename('pos'), bench_closes.rename('bench')], axis=1, join='inner'
    ).sort_index()
    if len(aligned) < lookback_days + 1:
        raise ValueError(
            f"Not enough overlapping trading days for '{symbol}' vs '{benchmark}' "
            f"(need {lookback_days + 1}, have {len(aligned)})")
    aligned = aligned.iloc[-(lookback_days + 1):]

    pos_returns = aligned['pos'].pct_change().dropna().to_numpy()
    bench_returns = aligned['bench'].pct_change().dropna().to_numpy()

    bench_var = np.var(bench_returns, ddof=1)
    if bench_var == 0 or np.isnan(bench_var):
        raise ValueError(f"Benchmark '{benchmark}' has zero return variance over this window")
    cov = np.cov(pos_returns, bench_returns, ddof=1)[0, 1]
    return float(cov / bench_var)


def get_cached_beta(symbol, benchmark=BETA_BENCHMARK_SYMBOL, lookback_days=BETA_LOOKBACK_DAYS,
                     max_age_hours=BETA_CACHE_MAX_AGE_HOURS, force_refresh=False, currency=None):
    """Returns (beta, error, computed_at, from_cache) for one symbol -- uses
    the cached value if it's younger than max_age_hours (see
    BETA_CACHE_MAX_AGE_HOURS) unless force_refresh is set, else recomputes
    and caches the result (success or failure) via db.upsert_beta_cache so a
    symbol that's known to fail isn't retried on every single page load.
    from_cache tells a batch caller (compute_portfolio_beta_exposure) whether
    a live yfinance fetch actually happened, so it knows when it's worth
    pausing before the next symbol to avoid tripping a rate limit -- no point
    throttling between two cache hits that made no network call at all."""
    currency = (currency or '').upper()
    cached = None if force_refresh else db.fetch_beta_cache(symbol, benchmark, lookback_days, currency=currency)
    if cached is not None:
        computed_at = pd.to_datetime(cached['computed_at'], utc=True, errors='coerce')
        if pd.notna(computed_at):
            age_hours = (pd.Timestamp.now(tz='UTC') - computed_at).total_seconds() / 3600
            if age_hours <= max_age_hours:
                return cached.get('beta'), cached.get('error'), cached['computed_at'], True
    now = db.utc_now().isoformat()
    try:
        beta = compute_beta(symbol, benchmark, lookback_days, currency=currency or None)
        db.upsert_beta_cache(symbol, benchmark, lookback_days, beta, error=None, currency=currency)
        return beta, None, now, False
    except Exception as e:
        db.upsert_beta_cache(symbol, benchmark, lookback_days, None, error=str(e), currency=currency)
        return None, str(e), now, False


def compute_portfolio_beta_exposure(open_positions, priced_symbols, cash_equiv_symbols, portfolio_value,
                                     benchmark=BETA_BENCHMARK_SYMBOL, lookback_days=BETA_LOOKBACK_DAYS,
                                     force_refresh=False, fx=None):
    """Beta-weighted net market exposure: sum across open, non-cash-equivalent,
    priced positions of (market value * beta), where market value reuses the
    SAME priced_symbols dict compute_mark_to_market_balance already built --
    "account equity" stays defined identically for this metric and portfolio
    heat, since both ultimately divide by the same mark-to-market total.

    A position whose beta can't be computed (unresolvable symbol,
    insufficient history, a yfinance failure) is EXCLUDED from the sum, not
    treated as beta=0 -- reported in `errors` instead, same "don't silently
    treat a failure as zero" principle as compute_mark_to_market_balance's
    own errors dict.

    A small pause is inserted between symbols whenever a live yfinance fetch
    actually happened (never between two cache hits, which made no network
    call) -- found necessary during testing: a burst of several compute_beta
    calls in quick succession (a full lookback window of daily history for
    both the position AND the benchmark, per symbol) occasionally got a
    truncated/rate-limited response back from Yahoo that still LOOKED like
    valid data (right shape, no exception raised) but produced an implausible
    beta -- a more dangerous failure mode than a clean error, since nothing
    here would otherwise catch a wrong-but-plausible number. This is a
    mitigation, not a guarantee; yfinance's data quality is fundamentally
    best-effort, matching this codebase's existing tolerance for that
    elsewhere (no retries exist for ATR/price either).

    Returns (total_exposure_dollars, total_exposure_pct, per_symbol, errors,
    betas_as_of, per_account). per_symbol is {price_key: {'beta',
    'market_value'}} -- beta is an instrument-level property (one
    get_cached_beta call per instrument, however many lots or accounts hold
    it), so lots ACCUMULATE into one entry's market_value. per_account is
    {account_id: exposure_dollars}, the same sum split by account (shown for
    information; the total spans the whole portfolio). betas_as_of is the
    OLDEST computed_at among instruments actually used in the total (the most
    stale contributor) -- the honest "as of" figure for the aggregate as a
    whole, not the freshest one.

    With fx (see usable_fx), total_exposure and per_account are in the base
    currency and portfolio_value must be too; per_symbol market_value stays
    in the instrument's own currency."""
    cash_equiv = set(cash_equiv_symbols or [])
    total_exposure = 0.0
    per_symbol = {}
    per_account = {}
    errors = {}
    computed_ats = []
    beta_by_key = {}  # price_key -> beta, computed once even if held in several lots/accounts
    for pos in (open_positions or []):
        symbol = pos['symbol']
        key = price_key(pos)
        if symbol in cash_equiv:
            continue
        if key not in priced_symbols:
            errors[key] = "No current price available (click \"Refresh Live Prices\" first)"
            continue
        if key in beta_by_key:
            beta = beta_by_key[key]
        else:
            beta, error, computed_at, from_cache = get_cached_beta(
                symbol, benchmark, lookback_days, force_refresh=force_refresh,
                currency=_scalar(pos.get('currency')))
            if not from_cache:
                time.sleep(BETA_FETCH_THROTTLE_SECONDS)
            if beta is None:
                errors[key] = error or "Beta unavailable"
                beta_by_key[key] = None
                continue
            beta_by_key[key] = beta
            computed_ats.append(computed_at)
        if beta is None:
            continue
        market_value = priced_symbols[key]['price'] * pos['qty_open']
        entry = per_symbol.setdefault(key, {'beta': beta, 'market_value': 0.0})
        entry['market_value'] += market_value
        exposure = _in_base(market_value * beta, pos, fx)
        total_exposure += exposure
        acct = _scalar(pos.get('account_id'))
        per_account[acct] = per_account.get(acct, 0.0) + exposure
    total_exposure_pct = (total_exposure / portfolio_value * 100) if portfolio_value else None
    betas_as_of = min(computed_ats) if computed_ats else None
    return total_exposure, total_exposure_pct, per_symbol, errors, betas_as_of, per_account


# ==============================================================
# 2. Round-trip construction (FIFO matching of BUY/SELL fills)
# ==============================================================

def round_trip_id(entry_fills, exit_fills) -> str:
    """Deterministic id for a round trip, derived from the execution row ids
    that compose it (not a random uuid) so it's stable across rebuilds --
    manual plan<->round-trip match overrides (db.plan_matches) are keyed on it."""
    exec_ids = sorted(int(f['id']) for f in entry_fills + exit_fills)
    return hashlib.sha1(','.join(map(str, exec_ids)).encode()).hexdigest()[:12]


def lot_id(entry_fills) -> str:
    """Deterministic id for a still-OPEN lot, derived ONLY from its entry
    fills' execution row ids -- unlike round_trip_id, exit fills are
    deliberately excluded so this id stays stable across partial sells while
    the lot remains open (a round trip's id is only computed once it's fully
    closed and immutable, so including exit fills there is safe; a lot's
    exit_fill_list keeps growing while it's open, so including exit fills
    here would change the id on every partial sell and orphan any manual
    db.plan_lot_matches override pointing at it)."""
    exec_ids = sorted(int(f['id']) for f in entry_fills)
    return hashlib.sha1(('lot:' + ','.join(map(str, exec_ids))).encode()).hexdigest()[:12]


# Default tolerance (as a fraction of a plan's declared qty) for deciding a
# lot-bucket has "closed" at its plan's target -- see
# split_entry_fills_into_lot_buckets. Deliberately tight: this only needs to
# absorb ordinary rounding/fractional-share noise (the kind already seen
# elsewhere in this app, e.g. a sub-1-share dividend-reinvestment drift), not
# a genuinely different quantity -- a loose tolerance would risk silently
# swallowing a separate plan's tranche into the wrong bucket (see the
# function's own docstring for the specific undershoot case this guards
# against).
LOT_SPLIT_QTY_TOLERANCE_PCT = 0.02


def split_entry_fills_into_lot_buckets(entry_fills, committed_buy_plans,
                                        qty_tolerance_pct=LOT_SPLIT_QTY_TOLERANCE_PCT):
    """Assign each of a symbol's still-open entry fills (chronological order)
    to a bucket label -- either a plan_id from committed_buy_plans, or the
    string 'SPILLOVER' for shares beyond every plan's declared quantity. A
    single fill is never split across two buckets. Returns None -- meaning
    "don't split, fall back to one blended lot" -- when the fill history
    can't be unambiguously partitioned; see build_round_trips, which only
    calls this when a symbol has 2+ committed BUY plans (with 0-1, there's
    nothing to disambiguate, so today's single-blended-lot behavior applies
    unchanged and this function is never called).

    Raw execution data has no signal distinguishing "part of the original
    position" from "a deliberate new pyramid tranche" -- both are just
    another BUY row. The only such signal TAPEs has is each plan's own
    declared qty, so plans are sorted by planned_date (falling back to
    created_at) and fills are greedily assigned to the earliest plan's
    bucket until its qty target is reached, then the next plan's.

    Why fail closed instead of guessing on a close-but-not-exact total: qty
    alone can't distinguish "one lot, bought a bit over/under plan" from
    "two lots, the first under-bought and the second's shares mistaken for
    more of the first" -- both produce an identical running total. Example:
    plan A wants 100, plan B wants 50; the user actually buys only 60 for A
    (real, ordinary under-buying) then 50 for B. A naive "keep accumulating
    until at-or-past the target, allow some overshoot" rule sees 60+50=110,
    which is within a loose tolerance of A's 100 and would silently swallow
    B's entire tranche into A's bucket. This function instead requires a
    bucket's cumulative qty to land at-or-near (within qty_tolerance_pct)
    its OWN plan's target before advancing -- 110 overshoots plan A's target
    by more than that tolerance, so this returns None rather than guess, and
    the caller falls back to one blended lot (with a manual lot-match
    override available if the user wants to correct it by hand).

    The one case allowed to fall short of its target is the FINAL plan's
    bucket -- there's no later plan for its remaining shares to be confused
    with, so "haven't finished buying the last tranche yet" is unambiguous
    and fine, exactly like today's single-lot model already tolerates buying
    less than a plan's stated qty."""
    if len(committed_buy_plans) < 2 or not entry_fills:
        return None

    plans_sorted = sorted(committed_buy_plans, key=lambda p: p.get('planned_date') or p.get('created_at') or '')
    n = len(plans_sorted)
    labels = []
    plan_idx = 0
    cumulative = 0.0

    for fill in entry_fills:
        if plan_idx >= n:
            labels.append('SPILLOVER')
            continue
        plan = plans_sorted[plan_idx]
        cumulative += float(fill['qty'])
        labels.append(plan['plan_id'])
        target = float(plan.get('qty') or 0)
        tolerance = target * qty_tolerance_pct
        if cumulative >= target - tolerance:
            if cumulative > target + tolerance:
                return None  # overshot by more than rounding noise -- likely someone else's tranche
            plan_idx += 1
            cumulative = 0.0

    if plan_idx < n - 1:
        # A plan strictly before the last one never reached its own target at
        # all -- can't tell whether any later fills we assigned to it were
        # really meant for it or for a plan after it.
        return None
    return labels


def _fill_list(fills):
    """[{qty, price, time}] for a list of fills, oldest first."""
    out = [{'qty': float(f['qty']), 'price': float(f['price']), 'time': f['timestamp'].isoformat()}
           for f in fills]
    return sorted(out, key=lambda f: f['time'])


def _rt_dict(symbol, entry_fills, exit_fills, currency, deterministic_plan_id=None, account_id=None):
    """Build one round-trip dict from its composing entry/exit fills -- shared
    by the plain (blended) path and the per-bucket pyramiding path so both
    produce the exact same shape. exit_fills items may be either raw
    execution rows (blended path, has ['timestamp']) or the synthetic
    per-bucket dicts built during bucket-aware FIFO consumption (has
    ['timestamp'] too, see build_round_trips) -- both expose qty/price/
    timestamp/commission, so this needs no branching on which kind it got."""
    total_qty = sum(f['qty'] for f in entry_fills)
    entry_cost = sum(f['qty'] * f['price'] for f in entry_fills)
    entry_price = entry_cost / total_qty if total_qty else np.nan

    exit_qty = sum(f['qty'] for f in exit_fills)
    exit_rev = sum(f['qty'] * f['price'] for f in exit_fills)
    exit_price = exit_rev / exit_qty if exit_qty else np.nan

    commission = (sum(f['commission'] for f in entry_fills) +
                  sum(f['commission'] for f in exit_fills))

    exit_fill_list = [
        {'qty': float(f['qty']), 'price': float(f['price']), 'time': f['timestamp'].isoformat()}
        for f in exit_fills
    ]
    entry_fill_list = _fill_list(entry_fills)

    return {
        # Deterministic (not random) so it's stable across rebuilds --
        # manual plan<->round-trip match overrides are keyed on this.
        'rt_id': round_trip_id(entry_fills, exit_fills),
        'account_id': account_id,
        'symbol': symbol,
        'direction': 'BUY',
        'entry_time': min(f['timestamp'] for f in entry_fills),
        'exit_time': max(f['timestamp'] for f in exit_fills),
        'qty': total_qty,
        'entry_price': entry_price,
        'exit_price': exit_price,
        'commission': commission,
        'realized_pnl': exit_rev - entry_cost - commission,
        'currency': currency,
        'n_entry_fills': len(entry_fills),
        'n_exit_fills': len(exit_fills),
        'exit_fill_list': exit_fill_list,
        # Each buy, in time order -- a scale-in campaign's journal row needs
        # the probe and every add separately, not just the blended average.
        'entry_fill_list': entry_fill_list,
        'first_entry_price': entry_fill_list[0]['price'] if entry_fill_list else np.nan,
        # Lets _reconcile_closed_lot_matches recompute this round trip's
        # lot_id-while-open equivalent (lot_id hashes only entry fills) to
        # find a manual plan_lot_matches override that needs migrating to
        # plan_matches now that the lot has closed.
        'entry_fill_ids': sorted(int(f['id']) for f in entry_fills),
        # deterministic_plan_id is set only when this round trip's lot was
        # cleanly attributed to a specific plan by quantity-bucketing while it
        # was still open (see split_entry_fills_into_lot_buckets) -- None for
        # the plain single-lot-per-symbol case, which still relies on
        # match_plans_to_roundtrips' existing fuzzy date/price matching.
        'deterministic_plan_id': deterministic_plan_id,
    }


def _open_pos_dict(symbol, lots_group, entry_fills, exit_fills, currency, deterministic_plan_id=None,
                   account_id=None):
    """Build one open-position dict from one lot-bucket's remaining lots and
    its own entry/exit fills -- shared by the plain and per-bucket paths, same
    reasoning as _rt_dict above."""
    open_qty = sum(l[0] for l in lots_group)
    open_cost = sum(l[0] * l[1] for l in lots_group)
    # entry_fills here are exactly this bucket's own fills -- so this is the
    # ORIGINAL total entry price across every buy fill that built this lot,
    # not just the cost basis of whatever's left after FIFO-reducing for
    # partial sells (that's avg_price, below). A plan's stated entry price
    # should be compared against this, not avg_price, or a partially-scaled-
    # out position would look like it deviated from plan when it hasn't.
    total_entry_qty = sum(f['qty'] for f in entry_fills)
    total_entry_cost = sum(f['qty'] * f['price'] for f in entry_fills)
    exit_fill_list = [
        {'qty': float(f['qty']), 'price': float(f['price']), 'time': f['timestamp'].isoformat()}
        for f in exit_fills
    ]
    return {
        'account_id': account_id,
        'symbol': symbol,
        'direction': 'BUY',
        'qty_open': open_qty,
        'avg_price': open_cost / open_qty if open_qty else np.nan,
        'currency': currency,
        'since': min(l[2] for l in lots_group),
        'entry_price': total_entry_cost / total_entry_qty if total_entry_qty else np.nan,
        'entry_time': min(f['timestamp'] for f in entry_fills) if entry_fills else min(l[2] for l in lots_group),
        'qty_total_entered': total_entry_qty,
        'exit_fill_list': exit_fill_list,
        'entry_fill_list': _fill_list(entry_fills),
        'first_entry_price': _fill_list(entry_fills)[0]['price'] if entry_fills else np.nan,
        # lot_id: stable across partial sells while open (entry-fills-only hash --
        # see lot_id()'s own docstring for why exit fills are excluded).
        'lot_id': lot_id(entry_fills),
        'deterministic_plan_id': deterministic_plan_id,
    }


def _scalar(v):
    """None for None/NaN/'' (pandas NaN-fills missing values in mixed columns,
    and bool(NaN) is True -- see the deterministic_plan_id bug in CLAUDE.md)."""
    if v is None or (isinstance(v, float) and np.isnan(v)) or v == '':
        return None
    return v


def plan_fits_position(plan, account_id, symbol, currency):
    """Can this plan describe a position in (account_id, symbol, currency)?
    Account must match exactly -- a plan never matches another account's
    fills. A plan with no currency on record (a never-executed symbol at
    migration) is treated as compatible with either currency. A cancelled
    plan never fits a position."""
    if is_cancelled(plan):
        return False
    if plan.get('symbol') != symbol or _scalar(plan.get('account_id')) != _scalar(account_id):
        return False
    plan_ccy, currency = _scalar(plan.get('currency')), _scalar(currency)
    return not plan_ccy or not currency or plan_ccy == currency


def build_round_trips(executions: pd.DataFrame, plans: list = None):
    """
    Group individual BUY/SELL fills into completed round-trip (long) trades
    using FIFO lot matching per symbol. Also returns any still-open positions.

    Only long trades (BUY to open, SELL to close) are auto-detected. Short
    trades are not present in Wealthsimple cash-account exports and are not
    matched here; if you plan a short manually, log its execution yourself.

    Pyramiding support: when a symbol has 2+ COMMITTED BUY plans that haven't
    already been consumed by an earlier chain (see below), a still-open chain's
    entry fills are run through split_entry_fills_into_lot_buckets to try to
    attribute them to specific plans -- each resulting bucket becomes its own
    lot/round-trip with its own lot_id and deterministic_plan_id, rather than
    being blended into one row per symbol. With 0-1 committed BUY plans (the
    overwhelming majority of symbols), or when the bucketing function can't
    unambiguously split a chain, this falls back to EXACTLY today's behavior:
    one blended lot per still-open chain, deterministic_plan_id=None (left for
    match_plans_to_roundtrips' existing fuzzy date/price matcher to resolve).

    A "chain" is a maximal run of fills for a symbol that doesn't return to
    fully flat until its own end -- the same boundary the pre-pyramiding code
    already used to decide when to start a fresh entry_fills list, just now
    detected up front (in a first pass) so a chain's FULL fill history is
    known before it's replayed a second time with bucket-aware FIFO
    consumption. Bucketing needs the whole chain in advance because a plan's
    qty target might only be satisfied by a fill that arrives after an
    interleaved partial sell earlier in the same chain.

    Plans already attributed to an earlier, already-closed chain for this
    symbol are excluded from a later chain's candidate pool (`used_plan_ids`)
    -- prevents a stale plan from an unrelated, long-since-closed position
    being mistaken for part of a new one.

    A single sell can now span two lots/buckets (e.g. it fully closes the
    older lot into a round trip and trims a newer one) -- each affected
    bucket gets a synthetic partial-qty copy of that sell, with commission
    apportioned by qty share, in place of the original whole-row reference.
    """
    round_trips = []
    open_positions = []
    if executions is None or executions.empty:
        return pd.DataFrame(), pd.DataFrame()

    # A position is (account, symbol, currency): the same ticker in two
    # accounts, or US- vs TSX-listed in one, is never blended. Missing values
    # become '' so groupby (which drops NaN keys) can't silently lose rows.
    executions = executions.copy()
    for col in ('account_id', 'currency'):
        if col not in executions.columns:
            executions[col] = ''
        executions[col] = executions[col].fillna('').astype(str)
    # A scale-in campaign's adds are its own fills, not separate lots, so a
    # campaign plan never takes part in pyramid splitting (one campaign per
    # account + symbol, and nothing else open beside it -- _campaign_conflict).
    committed_buy = [p for p in (plans or [])
                     if p.get('direction') == 'BUY' and p.get('status') == 'committed'
                     and not is_campaign(p) and not is_day_trade(p)]

    for (account_key, symbol, currency_key), g in executions.sort_values('timestamp').groupby(
            ['account_id', 'symbol', 'currency'], sort=True):
        account_id = account_key or None
        currency = currency_key or None
        committed_buy_plans = [p for p in committed_buy if plan_fits_position(p, account_id, symbol, currency)]

        # ---- Pass 1: split this symbol's fills into chains (maximal runs
        # that don't return to flat until their own end) ----
        chains = []
        current_chain = []
        running_qty = 0.0
        for _, row in g.iterrows():
            if row['action'] == 'BUY':
                if running_qty <= 1e-9 and current_chain:
                    chains.append(current_chain)
                    current_chain = []
                current_chain.append(row)
                running_qty += row['qty']
            elif row['action'] == 'SELL':
                if running_qty <= 1e-9:
                    # Sell with no open long lots -- can't attribute, skip (would be a short).
                    continue
                current_chain.append(row)
                running_qty = max(0.0, running_qty - row['qty'])
        if current_chain:
            chains.append(current_chain)

        used_plan_ids = set()
        for chain_rows in chains:
            entry_fills_all = [r for r in chain_rows if r['action'] == 'BUY']
            candidate_plans = [p for p in committed_buy_plans if p['plan_id'] not in used_plan_ids]
            labels = None
            if len(candidate_plans) >= 2:
                labels = split_entry_fills_into_lot_buckets(entry_fills_all, candidate_plans)
            if labels is None:
                labels = [None] * len(entry_fills_all)
            else:
                used_plan_ids |= {lbl for lbl in labels if lbl not in (None, 'SPILLOVER')}
            label_iter = iter(labels)

            # ---- Pass 2: replay this chain with bucket-aware FIFO ----
            lots = []  # [qty_remaining, price, timestamp, commission, bucket_label]
            entry_fills_by_bucket = {}
            exit_fills_by_bucket = {}
            last_row = None
            for row in chain_rows:
                last_row = row
                if row['action'] == 'BUY':
                    bucket = next(label_iter)
                    lots.append([row['qty'], row['price'], row['timestamp'], row['commission'], bucket])
                    entry_fills_by_bucket.setdefault(bucket, []).append(row)
                else:  # SELL
                    qty_to_sell = row['qty']
                    total_sell_qty = row['qty']
                    # Consecutive lots consumed by this ONE sell that share a
                    # bucket are merged into a single synthetic exit-fill entry
                    # (matching pre-pyramiding behavior exactly for the
                    # overwhelmingly common case of one bucket per symbol) --
                    # only a bucket CHANGE mid-sell produces more than one
                    # entry, which is the only situation new here.
                    consumed_by_bucket = {}   # bucket -> qty consumed from this sell
                    closed_buckets = []       # in consumption order
                    while qty_to_sell > 1e-9 and lots:
                        lot = lots[0]
                        matched_qty = min(lot[0], qty_to_sell)
                        bucket = lot[4]
                        consumed_by_bucket[bucket] = consumed_by_bucket.get(bucket, 0.0) + matched_qty
                        lot[0] -= matched_qty
                        qty_to_sell -= matched_qty
                        if lot[0] <= 1e-9:
                            lots.pop(0)
                            if not lots or lots[0][4] != bucket:
                                # This bucket's lots are all consumed -- since
                                # bucket labels are assigned in chronological
                                # order and never reused, every lot with this
                                # label is contiguous and FIFO-oldest, so this
                                # check fires exactly once per bucket.
                                closed_buckets.append(bucket)
                    for bucket, matched_qty in consumed_by_bucket.items():
                        apportioned_commission = (
                            row['commission'] * (matched_qty / total_sell_qty) if total_sell_qty else 0.0)
                        exit_fills_by_bucket.setdefault(bucket, []).append({
                            'qty': matched_qty, 'price': row['price'], 'timestamp': row['timestamp'],
                            'commission': apportioned_commission, 'id': row['id'],
                        })
                    for bucket in closed_buckets:
                        round_trips.append(_rt_dict(
                            symbol, entry_fills_by_bucket.pop(bucket),
                            exit_fills_by_bucket.pop(bucket, []),
                            currency,
                            deterministic_plan_id=(bucket if bucket not in (None, 'SPILLOVER') else None),
                            account_id=account_id))

            # Whatever remains in `lots` at the end of this chain is still
            # open -- group consecutive lots by bucket (contiguous, per above)
            # into one open-position row each.
            i = 0
            while i < len(lots):
                bucket = lots[i][4]
                group = []
                while i < len(lots) and lots[i][4] == bucket:
                    group.append(lots[i])
                    i += 1
                open_positions.append(_open_pos_dict(
                    symbol, group, entry_fills_by_bucket.get(bucket, []),
                    exit_fills_by_bucket.get(bucket, []), currency,
                    deterministic_plan_id=(bucket if bucket not in (None, 'SPILLOVER') else None),
                    account_id=account_id))

    rt_df = pd.DataFrame(round_trips)
    if not rt_df.empty:
        rt_df = rt_df.sort_values('exit_time', ascending=False).reset_index(drop=True)
    op_df = pd.DataFrame(open_positions)
    return rt_df, op_df


# ==============================================================
# 3. Plan <-> round-trip matching and scoring
# ==============================================================

# Sanity gates for auto-matching (see match_plans_to_roundtrips docstring): a
# candidate more than this many days from the plan's reference date, or whose
# entry price deviates from the plan's by more than this fraction, is dropped
# from consideration entirely rather than accepted as "closest available."
MATCH_MAX_DATE_GAP_DAYS = 30
MATCH_MAX_PRICE_DEVIATION = 0.20


LOCK_FIELDS = (('r_multiple', 'R'), ('actual_pnl', 'P&L'), ('score', 'Plan Score'), ('stop_outcome', 'Stop'))


def lock_snapshot(row):
    """What a closed trade's result was when it was locked."""
    return {f: (None if _scalar(row.get(f)) is None else row.get(f)) for f, _ in LOCK_FIELDS} | \
        {'currency': _scalar(row.get('currency'))}


def lock_changes(snapshot, row):
    """[(label, was, now)] where the result differs from the locked snapshot."""
    out = []
    for f, label in LOCK_FIELDS:
        was, now = (snapshot or {}).get(f), _scalar(row.get(f))
        if isinstance(was, (int, float)) and isinstance(now, (int, float)):
            if abs(was - now) > 0.005:
                out.append((label, was, now))
        elif (was or None) != (now or None):
            out.append((label, was, now))
    return out


def fmt_lock_value(v):
    return "—" if v is None else (f"{v:,.2f}" if isinstance(v, (int, float)) else str(v))


def plan_lock_updates(rows, locks):
    """(saves, deletes) to bring the locks in line with the journal: a newly closed
    trade (or one a manual match moved) is locked; a lock a manual match replaced on
    a trade that isn't closed is removed. A broken lock waits for the user (Accept)."""
    saves, deletes = [], []
    for r in rows or []:
        pid, state, lk = r.get('plan_id'), r.get('lock_state'), (locks or {}).get(r.get('plan_id'))
        if state == 'broken':
            continue
        if r.get('status') == 'Closed' and r.get('rt_id'):
            if not lk or lk.get('rt_id') != r['rt_id']:
                saves.append((pid, r['rt_id'], None if r.get('exit_time') is None else str(r['exit_time']),
                              lock_snapshot(r)))
        elif lk and state == 'superseded':
            deletes.append(pid)
    return saves, deletes


def sync_plan_locks(rows, locks):
    saves, deletes = plan_lock_updates(rows, locks)
    for pid, rid, closed_at, snap in saves:
        db.save_plan_lock(pid, rid, closed_at, snap)
    for pid in deletes:
        db.delete_plan_lock(pid)
    return len(saves), len(deletes)


def match_plans_to_roundtrips(plans, round_trips_df, open_positions_df=None, manual_matches=None,
                               manual_lot_matches=None, accounts=None, locks=None):
    """
    Match plans to round trips AND still-open LOTS, one-to-one, per
    symbol+direction. Both compete in the same candidate pool so a plan whose
    position hasn't closed yet matches its open lot rather than some
    unrelated prior round trip for the same symbol.

    Three ways a plan can end up matched to an open lot, checked in this order:
      1. A manual override (manual_lot_matches: {plan_id: lot_id}), same
         standing as a manual round-trip override -- an explicit user choice
         shouldn't be second-guessed.
      2. deterministic_plan_id: a lot that build_round_trips could cleanly
         attribute to one specific plan via quantity-bucketing (see
         split_entry_fills_into_lot_buckets) is force-assigned to that plan
         up front, bypassing the fuzzy matcher below entirely -- there's no
         ambiguity to adjudicate.
      3. The fuzzy date/price matcher below, for everything else: a lot with
         no committed plan at all, a lot from a symbol with 0-1 committed
         plans (deterministic_plan_id is always None there), or a spillover/
         fallback lot from a chain that couldn't be unambiguously bucketed.

    A round-trip OR open-lot candidate is only eligible if it's within
    MATCH_MAX_DATE_GAP_DAYS of the plan's reference date (when there's more
    than one candidate to choose from -- see below) AND within
    MATCH_MAX_PRICE_DEVIATION of the plan's entry price -- previously there
    was no such floor, so a plan for a brand-new position could get matched
    to the only available round trip for that symbol no matter how poor the
    fit (e.g. one closed months earlier, at a materially different price)
    and show up as "Closed". A plan with no candidate passing both gates is
    left unmatched rather than forced onto one.

    The date gate is skipped when a candidate is the ONLY one for its
    symbol+direction (round trip or open lot alike -- both get identical
    treatment; there's no longer a special case for one kind vs the other,
    since either kind can now have real competition once a symbol pyramids).
    Without this, a plan correctly matched to a long-held position (logged
    well after entry, which this exemption exists to allow) would silently
    fall to fully unmatched the moment that position closed and became a
    round trip -- confirmed live 2026-08-31 on a real IBB position. Date
    still breaks ties between competing plans below when there IS more than
    one candidate.

    Manual round-trip overrides (db.fetch_plan_matches(): {plan_id: rt_id})
    and manual lot overrides (manual_lot_matches) both win first and bypass
    the gates. Returns (journal_rows, unmatched_rt_ids).

    A round trip that closed before its account's tracking start date (see
    is_archived; `accounts` carries the dates) is never a fuzzy candidate:
    those trades predate written plans. Found 2026-10-26: a new EWZ plan,
    left without fills when an earlier plan for the same position took the
    lot, was matched through the sole-candidate exemption to an EWZ trade
    that closed in August, before the TFSA's start date. A manual match can
    still point at such a trade.

    Locks (db.fetch_plan_locks(), 2026-11-09) come right after manual matches:
    a closed trade keeps its round trip whatever later plans or imports would
    pick. Each row gets lock_state: 'locked'; 'broken' (the locked round trip no
    longer exists -- its fills changed -- so the plan was matched afresh and the
    user is asked to accept); 'superseded' (a manual match replaced it); or None.
    """
    manual_matches = manual_matches or {}
    manual_lot_matches = manual_lot_matches or {}
    valid_rt_ids = set(round_trips_df['rt_id']) if round_trips_df is not None and not round_trips_df.empty else set()
    valid_lot_ids = (set(open_positions_df['lot_id'])
                      if open_positions_df is not None and not open_positions_df.empty else set())
    # Ignore overrides pointing at a round trip/lot that no longer exists (e.g.
    # executions were deleted, or a lot has since closed, since the override was set).
    matches = {pid: rid for pid, rid in manual_matches.items() if rid in valid_rt_ids}
    manual_plan_ids = set(matches.keys())
    for pid, lid in manual_lot_matches.items():
        if lid in valid_lot_ids and pid not in matches:
            matches[pid] = f'open:{lid}'
            manual_plan_ids.add(pid)
    locks = locks or {}
    plan_ids = {p['plan_id'] for p in plans}
    locked_plan_ids = set()
    for pid, lk in locks.items():
        rid = lk.get('rt_id')
        if matches.get(pid) == rid:
            locked_plan_ids.add(pid)  # a manual match on the locked trade itself
        elif pid in plan_ids and pid not in matches and rid in valid_rt_ids and rid not in matches.values():
            matches[pid] = rid
            locked_plan_ids.add(pid)
    used_rt_ids = {v for v in matches.values() if not (isinstance(v, str) and v.startswith('open:'))}
    used_open_lots = {v[len('open:'):] for v in matches.values() if isinstance(v, str) and v.startswith('open:')}

    # Force-assign any lot (open OR already closed into a round trip)
    # build_round_trips could cleanly attribute to a specific plan by
    # quantity-bucketing -- no ambiguity, so it bypasses the fuzzy matcher
    # below entirely (and can't be reassigned by it). A round trip carries
    # this the same way an open lot does (see _rt_dict) since a lot can close
    # mid-call, within the very same build_round_trips invocation that
    # decided the split.
    deterministic_plan_ids = set()
    if open_positions_df is not None and not open_positions_df.empty:
        for _, op in open_positions_df.iterrows():
            det_plan_id = op.get('deterministic_plan_id')
            lid = op['lot_id']
            # pd.notna, not a bare truthiness check: a column mixing real
            # plan-id strings (some rows) with None (others -- the ordinary,
            # non-pyramided case) can come back from a DataFrame as NaN
            # rather than None for the None rows, and bool(float('nan')) is
            # True in Python -- a bare `if det_plan_id` would then force-
            # assign that lot/round-trip to a bogus "plan" whose id is
            # literally NaN, silently blocking its REAL plan from ever
            # reaching the fuzzy matcher below (confirmed live: a closed SSO
            # round trip fell to "Planned (no execution matched yet)"
            # instead of "Closed" for exactly this reason).
            if pd.notna(det_plan_id) and lid not in used_open_lots and det_plan_id not in matches:
                matches[det_plan_id] = f'open:{lid}'
                used_open_lots.add(lid)
                deterministic_plan_ids.add(det_plan_id)
    if round_trips_df is not None and not round_trips_df.empty:
        for _, rt in round_trips_df.iterrows():
            det_plan_id = rt.get('deterministic_plan_id')
            rid = rt['rt_id']
            if pd.notna(det_plan_id) and rid not in used_rt_ids and det_plan_id not in matches:
                matches[det_plan_id] = rid
                used_rt_ids.add(rid)
                deterministic_plan_ids.add(det_plan_id)

    candidates_by_key = {}
    if round_trips_df is not None and not round_trips_df.empty:
        for _, rt in round_trips_df.iterrows():
            if rt['rt_id'] in used_rt_ids:
                continue  # already force-assigned above, not a candidate for the fuzzy matcher
            if accounts and is_archived(rt, accounts):
                continue  # closed before the account's tracking start: never auto-matched
            # Keyed by account and currency too: a plan never matches another
            # account's trade, nor the other listing of the same ticker.
            key = (_scalar(rt.get('account_id')), rt['symbol'], _scalar(rt.get('currency')), rt['direction'])
            candidates_by_key.setdefault(key, []).append(('rt', rt['rt_id'], rt['entry_time'], rt['entry_price'],
                                                          rt.get('first_entry_price')))
    if open_positions_df is not None and not open_positions_df.empty:
        for _, op in open_positions_df.iterrows():
            if op['lot_id'] in used_open_lots:
                continue  # already force-assigned above, not a candidate for the fuzzy matcher
            key = (_scalar(op.get('account_id')), op['symbol'], _scalar(op.get('currency')),
                   op.get('direction', 'BUY'))
            candidates_by_key.setdefault(key, []).append(('open', op['lot_id'], op['entry_time'], op['entry_price'],
                                                          op.get('first_entry_price')))

    max_gap = pd.Timedelta(days=MATCH_MAX_DATE_GAP_DAYS)
    for (account_id, symbol, currency, direction), candidates in candidates_by_key.items():
        candidate_plans = [p for p in plans if p['direction'] == direction
                            and plan_fits_position(p, account_id, symbol, currency)
                            and p['plan_id'] not in matches]
        if not candidate_plans:
            continue
        pairs = []
        for p in candidate_plans:
            p_date = pd.to_datetime(p.get('planned_date') or p.get('created_at'), utc=True, errors='coerce')
            p_entry = p.get('entry_price')
            for kind, cid, c_entry_time, c_avg_price, c_first_price in candidates:
                # A campaign's planned entry is the probe; its average cost
                # climbs with every add, so compare against the first buy.
                c_entry_price = c_first_price if is_campaign(p) and _scalar(c_first_price) is not None \
                    else c_avg_price
                if kind == 'rt' and cid in used_rt_ids:
                    continue
                if kind == 'open' and cid in used_open_lots:
                    continue
                if pd.isna(p_date) or pd.isna(c_entry_time):
                    date_diff = pd.Timedelta(days=9999)
                else:
                    date_diff = abs(c_entry_time - p_date)
                # The date gate exists only to disambiguate between MULTIPLE
                # competing episodes for the same symbol+direction (see
                # 2026-08-27's IBIT fix) -- it should reject a candidate only
                # when there's actually something else it could be confused
                # with, so it's skipped entirely when this is the sole
                # candidate (round trip or open lot alike). Without this, a
                # plan correctly matched to a long-held position (logged well
                # after entry, which this exemption exists to allow) would
                # silently fall to fully unmatched the instant that position
                # closed and became a round trip -- not because anything
                # about the match got worse, but purely because closing
                # re-triggers a gate that was never meant to apply when
                # there's no ambiguity to resolve (confirmed live, 2026-08-31:
                # IBB's plan, written 64 days after entry, tracked the
                # position correctly while open, then dropped to "Planned (no
                # execution matched yet)" the moment it closed). Date still
                # breaks ties between competing plans below when there IS
                # more than one candidate.
                if len(candidates) > 1 and date_diff > max_gap:
                    continue
                # A day trade only matches a position opened on its own day (Strategy 4.5).
                if is_day_trade(p) and (pd.isna(p_date) or pd.isna(c_entry_time)
                                        or c_entry_time.date() != p_date.date()):
                    continue
                if p_entry not in (None, '') and c_entry_price not in (None, '') and not pd.isna(c_entry_price):
                    try:
                        p_entry_f = float(p_entry)
                        price_dev = abs(float(c_entry_price) - p_entry_f) / p_entry_f if p_entry_f else 0
                    except (TypeError, ValueError, ZeroDivisionError):
                        price_dev = 0
                    if price_dev > MATCH_MAX_PRICE_DEVIATION:
                        continue
                pairs.append((date_diff, p['plan_id'], kind, cid))
        pairs.sort(key=lambda x: x[0])
        claimed_plans = set()
        for date_diff, plan_id, kind, cid in pairs:
            if plan_id in claimed_plans:
                continue
            if kind == 'rt' and cid in used_rt_ids:
                continue
            if kind == 'open' and cid in used_open_lots:
                continue
            claimed_plans.add(plan_id)
            if kind == 'rt':
                matches[plan_id] = cid
                used_rt_ids.add(cid)
            else:
                matches[plan_id] = f'open:{cid}'
                used_open_lots.add(cid)

    rt_lookup = {}
    if round_trips_df is not None and not round_trips_df.empty:
        rt_lookup = {r['rt_id']: r for r in round_trips_df.to_dict('records')}
    open_lookup = {}
    if open_positions_df is not None and not open_positions_df.empty:
        open_lookup = {op['lot_id']: op for op in open_positions_df.to_dict('records')}

    journal_rows = []
    for p in plans:
        match_id = matches.get(p['plan_id'])
        rt = open_pos = None
        if isinstance(match_id, str) and match_id.startswith('open:'):
            open_pos = open_lookup.get(match_id[len('open:'):])
        elif match_id is not None:
            rt = rt_lookup.get(match_id)
        row = build_journal_row(p, rt=rt, open_pos=open_pos)
        row['manual_match'] = p['plan_id'] in manual_plan_ids
        row['deterministic_match'] = p['plan_id'] in deterministic_plan_ids
        row['rt_id'] = match_id if rt is not None else None
        lk = locks.get(p['plan_id'])
        if p['plan_id'] in locked_plan_ids:
            row['lock_state'] = 'locked'
            row['lock_changes'] = lock_changes(lk.get('snapshot'), row)
        elif lk:
            row['lock_state'] = 'broken' if lk.get('rt_id') not in valid_rt_ids else 'superseded'
            row['lock_changes'] = lock_changes(lk.get('snapshot'), row) if row['lock_state'] == 'broken' else []
        else:
            row['lock_state'] = None
            row['lock_changes'] = []
        row['lock'] = lk
        journal_rows.append(row)

    unmatched_rt_ids = []
    if round_trips_df is not None and not round_trips_df.empty:
        unmatched_rt_ids = [rid for rid in round_trips_df['rt_id'] if rid not in used_rt_ids]

    return journal_rows, unmatched_rt_ids


def _reconcile_closed_lot_matches(op_df, rt_df):
    """A manual plan_lot_matches override (see db.plan_lot_matches) only ever
    describes the OPEN phase of a lot. Once that lot fully closes into a
    round trip, its lot_id no longer appears in op_df at all -- without this
    step the override would silently orphan (never migrated, never cleaned
    up) instead of continuing to pin the same plan to what is now a round
    trip. Called wherever build_round_trips' output is consumed alongside a
    plan_lot_matches read (see _load_initial_state, import_csv) since it
    needs both DataFrames to detect a lot that vanished and find its match."""
    plan_lot_matches = db.fetch_plan_lot_matches()
    if not plan_lot_matches:
        return
    still_open_lot_ids = set(op_df['lot_id']) if op_df is not None and not op_df.empty else set()
    if rt_df is None or rt_df.empty:
        return
    # A round trip's lot_id-while-open equivalent is the hash of just its
    # entry fills (see lot_id()'s docstring) -- recomputed here from
    # entry_fill_ids since round trips only exist post-close and never
    # stored their own lot_id directly.
    rt_lot_id_lookup = {
        hashlib.sha1(('lot:' + ','.join(map(str, sorted(r['entry_fill_ids'])))).encode()).hexdigest()[:12]: r['rt_id']
        for r in rt_df.to_dict('records')
    }
    for plan_id, lid in plan_lot_matches.items():
        if lid in still_open_lot_ids:
            continue
        new_rt_id = rt_lot_id_lookup.get(lid)
        if new_rt_id:
            db.set_plan_match(plan_id, new_rt_id)
            db.clear_plan_lot_match(plan_id)


def _fmt_money(v):
    return None if v is None or pd.isna(v) else round(float(v), 2)


def _psych_scale_label(value, neg_label, pos_label, mid_label="Neutral"):
    """Render a -2..+2 psych-scale value (see psych_sliders_block) as a short
    readable phrase, e.g. 'Somewhat Fearful (-1)'. 0/None reads as mid_label."""
    if value in (None, 0):
        return f"{mid_label} (0)"
    value = int(value)
    intensity = "Very" if abs(value) == 2 else "Somewhat"
    direction = pos_label if value > 0 else neg_label
    return f"{intensity} {direction} ({value:+d})"


# Psych model v2 (2026-09-29). Each axis is a single dimension with opposite
# ends of the SAME thing -- the v1 model's Fear<->FOMO mixed two unrelated
# emotions. 'center' axes are ideal at 0 (both ends are distortions);
# 'high' axes are better the higher they go, so only the negative side counts
# as a distortion (being Sharp is never a penalty).
PSYCH_AXES = [
    {'key': 'psych_risk', 'id': 'psych-risk', 'title': 'Risk emotion',
     'neg': 'Fearful', 'mid': 'Calm', 'pos': 'Overconfident', 'ideal': 'center'},
    {'key': 'psych_urgency', 'id': 'psych-urgency', 'title': 'Urgency',
     'neg': 'Complacent', 'mid': 'Patient', 'pos': 'FOMO', 'ideal': 'center'},
    {'key': 'psych_focus', 'id': 'psych-focus', 'title': 'Focus',
     'neg': 'Fatigued / distracted', 'mid': 'Neutral', 'pos': 'Sharp', 'ideal': 'high'},
]

# Why the trade was opened -- a motive, not a feeling, so a multi-select
# rather than a slider. 'planned_setup' is the baseline every other trigger
# is compared against in the coaching aggregates.
TRADE_TRIGGERS = [
    ('planned_setup', 'Planned setup (per system)'),
    ('chased_breakout', 'Chased a breakout / late entry'),
    ('revenge', 'Revenge / make-back after a loss'),
    ('boredom', 'Boredom / wanting action'),
    ('news_tip', 'News / tip / social-driven'),
]
TRADE_TRIGGER_LABELS = dict(TRADE_TRIGGERS)

# A trigger/psych bucket needs at least this many closed trades before its
# averages are reported -- 2 revenge trades prove nothing either way.
PSYCH_MIN_SAMPLE = 5

# "Your last trade was a loss N days ago" nudge on the Add Plan form.
REVENGE_HINT_LOOKBACK_DAYS = 3


def _psych_val(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return int(v)


def has_psych_v2(record):
    return any(_psych_val(record.get(a['key'])) is not None for a in PSYCH_AXES)


def psych_distortion_index(record):
    """0 (calm, patient, not fatigued) .. 6 (maximally distorted). None if the
    record predates the v2 psych model."""
    if not has_psych_v2(record):
        return None
    total = 0
    for a in PSYCH_AXES:
        v = _psych_val(record.get(a['key'])) or 0
        total += abs(v) if a['ideal'] == 'center' else max(0, -v)
    return total


def psych_summary_text(record):
    """One-line readable psych reading for a plan or observation, falling back
    to the legacy two-axis reading for pre-v2 rows. None if nothing was rated."""
    if has_psych_v2(record):
        return " | ".join(
            f"{a['title']}: {_psych_scale_label(_psych_val(record.get(a['key'])), a['neg'], a['pos'], a['mid'])}"
            for a in PSYCH_AXES)
    legacy_imp = _psych_val(record.get('psych_impatience'))
    legacy_fomo = _psych_val(record.get('psych_fomo'))
    if legacy_imp or legacy_fomo:
        return ("Legacy reading -- Complacent↔Impatient: "
                f"{_psych_scale_label(legacy_imp, 'Complacent', 'Impatient')} | Fear↔FOMO: "
                f"{_psych_scale_label(legacy_fomo, 'Fear', 'FOMO')}")
    return None


def _psych_outputs(prefix, allow_duplicate=False):
    return [Output(f"{prefix}-{a['id']}", 'value', allow_duplicate=allow_duplicate) for a in PSYCH_AXES]


def _psych_states(prefix):
    return [State(f"{prefix}-{a['id']}", 'value') for a in PSYCH_AXES]


def _psych_fields(values):
    """Zip slider values (in PSYCH_AXES order) into plan/observation fields."""
    return {a['key']: int(v or 0) for a, v in zip(PSYCH_AXES, values)}


def trade_triggers_text(triggers):
    triggers = triggers if isinstance(triggers, list) else []
    return ", ".join(TRADE_TRIGGER_LABELS.get(t, t) for t in triggers) or None


# Data-freshness thresholds: Wealthsimple has no API (see the architecture
# note in CLAUDE.md) -- executions only ever enter TAPEs via a manual CSV
# re-import, so the whole app can silently drift out of sync with the real
# account with no visible signal that it's happened. This matters more now
# that Open Positions has a live risk warning (a stale "OK" reading is worse
# than no reading, since it gives false confidence exactly when accuracy
# matters most) -- these thresholds don't account for weekends/holidays
# (a Friday import checked Monday will read as "stale" even though nothing
# was missed), a deliberate simplification: better an occasional unnecessary
# caution than a silent gap.
DATA_FRESHNESS_CAUTION_HOURS = 24
DATA_FRESHNESS_WARNING_HOURS = 72


def _compute_data_freshness(executions_data):
    """(last_imported_timestamp, hours_ago) from the most recent execution's
    imported_at column, or None if there are no executions at all yet."""
    imported_ats = [e.get('imported_at') for e in (executions_data or []) if e.get('imported_at')]
    if not imported_ats:
        return None
    last = pd.to_datetime(imported_ats, utc=True, errors='coerce').max()
    if pd.isna(last):
        return None
    hours_ago = (pd.Timestamp.now(tz='UTC') - last).total_seconds() / 3600
    return last, hours_ago


def _format_age(hours_ago):
    if hours_ago < 1:
        return f"{max(1, int(hours_ago * 60))} min"
    if hours_ago < 48:
        return f"{hours_ago:.1f} hours"
    return f"{hours_ago / 24:.1f} days"


def _freshness_color(hours_ago):
    if hours_ago > DATA_FRESHNESS_WARNING_HOURS:
        return 'danger'
    if hours_ago > DATA_FRESHNESS_CAUTION_HOURS:
        return 'warning'
    return 'success'


def _render_freshness_banner(executions_data):
    info = _compute_data_freshness(executions_data)
    if info is None:
        return dbc.Alert("No executions imported yet.", color='secondary', className="py-1 mb-0 small")
    last, hours_ago = info
    return dbc.Alert(
        f"Data last imported from Wealthsimple: {last.strftime('%Y-%m-%d %H:%M UTC')} "
        f"({_format_age(hours_ago)} ago). Re-import if you've traded since.",
        color=_freshness_color(hours_ago), className="py-1 mb-0 small")


def _stop_tolerance(stop, atr):
    """(tolerance in price units, atr used or None) -- see STOP_SLIPPAGE_ATR_MULT."""
    if atr not in (None, '') and not pd.isna(atr) and float(atr) > 0:
        return STOP_SLIPPAGE_ATR_MULT * float(atr), float(atr)
    return abs(float(stop)) * STOP_SLIPPAGE_PCT_FALLBACK / 100, None


def classify_stop_exits(direction, exits, atr=None):
    """Classify a trade's stop discipline from its stop-side exits.

    `exits` is a list of (price, stop_in_effect_at_that_fill) for every exit
    fill NOT taken at a target. Each is measured by how far it filled past
    its own stop (direction-aware: below for BUY, above for SELL), relative
    to that stop's slippage tolerance; the worst one decides the outcome:
      'respected' -- no exit filled past its stop
      'slippage'  -- worst exit past its stop by no more than the tolerance
      'breached'  -- worst exit past its stop by more than the tolerance
    Returns {'outcome', 'index', 'price', 'stop', 'past_by', 'tolerance',
    'atr'} for the worst exit, or None if there's nothing to check."""
    sign = 1 if direction == 'BUY' else -1
    worst = None
    for i, (price, stop) in enumerate(exits):
        if stop in (None, '') or price is None:
            continue
        past_by = sign * (float(stop) - float(price))
        tolerance, atr_used = _stop_tolerance(stop, atr)
        severity = past_by / tolerance if tolerance else past_by
        if worst is None or severity > worst['severity']:
            worst = {'index': i, 'price': price, 'stop': stop, 'past_by': past_by,
                     'tolerance': tolerance, 'atr': atr_used, 'severity': severity}
    if worst is None:
        return None
    if worst['past_by'] <= 0:
        worst['outcome'] = 'respected'
    elif worst['past_by'] <= worst['tolerance']:
        worst['outcome'] = 'slippage'
    else:
        worst['outcome'] = 'breached'
    worst.pop('severity')
    return worst


def _slippage_text(worst):
    if worst['atr']:
        return (f"{worst['past_by'] / worst['atr']:.3f} ATR", f"{STOP_SLIPPAGE_ATR_MULT:.2f} ATR")
    return (f"{worst['past_by'] / abs(worst['stop']) * 100:.2f}%", f"{STOP_SLIPPAGE_PCT_FALLBACK:.2f}%")


def analyze_targets_and_stop(direction, entry_planned, initial_stop, targets, exit_fills,
                             atr=None, trailing=None):
    """
    Replay a scale-out plan (initial protective stop + up to 5 price targets,
    each optionally moving the stop when hit) against the actual exit fills
    of a closed round trip.

    Matching is intentionally simple: targets are checked in the order
    they'd naturally be reached (nearest-to-entry first), and each target is
    matched to the earliest not-yet-attributed exit fill whose price reached
    that level. This is a reasonable approximation for journaling purposes,
    not an exact fill-by-fill reconciliation (a single exit fill can only be
    attributed to one target).

    Stop discipline: every exit fill not taken at a target is checked against
    the stop in effect AT THAT FILL'S TIME, with a slippage tolerance from
    `atr` (see classify_stop_exits). The stop in effect is the initial stop,
    raised by each target's stop_after once that target's fill has happened.
    `trailing` (optional, for the atr_trail/ema_trail stop methods) layers on
    top and can only ever improve that stop, never lower it:
      {'activate_after_target': N,      # 1-based, nearest-to-entry order
       'entry_floor': price or None,    # stop >= entry once T1 is hit
       'series': compute_trailing_series(...) or None}
    The trailing level in effect on a fill's date is the last one set at a
    close BEFORE that date (trail_level_before). The journal passes only the
    entry floor (no series) for closed trades -- building the series needs a
    network fetch, which journal builds deliberately avoid.

    Returns (target_results, targets_hit, targets_total, final_effective_stop, stop_note,
    stop_breached, stop_outcome). stop_outcome is 'respected' / 'slippage' /
    'breached' when there are exits and a stop to check against, else None;
    stop_breached is True only for 'breached' (kept as its own boolean so
    callers can key the "exited contrary to plan" prompt off it reliably).
    """
    sign = 1 if direction == 'BUY' else -1
    clean_targets = [t for t in (targets or []) if t.get('price') not in (None, '')]
    # Nearest-to-entry first: ascending price for longs, descending for shorts.
    sorted_targets = sorted(clean_targets, key=lambda t: t['price'], reverse=(sign == -1))

    fills = sorted(exit_fills or [], key=lambda f: f['time'])
    fill_remaining = [f['qty'] for f in fills]
    target_fill_idx = set()

    current_stop = initial_stop
    target_results = []

    for t in sorted_targets:
        target_price = t['price']
        hit, matched_price, matched_qty, matched_time = False, None, None, None
        for i, f in enumerate(fills):
            if fill_remaining[i] <= 1e-9:
                continue
            reached = (f['price'] >= target_price) if sign == 1 else (f['price'] <= target_price)
            if reached:
                hit = True
                matched_price, matched_time = f['price'], f['time']
                matched_qty = fill_remaining[i]
                fill_remaining[i] = 0
                target_fill_idx.add(i)
                break
        if hit and t.get('stop_after') not in (None, ''):
            current_stop = t['stop_after']
        target_results.append({
            'price': target_price,
            'qty_pct': t.get('qty_pct'),
            'stop_after': t.get('stop_after'),
            'hit': hit,
            'matched_price': matched_price,
            'matched_qty': matched_qty,
            'matched_time': matched_time,
            'stop_after_hit': current_stop if hit else None,
        })

    targets_hit = sum(1 for r in target_results if r['hit'])
    targets_total = len(target_results)

    def _better(a, b):
        if a in (None, ''):
            return b
        if b in (None, ''):
            return a
        return max(a, b) if sign == 1 else min(a, b)

    floor_from, trail_from = None, None
    if trailing:
        if target_results and target_results[0]['hit'] and trailing.get('entry_floor') is not None:
            floor_from = target_results[0]['matched_time']
        idx = trailing['activate_after_target'] - 1
        if 0 <= idx < len(target_results) and target_results[idx]['hit']:
            trail_from = target_results[idx]['matched_time']

    def _stop_in_effect_at(when):
        stop = initial_stop
        for r in target_results:  # matched times are non-decreasing in ladder order
            if r['hit'] and (when is None or r['matched_time'] <= when):
                stop = r['stop_after_hit']
        if floor_from is not None and (when is None or floor_from <= when):
            stop = _better(stop, trailing['entry_floor'])
        if trail_from is not None and (when is None or trail_from <= when):
            stop = _better(stop, trail_level_before(trailing.get('series'), when))
        return stop

    if trailing:
        current_stop = _stop_in_effect_at(None)

    stop_note, stop_breached, stop_outcome = None, None, None
    if fills and current_stop not in (None, ''):
        exit_idx = [i for i in range(len(fills)) if i not in target_fill_idx]
        worst = classify_stop_exits(
            direction, [(fills[i]['price'], _stop_in_effect_at(fills[i]['time'])) for i in exit_idx], atr)
        final_fill = fills[-1]
        if worst is None or worst['outcome'] == 'respected':
            stop_outcome = 'respected'
            stop_note = (f"Final exit at {final_fill['price']:.2f} respected the stop level in effect "
                         f"at that point ({current_stop:.2f})")
        else:
            stop_outcome = worst['outcome']
            which = "Final exit" if exit_idx[worst['index']] == len(fills) - 1 else "Exit"
            past, tol = _slippage_text(worst)
            if stop_outcome == 'slippage':
                stop_note = (f"{which} at {worst['price']:.2f} hit the stop ({worst['stop']:.2f}) "
                             f"with {past} slippage, within tolerance ({tol})")
            else:
                stop_note = (f"{which} at {worst['price']:.2f} breached the stop level in effect at "
                             f"that point ({worst['stop']:.2f}) by {past}, beyond the {tol} "
                             f"slippage tolerance")
        stop_breached = stop_outcome == 'breached'

    return target_results, targets_hit, targets_total, current_stop, stop_note, stop_breached, stop_outcome


def compute_plan_drift(direction, entry_planned, initial_stop, targets, exit_fills,
                        qty_total_entered, qty_open, current_price, trailing=None):
    """Compare a plan's ACTUAL progress (from real exit fills, via
    analyze_targets_and_stop) against what LIVE PRICE says should have
    happened by now. Wealthsimple has no API -- fills only reach TAPEs via
    manual CSV re-import -- so price can move past a target, or even through
    the stop, before the corresponding Wealthsimple order is placed or
    filled and shows up here. This closes a real gap in
    analyze_targets_and_stop itself: its own stop-breach check only ever
    evaluates against the LAST EXIT FILL's price, so a position with zero
    exits so far (the exact case this matters most for) never gets checked
    against a stop at all today.

    Reuses analyze_targets_and_stop's own nearest-to-entry-first
    target_results and ratcheted stop for the "actual" side rather than
    re-deriving the same sort/ratchet logic -- this only adds a second,
    price-only pass over the targets analyze_targets_and_stop ALREADY
    determined hadn't been hit by a real fill, checking instead whether
    live price has reached them anyway.

    Returns a dict:
      actual_stop            -- the real, execution-based ratcheted stop (unchanged from
                                 analyze_targets_and_stop)
      should_be_stop          -- actual_stop further ratcheted through any not-yet-executed
                                 target that live price has already reached
      next_target              -- the first not-yet-executed target (nearest-to-entry order),
                                 or None if every target is already hit or there are none
      next_target_reached      -- True if live price has already reached next_target's price
      stop_breached_live       -- True if live price is now past should_be_stop
      should_be_qty_open        -- qty_total_entered reduced by every target (executed OR
                                 live-price-reached) -- what you'd be holding if every
                                 triggered scale-out had actually been placed
      actual_qty_open          -- passed through unchanged, for convenience at the call site
      qty_drift                -- actual_qty_open - should_be_qty_open; positive means
                                 you're holding more than the plan says you should be by now
    Degrades correctly with no targets at all (should_be_qty_open stays
    qty_total_entered, but stop_breached_live still checks live price against
    initial_stop -- the single most valuable check, since it catches a naked
    stop violation even on a plan with no scale-out ladder).

    `trailing` (optional; same dict as analyze_targets_and_stop's, built by
    _build_trailing_for_row) affects the stop ONLY, never the scale-out
    ladder, and can only improve the stop, never lower it. actual_stop gets
    the entry floor / trailing level once T1 / the activation target was
    actually hit by a fill; should_be_stop also gets them once those targets
    are merely live-price-reached. should_be_stop is the level to set for the
    next session. Pure: the caller pre-fetches the series."""
    sign = 1 if direction == 'BUY' else -1

    def _better(a, b):
        if a in (None, ''):
            return b
        if b in (None, ''):
            return a
        return max(a, b) if sign == 1 else min(a, b)

    target_results, _, _, actual_stop, _, _, _ = analyze_targets_and_stop(
        direction, entry_planned, initial_stop, targets, exit_fills, trailing=trailing)

    should_be_stop = actual_stop
    should_be_qty_pct_sold = sum(r['qty_pct'] or 0 for r in target_results if r['hit'])
    next_target = next((r for r in target_results if not r['hit']), None)
    next_target_reached = False
    live_reached = [False] * len(target_results)

    for i, r in enumerate(target_results):
        if r['hit']:
            live_reached[i] = True
            continue
        reached = (current_price >= r['price']) if sign == 1 else (current_price <= r['price'])
        if not reached:
            break  # nearest-first order -- once one isn't reached, none further can be either
        live_reached[i] = True
        should_be_qty_pct_sold += r['qty_pct'] or 0
        should_be_stop = _better(should_be_stop, r.get('stop_after'))
        if r is next_target:
            next_target_reached = True

    trail_level, trailing_active = None, False
    if trailing and target_results:
        if trailing.get('entry_floor') is not None and live_reached[0]:
            should_be_stop = _better(should_be_stop, trailing['entry_floor'])
        idx = trailing['activate_after_target'] - 1
        if 0 <= idx < len(target_results) and live_reached[idx]:
            trail_level = trail_level_before(trailing.get('series'))
            trailing_active = trail_level is not None
            should_be_stop = _better(should_be_stop, trail_level)

    should_be_qty_open = qty_total_entered * (1 - should_be_qty_pct_sold / 100)
    stop_breached_live = should_be_stop not in (None, '') and (
        (current_price < should_be_stop) if sign == 1 else (current_price > should_be_stop))

    return {
        'actual_stop': actual_stop,
        'should_be_stop': should_be_stop,
        'next_target': next_target,
        'next_target_reached': next_target_reached,
        'stop_breached_live': stop_breached_live,
        'should_be_qty_open': should_be_qty_open,
        'actual_qty_open': qty_open,
        'qty_drift': qty_open - should_be_qty_open,
        'trailing_active': trailing_active,
        'trail_level': trail_level,
    }


def _fill_list_of(record, key):
    """A fill-list field from a record that may have been through a
    DataFrame/JSON round trip (NaN or missing -> [])."""
    value = record.get(key)
    return value if isinstance(value, list) else []


def compute_campaign_drift(plan, entry_fills, exit_fills, qty_open, current_price, trailing=None):
    """compute_plan_drift for a scale-in campaign: the next step is an ADD,
    not a target. Live price reaching an add's trigger means "raise the stop
    to that add's stop_before, then buy" -- should_be_stop and
    should_be_qty_open move with every add live price has reached, so a
    negative qty_drift means shares still to buy. The trailing level applies
    once the trail's start (probe, or add k) is in -- actually for
    actual_stop, live-reached for should_be_stop -- and never lowers the
    scheduled stop. Same return keys as compute_plan_drift (next_target is
    None; next_target_reached mirrors next_add_reached so the report sorts
    the same way), plus plan_type, next_add, next_add_reached, adds_done and
    adds_planned. Pure: the caller pre-fetches the trailing series."""
    direction = plan['direction']
    sign = 1 if direction == 'BUY' else -1

    def _better(a, b):
        if a in (None, ''):
            return b
        if b in (None, ''):
            return a
        return max(float(a), float(b)) if sign == 1 else min(float(a), float(b))

    analysis = analyze_campaign(plan, entry_fills, exit_fills)
    adds = [a for a in (plan.get('adds') or []) if a.get('price') not in (None, '')]
    planned_qty = [float(plan.get('qty') or 0)] + [float(a.get('qty') or 0) for a in adds]
    done = analysis['adds_done']
    actual_stop = should_be_stop = analysis['scheduled_stop']
    reached = done
    if analysis['extra_qty'] <= 1e-9:
        for a in adds[done:]:
            hit = (current_price >= float(a['price'])) if sign == 1 else (current_price <= float(a['price']))
            if not hit:
                break
            reached += 1
            should_be_stop = _better(should_be_stop, a.get('stop_before'))

    trail_level, trailing_active = None, False
    if trailing:
        k = int(trailing.get('activate_after_add') or 0)
        level = trail_level_before(trailing.get('series'))
        if level is not None and done >= k:
            actual_stop = _better(actual_stop, level)
        if level is not None and reached >= k:
            trail_level, trailing_active = level, True
            should_be_stop = _better(should_be_stop, level)

    # A same-day trim of an over-buy (find_overbuy_trims) sold shares the plan
    # never called for, so it isn't a reduction of the planned position.
    sold = sum(f['qty'] for f in exit_fills or []) - sum(t['qty'] for t in analysis['trims'])
    should_be_qty_open = max(0.0, sum(planned_qty[:reached + 1]) - sold)
    stop_breached_live = should_be_stop not in (None, '') and (
        (current_price < float(should_be_stop)) if sign == 1 else (current_price > float(should_be_stop)))
    next_add = analysis['next_add']
    return {
        'actual_stop': actual_stop, 'should_be_stop': should_be_stop,
        'next_target': None, 'next_target_reached': reached > done,
        'stop_breached_live': stop_breached_live,
        'should_be_qty_open': should_be_qty_open, 'actual_qty_open': qty_open,
        'qty_drift': qty_open - should_be_qty_open,
        'trailing_active': trailing_active, 'trail_level': trail_level,
        'plan_type': 'scale_in', 'next_add': next_add, 'next_add_reached': reached > done,
        'adds_done': done, 'adds_planned': analysis['adds_planned'],
    }


def _drift_for_row(row, pos, current_price, trailing):
    """compute_plan_drift or compute_campaign_drift for one journal row and
    its open lot -- shared by the drift report's UI callback and MCP tool."""
    if is_campaign(row):
        return compute_campaign_drift(row, _fill_list_of(pos, 'entry_fill_list'),
                                      _fill_list_of(pos, 'exit_fill_list'), pos['qty_open'], current_price,
                                      trailing=trailing)
    result = compute_plan_drift(
        row['direction'], row.get('entry_price'), row.get('stop_loss'), row.get('targets') or [],
        pos.get('exit_fill_list') or [], pos['qty_total_entered'], pos['qty_open'], current_price,
        trailing=trailing)
    if is_day_trade(row):
        # Still open after its session: the whole position should be sold (Strategy 4.5).
        day = str(pos.get('entry_time') or '')[:10]
        result['plan_type'] = 'day_trade'
        result['day_trade_overdue'] = bool(day) and day < dt.date.today().isoformat()
        if result['day_trade_overdue']:
            result['should_be_qty_open'] = 0.0
            result['qty_drift'] = float(pos['qty_open'])
    return result


def stop_method_of(record):
    """'ladder' | 'atr_trail' | 'ema_trail'. Plans saved before stop_method
    existed fall back to their old trailing_stop_enabled switch."""
    method = record.get('stop_method')
    if method in dict(STOP_METHODS):
        return method
    enabled = record.get('trailing_stop_enabled')
    if enabled and not (isinstance(enabled, float) and np.isnan(enabled)):
        return 'atr_trail'
    return 'ladder'


def _stop_action(direction, actual_stop, should_be_stop):
    """'raise' if the stop to set improves on the current one, else 'hold'."""
    if should_be_stop in (None, ''):
        return None
    if actual_stop in (None, ''):
        return 'raise'
    sign = 1 if direction == 'BUY' else -1
    return 'raise' if sign * (float(should_be_stop) - float(actual_stop)) > 1e-9 else 'hold'


def _trailing_spec(record, entry_price):
    """The no-network part of a trailing plan's spec: activation target and
    the entry floor (stop >= actual entry once T1 is hit). None for ladder."""
    method = stop_method_of(record)
    if method == 'ladder':
        return None
    activate = int(_psych_val(record.get('trailing_activate_after_target')) or 1)
    return {'method': method, 'activate_after_target': activate,
            'atr_multiplier': record.get('trailing_atr_multiplier') or 2.0,
            'atr_timeframe': atr_timeframe_of(record) if method == 'atr_trail' else DEFAULT_ATR_TIMEFRAME,
            'entry_floor': float(entry_price) if entry_price not in (None, '') else None,
            'series': None}


def _build_trailing_for_row(row, pos, fetch_history=None, fetch_four_hour=None):
    """Trailing spec for an OPEN lot (drift report, both the UI callback and
    the MCP tool), including the daily trailing-level series. The series
    starts at the activation target's own fill date if it was hit, else today
    (the target is at most live-price-reached -- there's no earlier anchor).
    The entry floor uses the lot's ACTUAL average entry, not the planned one.

    Returns (trailing, error). trailing is None for ladder plans. A history
    fetch failure is returned as `error`, not raised; the spec still carries
    the entry floor so that lot's report degrades to ladder + floor."""
    trailing = _trailing_spec(row, pos.get('entry_price') or row.get('entry_price'))
    if trailing is None:
        return None, None
    activation = dt.date.today().isoformat()
    campaign = is_campaign(row)
    if campaign:
        # A campaign's trail starts at the probe (trail_start_add 0) or once
        # add k is in, and has no entry floor -- the add ladder's own stop
        # schedule is the floor.
        k = int(_scalar(row.get('trail_start_add')) or 0)
        trailing.update({'entry_floor': None, 'activate_after_add': k})
        stages = analyze_campaign(row, _fill_list_of(pos, 'entry_fill_list'),
                                  _fill_list_of(pos, 'exit_fill_list'))['stages']
        if k < len(stages) and stages[k]['time']:
            activation = stages[k]['time']
    else:
        target_results = analyze_targets_and_stop(
            row['direction'], row.get('entry_price'), row.get('stop_loss'), row.get('targets') or [],
            pos.get('exit_fill_list') or [])[0]
        idx = trailing['activate_after_target'] - 1
        if 0 <= idx < len(target_results) and target_results[idx]['hit'] and target_results[idx].get('matched_time'):
            activation = target_results[idx]['matched_time']
    try:
        currency = _scalar(pos.get('currency'))
        fetcher = fetch_history or (lambda sym, since: fetch_daily_history(sym, since, currency=currency))
        bars = fetcher(pos['symbol'], activation)
        # The ATR trail's distance follows the plan's ATR timeframe; the EMA
        # trail is always daily (its EMA and buffer both come from daily bars).
        trail_atr = None
        if trailing['method'] == 'atr_trail' and trailing['atr_timeframe'] != DEFAULT_ATR_TIMEFRAME:
            four_hour = None
            if trailing['atr_timeframe'] == '240':
                four_hour = (fetch_four_hour or (lambda sym, since: fetch_four_hour_history(
                    sym, since, currency=currency)))(pos['symbol'], activation)
            trail_atr = timeframe_atr_by_day(bars, trailing['atr_timeframe'], four_hour)
        trailing['series'] = compute_trailing_series(
            bars, row['direction'], trailing['method'], activation,
            trailing['entry_floor'], trailing['atr_multiplier'], trail_atr=trail_atr,
            ema_gate='close' if campaign else 'entry')
        return trailing, None
    except Exception as e:
        return trailing, str(e)


def drift_daily_atr(pos, cache, fetch_history=None):
    """(atr, error): the lot's daily ATR(14) for the drift report, whatever
    timeframe its plan uses. Completed bars only, like the trailing levels, and
    fetched once per listing (`cache`, keyed by price_key, covers pyramided
    lots). A failed fetch is returned as `error`, not raised."""
    key = price_key(pos)
    if key not in cache:
        try:
            currency = _scalar(pos.get('currency'))
            fetcher = fetch_history or (lambda sym, since: fetch_daily_history(sym, since, currency=currency))
            cache[key] = (float(compute_atr(fetcher(pos['symbol'], dt.date.today().isoformat())).dropna().iloc[-1]),
                          None)
        except Exception as e:
            cache[key] = (None, str(e))
    return cache[key]


LADDER_PCT_TOLERANCE = 0.01


def compute_plan_ladder(direction, entry, stop, targets, qty, gap=0.0):
    """Pre-trade risk/reward that follows the plan's scale-out ladder
    (2026-10-10). targets are the plan's {price, qty_pct, stop_after} dicts.

    - max_loss: the full position stopped out at the initial stop before any
      target -- the true worst case (and what the risk caps use).
    - ladder_reward: each target's Qty % of the position exiting at that
      target -- the plan's own full outcome, the same weighting the Plan
      Score's blended target uses after the trade.
    - best_target_reward: the old figure (everything exiting at the furthest
      target), kept only as the fallback for a plan with no Qty % at all.
    - reward / rr: ladder_reward and its ratio to max_loss, or the
      best-target figure when no target has a Qty % (reward_basis says which).
    - scenarios: the result if stopped out after each stage -- before T1,
      after T1, after T1+T2, ... -- with the slices already sold at their
      targets and the rest stopped at the stop in effect then (each hit
      target's "Move Stop To" replaces the stop, the same rule the journal's
      replay uses). For a trailing-stop plan that stop is the floor; a trail
      can only do better.
    - max_loss_gap / rr_gap: the same with `gap` (gap allowance per share,
      see gap_per_share) added to the stop distance -- a stop-out that a gap
      jumps. None when gap is 0.
    Targets are taken nearest-to-entry first. Returns None without an entry
    or quantity."""
    try:
        entry_f, qty_f = float(entry), float(qty)
    except (TypeError, ValueError):
        return None
    sign = 1 if direction == 'BUY' else -1

    def num(v):
        try:
            return float(v) if v not in (None, '') else None
        except (TypeError, ValueError):
            return None

    stop_f = num(stop)
    ladder = [{'price': num(t.get('price')), 'pct': num(t.get('qty_pct')) or 0.0,
               'stop_after': num(t.get('stop_after'))} for t in (targets or [])]
    ladder = sorted([t for t in ladder if t['price'] is not None], key=lambda t: t['price'],
                    reverse=(sign == -1))

    def pnl(price, pct):
        return sign * (price - entry_f) * qty_f * pct / 100

    max_loss = abs(entry_f - stop_f) * qty_f if stop_f is not None else None
    best_target_reward = max((abs(t['price'] - entry_f) * qty_f for t in ladder), default=None) \
        if ladder else None
    assigned = sum(t['pct'] for t in ladder)
    ladder_reward = sum(pnl(t['price'], t['pct']) for t in ladder) if assigned > 0 else None
    if ladder_reward is not None:
        reward, basis = ladder_reward, 'ladder'
    else:
        reward, basis = best_target_reward, 'best_target'
    rr = reward / max_loss if (reward is not None and max_loss) else None
    gap = float(gap or 0.0)
    max_loss_gap = (abs(entry_f - stop_f) + gap) * qty_f if (stop_f is not None and gap) else None
    rr_gap = reward / max_loss_gap if (reward is not None and max_loss_gap) else None

    scenarios = []
    if stop_f is not None and ladder_reward is not None:
        def r_of(v):
            return v / max_loss if max_loss else None
        loss = pnl(stop_f, 100)
        scenarios.append({'label': "Stopped out before T1", 'pnl': loss, 'r': r_of(loss),
                          'stop': stop_f, 'exited_pct': 0.0})
        current_stop, sold, banked = stop_f, 0.0, 0.0
        for k, t in enumerate(ladder, start=1):
            if t['pct'] <= 0:
                continue
            sold += t['pct']
            banked += pnl(t['price'], t['pct'])
            if t['stop_after'] is not None:
                current_stop = t['stop_after']
            rest = max(0.0, 100 - sold)
            hit = "T1" if k == 1 else f"T1–T{k}"
            if rest <= LADDER_PCT_TOLERANCE:
                label = "All targets hit" if k == len(ladder) else f"{hit} hit (position fully sold)"
                scenarios.append({'label': label, 'pnl': banked, 'r': r_of(banked), 'stop': None,
                                  'exited_pct': sold})
                break
            total = banked + pnl(current_stop, rest)
            scenarios.append({'label': f"{hit} hit, remaining {rest:g}% stopped", 'pnl': total,
                              'r': r_of(total), 'stop': current_stop, 'exited_pct': sold})
    return {'max_loss': max_loss, 'ladder_reward': ladder_reward, 'best_target_reward': best_target_reward,
            'reward': reward, 'reward_basis': basis, 'rr': rr, 'assigned_pct': assigned,
            'gap_per_share': gap, 'max_loss_gap': max_loss_gap, 'rr_gap': rr_gap,
            'ladder_complete': abs(assigned - 100) <= LADDER_PCT_TOLERANCE, 'scenarios': scenarios}


# ---- Scale-in campaigns (2026-10-19) ----
# A reversal strategy: a small probe position, then adds as the reversal
# confirms (each add's trigger is beyond the last), exiting on a trailing stop
# only. The rules agreed with the user:
#   - adds are equal-sized for now (each add's qty is stored, so scaled adds
#     later are a form option, not a data change);
#   - the stop must move up BEFORE each add (stop_before), strictly ratcheting;
#   - adds only on confirmation -- never averaging down;
#   - the 2% per-trade cap applies to the campaign's highest-risk stage;
#   - the trail can run from the probe (trail_start_add = 0) or start after add k.
PLAN_TYPES = [('scale_out', 'Scale-out (targets)'), ('scale_in', 'Scale-in campaign'), ('day_trade', 'Day trade')]
DEFAULT_CAMPAIGN_ADDS = 3


def plan_type_of(record):
    """'scale_in' for a campaign, 'day_trade' for a day trade; everything else
    (incl. every plan from before either existed) is the original 'scale_out'
    targets plan."""
    t = _scalar(record.get('plan_type')) if record is not None else None
    return t if t in ('scale_in', 'day_trade') else 'scale_out'


def is_campaign(record):
    return plan_type_of(record) == 'scale_in'


# ---- Day trades (2026-11-01, Strategy 4.5) ----
# A news-catalyst trade opened and closed in one session. It is a scale-out
# plan underneath (entry, intraday stop, T1/T2, Ladder stop), so scoring, R,
# heat and the gap allowance work unchanged. On top: a written catalyst and a
# named setup (both required), 5m/15m ATR, T1 moves the stop to the entry, it
# owns its symbol in its account (no day trade on a held symbol), it only
# matches a position opened on its own day, and it's flagged when still open
# after its session. The entry window (9:45-14:30 ET) and the 15:45 time stop
# aren't checked: export times can't be trusted to the minute.
DAY_SETUPS = [('panic_fade', 'Panic fade'), ('gap_and_go', 'Gap-and-go'), ('vwap_pullback', 'VWAP pullback')]
DAY_TRADE_RISK_FRACTION = 0.5     # of the per-trade cap (half the effective per-trade risk)
DAY_TRADE_MAX_PER_DAY = 2
DAY_TRADE_DAILY_LOSS_PCT = 1.0    # of the portfolio: no more day trades that day once reached
DAY_TRADE_TIME_STOP = '15:45 ET'
DAY_TRADE_DEFAULT_TIMEFRAME = '5'
DAY_TRADE_DEFAULT_STOP_ATR = 1.0


def is_day_trade(record):
    return plan_type_of(record) == 'day_trade'


def day_setup_label(value):
    return dict(DAY_SETUPS).get(_scalar(value) or '', None)


def day_trade_targets(direction, entry, targets):
    """A day trade's T1 (the target nearest the entry) moves the stop to the
    entry unless a Move Stop To was typed (Strategy 4.5)."""
    ts = [dict(t) for t in (targets or [])]
    try:
        e = float(entry)
    except (TypeError, ValueError):
        return ts
    priced = [t for t in ts if t.get('price') not in (None, '')]
    if priced:
        sign = 1 if direction == 'BUY' else -1
        t1 = min(priced, key=lambda t: sign * float(t['price']))
        if t1.get('stop_after') in (None, ''):
            t1['stop_after'] = e
    return ts


def validate_day_trade(direction, entry, stop, targets, stop_method, timeframe, catalyst, setup):
    """An error message for a day-trade plan that breaks Strategy 4.5's
    structural rules, or None. The size limits are warnings elsewhere."""
    if not (catalyst or '').strip():
        return "A day trade needs its catalyst written down: what happened, when, and the source (Strategy 4.5)."
    if setup not in dict(DAY_SETUPS):
        return "Pick the day-trade setup: Panic fade, Gap-and-go or VWAP pullback (Strategy 4.5)."
    if timeframe not in dict(INTRADAY_TIMEFRAMES):
        return "A day trade is planned on 5-minute (or 15-minute) bars: set the Timeframe to 5m or 15m."
    if stop_method not in (None, '', 'ladder'):
        return "A day trade uses the Ladder stop method (T1 moves the stop to the entry); it has no trailing stop."
    if stop in (None, ''):
        return "A day trade needs its stop, beyond the setup's invalidation level."
    try:
        e, st = float(entry), float(stop)
    except (TypeError, ValueError):
        return "Entry and stop must be numbers."
    if (1 if direction == 'BUY' else -1) * (e - st) <= 0:
        return "The stop must be on the losing side of the entry."
    if not [t for t in (targets or []) if t.get('price') not in (None, '')]:
        return "A day trade needs its targets: T1 at 1R and T2 at 2R (or the gap fill / prior close)."
    return None


def validate_swing_timeframe(timeframe):
    """5m/15m are for day trades only."""
    if timeframe in dict(INTRADAY_TIMEFRAMES):
        return "5m and 15m are for day trades only: pick 4H, 1D or 1W, or set Plan type to Day trade."
    return None


def _held_conflict(account_id, symbol, currency=None):
    """An error when the symbol is already held in that account: a day trade
    can't share its symbol with a position, or the fills would mix
    (Strategy 4.5). Reads the database's current open positions."""
    _, op = build_round_trips(db.fetch_executions(), db.fetch_plans())
    if op is None or op.empty:
        return None
    sym = (symbol or '').strip().upper()
    held = op[(op['account_id'] == account_id) & (op['symbol'] == sym)]
    if currency:
        held = held[held['currency'].fillna(currency) == currency]
    if held.empty:
        return None
    return (f"{sym} is already held in this account ({held['qty_open'].sum():g} shares). A day trade can't share "
            f"a symbol with a position -- its fills would mix with the position's (Strategy 4.5).")


def _add_day_trade_notes(row, rt=None, open_pos=None, today=None):
    """A day trade's same-day check (Strategy 4.5): flat the same day, open
    today, or carried overnight (row['carried_overnight'])."""
    today = today or dt.date.today().isoformat()
    notes = list(row.get('exec_notes') or [])
    if open_pos is not None:
        day = str(open_pos.get('entry_time') or '')[:10]
        row['carried_overnight'] = bool(day) and day < today
        notes.insert(0, (f"⚠ Day trade still open after its session (entered {day}): sell it -- a day trade is "
                         f"never carried overnight (Strategy 4.5).") if row['carried_overnight']
                     else f"Day trade open: flat by {DAY_TRADE_TIME_STOP}.")
    elif rt is not None:
        d1, d2 = str(rt.get('entry_time') or '')[:10], str(rt.get('exit_time') or '')[:10]
        row['carried_overnight'] = bool(d1 and d2) and d2 > d1
        notes.insert(0, (f"⚠ Day trade carried overnight: entered {d1}, last exit {d2} -- a day trade is flat "
                         f"by the close (Strategy 4.5).") if row['carried_overnight']
                     else "Day trade: flat the same day.")
    else:
        row['carried_overnight'] = None
    row['exec_notes'] = notes
    return row


# ---- Cancelled plans (2026-10-20) ----
# A committed plan abandoned before any fill. Kept, not deleted, with a reason
# (from an editable list), a note, an optional psych reading, and a replay of
# what would have happened had it been kept (replay_cancelled_plan).
DEFAULT_CANCEL_REASONS = [
    "Regime or conditions changed",
    "Price ran away before I could enter",
    "A better opportunity came up",
    "Over the risk cap or heat limit",
    "Changed my mind",
]
DEFAULT_STALE_PLAN_DAYS = 10     # a Planned plan with no fills this old is flagged stale
CANCEL_REPLAY_BARS = 60          # trading days the "what would have happened" replay covers
CANCEL_OUTCOME_TEXT = {'never_filled': 'never filled', 'stopped_out': 'stopped out',
                       'targets_done': 'all targets hit', 'still_open': 'still open'}


def is_cancelled(record):
    return bool(record) and _scalar(record.get('status')) == 'cancelled'


def load_cancel_reasons():
    saved = db.get_setting('cancel_reasons')
    return list(saved) if saved else list(DEFAULT_CANCEL_REASONS)


def load_stale_plan_days():
    saved = db.get_setting('stale_plan_days')
    try:
        return max(1, int(saved)) if saved not in (None, '') else DEFAULT_STALE_PLAN_DAYS
    except (TypeError, ValueError):
        return DEFAULT_STALE_PLAN_DAYS


def plan_start_date(plan):
    """The day a plan became live: committed_at, else planned_date, else created_at."""
    for key in ('committed_at', 'planned_date', 'created_at'):
        v = _scalar(plan.get(key))
        if v:
            ts = pd.to_datetime(v, errors='coerce', utc=True)
            if pd.notna(ts):
                return ts.date()
    return None


def plan_age_days(plan, today=None):
    start = plan_start_date(plan)
    if start is None:
        return None
    return ((today or dt.date.today()) - start).days


def _num(v):
    v = _scalar(v)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def replay_cancelled_plan(plan, bars, max_bars=CANCEL_REPLAY_BARS):
    """What would have happened had a cancelled plan been kept. Pure: `bars`
    are completed daily bars (fetch_daily_history) covering the plan's start.

    - Window: up to max_bars sessions after the plan went live
      (plan_start_date), i.e. from the next session.
    - Entry: a stop-type entry when the entry was beyond the last close
      (breakout) -- filled once price trades through it, at the open if it
      gapped past; otherwise a limit-type entry, filled once price comes back
      to it. Never reached in the window = 'never_filled'.
    - Scale-out plans: the stop is checked first each bar (worst case), then
      targets nearest-first, each selling its Qty % and moving the stop to its
      Move Stop To. Trailing plans get the entry floor at T1 and the trail
      (daily bars) from their activation target.
    - Scale-in campaigns: the stop first, then each add at its trigger with
      the stop raised to its stop_before; the trail runs from the probe or
      add k (EMA trail gated on a close above EMA(21)).
    - Still open at the end of the data: marked at the last close.
    R is the journal's R: scale-out = P&L per share / planned entry-to-stop;
    campaign = P&L / peak risk taken (analyze_campaign). 'final' is True once
    the replay exited or covered the full window, so it needn't be redone."""
    direction = plan['direction']
    sign = 1 if direction == 'BUY' else -1
    entry, stop0 = _num(plan.get('entry_price')), _num(plan.get('stop_loss'))
    start = plan_start_date(plan)
    result = {'outcome': None, 'final': False, 'r': None, 'bars': 0, 'window_bars': max_bars,
              'filled_at': None, 'fill_price': None, 'exited_at': None, 'exit_price': None,
              'evaluated_through': None, 'events': []}
    if entry is None or start is None or bars is None or bars.empty:
        result.update({'final': True, 'note': "No entry price or start date -- nothing to replay."})
        return result
    dates = _day_index(bars.index)
    window = [i for i, d in enumerate(dates) if d.date() > start][:max_bars]
    if not window:
        result['note'] = "No completed sessions since the plan went live yet."
        return result
    full = len(window) >= max_bars
    result.update({'bars': len(window), 'evaluated_through': dates[window[-1]].date().isoformat()})
    o_, h_, l_, c_ = (bars[k].to_numpy(dtype=float) for k in ('Open', 'High', 'Low', 'Close'))
    first = window[0]
    ref = c_[first - 1] if first > 0 else o_[first]
    breakout = sign * (entry - ref) > 0

    def better(a, b):
        if a is None:
            return b
        if b is None:
            return a
        return max(a, b) if sign == 1 else min(a, b)

    def stop_hit(i, level):
        return level is not None and (l_[i] <= level if sign == 1 else h_[i] >= level)

    def reached(i, level):
        return h_[i] >= level if sign == 1 else l_[i] <= level

    fill_i = fill_px = None
    for i in window:
        if breakout and reached(i, entry):
            fill_i, fill_px = i, (max(o_[i], entry) if sign == 1 else min(o_[i], entry))
        elif not breakout and (l_[i] <= entry if sign == 1 else h_[i] >= entry):
            fill_i, fill_px = i, (min(o_[i], entry) if sign == 1 else max(o_[i], entry))
        if fill_i is not None:
            break
    if fill_i is None:
        result.update({'outcome': 'never_filled', 'final': full})
        return result
    result.update({'filled_at': dates[fill_i].date().isoformat(), 'fill_price': round(float(fill_px), 4)})
    method = stop_method_of(plan)
    mult = _num(plan.get('trailing_atr_multiplier')) or 2.0
    span = [i for i in window if i >= fill_i]
    if method == 'atr_trail' and atr_timeframe_of(plan) != DEFAULT_ATR_TIMEFRAME:
        result['note'] = "Replayed on daily bars; the trail uses daily ATR, not the plan's timeframe."

    def trail_from(i, entry_px, gate):
        return compute_trailing_series(bars, direction, method, dates[i], entry_px, mult, ema_gate=gate)

    exit_i = exit_px = None
    if is_campaign(plan):
        adds = [a for a in (plan.get('adds') or []) if _num(a.get('price')) is not None]
        probe = _num(plan.get('qty')) or 0.0
        fills = [{'qty': probe, 'price': float(fill_px), 'time': dates[fill_i].isoformat()}]
        stop, done, shares = stop0, 0, probe
        k_trail = int(_num(plan.get('trail_start_add')) or 0)
        series = trail_from(fill_i, fill_px, 'close') if method != 'ladder' and k_trail == 0 else None
        for i in span:
            eff = better(stop, trail_level_before(series, dates[i])) if series is not None else stop
            if stop_hit(i, eff):
                exit_i, exit_px = i, (eff if i == fill_i else (min(o_[i], eff) if sign == 1 else max(o_[i], eff)))
                break
            while done < len(adds) and reached(i, _num(adds[done]['price'])):
                a = adds[done]
                trig = _num(a['price'])
                stop = better(stop, _num(a.get('stop_before')))
                px = max(o_[i], trig) if sign == 1 else min(o_[i], trig)
                qty = _num(a.get('qty')) or probe
                fills.append({'qty': qty, 'price': float(px), 'time': dates[i].isoformat()})
                shares += qty
                done += 1
                result['events'].append(f"Add {done} {dates[i].date().isoformat()} @ {px:.2f}")
                if method != 'ladder' and series is None and done == k_trail:
                    series = trail_from(i, fill_px, 'close')
        last = exit_i if exit_i is not None else span[-1]
        px_out = exit_px if exit_i is not None else c_[last]
        cost = sum(f['qty'] * f['price'] for f in fills)
        pnl = sign * (px_out * shares - cost)
        exits = [{'qty': shares, 'price': float(px_out), 'time': dates[last].isoformat()}] if exit_i is not None else []
        peak = analyze_campaign(plan, fills, exits)['peak_risk_taken']
        result['r'] = round(pnl / peak, 2) if peak else None
        result['pnl'] = round(pnl, 2)
    else:
        targets = sorted([t for t in (plan.get('targets') or []) if _num(t.get('price')) is not None],
                         key=lambda t: sign * (_num(t['price']) - entry))
        pcts = [_num(t.get('qty_pct')) or 0.0 for t in targets]
        if targets and not any(pcts):
            pcts = [100.0] + [0.0] * (len(targets) - 1)      # no Qty % at all: everything at T1
        act = int(_num(plan.get('trailing_activate_after_target')) or 1) - 1
        stop, remaining, pnl_ps, series = stop0, 100.0, 0.0, None
        hit = [False] * len(targets)
        for i in span:
            eff = better(stop, trail_level_before(series, dates[i])) if series is not None else stop
            if stop_hit(i, eff):
                exit_i, exit_px = i, (eff if i == fill_i else (min(o_[i], eff) if sign == 1 else max(o_[i], eff)))
                break
            for k, t in enumerate(targets):
                if hit[k]:
                    continue
                tp = _num(t['price'])
                if not reached(i, tp):
                    break
                hit[k] = True
                sell = min(pcts[k], remaining)
                pnl_ps += sell / 100.0 * sign * (tp - fill_px)
                remaining -= sell
                stop = better(stop, _num(t.get('stop_after')))
                if method != 'ladder' and k == 0:
                    stop = better(stop, float(fill_px))
                if method != 'ladder' and k == act and series is None:
                    series = trail_from(i, fill_px, 'entry')
                result['events'].append(f"T{k + 1} {dates[i].date().isoformat()} @ {tp:.2f}")
            if remaining <= 1e-9:
                exit_i, exit_px = i, None
                break
        if exit_i is not None and exit_px is not None:
            pnl_ps += remaining / 100.0 * sign * (exit_px - fill_px)
            remaining = 0.0
        elif exit_i is None:
            pnl_ps += remaining / 100.0 * sign * (c_[span[-1]] - fill_px)
        risk = abs(entry - stop0) if stop0 is not None else None
        result['r'] = round(pnl_ps / risk, 2) if risk else None
        if exit_i is not None and exit_px is None:
            result.update({'outcome': 'targets_done', 'final': True,
                           'exited_at': dates[exit_i].date().isoformat()})
            return result
    if exit_i is not None:
        result.update({'outcome': 'stopped_out', 'final': True, 'exited_at': dates[exit_i].date().isoformat(),
                       'exit_price': round(float(exit_px), 4)})
    else:
        result.update({'outcome': 'still_open', 'final': full})
    return result


def counterfactual_text(cf, currency=None):
    """One line for the journal / detail view / weekly prompt."""
    if not isinstance(cf, dict) or not cf.get('outcome'):
        return "If kept: not checked yet (use \"Check what would have happened\")."
    so_far = "" if cf.get('final') else " so far"
    ccy = f"{currency} " if currency else ""
    if cf['outcome'] == 'never_filled':
        return f"If kept: the entry was never reached in {cf.get('bars', 0)} sessions{so_far}."
    r = f"{cf['r']:+.2f}R" if cf.get('r') is not None else "no R (no stop)"
    text = f"If kept: filled {cf.get('filled_at')} at {ccy}{cf.get('fill_price'):.2f}"
    if cf['outcome'] == 'stopped_out':
        text += f", stopped out {cf.get('exited_at')} at {ccy}{cf.get('exit_price'):.2f}"
    elif cf['outcome'] == 'targets_done':
        text += f", all targets hit by {cf.get('exited_at')}"
    else:
        text += f", still open on {cf.get('evaluated_through')}"
    return text + f" → {r}{so_far}."


def refresh_cancelled_outcomes(plans, fetch=None):
    """Replay every cancelled plan whose result isn't final yet, saving each
    to the database. Returns ({plan_id: counterfactual}, {symbol: error}).
    fetch(symbol, since, currency) -> daily bars; defaults to Yahoo."""
    fetch = fetch or (lambda sym, since, ccy: fetch_daily_history(sym, since, currency=ccy))
    updated, errors = {}, {}
    for p in plans or []:
        cf = p.get('counterfactual')
        if not is_cancelled(p) or (isinstance(cf, dict) and cf.get('final')) or is_day_trade(p):
            continue  # a day trade would need intraday bars to replay
        try:
            bars = fetch(p['symbol'], plan_start_date(p), _scalar(p.get('currency')))
            cf = replay_cancelled_plan(p, bars)
            cf['checked_at'] = db.utc_now().isoformat(timespec='seconds')
            db.update_plan(p['plan_id'], {'counterfactual': cf})
            updated[p['plan_id']] = cf
        except Exception as e:
            errors[p['symbol']] = str(e)
    return updated, errors


def cancelled_journal_row(plan):
    """A cancelled plan's journal row: the unmatched plan's shape (planned
    ladder, max loss, R:R), status 'Cancelled', with the reason, note and the
    "what would have happened" result. Never scored: no P&L, no R."""
    row = build_journal_row(plan)
    cf = plan.get('counterfactual') if isinstance(plan.get('counterfactual'), dict) else None
    notes = [f"Cancelled {str(_scalar(plan.get('cancelled_at')) or '')[:10]}: "
             f"{_scalar(plan.get('cancel_reason')) or 'no reason given'}"]
    if _scalar(plan.get('cancel_note')):
        notes.append(plan['cancel_note'])
    notes.append("Not replayed: a day trade would need intraday bars." if is_day_trade(plan)
                 else counterfactual_text(cf, row.get('currency')))
    row.update({'status': 'Cancelled', 'color': 'secondary', 'exec_notes': notes, 'counterfactual': cf,
                'cf_r': cf.get('r') if cf else None, 'cf_outcome': cf.get('outcome') if cf else None,
                'cf_final': bool(cf.get('final')) if cf else False, 'stale': False, 'age_days': None,
                'manual_match': False})
    return row


STRATEGY_VIEWS = [('all', 'All strategies'), ('scale_out', 'Scale-out'), ('scale_in', 'Scale-in campaigns'),
                  ('day_trade', 'Day trades')]


def _strategy_filter(rows, strategy):
    """Journal rows for one strategy ('scale_out' / 'scale_in'); everything
    for 'all' or None."""
    if strategy in (None, '', 'all'):
        return list(rows or [])
    return [r for r in (rows or []) if plan_type_of(r) == strategy]


def _strategy_suffix(strategy):
    return f" · {dict(STRATEGY_VIEWS)[strategy]}" if strategy in dict(STRATEGY_VIEWS) and strategy != 'all' else ""


def default_campaign_adds(direction, entry, stop, add_qty, n=DEFAULT_CAMPAIGN_ADDS):
    """The default add ladder: triggers at 1R, 2R, 3R... beyond the probe entry
    (R = the probe's risk per share), equal size, and the stop before each add
    at the previous level (the probe entry before add 1, then each previous
    trigger). With equal adds this keeps the campaign's risk at or below the
    probe's."""
    try:
        entry_f, stop_f = float(entry), float(stop)
    except (TypeError, ValueError):
        return []
    risk = abs(entry_f - stop_f)
    if risk <= 0:
        return []
    sign = 1 if direction == 'BUY' else -1
    adds, prev = [], entry_f
    for k in range(1, int(n) + 1):
        trigger = round(entry_f + sign * k * risk, 4)
        adds.append({'price': trigger, 'qty': add_qty, 'stop_before': round(prev, 4)})
        prev = trigger
    return adds


def compute_campaign_stages(direction, entry, stop, qty, adds, gap=0.0):
    """The campaign's risk at each stage, assuming every add fills at its
    trigger: shares held, average cost, the stop in place, and the result if
    stopped there (negative = a loss). peak_risk is the largest loss at any
    stage -- what the 2% cap is measured against, and the campaign's 1R for
    scoring if every add is made. peak_risk_gap is the same with `gap` per
    share (see gap_per_share) on every share held at each stage -- the figure
    the pre-trade check uses. Returns None without entry/stop/qty."""
    try:
        entry_f, stop_f, qty_f = float(entry), float(stop), float(qty)
    except (TypeError, ValueError):
        return None
    sign = 1 if direction == 'BUY' else -1
    shares, cost, current_stop = qty_f, entry_f * qty_f, stop_f
    stages = [{'label': 'Probe', 'trigger': entry_f, 'add_qty': qty_f, 'shares': shares,
               'avg': entry_f, 'stop': stop_f, 'pnl_if_stopped': sign * (stop_f - entry_f) * qty_f}]
    for k, a in enumerate(adds or [], start=1):
        try:
            price, q = float(a['price']), float(a.get('qty') or 0)
        except (TypeError, ValueError, KeyError):
            continue
        if q <= 0:
            continue
        if a.get('stop_before') not in (None, ''):
            current_stop = float(a['stop_before'])
        shares += q
        cost += price * q
        avg = cost / shares
        stages.append({'label': f'Add {k}', 'trigger': price, 'add_qty': q, 'shares': shares, 'avg': avg,
                       'stop': current_stop, 'pnl_if_stopped': sign * (current_stop - avg) * shares})
    peak_risk = max([0.0] + [-st['pnl_if_stopped'] for st in stages])
    gap = float(gap or 0.0)
    peak_risk_gap = max([0.0] + [-st['pnl_if_stopped'] + gap * st['shares'] for st in stages])
    return {'stages': stages, 'peak_risk': peak_risk, 'probe_risk': max(0.0, -stages[0]['pnl_if_stopped']),
            'gap_per_share': gap, 'peak_risk_gap': peak_risk_gap}


def validate_campaign(direction, entry, stop, adds, stop_method):
    """An error message for a campaign plan that breaks its rules, or None.
    Structural rules only -- the 2% cap is a warning elsewhere, like every
    other risk cap in TAPEs."""
    if stop in (None, ''):
        return "A scale-in campaign needs an initial protective stop for the probe."
    if stop_method not in ('atr_trail', 'ema_trail'):
        return "A scale-in campaign exits on a trailing stop only -- pick the ATR(14) or EMA(21) trail."
    try:
        entry_f, stop_f = float(entry), float(stop)
    except (TypeError, ValueError):
        return "Entry and stop must be numbers."
    sign = 1 if direction == 'BUY' else -1
    if sign * (entry_f - stop_f) <= 0:
        return "The initial stop must be on the losing side of the entry."
    prev_trigger, prev_stop = entry_f, stop_f
    for k, a in enumerate(adds or [], start=1):
        if a.get('qty') in (None, '') or float(a['qty']) <= 0:
            return f"Add {k} needs a number of shares."
        if a.get('stop_before') in (None, ''):
            return f"Add {k} needs the stop that must be in place before it."
        price, stop_k = float(a['price']), float(a['stop_before'])
        if sign * (price - prev_trigger) <= 0:
            beyond = "above" if sign == 1 else "below"
            return (f"Add {k}'s trigger ({price:g}) must be {beyond} the "
                    f"{'entry' if k == 1 else f'add {k - 1} trigger'} ({prev_trigger:g}) -- adds are only made "
                    f"once the reversal confirms, never to average down.")
        if sign * (stop_k - prev_stop) <= 0:
            return (f"The stop before add {k} ({stop_k:g}) must be {'higher' if sign == 1 else 'lower'} than "
                    f"the stop before it ({prev_stop:g}) -- the stop moves up before every add.")
        if sign * (price - stop_k) <= 0:
            return f"The stop before add {k} ({stop_k:g}) must be below that add's trigger ({price:g})."
        prev_trigger, prev_stop = price, stop_k
    return None


def compute_plan_risk_reward(direction, entry, stop, target_prices, qty):
    """Best-case / worst-case bounds for a plan, computed purely from the plan
    itself (entry/stop/targets/qty) -- independent of whether or how it was
    executed. This is a different concept from the qty%-weighted "blended
    target" used for the post-trade Plan Score: that one estimates the
    realistic outcome of the scale-out plan executing as designed; this one
    answers "what's the most I could lose / gain on this trade" --
    max_loss assumes the full position is stopped out at the initial stop,
    max_reward assumes the full position exits at the single most favorable
    target price (the actual scale-out plan would realize less than this,
    since only a portion exits at the best target).

    Returns (max_loss, max_reward, risk_reward_ratio) in dollar terms (ratio
    is unitless, reward/loss); any can be None if there isn't enough info
    (no stop set -> no max_loss; no targets set -> no max_reward).
    """
    if entry in (None, '') or qty in (None, ''):
        return None, None, None
    try:
        entry_f, qty_f = float(entry), float(qty)
    except (TypeError, ValueError):
        return None, None, None
    sign = 1 if direction == 'BUY' else -1

    max_loss = None
    if stop not in (None, ''):
        try:
            max_loss = abs(entry_f - float(stop)) * qty_f
        except (TypeError, ValueError):
            max_loss = None

    max_reward = None
    valid_targets = []
    for t in (target_prices or []):
        if t in (None, ''):
            continue
        try:
            valid_targets.append(float(t))
        except (TypeError, ValueError):
            continue
    if valid_targets:
        best_target = max(valid_targets) if sign == 1 else min(valid_targets)
        max_reward = abs(best_target - entry_f) * qty_f

    risk_reward_ratio = None
    if max_loss not in (None, 0) and max_reward is not None:
        risk_reward_ratio = max_reward / max_loss

    return max_loss, max_reward, risk_reward_ratio


def compute_pretrade_risk_check(direction, entry, stop, qty, portfolio_value,
                                 existing_total_risk_dollars, trade_fx_rate=1.0, max_loss_override=None,
                                 gap_per_share=0.0, cap_pct=PER_TRADE_RISK_CAP_PCT,
                                 caution_pct=PORTFOLIO_RISK_CAUTION_PCT, warning_pct=PORTFOLIO_RISK_WARNING_PCT):
    """Informational-only pre-trade risk check for a NEW plan being drafted:
    this trade's own risk as a % of portfolio value, and the portfolio's
    projected total risk (the existing ratcheted-stop risk already carried by
    other open positions, plus this new trade) if it's added. Never blocks
    Save Draft/Commit -- purely a warning banner, matching the existing
    portfolio-risk banner's own non-blocking precedent on Open Positions.

    Reuses compute_plan_risk_reward's max_loss (full position stopped out at
    the initial stop) rather than recomputing the same number a second way;
    direction doesn't actually change that figure (it's a plain abs()), but is
    threaded through for a consistent call shape with that function.

    trade_fx_rate converts the trade's own currency into the currency
    portfolio_value and existing_total_risk_dollars are in (CAD per USD for a
    USD trade against a CAD-valued portfolio; 1.0 when they match).
    trade_risk_dollars stays in the trade's currency; the percentages use the
    converted figure.

    Returns None if there isn't enough info yet (no entry/stop/qty, or no
    portfolio value staged) -- the caller shows a "Click Refresh Live Prices
    first" guard in that case, same as the existing portfolio-risk banner.
    """
    if not portfolio_value:
        return None
    # A scale-in campaign passes its peak-stage risk (compute_campaign_stages):
    # the 2% cap applies to the campaign at its riskiest, not just the probe.
    # (Pass the campaign's peak_risk_gap there to include the gap allowance.)
    # A scale-out trade adds gap_per_share x qty: the stop-out a gap jumps.
    max_loss = max_loss_override if max_loss_override is not None else \
        compute_plan_risk_reward(direction, entry, stop, [], qty)[0]
    if max_loss is None:
        return None
    if max_loss_override is None and gap_per_share:
        max_loss += float(gap_per_share) * float(qty)
    trade_risk_converted = max_loss * float(trade_fx_rate)
    trade_risk_pct = trade_risk_converted / portfolio_value * 100
    projected_total_risk = (existing_total_risk_dollars or 0) + trade_risk_converted
    projected_total_risk_pct = projected_total_risk / portfolio_value * 100
    return {
        'trade_risk_dollars': max_loss,
        'trade_risk_pct': trade_risk_pct,
        'over_per_trade_cap': trade_risk_pct > cap_pct,
        'cap_pct': cap_pct,
        'existing_total_risk_pct': (existing_total_risk_dollars or 0) / portfolio_value * 100,
        'projected_total_risk_dollars': projected_total_risk,
        'projected_total_risk_pct': projected_total_risk_pct,
        'projected_over_caution': projected_total_risk_pct > caution_pct,
        'projected_over_warning': projected_total_risk_pct > warning_pct,
    }


def find_overbuy_trims(entries, exits, cumulative, schedule, sign, atr=None):
    """Same-day corrections of an over-bought campaign stage (2026-10-29, MCD:
    24 shares bought for a 12-share add, 12 sold minutes later at about the
    same price). A sell is a trim when, at that moment:
    - more shares are held than the plan calls for at the stage reached
      (cumulative planned shares), and the sell is no larger than that excess;
    - it's on the same day as the buy that created the excess;
    - it's above the scheduled stop by more than the slippage tolerance (a
      stop-out is never a trim).
    Returns [{exit_index, entry_index, qty, buy_price, sell_price, pnl}]; the
    trimmed shares come off that buy (most recent buy first)."""
    events = [(f['time'], 0, i, f) for i, f in enumerate(entries)] + \
        [(f['time'], 1, j, f) for j, f in enumerate(exits)]
    events.sort(key=lambda e: (str(e[0]), e[1]))
    held = bought = 0.0
    reached = 0
    open_buys = []          # [entry_index, untrimmed qty] of buys, most recent last
    trims = []
    for _, is_exit, idx, f in events:
        if not is_exit:
            stage = 0
            while stage < len(cumulative) - 1 and bought >= cumulative[stage] * (1 - LOT_SPLIT_QTY_TOLERANCE_PCT) - 1e-9:
                stage += 1
            reached = max(reached, stage)
            bought += f['qty']
            held += f['qty']
            open_buys.append([idx, f['qty']])
            continue
        excess = held - cumulative[reached]
        stop = schedule[reached]
        above_stop = stop in (None, '') or sign * (f['price'] - float(stop)) > _stop_tolerance(stop, atr)[0]
        last = open_buys[-1] if open_buys else None
        same_day = last is not None and str(entries[last[0]]['time'])[:10] == str(f['time'])[:10]
        if (excess > 1e-9 and f['qty'] <= excess * (1 + LOT_SPLIT_QTY_TOLERANCE_PCT) + 1e-9 and above_stop
                and same_day and f['qty'] <= last[1] + 1e-9):
            buy = entries[last[0]]
            last[1] -= f['qty']
            trims.append({'exit_index': idx, 'entry_index': last[0], 'qty': f['qty'], 'buy_price': buy['price'],
                          'sell_price': f['price'], 'pnl': sign * (f['price'] - buy['price']) * f['qty']})
        held -= f['qty']
    return trims


def analyze_campaign(plan, entry_fills, exit_fills, atr=None):
    """Replay a scale-in campaign's real fills against its plan.

    Buys are assigned to the probe and then each add in order, by quantity
    (a stage is complete once its planned shares are in, within
    LOT_SPLIT_QTY_TOLERANCE_PCT); anything past the last add is "extra".
    The planned stop schedule is the initial stop with the probe only, then
    each add's stop_before once that add is in.

    peak_risk_taken: the most the open position stood to lose at the stop
    then scheduled, measured after every buy (average cost of the shares
    still held). It's the campaign's 1R. Exits are checked against the
    scheduled stop in effect at the time (the floor the plan committed to
    -- the trailing level isn't replayed here, same as scale-out journal
    rows). Adds bought on the wrong side of their trigger by more than the
    stop-slippage tolerance are flagged: adds are meant to wait for the
    reversal to confirm, never to average down.

    An over-buy sold back the same day (find_overbuy_trims) is netted out
    first: the stage counts only the planned shares, the trim isn't an exit
    for the stop check, and peak risk is measured without the excess. The
    trim's P&L is real and stays in the trade's P&L; `trims`, `net_bought`
    and `net_avg_cost` describe it."""
    direction = plan['direction']
    sign = 1 if direction == 'BUY' else -1
    adds = [a for a in (plan.get('adds') or []) if a.get('price') not in (None, '')]
    planned_qty = [float(plan.get('qty') or 0)] + [float(a.get('qty') or 0) for a in adds]
    triggers = [plan.get('entry_price')] + [a.get('price') for a in adds]
    schedule = [plan.get('stop_loss')] + [a.get('stop_before') for a in adds]
    labels = ['Probe'] + [f'Add {k}' for k in range(1, len(adds) + 1)]
    cumulative = list(np.cumsum(planned_qty))

    gross_entries = sorted(entry_fills or [], key=lambda f: str(f['time']))
    gross_exits = sorted(exit_fills or [], key=lambda f: str(f['time']))
    trims = find_overbuy_trims(gross_entries, gross_exits, cumulative, schedule, sign, atr)
    trimmed = {}
    for t in trims:
        trimmed[t['entry_index']] = trimmed.get(t['entry_index'], 0.0) + t['qty']
    entries = [dict(f, qty=f['qty'] - trimmed.get(i, 0.0)) for i, f in enumerate(gross_entries)
               if f['qty'] - trimmed.get(i, 0.0) > 1e-9]
    trim_exits = {t['exit_index'] for t in trims}
    exit_fills = [f for j, f in enumerate(gross_exits) if j not in trim_exits]
    stage_of, stage_fills, extra_qty = [], [[] for _ in planned_qty], 0.0
    bought, stage = 0.0, 0
    for f in entries:
        while stage < len(planned_qty) and bought >= cumulative[stage] * (1 - LOT_SPLIT_QTY_TOLERANCE_PCT) - 1e-9:
            stage += 1
        if stage >= len(planned_qty):
            extra_qty += f['qty']
            stage_of.append(len(planned_qty) - 1)
        else:
            stage_fills[stage].append(f)
            stage_of.append(stage)
        bought += f['qty']

    stages, notes = [], []
    for k, fills in enumerate(stage_fills):
        filled = sum(f['qty'] for f in fills)
        avg = sum(f['qty'] * f['price'] for f in fills) / filled if filled else None
        early = False
        if k > 0 and avg is not None and triggers[k] not in (None, ''):
            tolerance, _ = _stop_tolerance(triggers[k], atr)
            early = sign * (float(triggers[k]) - avg) > tolerance
        stages.append({'label': labels[k], 'trigger': triggers[k], 'planned_qty': planned_qty[k],
                       'stop': schedule[k], 'filled_qty': filled, 'avg_price': avg,
                       'time': fills[0]['time'] if fills else None, 'early': early})
        if early:
            notes.append(f"{labels[k]} bought at {avg:.2f}, {'below' if sign > 0 else 'above'} its trigger "
                         f"{float(triggers[k]):.2f} -- adds wait for the reversal to confirm, never average down.")
        elif k > 0 and 0 < filled < planned_qty[k] * (1 - LOT_SPLIT_QTY_TOLERANCE_PCT):
            notes.append(f"{labels[k]} filled {filled:.4g} of {planned_qty[k]:.4g} planned shares.")
    adds_done = sum(1 for st in stages[1:] if st['filled_qty'] > 0)
    if extra_qty > 1e-9:
        notes.append(f"{extra_qty:.4g} shares bought beyond the planned add ladder.")
    for t in trims:
        notes.append(f"Over-bought by {t['qty']:.4g} at {t['buy_price']:.2f} and sold {t['qty']:.4g} back at "
                     f"{t['sell_price']:.2f} the same day ({t['pnl']:+,.2f}) -- counted as a correction: the "
                     f"stage holds only its planned shares, and the excess isn't part of the campaign's risk.")
    net_bought = sum(f['qty'] for f in entries)
    net_avg_cost = sum(f['qty'] * f['price'] for f in entries) / net_bought if net_bought else None

    # Walk buys and sells in time order (a buy before a sell at the same time).
    events = [(f['time'], 0, f, stage_of[i]) for i, f in enumerate(entries)] + \
        [(f['time'], 1, f, None) for f in sorted(exit_fills or [], key=lambda f: f['time'])]
    events.sort(key=lambda e: (e[0], e[1]))
    shares = cost = 0.0
    reached, peak, stop_exits = 0, None, []
    for _, is_exit, f, k in events:
        if not is_exit:
            shares += f['qty']
            cost += f['qty'] * f['price']
            reached = max(reached, k)
            stop = schedule[reached]
            if stop not in (None, ''):
                risk = sign * (cost - float(stop) * shares)
                peak = risk if peak is None else max(peak, risk)
        else:
            if shares > 1e-9:
                avg_cost = cost / shares
                sold = min(f['qty'], shares)
                cost -= avg_cost * sold
                shares -= sold
            stop_exits.append((f['price'], schedule[reached]))

    worst = classify_stop_exits(direction, stop_exits, atr) if stop_exits else None
    stop_note = None
    if worst:
        side = 'below' if sign > 0 else 'above'
        if worst['outcome'] == 'respected':
            stop_note = "Every exit was at or beyond the scheduled stop (the trail did its job)."
        else:
            past, tol = _slippage_text(worst)
            stop_note = (f"Exit at {worst['price']:.2f} was {side} the scheduled stop ({float(worst['stop']):.2f}) "
                         f"by {past}" + (f", within the slippage tolerance ({tol})."
                                         if worst['outcome'] == 'slippage'
                                         else f", beyond the {tol} slippage tolerance."))
    next_add = None
    if adds_done < len(adds) and extra_qty <= 1e-9:
        a = adds[adds_done]
        next_add = {'label': f'Add {adds_done + 1}', 'price': a.get('price'), 'qty': a.get('qty'),
                    'stop_before': a.get('stop_before')}
    buys = [{'time': f['time'], 'qty': f['qty'], 'price': f['price'], 'stage': stage_of[i]}
            for i, f in enumerate(entries)]
    return {'stages': stages, 'adds_planned': len(adds), 'adds_done': adds_done, 'extra_qty': extra_qty,
            'trims': trims, 'net_bought': net_bought, 'net_avg_cost': net_avg_cost, 'buys': buys,
            'exits': exit_fills,
            'peak_risk_taken': peak if peak is not None and peak > 0 else None,
            'scheduled_stop': schedule[reached], 'next_add': next_add, 'notes': notes,
            'stop_outcome': worst['outcome'] if worst else None,
            'stop_breached': (worst['outcome'] == 'breached') if worst else None, 'stop_note': stop_note}


def _campaign_journal_row(row, plan, rt=None, open_pos=None):
    """build_journal_row for a scale-in campaign. R is measured against the
    peak risk actually taken (analyze_campaign), so a campaign's R sits on
    the same scale as a scale-out trade's. There's no Plan Score: a campaign
    has no targets to score against."""
    sign = 1 if plan['direction'] == 'BUY' else -1
    stages = compute_campaign_stages(plan['direction'], plan.get('entry_price'), plan.get('stop_loss'),
                                     plan.get('qty'), plan.get('adds') or [])
    planned_peak = stages['peak_risk'] if stages else None
    row.update({'max_loss': _fmt_money(planned_peak), 'max_reward': None, 'risk_reward_ratio': None,
                'reward_basis': 'campaign', 'targets_total': 0, 'target_results': [], 'campaign': None})
    pos = open_pos if open_pos is not None else rt
    if pos is None:
        row['campaign'] = analyze_campaign(plan, [], [])
        row['status'] = 'Planned (no execution matched yet)'
        return row

    entry_fills = pos.get('entry_fill_list')
    entry_fills = entry_fills if isinstance(entry_fills, list) else []
    exit_fills = pos.get('exit_fill_list')
    exit_fills = exit_fills if isinstance(exit_fills, list) else []
    analysis = analyze_campaign(plan, entry_fills, exit_fills, atr=plan.get('atr_at_commit'))
    peak = analysis['peak_risk_taken']
    notes = [f"{analysis['adds_done']} of {analysis['adds_planned']} planned adds made."] + analysis['notes']
    if analysis['stop_note']:
        notes.append(f"[{pos.get('currency') or ''}] {analysis['stop_note']}")
    if peak is not None:
        notes.append(f"Peak risk taken: {pos.get('currency') or ''} {peak:,.2f} (the campaign's 1R"
                     + (f"; planned {planned_peak:,.2f})." if planned_peak else ")."))

    if open_pos is not None:
        entry_actual = open_pos['entry_price']
        partial_pnl = sum(sign * (f['price'] - entry_actual) * f['qty'] for f in exit_fills) if exit_fills else None
        if analysis['trims']:
            # Net of the corrected over-buy: cost of the shares actually kept, and
            # P&L = the trims' own result plus any real exits against that cost.
            entry_actual = analysis['net_avg_cost'] or entry_actual
            trim_prices = [(t['sell_price'], t['qty']) for t in analysis['trims']]
            real_exits = list(exit_fills)
            for price, q in trim_prices:
                for k, f in enumerate(real_exits):
                    if abs(f['price'] - price) < 1e-9 and abs(f['qty'] - q) < 1e-9:
                        real_exits.pop(k)
                        break
            partial_pnl = sum(t['pnl'] for t in analysis['trims']) + \
                sum(sign * (f['price'] - entry_actual) * f['qty'] for f in real_exits)
        nxt = analysis['next_add']
        if nxt and nxt.get('price') not in (None, ''):
            notes.insert(0, f"Next: raise the stop to {float(nxt['stop_before']):.2f}, then buy "
                            f"{float(nxt['qty'] or 0):.4g} at {float(nxt['price']):.2f} ({nxt['label']}).")
        notes.insert(0, f"Campaign open -- {open_pos['qty_open']:.4g} shares held.")
        row.update({
            'entry_actual': _fmt_money(entry_actual),
            'qty_actual': _fmt_money(analysis['net_bought'] if analysis['trims'] else open_pos['qty_total_entered']),
            'entry_time': open_pos.get('entry_time'), 'actual_pnl': _fmt_money(partial_pnl), 'color': 'info',
            'exec_notes': notes, 'currency': open_pos.get('currency'), 'status': 'Open (in progress)',
            'final_effective_stop': analysis['scheduled_stop'], 'exit_fill_list': exit_fills,
            'lot_id': open_pos.get('lot_id'), 'campaign': analysis,
        })
        return row

    entry_actual, exit_actual, qty_actual = rt['entry_price'], rt['exit_price'], rt['qty']
    actual_pnl = sign * (exit_actual - entry_actual) * qty_actual
    actual_return_pct = sign * (exit_actual - entry_actual) / entry_actual * 100 if entry_actual else None
    r_multiple = actual_pnl / peak if peak else None
    row.update({
        'entry_actual': _fmt_money(entry_actual), 'exit_actual': _fmt_money(exit_actual),
        'qty_actual': _fmt_money(qty_actual), 'entry_time': rt['entry_time'], 'exit_time': rt['exit_time'],
        'actual_pnl': _fmt_money(actual_pnl), 'actual_return_pct': _fmt_money(actual_return_pct),
        'r_multiple': _fmt_money(r_multiple), 'score': None,
        'color': 'success' if actual_pnl > 0 else ('secondary' if actual_pnl == 0 else 'danger'),
        'exec_notes': notes, 'currency': rt.get('currency'), 'status': 'Closed',
        'final_effective_stop': analysis['scheduled_stop'], 'exit_fill_list': exit_fills,
        'stop_breached': analysis['stop_breached'], 'stop_outcome': analysis['stop_outcome'],
        'campaign': analysis,
    })
    return row


def build_journal_row(plan, rt=None, open_pos=None):
    """_journal_row, plus a day trade's same-day check (_add_day_trade_notes)."""
    row = _journal_row(plan, rt=rt, open_pos=open_pos)
    pos = open_pos if open_pos is not None else rt
    row['entry_fill_list'] = _fill_list_of(pos, 'entry_fill_list') if pos is not None else []
    if is_day_trade(plan):
        _add_day_trade_notes(row, rt=rt, open_pos=open_pos)
    return row


def _journal_row(plan, rt=None, open_pos=None):
    """Merge a plan with its matched round trip or still-open position (at most
    one of the two -- see match_plans_to_roundtrips) and compute scores.

    `open_pos` covers a plan whose position hasn't closed yet: there's no
    realized P&L/R-multiple/Plan Score (the trade isn't over), just the entry
    deviation, target-ladder progress against whatever partial exits have
    already happened, and P&L realized on the shares sold so far (the still-
    open remainder's eventual P&L isn't knowable yet)."""
    row = dict(plan)
    row['matched'] = rt is not None or open_pos is not None
    sign = 1 if plan['direction'] == 'BUY' else -1

    entry_planned = plan.get('entry_price')
    initial_stop = plan.get('stop_loss')
    targets = plan.get('targets') or []
    qty_planned = plan.get('qty')

    ladder = compute_plan_ladder(plan['direction'], entry_planned, initial_stop, targets, qty_planned) or {}
    max_loss, max_reward, risk_reward_ratio = ladder.get('max_loss'), ladder.get('reward'), ladder.get('rr')

    row.update({
        'entry_actual': None, 'exit_actual': None, 'qty_actual': None,
        'entry_time': None, 'exit_time': None, 'actual_pnl': None,
        'actual_return_pct': None, 'r_multiple': None, 'score': None,
        # Falls back to the plan's OWN declared currency (unmatched/"Planned"
        # rows) -- overwritten below with the execution-confirmed currency
        # once a real round trip/open position is matched, which is more
        # authoritative than what the user picked when the plan was created.
        'color': 'secondary', 'exec_notes': [], 'currency': plan.get('currency'),
        'target_results': [], 'targets_hit': 0, 'targets_total': len(
            [t for t in targets if t.get('price') not in (None, '')]),
        'final_effective_stop': initial_stop,
        'max_loss': _fmt_money(max_loss), 'max_reward': _fmt_money(max_reward),
        'reward_basis': ladder.get('reward_basis'),
        'risk_reward_ratio': round(risk_reward_ratio, 2) if risk_reward_ratio is not None else None,
        'stop_breached': None,
        'stop_outcome': None,
        'lot_id': None,
        # Always present, even unmatched -- journal_rows is a mix of matched
        # and unmatched plans, and pd.DataFrame(journal_rows) NaN-fills any
        # column missing from a given row when OTHER rows in the same list
        # have it (a float, not None -- `NaN or []` doesn't fall back to []
        # since NaN is truthy). A consistently-present [] here means every
        # row has the same type for this column, so that NaN-fill never
        # triggers (confirmed crash: show_detail on an unmatched "Planned"
        # plan, TypeError: 'float' object is not iterable, sorting this).
        'exit_fill_list': [],
        'campaign': None,
    })
    if is_campaign(plan):
        return _campaign_journal_row(row, plan, rt=rt, open_pos=open_pos)

    if open_pos is not None:
        entry_actual = open_pos['entry_price']
        qty_total = open_pos['qty_total_entered']
        exit_fills = open_pos.get('exit_fill_list') or []
        row['lot_id'] = open_pos.get('lot_id')

        target_results, targets_hit, targets_total, final_stop, _, _, _ = analyze_targets_and_stop(
            plan['direction'], entry_planned, initial_stop, targets, exit_fills,
            trailing=_trailing_spec(plan, entry_actual))

        notes = [f"Position still open -- {open_pos['qty_open']:.4g} of {qty_total:.4g} shares remain held."]
        # Open positions aren't date-gated in match_plans_to_roundtrips (there's
        # at most one per symbol, so there's no other episode to confuse it
        # with, and a plan is often deliberately written well after a
        # long-held position was actually entered). That's usually right, but
        # it does mean a wide gap gets waved through where a round-trip match
        # would have been rejected -- surface it so a genuinely wrong match
        # (e.g. two competing plans for the same symbol) is visible, not silent.
        p_date = pd.to_datetime(plan.get('planned_date') or plan.get('created_at'), utc=True, errors='coerce')
        op_entry_time = open_pos.get('entry_time')
        if pd.notna(p_date) and op_entry_time is not None and pd.notna(op_entry_time):
            gap_days = abs(pd.Timestamp(op_entry_time) - p_date).days
            if gap_days > MATCH_MAX_DATE_GAP_DAYS:
                notes.append(f"Matched by symbol only, {gap_days} days between this plan and the position's "
                             f"entry -- verify this is the trade you meant.")
        if targets_total > 0:
            notes.append(f"Hit {targets_hit} of {targets_total} planned price target(s) so far.")
        if entry_planned not in (None, '') and entry_planned != 0:
            entry_dev_pct = (entry_planned - entry_actual) / entry_planned * 100 * sign
            if abs(entry_dev_pct) >= 1:
                better_worse = "better" if entry_dev_pct > 0 else "worse"
                notes.append(f"Entered {abs(entry_dev_pct):.1f}% {better_worse} than planned entry")

        partial_pnl = None
        if exit_fills:
            partial_pnl = sum(sign * (f['price'] - entry_actual) * f['qty'] for f in exit_fills)

        row.update({
            'entry_actual': _fmt_money(entry_actual),
            'qty_actual': _fmt_money(qty_total),
            'entry_time': open_pos.get('entry_time'),
            'actual_pnl': _fmt_money(partial_pnl),
            'color': 'info',
            'exec_notes': notes,
            'currency': open_pos.get('currency'),
            'status': 'Open (in progress)',
            'target_results': target_results,
            'targets_hit': targets_hit,
            'targets_total': targets_total,
            'final_effective_stop': final_stop,
            'exit_fill_list': exit_fills,
        })
        return row

    if rt is None:
        # No execution yet -- still show the planned target ladder, all unhit.
        target_results, _, targets_total, _, _, _, _ = analyze_targets_and_stop(
            plan['direction'], entry_planned, initial_stop, targets, [])
        row['target_results'] = target_results
        row['targets_total'] = targets_total
        row['status'] = 'Planned (no execution matched yet)'
        return row

    entry_actual = rt['entry_price']
    exit_actual = rt['exit_price']
    qty_actual = rt['qty']
    exit_fills = rt.get('exit_fill_list') or []

    actual_pnl_per_share = sign * (exit_actual - entry_actual)
    actual_pnl = actual_pnl_per_share * qty_actual
    actual_return_pct = (actual_pnl_per_share / entry_actual * 100) if entry_actual else None

    r_multiple = None
    if initial_stop not in (None, '', 0) and entry_planned not in (None, ''):
        risk_per_share = abs(entry_planned - initial_stop)
        if risk_per_share > 0:
            r_multiple = actual_pnl_per_share / risk_per_share

    (target_results, targets_hit, targets_total, final_stop, stop_note, stop_breached,
     stop_outcome) = analyze_targets_and_stop(
        plan['direction'], entry_planned, initial_stop, targets, exit_fills, atr=plan.get('atr_at_commit'),
        trailing=_trailing_spec(plan, entry_actual))

    # Blended planned exit price across active targets (weighted by qty_pct),
    # used to generalize the old single-take-profit "Plan Score" to multiple targets.
    # This weighted average is only correct if qty_pct values are percentages of
    # the SAME base (the original position size) -- e.g. 50/25/25, not a cascading
    # "% of what's remaining" convention (50/50/100) that a scale-out strategy
    # might otherwise suggest. See target_rows_form's helper text.
    active = [t for t in targets if t.get('price') not in (None, '') and t.get('qty_pct') not in (None, '', 0)]
    score = None
    if active and entry_planned not in (None, ''):
        total_weight = sum(t['qty_pct'] for t in active)
        if total_weight > 0:
            blended_target = sum(t['price'] * t['qty_pct'] for t in active) / total_weight
            reward_per_share = abs(blended_target - entry_planned)
            planned_reward_total = reward_per_share * (qty_planned or qty_actual)
            if planned_reward_total > 0:
                score = actual_pnl / planned_reward_total * 100
            else:
                score = 100.0 if actual_pnl > 0 else (0.0 if actual_pnl == 0 else -100.0)

    notes = []
    if stop_note:
        # analyze_targets_and_stop's own note has no currency (it's a lower-level
        # function with no currency concept) -- prepend it here, since rt's
        # currency IS in scope at this call site.
        rt_currency = (rt or {}).get('currency') or ''
        notes.append(f"[{rt_currency}] {stop_note}" if rt_currency else stop_note)
    if targets_total > 0:
        notes.append(f"Hit {targets_hit} of {targets_total} planned price target(s)")
    if entry_planned not in (None, '') and entry_planned != 0:
        # Positive = actual entry was more favorable than planned (cheaper for a long,
        # higher for a short); negative = less favorable.
        entry_dev_pct = (entry_planned - entry_actual) / entry_planned * 100 * sign
        if abs(entry_dev_pct) >= 1:
            better_worse = "better" if entry_dev_pct > 0 else "worse"
            notes.append(f"Entered {abs(entry_dev_pct):.1f}% {better_worse} than planned entry")
    if not notes:
        notes.append("No stop/targets set to compare against")

    if score is not None:
        color = 'success' if score >= 80 else ('warning' if score >= 50 else 'danger')
    else:
        color = 'success' if actual_pnl > 0 else ('secondary' if actual_pnl == 0 else 'danger')

    row.update({
        'entry_actual': _fmt_money(entry_actual),
        'exit_actual': _fmt_money(exit_actual),
        'qty_actual': _fmt_money(qty_actual),
        'entry_time': rt['entry_time'],
        'exit_time': rt['exit_time'],
        'actual_pnl': _fmt_money(actual_pnl),
        'actual_return_pct': _fmt_money(actual_return_pct),
        'r_multiple': _fmt_money(r_multiple),
        'score': _fmt_money(score),
        'color': color,
        'exec_notes': notes,
        'currency': rt.get('currency'),
        'status': 'Closed',
        'target_results': target_results,
        'targets_hit': targets_hit,
        'targets_total': targets_total,
        'final_effective_stop': final_stop,
        'exit_fill_list': exit_fills,
        'stop_breached': stop_breached,
        'stop_outcome': stop_outcome,
    })
    return row


# ==============================================================
# 4. App layout
# ==============================================================

app = Dash(__name__, external_stylesheets=[dbc.themes.JOURNAL], enable_mcp=True)
server = app.server
app.title = "TAPEs"

# Raw Dash callbacks make awkward MCP tools -- most are pure-render (need fake
# Store data as input) or keyed to a specific button's n_clicks alongside a
# dozen unrelated State args (e.g. add_plan's full target-row list). The
# @mcp_enabled functions below (search "MCP tools" further down this file) are
# purpose-built instead: clean signatures wrapping the same db.py/app.py logic
# the callbacks already use. include_layout stays on (harmless, and
# get_dash_component/dash://layout are genuinely useful for inspecting the
# live page without reading source).
configure_mcp_server(include_callbacks=False)


# ------------------------------------------------------------
# Load persisted data from SQLite at startup
# ------------------------------------------------------------
db.init_db()

# Manually-attached chart screenshots live under assets/ so Dash serves them
# directly (app.get_asset_url) with no extra Flask route needed. Anchored to
# this file's own directory rather than the process's c wd -- same reasoning
# as db.DB_PATH: the app shouldn't silently write somewhere else just because
# it was launched from a different working directory.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
SCREENSHOT_DIR = os.path.join(_APP_DIR, 'assets', 'plan_screenshots')


def screenshot_url(rel_path):
    """Browser URL for a plan's screenshot, with the file's modified time as
    a version stamp. Replacing a screenshot reuses the same file name (the
    plan id), so without the stamp a browser could keep showing its cached
    old copy."""
    full = screenshot_file(rel_path)
    if not full:
        return None
    return f"{app.get_asset_url(_scalar(rel_path))}?v={int(os.path.getmtime(full))}"


def screenshot_file(rel_path):
    """Absolute path of a plan's saved screenshot (plans store it relative to
    assets/), or None if there isn't one or the file is gone. Lets Claude
    open the image itself via the MCP tools (2026-10-10): Dash sends a
    decorated tool's result as JSON text, so the image can't travel inside
    the tool result -- a file path Claude Code can read is the clean route."""
    rel_path = _scalar(rel_path)
    if not rel_path:
        return None
    full = os.path.normpath(os.path.join(_APP_DIR, 'assets', rel_path))
    return full if os.path.isfile(full) else None
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# Weekly Review (see the "7b." section further down for the actual
# computation): how many trailing weeks of trend aggregates to summarize,
# and the one-time setup message for a dedicated external coaching chat --
# defined here, not in that section, since serve_layout's Container (built
# well before that section of the file is reached) references
# WEEKLY_COACHING_SYSTEM_PROMPT directly.
WEEKLY_TREND_WEEKS = 8

WEEKLY_COACHING_SYSTEM_PROMPT = (
    "You are my trading coach for TAPEs, a discretionary-trading journal I log every trade "
    "and weekly review in. Each week I'll paste you a message with: my structured weekly entry, "
    "this week's closed trades, TAPEs's list of open positions, my daily journal entries, the "
    "coaching actions still open from earlier weeks, and precomputed trend statistics over the "
    "trailing weeks (win rate, Plan Score, stop-breach frequency, psychological-state readings, "
    "recurring symbols in losses/deviations) -- not my full raw trade history, so treat those "
    "statistics as authoritative rather than trying to recall exact past numbers yourself.\n\n"
    "Rules and citations: my strategy's rules are at the end of this message (Strategy.md). "
    "Judge everything against them, and cite the rule behind every finding and every focus "
    "item by its number: F1-F10 = Part 2 Foundations; S1-S7 = Part 3 steps (S6.3 = Step 6, "
    "check 3); 4.1-4.5 = Part 4 sections; P5 = Part 5 (inverse ETFs). If something isn't "
    "covered by a rule, say so rather than inventing one. Rule changes happen only at the weekly "
    "review: when one is warranted, propose the exact change-log line. If strategy_version in my "
    "weekly message differs from the version below, tell me to re-paste this setup message.\n\n"
    "Weekly entry: weekly_entry is my review on a fixed template -- the weekly and daily regime, "
    "the posture TAPEs worked out for next week (effective regime and its limits), the book "
    "(heat, positions, beta, largest theme), results in R (Sniper = scale_out, Pyramid = "
    "scale_in, Day = day_trade; gap losses are trades that lost more than 1R), my decision on each daily lesson "
    "(strategy change or dropped), graduations/trims, the watchlist and any strategy changes. "
    "Check the posture and the book against the limits for the effective regime, and the "
    "watchlist against the allowed tactics.\n\n"
    "Focus actions: every item in your FOCUS NEXT WEEK block becomes a tracked action with an "
    "id (A12). focus_actions.open lists the ones I haven't finished, with when each was raised "
    "and its history (done / not_done / dropped by week, with my reason). Review them first. "
    "Never restate an open action as a new item: refer to it by its id. If one has been not done "
    "for 2+ weeks, say why it's stalling and either make it smaller and more concrete or "
    "recommend dropping it.\n\n"
    "Stop outcomes: a trade's stop_outcome is 'respected', 'slippage' (the stop triggered "
    "and filled slightly past its level, within a small ATR-based tolerance -- a normal "
    "stop-out, not a mistake), or 'breached' (held or exited well past the stop). Only "
    "'breached' counts in stop_breach_rate_pct; slippage is reported separately as "
    "stop_slippage_rate_pct -- don't treat slippage as a discipline problem.\n\n"
    "Cancelled plans: a committed plan abandoned before any fill, with a reason and an "
    "'if kept' replay of what it would have done (filled or not, R). Use them to judge my "
    "cancelling: cancels that would have lost are good discipline; a run of cancels that "
    "would have won -- especially with a Fearful reading at cancel time, or 'Price ran away' "
    "as the reason -- is a pattern worth naming. Results marked 'so far' are still running. "
    "stale_planned_plans are plans with no fills left open too long: nudge me to cancel or "
    "act on them.\n\n"
    "daily_journal_this_week holds my structured daily entries: the regime I logged, the "
    "effective regime TAPEs worked out from it, heat, and a one-line lesson or proposal. Review "
    "every lesson/proposal against my decision on it in weekly_entry: say which deserve a "
    "strategy change (with the evidence) and which don't, and point out days where my actions "
    "didn't match the effective regime.\n\n"
    "Encouragement comes first. Open every reply with what went well or improved this week, "
    "from lessons_and_improvement.encouragement and practices: streaks (current vs best), "
    "trades that followed the plan -- a loss that followed the plan is a good trade, say so -- "
    "lessons that became changes (adopted_this_week) and any 'keep' lessons. Be specific and "
    "genuine, not generic praise; then move on to exceptions and what to change. My lessons are "
    "written as keep (what worked) and change (what to do differently). For each trade closed "
    "this week without a lesson, ask me for one. register_open lists change-lessons not yet acted "
    "on: suggest which deserve to become a practice, a Strategy rule or a TAPEs feature, and which "
    "to drop. adopted_awaiting_verification lists lessons I've adopted, with what each became "
    "(a practice, a Strategy version, a focus action or a TAPEs feature) and the evidence since "
    "adoption (the practice's results, the action's status): say which look ready to mark verified "
    "(the change has stuck) and which are slipping.\n\n"
    "regime_checks_this_week lists every day I bought (an entry, a probe or an add) with the "
    "effective regime in force at that day's open -- the newest entry saved before 09:30 ET, "
    "usually the previous evening's -- and TAPEs' check of the buy against it: the tactic "
    "allowed, the per-trade risk cap (risk to the stop plus the gap allowance) and the max "
    "position. Any breach is a rule broken: name it, and look for what came before it (the "
    "psych reading, the trigger, a loss). Sells never break a regime rule.\n\n"
    "practices are how I execute the Strategy, measured from my fills: trading within the "
    "regime at the open, having the regime set before the open (an entry for the previous "
    "session or that morning), committing the plan before the first fill, no buys before "
    "10:00 ET (9:45 for a day trade -- I adopted this because my early buys were driven by "
    "FOMO into the open's volatility), day trades closed by 16:00 ET the same day, and each "
    "campaign add protected at its stop-before level by the next stop review. Each has this "
    "week's and all-time counts, unknowns (not counted) and this week's exceptions. Name every "
    "exception; if one recurs, propose it as a FOCUS item.\n\n"
    "Strategies: each trade's strategy is 'scale_out' (one entry, sold in stages at price "
    "targets) or 'scale_in' (a reversal campaign: a small probe, adds as the reversal "
    "confirms -- each only after the stop was raised -- and a trailing-stop exit). A "
    "campaign has no Plan Score; its R is measured against the peak risk it actually took, "
    "and its notes flag adds bought before their trigger (averaging down), which is the "
    "campaign's main discipline check. 'day_trade' is a news-catalyst trade opened and closed in "
    "one session (Strategy 4.5): judge it on its catalyst and setup, its R against the intraday "
    "stop, and whether it was flat the same day -- carried_overnight true breaks 4.5.\n\n"
    "Psychological readings use three -2..+2 axes: Risk emotion (Fearful <-> Overconfident, "
    "Calm = 0 is ideal), Urgency (Complacent <-> FOMO, Patient = 0 is ideal), and Focus "
    "(Fatigued <-> Sharp, higher is better). The distortion index (0-6) sums how far a reading "
    "is from ideal. Each trade can also carry one or more trade triggers (Planned setup, Chased "
    "a breakout, Revenge, Boredom, News/tip) -- compare the others against Planned setup. "
    "Fear should show up as early exits before the first target; overconfidence and FOMO as "
    "stop breaches -- check whether my numbers actually bear that out. Any bucket marked "
    "insufficient_sample has too few trades to draw conclusions from; say so rather than "
    "reading into it.\n\n"
    "Your job each week: identify recurring patterns or issues using the trend statistics, be "
    "specific and concrete, reference actual numbers I gave you, and keep it short. Don't "
    "invent numbers that aren't in what I give you. Remember what we've discussed in this "
    "conversation across weeks, but always defer to the current week's data over your memory "
    "of past weeks.\n\n"
    "Weekly reconciliation: if I also attach a Wealthsimple holdings export (CSV) in this "
    "same message, cross-check it against the open-position list and flag any discrepancy by "
    "account and symbol -- a position on one side missing from the other, or a quantity that "
    "doesn't match. Don't flag a holding I've told you is intentionally outside TAPEs's scope. "
    "This check only runs when I actually attach a holdings export.\n\n"
    "End every reply with this block and nothing after it (TAPEs reads it to create the "
    "actions):\n"
    "FOCUS NEXT WEEK\n"
    "1. A12 (carried): one line on what changes about it [S6.3]\n"
    "2. A new, concrete, checkable action [F8]\n"
    "At most 3 items, one line each. Start a carried action with its id; end every item with "
    "its rule numbers in square brackets.\n\n"
    "I'll start now with this week's data."
)


# ==============================================================
# Daily/weekly workflow checklists (implemented 2026-09-03). Since 2026-10-09
# these constants are only the DEFAULTS: the user edits both lists in the app
# (see "Editable checklists" below; saved in db.checklist_definitions), and
# which item ids are checked is persisted per day/week
# (db.daily_checklist_state / weekly_checklist_state). Each item is
# {id, label, help} -- id is the stable key stored in the DB (never rename
# an existing id without also migrating stored rows), label is the short
# checkbox text, help is the fuller reminder shown via help_icon. Several
# items deliberately reference an existing TAPEs feature by name (and, where
# one exists, are followed by a same-page anchor link to it) rather than
# being inert text -- the checklist is meant to walk you through the app,
# not duplicate what it already shows. Items with no TAPEs feature to check
# against (regime, Sniper dashboard, Options Walls, placing the order) are
# plain self-reported checkboxes -- TAPEs has no connector to any of those
# three systems and can't verify them (see the Three-System Architecture
# note above), so don't expect these to ever auto-check.
# ==============================================================

# Grouped into three sections matching the trading day's actual shape
# (pre-market / during hours / post-close), with "When a Trade Is Found" as
# a conditional sub-group inside "During Market Hours" rather than its own
# top-level section, matching the user's own nesting -- sizing/committing/
# placing an order only applies on days a qualifying setup actually turns
# up, not every session. Rendered as a dbc.Accordion (see
# _render_daily_checklist_accordion below) so each section can be rolled up
# once done, rather than one long flat list on the page at all times.
DAILY_CHECKLIST_SECTIONS = [
    {
        'title': 'Pre-Market Open',
        'items': [
            {'id': 'check_regime', 'label': 'Check the regime -- have conditions changed? (Macro Regime Model)',
             'help': "Note the current state (Full Risk-On through Full Risk-Off) and any active "
                     "credit-divergence/stress-override flag. Sets today's aggressiveness bias and "
                     "whether you're even looking for new entries. Checked in TradingView, not TAPEs."},
            {'id': 'review_open_positions',
             'label': 'Review open positions -- adjust stops for trailing-stop entries, plan sizing changes',
             'help': "For each open position, check Sniper's Hold Advice, stop state (staircase rung), "
                     "and trend-invalidation distance in TradingView; adjust any stop that should now be "
                     "trailing. Then check portfolio heat below against the 5-6% ceiling -- click "
                     "\"Refresh Live Prices\" first if it's stale -- and plan any sizing change today's "
                     "conditions call for."},
            {'id': 'update_tapes_plan_changes', 'label': 'Update TAPEs with any plan changes made',
             'help': "If a stop moved (staircase advance, a manual trim/add) or sizing changed, edit the "
                     "committed plan or log an Observation so TAPEs's own record matches reality -- "
                     "portfolio heat below is computed from TAPEs's stored stop, so a stale one "
                     "understates real risk."},
        ],
    },
    {
        'title': 'During Market Hours',
        'items': [
            {'id': 'scan_watchlist', 'label': 'Scan the watchlist for new Sniper signals',
             'help': "Only signals clearing the entry gate (score, HTF agreement, not in chop). For "
                     "anything live, run the multi-timeframe check (2hr/4hr/daily/weekly/4-week) before "
                     "treating it as real."},
            {'id': 'check_options_walls', 'label': 'Sanity-check any candidates against Options Walls',
             'help': "Confirm the proposed stop and target aren't sitting on top of a put/call wall or "
                     "gamma flip likely to cap the move or trigger a reversal."},
        ],
        'subsections': [
            {
                'title': 'When a Trade Is Found',
                'items': [
                    {'id': 'size_trade', 'label': 'Size the trade (1-2% risk cap + heat ceiling)',
                     'help': "Size to the stop distance, never the other way around. If adding this "
                             "position would push total open risk over the 5-6% portfolio heat ceiling, "
                             "it doesn't go on, full stop -- regardless of how good the setup looks."},
                    {'id': 'commit_plan', 'label': 'Commit the plan in TAPEs before placing the order',
                     'help': "Locks the ATR/target methodology and captures the reasoning and "
                             "timeframe. No trade without a committed plan -- see \"1. Add a Trade "
                             "Plan\" below."},
                    {'id': 'place_order', 'label': 'Place the order manually in Wealthsimple',
                     'help': "Execution only, manual, by design -- nothing here should ever place an "
                             "order automatically. Wealthsimple is the sole source of truth for real "
                             "financial risk."},
                ],
            },
        ],
    },
    {
        'title': 'Post-Market Close',
        'items': [
            {'id': 'upload_ws_changes', 'label': 'Upload any position changes made in Wealthsimple to TAPEs',
             'help': "If you traded today, re-export the Wealthsimple activity CSV and drop it in "
                     "\"Import Executions (CSV)\" below so today's fills land in TAPEs before you close "
                     "out -- the lightweight, daily counterpart to the deeper Weekly Reconciliation card."},
            {'id': 'eod_check', 'label': 'Confirm plans and fills match',
             'help': "Confirm every open position has a matching plan (see \"Open Positions\" below for "
                     "any \"No plan\" badge) and that today's fills matched to the intended plan (see "
                     "the Trade Journal's \"Match\" button on any row that matched wrong)."},
        ],
    },
]


def _section_all_items(section):
    """Every item in a section, including any subsection's items -- for
    progress counting and for building DAILY_CHECKLIST_ITEMS below."""
    items = list(section['items'])
    for sub in section.get('subsections', []):
        items.extend(sub['items'])
    return items


# Flattened view of DAILY_CHECKLIST_SECTIONS -- used wherever the section
# structure doesn't matter (overall progress count, the toggle callback's
# id list), so those call sites don't need to know sections exist at all.
DAILY_CHECKLIST_ITEMS = [item for section in DAILY_CHECKLIST_SECTIONS for item in _section_all_items(section)]

WEEKLY_CHECKLIST_ITEMS = [
    {'id': 'reconcile_ws', 'label': 'Reconcile Wealthsimple against TAPEs',
     'help': "Export a CSV of current Wealthsimple positions and upload it in the Holdings "
             "Reconciliation card below. Resolve every discrepancy -- a position TAPEs doesn't "
             "know about, a quantity mismatch, or a trade that closed without ever getting a "
             "plan -- before treating this week's journal or heat numbers as trustworthy."},
    {'id': 'run_weekly_review', 'label': "Run the TAPEs Weekly Review",
     'help': "Compute this week's mark-to-market balance and generate the trend-aggregate data "
             "prompt below."},
    {'id': 'walk_journal', 'label': 'Walk the Trade Journal',
     'help': "For every trade closed this week: Plan Score, and the qualitative adherence check "
             "-- was the stop respected, did targets hit in order, how far did entry deviate "
             "from plan."},
    {'id': 'coaching_chat', 'label': "Take this week's prompt into the coaching conversation",
     'help': "Paste the generated prompt (below) into your dedicated external coaching chat to "
             "discuss patterns, not just outcomes."},
    {'id': 'recompute_heat', 'label': 'Recompute portfolio heat and market exposure',
     'help': "Recheck portfolio heat against the 5-6% ceiling and rebalance/trim if it's crept "
             "above it. Also click \"Compute Market Exposure\" on Open Positions -- several "
             "leveraged/single-theme ETFs can be one larger correlated bet even while each "
             "position's own heat looks fine individually."},
    {'id': 'recheck_symbols', 'label': "Re-check each traded symbol against Sniper's own data",
     'help': "Typical Run and the twin-backtest profit factor, per Sniper's own comments, don't "
             "carry across symbols. A symbol drifting toward a poor profit factor is a candidate "
             "to drop or de-weight, not to keep trading on faith."},
    {'id': 'recheck_regime', 'label': 'Re-check the Macro Regime for the week ahead',
     'help': "Adjust watchlist/sizing bias accordingly."},
    {'id': 'write_process_change', 'label': 'Write down one concrete process change to test next week',
     'help': "The actual mechanism for \"constantly improving\" -- without a specific, written "
             "adjustment, the review is a summary, not an iteration."},
]


def _render_checklist_rows(items, checked_ids, id_type):
    """One row per checklist item: a checkbox (pattern-matching id, so a
    single callback can read every item's state via ALL) plus its fuller
    help text via help_icon. Shared by the daily and weekly checklist cards
    -- both are pure rendering of the same {id, label, help} shape, just
    against a different item list/id_type/checked-set."""
    checked = set(checked_ids or [])
    rows = []
    for item in items:
        auto = is_auto_item(item)
        label = item['label'] if not auto else html.Span([
            item['label'], " ", html.Span("ticked by the stop review", className="badge bg-light text-dark",
                                          title=STOP_REVIEW_AUTO_HELP)])
        rows.append(dbc.Row([
            dbc.Col(dbc.Checkbox(
                id={'type': id_type, 'index': item['id']},
                label=label,
                value=item['id'] in checked,
                disabled=auto,
            ), width=11),
            dbc.Col(help_icon(item['help'] if not auto
                              else f"{item.get('help') or ''} {STOP_REVIEW_AUTO_HELP}".strip()), width=1),
        ], className="mb-1 align-items-center"))
    return rows


# A daily checklist item marked {'auto': 'stop_review'} can't be ticked by hand:
# the Drift Report's stop review ticks it when every Wealthsimple stop matches
# and unticks it when a later check finds a problem (2026-11-05).
STOP_REVIEW_AUTO = 'stop_review'
STOP_REVIEW_AUTO_HELP = ("This ticks itself: run the Drift Report, paste your Wealthsimple pending orders and "
                         "click Check stops. It's ticked when every open position has a stop for its full "
                         "share count at or above the report's Set Stop To, with no leftover orders.")


def is_auto_item(item):
    return (item or {}).get('auto') == STOP_REVIEW_AUTO


def apply_auto_ticks(entries, checked_ids, saved_ids):
    """Ticks to save: what's ticked on screen, except that a self-ticking item
    keeps its saved state (it's set only by the stop review)."""
    auto = {e['id'] for e in checklist_items(entries) if is_auto_item(e)}
    return sorted((set(checked_ids or []) - auto) | (set(saved_ids or []) & auto))


def _checklist_progress_text(items, checked_ids):
    checked = set(checked_ids or [])
    n = sum(1 for item in items if item['id'] in checked)
    return f"{n}/{len(items)} done"


# ---- Editable checklists (2026-10-09) ----
# Each checklist is an ordered list of entries, either
#   {'kind': 'heading', 'id', 'text', 'level': 1 | 2}   (1 = section, 2 = sub-heading)
#   {'kind': 'item', 'id', 'label', 'help'}
# The user edits them in the checklist editor; the saved version lives in
# db.checklist_definitions, and the constants above are only the defaults.
# Item ids are stable across edits (a renamed or moved item keeps its id), so
# a day's or week's saved ticks still apply; a new item gets a fresh id.
CHECKLIST_NAMES = {'daily': 'Daily Checklist', 'weekly': 'Weekly Checklist'}


def _sections_to_entries(sections):
    entries = []
    for section in sections:
        # Heading ids for the defaults are the old titles, so the remembered
        # open/closed state of the daily accordion (keyed on them) carries over.
        entries.append({'kind': 'heading', 'id': section['title'], 'text': section['title'], 'level': 1})
        entries.extend({'kind': 'item', **item} for item in section['items'])
        for sub in section.get('subsections', []):
            entries.append({'kind': 'heading', 'id': sub['title'], 'text': sub['title'], 'level': 2})
            entries.extend({'kind': 'item', **item} for item in sub['items'])
    return entries


DEFAULT_CHECKLISTS = {
    'daily': _sections_to_entries(DAILY_CHECKLIST_SECTIONS),
    'weekly': [{'kind': 'item', **item} for item in WEEKLY_CHECKLIST_ITEMS],
}


def load_checklist(name, db_path=None):
    """The user's saved checklist, or a copy of the built-in default."""
    saved = db.fetch_checklist_definition(name, **({'db_path': db_path} if db_path else {}))
    return saved if saved is not None else [dict(e) for e in DEFAULT_CHECKLISTS[name]]


def checklist_items(entries):
    return [e for e in (entries or []) if e.get('kind') == 'item']


def _block_end(entries, idx):
    """End (exclusive) of the block a heading at idx starts: everything up to
    the next heading of the same or a higher level (a section owns its
    sub-headings; a sub-heading owns only its items)."""
    level = entries[idx]['level']
    end = idx + 1
    while end < len(entries) and not (entries[end]['kind'] == 'heading' and entries[end]['level'] <= level):
        end += 1
    return end


def move_checklist_entry(entries, entry_id, direction):
    """A new list with one entry moved up (-1) or down (+1).

    - An item moves one row, so it can cross into the neighbouring
      sub-heading or section.
    - A heading moves together with its block (its items, and a section's
      sub-headings) past the neighbouring unit: the previous/next block at
      the same level, or a single row (a loose item, or the parent heading
      -- which moves a sub-heading into the previous/next section).
    Out-of-range moves return the list unchanged."""
    entries = list(entries)
    idx = next((i for i, e in enumerate(entries) if e['id'] == entry_id), None)
    if idx is None:
        return entries
    if entries[idx]['kind'] == 'item':
        j = idx + direction
        if 0 <= j < len(entries):
            entries[idx], entries[j] = entries[j], entries[idx]
        return entries

    level = entries[idx]['level']
    end = _block_end(entries, idx)
    block = entries[idx:end]
    if direction < 0:
        if idx == 0:
            return entries
        start = idx - 1                      # default unit: the single row above
        k = idx - 1
        while k >= 0 and not (entries[k]['kind'] == 'heading' and entries[k]['level'] <= level):
            k -= 1
        if k >= 0 and entries[k]['level'] == level:
            start = k                        # the previous sibling block
        return entries[:start] + block + entries[start:idx] + entries[end:]
    if end >= len(entries):
        return entries
    nxt = entries[end]                       # always a heading of level <= ours
    unit_end = _block_end(entries, end) if nxt['level'] == level else end + 1
    return entries[:idx] + entries[end:unit_end] + block + entries[unit_end:]


def validate_checklist(entries):
    """An error message, or None. Every heading and item needs text."""
    for e in entries:
        text = (e.get('text') if e['kind'] == 'heading' else e.get('label')) or ''
        if not text.strip():
            return "Every heading and item needs some text -- fill in or delete the empty one."
    if len({e['id'] for e in entries}) != len(entries):
        return "Duplicate entries -- reset or re-add the affected rows."
    return None


def _render_checklist_entries(entries, checked_ids, id_type):
    """Checkbox rows for items, with headings shown inline as text (a
    section in bold, a sub-heading smaller) -- the weekly list, and the body
    of each daily section."""
    checked = set(checked_ids or [])
    out = []
    for e in entries:
        if e['kind'] == 'heading':
            out.append(html.P(e['text'], className=("fw-bold mt-2 mb-1" if e['level'] == 1
                                                     else "fw-bold small mt-2 mb-1")))
        else:
            out.extend(_render_checklist_rows([e], checked, id_type))
    return out


def _daily_sections(entries):
    """Group entries into accordion sections by section (level-1) headings.
    Items before the first section heading go in an untitled 'Checklist'
    section so nothing is ever hidden."""
    sections, current = [], None
    for e in entries:
        if e['kind'] == 'heading' and e['level'] == 1:
            current = {'id': e['id'], 'title': e['text'], 'entries': []}
            sections.append(current)
        else:
            if current is None:
                current = {'id': '__top__', 'title': 'Checklist', 'entries': []}
                sections.append(current)
            current['entries'].append(e)
    return sections


def _render_daily_checklist_accordion(entries, checked_ids):
    """One dbc.Accordion, one AccordionItem per section (each independently
    collapsible, several open at once via always_open -- "roll up" a section
    once it's done without hiding the others). The first section starts
    expanded; the open/closed state is remembered (PERSIST). A sub-heading
    renders as a small heading inside its section."""
    checked = set(checked_ids or [])
    accordion_items = []
    sections = _daily_sections(entries)
    for section in sections:
        items = checklist_items(section['entries'])
        n_done = sum(1 for item in items if item['id'] in checked)
        accordion_items.append(dbc.AccordionItem(
            _render_checklist_entries(section['entries'], checked, 'daily-check-item'),
            title=f"{section['title']} ({n_done}/{len(items)})", item_id=section['id']))
    if not accordion_items:
        return html.P("This checklist is empty -- click \"Edit list\" to add items.", className="text-muted small")
    return dbc.Accordion(accordion_items, always_open=True, active_item=[sections[0]['id']],
                         id='daily-checklist-accordion', **PERSIST)


def todo_state_on(todo, date):
    """An item's state as of `date`: closed later than that means it was still open."""
    if todo['status'] == 'open' or (todo.get('closed_date') or '') > date:
        return 'open'
    return todo['status']


def todos_for_date(todos, date):
    """The to-do list as it stands on `date`: anything added by then that's
    still open, plus what was closed that day, which sorts last and stays
    visible so a mistaken click can be undone. Oldest first within each."""
    visible = [t for t in (todos or []) if t['added_date'] <= date
               and (todo_state_on(t, date) == 'open' or t.get('closed_date') == date)]
    return sorted(visible, key=lambda t: (todo_state_on(t, date) != 'open', t['added_date'], t['id']))


def todo_age_text(added_date, date):
    days = (dt.date.fromisoformat(date) - dt.date.fromisoformat(added_date)).days
    if days <= 0:
        return ''
    return 'from yesterday' if days == 1 else f'from {days} days ago'


def render_todos(todos, date):
    items = todos_for_date(todos, date)
    if not items:
        return html.P("Nothing on the list.", className="text-muted small mb-0")
    rows = []
    for t in items:
        state = todo_state_on(t, date)
        closed = state != 'open'
        if state == 'dropped':
            main = html.Span(t['text'], className="text-muted text-decoration-line-through")
        else:
            main = dbc.Checkbox(id={'type': 'todo-check', 'index': t['id']}, value=closed, label=t['text'],
                                label_class_name="text-muted text-decoration-line-through" if closed else None,
                                className="mb-0")
        age = '' if closed else todo_age_text(t['added_date'], date)
        rows.append(html.Div([
            main,
            html.Span(age, className="text-muted small ms-2 text-nowrap") if age else None,
            dbc.Button("Undo" if state == 'dropped' else "×", id={'type': 'todo-drop', 'index': t['id']},
                       color='link', size='sm', className="text-muted py-0 ms-auto",
                       title="Put it back on the list" if state == 'dropped' else "Drop this item"),
        ], className="d-flex align-items-center"))
    return rows


# ==============================================================
# Help pane (implemented 2026-09-03): static reference content for the
# slide-in Offcanvas, covering where each tool in the wider trading
# framework fits and the hard risk-framework constraints. Deliberately
# excludes any point-in-time figure (e.g. "heat is currently running X%",
# "N trades have no plan") -- those belong in memory/CLAUDE.md's evolving
# narrative or in the live banners that already compute them fresh, not in
# a static reference pane that would otherwise go stale the next time it's
# read. Written as one dcc.Markdown blob rather than nested html.P/html.Li
# components -- much less boilerplate for prose this long, and nothing here
# needs a callback to target individual lines.
# ==============================================================

HELP_PANE_MARKDOWN = """
This pane summarises the strategy (v1.2, 2026-10-31) and how TAPEs supports it. The full rules
live in `claude-project/Strategy.md`, and the day-to-day steps are in the Daily and Weekly
Checklists. Live figures (heat, exposure, regime) are in the banners and reports, not here.

#### Where each tool fits

- **Macro Regime Model v7.5:** checked first, before any symbol.
  - Sets which tactics are allowed and how much risk a new trade may take.
  - Read it on the **daily** chart (its finest horizon), with the weekly for context.
  - Watch the STRESS OVERRIDE, CREDIT DIVERGENCE and BREADTH DIVERGENCE flags.
- **Sniper Entry Exit with Stops and Targets v2.2:** **momentum** trades (scale-out).
  - Signals: ENTRY GATE, TRADE QUALITY and HOLD ADVICE.
  - Its edge differs by symbol: only trade symbols whose *Typical run* reads ≥ 1.2R over 6+
    signals.
- **ADX Pyramid Planner v0.5:** **pyramid** (scale-in) campaigns on reversals.
  - Its "TAPEs ladder" mode prints the plan to enter here.
- **FOMO & Panic Selling:** entry **timing**, for every tactic.
  - It measures market-wide (NYSE/Nasdaq) volume.
  - Panic in an uptrend = pullback timing; FOMO = don't chase.
- **Options Walls v0.1 + OptionsLevelsforTradingview.py:** check that stops sit beyond walls and
  targets sit before them. Refresh the levels weekly.
- **TAPEs** (this app): plans committed before entry, fills reconciled to plans, the daily Drift
  Report, the journals, and weekly reviews.
  - While TAPEs is running, Claude Code can read and write it over MCP. Refresh this page to see
    what it changed.
- **Wealthsimple:** execution only, by hand. The record of truth; TAPEs is only as current as the
  last import.
- **Claude Project:** evaluates symbols against the strategy and coaches the weekly review.

---

#### Regime → limits and tactics

**Work out the effective regime first.** Use the daily composite and its change over 5 and 20
sessions (Δ5, Δ20):
- **Flickering** (2+ state changes in 10 sessions): use the worse of the two states.
- **Deteriorating** (Δ5 ≤ −0.95, a drop of a state in the last 5 sessions, or any flag): use the
  next-worse state's risk limits.
- **Improving** (Δ5 ≥ +0.95, Δ20 ≥ 0, no flags): the next-better state's tactics are allowed, at
  the current state's limits. Never unlocks trades in Full Risk-Off.
- **Daily and weekly two or more states apart:** use the more cautious.

| Effective regime | Per-trade risk | Heat ceiling | Max position | Tactics |
|---|---|---|---|---|
| Full Risk-On | 2.0% | 6% | 10% | Sniper; Pyramid; Day trade |
| Partial Risk-On | 1.5% | 5% | 10% | Sniper; Pyramid; Day trade |
| Neutral | 1.0% | 4% | 7.5% | Pyramid; Day trade |
| Partial Risk-Off | 0.5% | 2% | 5% | Pyramid probes only, no adds |
| Full Risk-Off | 0% | — | — | **No new trades** |

**When the regime worsens:**
- **Into Neutral:** no new Sniper trades; open ones run on their plans.
- **Into Partial Risk-Off:** stop adding, and tighten stops to the tighter of the current stop
  and an EMA(21) trail.
- **Into Full Risk-Off** (confirmed close): exit trades at the next open into cash-equivalents.
  Graduated investments instead tighten to their weekly ATR trail.
- **Heat above the new ceiling:** no new trades or adds until it's back under. Don't force-sell
  to get there.

---

#### Hard risk limits

TAPEs only warns about these, so the checklist enforces them.

- **Portfolio value** means every account's positions at current prices plus each account's cash
  from its latest holdings export, in CAD at the Bank of Canada rate. Every % below is of that.
- **Per-trade risk** never above 2% of the portfolio (less by regime; half for a day trade). Size
  from the stop distance plus a **0.5 × ATR gap allowance** (Step 6.1); never pick a stop to fit a
  size.
  - The allowance covers a stop that an opening gap jumps. TAPEs applies it to max loss and R:R
    on the plan form, the pre-trade check (with the most shares within the cap), campaign peak
    risk and heat. The 0.5 is editable in Data.
  - Closed trades' R is unchanged, so a gap still shows as a loss beyond −1R.
- **Portfolio heat** never above 6% (caution above 5%, less by regime).
- **One position** never above 10% of the portfolio (less by regime). Shares = the smaller of the
  risk-based size and the position cap.
- **At most 10 open positions.** A campaign counts as one; cash-equivalents don't count.
- **Concentration:** no more than 3 positions or 25% in one sector or theme. A 2× ETF counts
  double.
- **Beta-weighted exposure** soft ceiling: 100% in Risk-On, 60% Neutral, 30% Partial Risk-Off.
- **Reward:** a Sniper plan's ladder R:R ≥ 1.5 : 1; below 1 : 1 is a pass.
- **The right listing:** name the exchange, ticker and currency (`TSX:CHPS` CAD is not
  `NASDAQ:CHPS` USD). Set the plan's Currency to match.
- **Long only.** Inverse ETFs are a separate case, off by default, and never in Full Risk-Off.
- **No trade without a committed plan,** and a real stop order placed with the entry.
- **Never average down.** Stops only move in the trade's favour.

---

#### The tactics

**Sniper momentum** (Plan type: Scale-out)
- **Stop:** 2.0 / 2.5 / 3.0 × ATR (4H / 1D / 1W).
- **Ladder (1D):** T1 1R sell 50% → stop to entry; T2 2.25R sell 25% → stop to T1; T3 3.5R sell
  25%. On 4H it's 1/2/3R, on 1W 1/2.75/4.5R. Treat anything above T2 as a bonus.
- **Also exit on** a close back through EMA50 and on the HOLD ADVICE time stop. TAPEs doesn't
  track these exits: act on the alert, then import the fill.

**ADX Pyramid campaign** (Plan type: Scale-in campaign)
- **Probe:** sized so the campaign's peak risk *with the gap allowance* fits the per-trade risk.
- **Adds:** the probe's size at +1R, +2R and +3R, each needing ADX > 20 with DI+ leading at the
  close. Each add is "raise the stop first, then buy". No adds in Partial Risk-Off.
- **Skip or shrink** any add that would breach the position cap.
- **Exit:** ATR trail from the probe (2.5 × ATR on 1D). Also act on the static (no new high in
  20 bars) and trend-failure alerts.
- **One campaign per symbol per account,** with nothing else open in it.

**Day trade** (Plan type: Day trade; Strategy 4.5, Neutral or better)
- **Catalyst and setup:** a fresh news catalyst written down (what, when, source) and one setup:
  panic fade, gap-and-go or VWAP pullback. Liquid symbols only, and not one you already hold.
- **Plan:** 5m ATR; stop beyond the setup's level (at least 1 × ATR); T1 1R sells 50% and moves the
  stop to entry; T2 2R (or the gap fill) sells the rest. Half the per-trade risk.
- **Session:** no entry before 9:45 or after 14:30 ET; flat by 15:45 ET; never carried overnight.
  TAPEs flags a day trade still open after its session.
- **Limits:** at most 2 a day, one open at a time; no more after day-trade losses reach 1% of the
  portfolio. After 20 day trades, a negative expectancy suspends them.

**Trade → investment** (RRSP/LIRA)
- **Eligible after:** 60+ trading days, with the stop at or above entry, price above the 40-week
  average, and the regime Neutral or better.
- **Then:** switch the plan to 1W with a 3 × weekly ATR trail, review it weekly, and trim back to
  10% above 15%.

**Discipline check** (Step 7): chasing (an entry more than ~1 ATR past the signal bar), revenge,
boredom, news/tips (except a day trade's written catalyst), or bending a rule to fit. Record the
honest trigger on the plan.

---

#### TAPEs screen by screen

- **Header:**
  - **Account view:** All accounts, or one account; it filters every list and report.
    Risk caps are always measured on the whole portfolio.
  - **Help** (this pane) and **Data:** accounts (nicknames, tracking start date), cancellation
    reasons and the stale threshold, the stop gap allowance, and bulk deletes.
  - **The freshness banner** shows when executions were last imported.
- **Summary Statistics / Performance Over Time:** results in R. The strategy picker shows
  Scale-out, Scale-in or Day trades alone. Mind the sample size.
- **Practices:** how the Strategy is executed, measured from your fills and reported, never
  enforced: trade within the regime at the open, regime set before the open, plan committed
  before the trade, no buys before 10:00 ET (day trades 9:45), day trades closed by 16:00 ET,
  and each campaign add protected at its stop-before level by the next stop review. Counts this
  week, over 4 weeks and since each practice's start; expand a row for its exceptions, or for
  unknowns (not counted). *⚠ Plan after fill* marks a trade in the journal. Today's entry in the
  Daily Trading Journal pre-fills EXECUTION with the day's exceptions -- import the day's
  activity first. The green strip on top counts your lesson streak, trades that followed the
  plan (a loss that did is a good trade) and changes made from your own lessons; the Streak
  column is each practice followed in a row.
- **Lessons:** each trade (Edit, once closed) and each daily entry has two halves -- *What worked
  (keep)* and *What to change*. A closed trade without either shows *Lesson?* in the journal,
  and the evening entry reminds you of trades closed that day.
- **Improvement register** (button on the Practices card): every *what to change* lesson, from
  trades and days. Give each a type (practice, Strategy rule, TAPEs feature, observation) and
  follow it through: *Adopted*, *Verified* once the change has stuck, or *Dropped*. An adopted
  lesson must say what it *became*: a practice, a Strategy version, a focus action and/or a
  TAPEs feature, several if needed. Linked practices and actions show how they've gone since.
- **Daily and Weekly Checklists:** tick as you go. *Edit list* changes the items and headings.
- **To-do (on the Daily Checklist):** one-off items for the day, typed in and ticked or dropped.
  Unfinished items carry forward and show their age. They don't count toward checklist progress.
- **Limits and tactics banner (top of the page):** the effective regime from your newest daily or
  weekly entry, with its date. The pre-trade check and the heat banner measure against these limits,
  and the pre-trade check also flags a plan type the tactics don't allow or a position over the max.
  It reflects that entry's close, so re-read the regime before trading.
- **Daily Trading Journal (after the close):** *Today's entry* opens the template.
  - Enter the composite, label, layers, flags and weekly; TAPEs computes Δ5/Δ20 and flicker from
    earlier entries and shows the **effective** limits and tactics. Type Δ5/Δ20 only for a day you
    didn't log.
  - Portfolio and drift are pre-filled for today (run Refresh Live Prices, Compute Market Exposure
    and the Drift Report first). The watchlist is checked against plans, holdings and the effective
    tactics.
  - The lesson/proposal line goes to the weekly review.
- **Weekly Reconciliation:** upload the Wealthsimple *holdings* export. It compares each
  account's holdings with TAPEs and stores each account's cash for the portfolio value. Dismiss a
  known, intentional gap.
- **Weekly Review** (after Friday's daily entry):
  - *Compute Balance*, then *Weekly entry*: regime, posture next week, book, results in R, last
    week's actions, daily lessons, graduations/trims, watchlist, strategy changes, market. A new
    entry pre-fills its regime and watchlist from the week's last daily entry.
  - *Generate Prompt*, paste it into the coaching chat, paste the reply back, *Save Week*.
  - The coach ends each reply with a *FOCUS NEXT WEEK* block; saving turns its items (numbered,
    bulleted or one per line) into actions A1, A2…, reviewed as Done / Not done / Dropped in the
    next weekly entry.
  - *Action register* lists every action, current and closed, with its review history.
  - The coach cites rules by number: F1–F10, S1–S7 (S6.3 = Step 6, check 3), 4.1–4.5, P5.
    Re-copy the setup message whenever Strategy.md changes.
- **1. Add a Trade Plan:**
  - Pick the **plan type** (Scale-out, Scale-in campaign, Day trade), **account** and
    **currency**.
  - Typing the symbol fetches ATR for the chosen **timeframe** (5m/15m for day trades; 4H/1D/1W
    otherwise). It reads TradingView Desktop first, switching its chart to that symbol and
    timeframe, and falls back to Yahoo.
  - **ATR-derived** recomputes the stop and targets from entry, ATR and the R× per target;
    **Manual override** freezes what you typed.
  - **Stop method:** Ladder (each target's Move Stop To), ATR trail or EMA(21) trail, each
    starting after a chosen target.
  - Target Qty % must add up to 100. A campaign gets an add ladder instead (*Fill defaults*).
  - Rate your psych state (risk emotion, urgency, focus) and tick the trade triggers. A recent
    loss shows a revenge-trade hint.
  - The summary shows max loss, ladder reward and R:R (with and without the gap allowance) and
    the stop-out at each stage. The pre-trade check and account balance need *Refresh Live
    Prices*.
  - *Save as Draft* to keep working; *Commit Plan* before or at entry.
- **2. Import Executions:** the Wealthsimple *activity* export, at the end of any day you traded.
  A generic CSV needs its account picked first.
- **Open Positions:**
  - *Refresh Live Prices* shows current R (against the original risk), a *Risk-Free* badge once
    the stop is at or above entry, and the heat banner (in CAD, with cash and the gap allowance).
  - *Compute Market Exposure* shows beta-weighted exposure as % of the portfolio, by account and
    by position.
  - A position without a plan shows *Log Plan*. List parking ETFs (BIL, CSAV) as
    **cash-equivalent symbols** so they never need one.
- **Drift Report (each morning):** *Check Plan Drift*. Raise stops to *Set Stop To*, catch up on
  reached targets, and for triggered adds raise the stop, then buy. Tick *Reconciled*.
  - **Trailing stops** are computed from completed daily bars after the close and apply next
    session. Never lower a stop to match a report.
  - **Stop review:** with the report open, press Ctrl+V to paste a screenshot of Wealthsimple's
    *Pending orders* (or drop the image). TAPEs reads it on this computer, you fix anything
    misread, then *Check stops* compares each order with *Set Stop To* and the shares held. It
    flags stops to raise, missing or leftover orders and wrong share counts, and ticks
    *Reconciled* for each match. A stop above *Set Stop To* is fine. Checking every account
    together ticks the daily checklist's drift item when every stop matches.
  - **Stop limit orders:** the list shows the *limit*, not the stop, so it goes in the Limit
    column. A limit at or above *Set Stop To* matches (the stop is at or above its limit); below
    it, type the stop from the order's details into the Stop column. A stop limit won't sell if
    price gaps below the limit.
- **Draft Trade Plans:** Edit, Commit or Discard.
- **Executed Trades Without a Plan:** *Log Plan for Selected Trade*, or *Dismiss*. Trades closed
  before an account's tracking start date are archived here.
- **Trade Journal:** Active / Closed / Cancelled / All tabs, with period and sort.
  - **R:** scale-out = P&L ÷ planned risk per share; campaign = P&L ÷ peak risk taken.
  - **Stop outcomes:** respected / slippage (≤ 0.1 ATR) / breached.
  - **Regime:** each buy is checked against the effective regime saved before 09:30 ET that
    day (usually the previous evening's entry; later edits don't count): the tactic allowed,
    the per-trade risk cap (stop + gap allowance) and the max position, against the latest
    weekly balance. *✓ Regime* or *⚠ Regime n* under the status; details in the trade view.
  - **Match** fixes a wrong pairing of a plan with its fills. *Notes* logs a dated note.
  - **Pending plans:** *Cancel* one that no longer fits (with a reason) instead of deleting it.
    *Stale* (10+ days with no fills) means cancel or recommit. *Check what would have happened*
    replays cancelled plans.
  - Two committed plans for one position read as a pyramid: cancel or edit a superseded plan
    rather than leaving it beside the new one.
  - Click a row for its details, price path and chart screenshot.
- **Rule changes** happen at the weekly review only, recorded in the Strategy change log.
"""


# ==============================================================
# Strategy rules in the coaching prompt (2026-10-23)
# The rules the coach judges against are read from claude-project/Strategy.md
# at the time the setup message is shown, so it's always the current version:
# Parts 2-5 and the change log. Part 1 (how to evaluate a symbol) and Part 6
# (the evaluation output format) are about symbol evaluation, not coaching.
# ==============================================================
STRATEGY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'claude-project', 'Strategy.md')
STRATEGY_COACH_PARTS = ('Part 2', 'Part 3', 'Part 4', 'Part 5', 'Change log')


def strategy_version(text):
    m = re.search(r'\*\*Version\s+([0-9.]+)', text or '')
    return m.group(1) if m else None


def strategy_rules_text(path=None):
    """(version, the rules sections of Strategy.md), or (None, None) when the
    file can't be read."""
    try:
        with open(path or STRATEGY_PATH, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return None, None
    sections = re.split(r'\n(?=## )', text)
    keep = [sec.strip().replace('\n---', '') for sec in sections
            if sec.startswith('## ') and sec[3:].startswith(STRATEGY_COACH_PARTS)]
    return strategy_version(text), "\n\n".join(keep)


def coaching_system_prompt(path=None):
    """The one-time setup message for the coaching chat: the instructions
    plus the strategy's rules, so the coach can cite them by number."""
    version, rules = strategy_rules_text(path)
    if not rules:
        return (WEEKLY_COACHING_SYSTEM_PROMPT + "\n\n(Strategy.md couldn't be read, so the rules "
                "aren't included. Ask me to paste them.)")
    return (WEEKLY_COACHING_SYSTEM_PROMPT + f"\n\n==== MY STRATEGY RULES (Strategy.md v{version}) ====\n\n"
            + rules)


FOCUS_HEADER = re.compile(r'^[\s#>*_]*focus(\s+(for\s+)?next\s+week)?\b', re.I)
FOCUS_ITEM = re.compile(r'^\s*(?:\d+[.)]|[-*\u2022])\s+(.*\S)')
RULE_REF = re.compile(r'\b(F(?:10|[1-9])|S[1-7](?:\.[1-9])?|4\.[1-5]|P5)\b')
ACTION_REF = re.compile(r'^\**\s*A(\d+)\b')


def parse_focus_items(text):
    """The items of a coaching reply's FOCUS NEXT WEEK block (the last such
    block): [{text, rules, ref}]. rules is 'S6.3, F8' from the item's
    citations; ref is the action id when the item carries an existing action
    ('A12 (carried): ...'), else None. [] when there's no block."""
    lines = (text or '').splitlines()
    starts = [i for i, ln in enumerate(lines) if FOCUS_HEADER.match(ln)]
    if not starts:
        return []
    block = lines[starts[-1] + 1:]
    bodies = []
    for ln in block:
        m = FOCUS_ITEM.match(ln)
        if not m:
            if bodies and ln.strip():
                break
            continue
        bodies.append(m.group(1))
    if not bodies:
        # Copying a reply out of a chat app drops the list markers, leaving one
        # item per plain line. Take the lines that end with a rule citation, or
        # failing that the first three lines.
        plain = [ln.strip() for ln in block if ln.strip()]
        bodies = [ln for ln in plain if re.search(r'\[[^\]]*\]\W*$', ln)] or plain[:3]
    items = []
    for body in bodies:
        body = body.strip().strip('*').strip()
        cites = ' '.join(a or b for a, b in re.findall(r'\[([^\]]*)\]|\(([^)]*)\)', body))
        rules = list(dict.fromkeys(RULE_REF.findall(cites)))
        ref = ACTION_REF.match(body)
        items.append({'text': body, 'rules': ", ".join(rules) or None,
                      'ref': int(ref.group(1)) if ref else None})
    return items


def action_label(action):
    return f"A{action['action_id']}"


def action_window_start(reviews, week):
    """The previous weekly review's week_ending before `week` ('' if none). A review made
    after it, and on or before `week`, counts toward `week`'s entry -- so an action closed
    mid-week in the action register (stamped with that day's date) already counts."""
    return max((r['week_ending'] for r in reviews or [] if r.get('week_ending') and r['week_ending'] < week),
               default='')


def in_review_window(day, week, since=None):
    """Whether a review dated `day` counts toward `week`'s entry. since=None: only `week` itself."""
    day = day or ''
    return day == week if since is None else bool(day) and since < day <= week


def reviewed_in_window(action, week, since=None):
    return in_review_window(action.get('reviewed_week'), week, since)


def actions_for_week(actions, week, since=None):
    """The actions to review in `week`'s entry: raised in an earlier week and still
    active (open or not done), plus any already reviewed in its window (see
    action_window_start; since=None means reviewed in `week` itself)."""
    out = []
    for a in actions or []:
        if a['source_week'] >= week:
            continue
        rw = a.get('reviewed_week') or ''
        if reviewed_in_window(a, week, since) or (a['status'] in ('open', 'not_done') and rw < week
                                                  and not (since is not None and rw > since)):
            out.append(a)
    return out


def weekly_prompt_blocker(week, reviews, actions):
    """Why the weekly prompt can't be generated yet, or None: the week's entry
    must be saved and every carried action reviewed in it (2026-11-07)."""
    row = next((r for r in reviews or [] if r.get('week_ending') == week), None)
    if not row or not row.get('entry_saved_at'):
        return f'Save the weekly entry for {week} first ("Weekly entry").'
    since = action_window_start(reviews, week)
    unreviewed = [action_label(a) for a in actions_for_week(actions, week, since)
                  if not reviewed_in_window(a, week, since)]
    if unreviewed:
        return (f"Review every carried action in the weekly entry first: {', '.join(unreviewed)} "
                "still has no status." if len(unreviewed) == 1 else
                f"Review every carried action in the weekly entry first: {', '.join(unreviewed)} "
                "still have no status.")
    return None


def actions_for_prompt(actions, week, since=None):
    """focus_actions for the weekly prompt: every active action raised before
    or in `week` (with its history), and those reviewed this week."""
    def brief(a):
        return {'id': action_label(a), 'text': a['text'], 'rules': a.get('rules'),
                'raised_week': a['source_week'], 'status': a['status'],
                'times_not_done': sum(1 for h in a.get('history') or [] if h.get('status') == 'not_done'),
                'history': a.get('history') or []}
    return {'open': [brief(a) for a in actions or []
                     if a['source_week'] <= week and a['status'] in ('open', 'not_done')],
            'reviewed_this_week': [brief(a) for a in actions or [] if reviewed_in_window(a, week, since)]}


# ==============================================================
# Weekly template (2026-10-23): one structured entry per week, in the weekly
# checklist's order, stored on the week's weekly_reviews row.
# ==============================================================
WK_WATCH_ROWS = 6
ACTION_STATUSES = [('done', 'Done'), ('not_done', 'Not done'), ('dropped', 'Dropped')]
LESSON_DECISIONS = [('strategy_change', 'Strategy change'), ('dropped', 'Dropped')]


def _week_window(week_ending):
    end = _as_date(week_ending)
    return end - dt.timedelta(days=6), end


def latest_daily_entry(entries, week_ending):
    """The structured daily entry closest to week_ending within its week."""
    start, end = _week_window(week_ending)
    inside = [e for e in entries or [] if start <= _as_date(e['obs_date']) <= end]
    return max(inside, key=lambda e: e['obs_date']) if inside else None


def weekly_lessons(entries, week_ending):
    start, end = _week_window(week_ending)
    return [{'obs_date': e['obs_date'], 'lesson': e['lesson']}
            for e in sorted(entries or [], key=lambda e: e['obs_date'])
            if e.get('lesson') and start <= _as_date(e['obs_date']) <= end]


def weekly_results(journal_rows, week_ending):
    """RESULTS (R) for closed trades that exited in the 7 days to week_ending:
    trades, win %, average win and loss in R, expectancy, the same by tactic
    (Sniper = scale-out, Pyramid = scale-in), and gap losses (beyond -1R)."""
    start, end = _week_window(week_ending)
    rows = []
    for r in journal_rows or []:
        if r.get('status') != 'Closed' or not r.get('exit_time'):
            continue
        exit_day = pd.to_datetime(r['exit_time'], utc=True, errors='coerce')
        if pd.isna(exit_day) or not (start <= exit_day.date() <= end):
            continue
        rows.append(r)

    def r_of(row):
        v = _num_or_none(row.get('r_multiple'))
        return None if v is None or np.isnan(v) else v

    def mean(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    rs = [r_of(r) for r in rows if r_of(r) is not None]
    wins = [r for r in rows if (_num_or_none(r.get('actual_pnl')) or 0) > 0]
    by = {}
    for label, ptype in (('Sniper', 'scale_out'), ('Pyramid', 'scale_in'), ('Day', 'day_trade')):
        sub = [r_of(r) for r in rows if plan_type_of(r) == ptype]
        by[label] = {'trades': len(sub), 'expectancy_r': mean([x for x in sub if x is not None])}
    return {'trades': len(rows), 'trades_with_r': len(rs),
            'win_pct': round(len(wins) / len(rows) * 100, 1) if rows else None,
            'avg_win_r': mean([x for x in rs if x > 0]), 'avg_loss_r': mean([x for x in rs if x < 0]),
            'expectancy_r': mean(rs), 'by_tactic': by,
            'gap_losses': sum(1 for x in rs if x < -1),
            'day_trades_carried_overnight': sum(1 for r in rows if is_day_trade(r)
                                                and r.get('carried_overnight') is True)}


def weekly_balance_delta(reviews, week, staged=None):
    """Balance for the week (the staged Compute Balance if there is one, else
    the saved one) and its change vs. the previous week on the same basis, in
    the balance's currency and %. None when there's no balance."""
    cur = next((r for r in reviews or [] if r['week_ending'] == week and r.get('balance') is not None), None)
    if staged and staged.get('total') is not None:
        bal, basis, src = staged['total'], (staged.get('currency'), 'cash_by_currency' in staged), 'computed'
    elif cur:
        bal, basis, src = cur['balance'], balance_basis(cur), 'saved'
    else:
        return None
    prior = sorted([r for r in reviews or [] if r.get('balance') is not None and r['week_ending'] < week],
                   key=lambda r: r['week_ending'])
    out = {'balance': round(bal, 2), 'currency': basis[0], 'source': src, 'change': None, 'pct': None}
    if prior:
        p = prior[-1]
        if balance_basis(p) != basis:
            out['change'] = 'basis_changed'
        else:
            out['change'] = round(bal - p['balance'], 2)
            out['pct'] = round((bal - p['balance']) / p['balance'] * 100, 2) if p['balance'] else None
    return out


WK_INPUTS = ([('wk-weekly-regime', 'value'), ('wk-weekly-composite', 'value'), ('wk-daily-regime', 'value'),
              ('wk-daily-composite', 'value'), ('wk-d20', 'value'), ('wk-flags', 'value'),
              ('wk-heat', 'value'), ('wk-positions', 'value'), ('wk-beta', 'value'),
              ('wk-theme', 'value'), ('wk-theme-pct', 'value'), ('wk-graduations', 'value')]
             + [(f'wk-w{i}-{f}', 'value') for i in range(1, WK_WATCH_ROWS + 1) for f in ('sym', 'tactic', 'trigger')]
             + [('wk-strategy-changes', 'value'), ('wk-market', 'value')])


def wk_form_values(entry):
    """The form's values (WK_INPUTS order) for a weekly entry dict."""
    e = entry or {}
    vals = {'wk-weekly-regime': e.get('weekly_regime'), 'wk-weekly-composite': e.get('weekly_composite'),
            'wk-daily-regime': e.get('daily_regime'), 'wk-daily-composite': e.get('daily_composite'),
            'wk-d20': e.get('delta20') if e.get('delta20_manual') else None,
            'wk-flags': list(e.get('flags') or []),
            'wk-heat': e.get('heat_pct'), 'wk-positions': e.get('positions'), 'wk-beta': e.get('beta_pct'),
            'wk-theme': e.get('largest_theme'), 'wk-theme-pct': e.get('largest_theme_pct'),
            'wk-graduations': e.get('graduations'), 'wk-strategy-changes': e.get('strategy_changes'),
            'wk-market': e.get('market')}
    rows = list(e.get('watchlist') or []) + [{}] * WK_WATCH_ROWS
    for i, w in enumerate(rows[:WK_WATCH_ROWS], start=1):
        vals[f'wk-w{i}-sym'] = w.get('symbol')
        vals[f'wk-w{i}-tactic'] = w.get('tactic')
        vals[f'wk-w{i}-trigger'] = w.get('trigger')
    return [vals[cid] for cid, _ in WK_INPUTS]


def wk_watchlist(v):
    rows = []
    for i in range(1, WK_WATCH_ROWS + 1):
        sym = (v.get(f'wk-w{i}-sym') or '').strip().upper()
        if sym:
            rows.append({'symbol': sym, 'tactic': v.get(f'wk-w{i}-tactic') or None,
                         'trigger': (v.get(f'wk-w{i}-trigger') or '').strip() or None})
    return rows


def wk_posture(week, v, daily_entries):
    return compute_effective_regime(
        journal_history(daily_entries), week, _num_or_none(v.get('wk-daily-composite')),
        v.get('wk-daily-regime') or None, v.get('wk-flags'), v.get('wk-weekly-regime') or None,
        delta20=_num_or_none(v.get('wk-d20')))


def wk_entry_from_values(week, vals, daily_entries, journal_rows, lesson_decisions):
    """The weekly entry to store from the form's values (WK_INPUTS order), with
    the posture computed from the regime fields and the results from the
    journal. Returns (entry, posture)."""
    v = dict(zip([cid for cid, _ in WK_INPUTS], vals))
    posture = wk_posture(week, v, daily_entries)
    text = lambda k: (v.get(k) or '').strip() or None  # noqa: E731
    positions = _num_or_none(v.get('wk-positions'))
    entry = {'weekly_regime': v.get('wk-weekly-regime') or None,
             'weekly_composite': _num_or_none(v.get('wk-weekly-composite')),
             'daily_regime': v.get('wk-daily-regime') or None,
             'daily_composite': _num_or_none(v.get('wk-daily-composite')),
             'delta20': posture['delta20'], 'delta20_manual': 1 if posture['delta20_src'] == 'manual' else 0,
             'flags': list(v.get('wk-flags') or []),
             'posture_limits': posture.get('limits_state'), 'posture_tactics': posture.get('tactics_state'),
             'posture_note': " ".join(posture['reasons']) or None,
             'heat_pct': _num_or_none(v.get('wk-heat')),
             'positions': int(positions) if positions is not None else None,
             'beta_pct': _num_or_none(v.get('wk-beta')),
             'largest_theme': text('wk-theme'), 'largest_theme_pct': _num_or_none(v.get('wk-theme-pct')),
             'results': weekly_results(journal_rows, week),
             'lesson_decisions': lesson_decisions,
             'graduations': text('wk-graduations'), 'watchlist': wk_watchlist(v),
             'strategy_changes': text('wk-strategy-changes'), 'market': text('wk-market')}
    return entry, posture


def weekly_entry_modal():
    regime_opts = [{'label': '—', 'value': ''}] + [{'label': r, 'value': r} for r in reversed(REGIME_ORDER)]
    num = dict(type='number', size='sm', step='any')

    def lab(t, tip=None):
        return dbc.Label([t, help_icon(tip)] if tip else t, size='sm', className="mb-0 small fw-semibold")

    section = lambda title: html.H6(title, className="mt-3 mb-1 text-uppercase small fw-bold text-secondary")  # noqa: E731
    watch_rows = [dbc.Row([
        dbc.Col(dbc.Input(id=f'wk-w{i}-sym', size='sm', placeholder="EXCH:TICKER"), width=3),
        dbc.Col(dbc.Select(id=f'wk-w{i}-tactic', size='sm',
                           options=[{'label': '—', 'value': ''}] + [{'label': l, 'value': v} for v, l in DJ_TACTICS]),
                width=3),
        dbc.Col(dbc.Input(id=f'wk-w{i}-trigger', size='sm', placeholder="trigger"), width=6),
    ], className="mb-1 g-1") for i in range(1, WK_WATCH_ROWS + 1)]
    body = [
        html.Div(id='wk-head'),
        html.Div(id='wk-loaded', className="small text-muted"),
        section("Regime (weekly close)"),
        dbc.Row([
            dbc.Col([lab("Weekly label"), dbc.Select(id='wk-weekly-regime', size='sm', options=regime_opts)], width=3),
            dbc.Col([lab("Weekly composite"), dbc.Input(id='wk-weekly-composite', **num)], width=2),
            dbc.Col([lab("Daily label"), dbc.Select(id='wk-daily-regime', size='sm', options=regime_opts)], width=3),
            dbc.Col([lab("Daily composite"), dbc.Input(id='wk-daily-composite', **num)], width=2),
            dbc.Col([lab("Δ20", "Computed from the daily journal entry 20 sessions back; type it only if "
                                "that day wasn't logged."), dbc.Input(id='wk-d20', placeholder="auto", **num)],
                    width=2),
        ], className="g-2"),
        dbc.Checklist(id='wk-flags', inline=True, className="small mt-1",
                      options=[{'label': k, 'value': k} for k, _ in REGIME_FLAGS], value=[]),
        section("Posture next week"),
        html.Div(id='wk-posture'),
        section("Book"),
        dbc.Row([
            dbc.Col([lab("Heat %"), dbc.Input(id='wk-heat', **num)], width=2),
            dbc.Col([lab(f"Positions (of {MAX_OPEN_POSITIONS})"),
                     dbc.Input(id='wk-positions', type='number', size='sm', step=1)], width=2),
            dbc.Col([lab("Beta %"), dbc.Input(id='wk-beta', **num)], width=2),
            dbc.Col([lab("Largest theme", "TAPEs doesn't tag sectors or themes, so this is typed."),
                     dbc.Input(id='wk-theme', size='sm', placeholder="e.g. Semis")], width=4),
            dbc.Col([lab("Theme %"), dbc.Input(id='wk-theme-pct', **num)], width=2),
        ], className="g-2"),
        html.Div(id='wk-book-check', className="mt-1"),
        section("Results (R)"),
        html.Div(id='wk-results'),
        section("Last week's actions"),
        html.Div(id='wk-actions'),
        section("Daily lessons"),
        html.Div(id='wk-lessons'),
        section("Graduations / trims"),
        dbc.Textarea(id='wk-graduations', rows=2, size='sm', placeholder="Positions graduated to investments, "
                                                                         "or trimmed back to 10% -- or 'none'"),
        section("Watchlist"),
        *watch_rows,
        html.Div(id='wk-watch-check', className="mt-1"),
        section("Strategy changes"),
        dbc.Textarea(id='wk-strategy-changes', rows=2, size='sm',
                     placeholder="Change-log lines for Strategy.md (date, change, reason) -- or 'none'"),
        section("Market (one line)"),
        dbc.Input(id='wk-market', size='sm'),
        html.Div(id='wk-status', className="mt-3"),
    ]
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Weekly entry")),
        dbc.ModalBody(body),
        dbc.ModalFooter([dbc.Button("Close", id='btn-wk-close', color='secondary'),
                         dbc.Button("Save entry", id='btn-wk-save', color='primary')]),
    ], id='wk-modal', size='xl', is_open=False, scrollable=True)


def render_weekly_head(week, delta):
    parts = [html.B(f"Week ending {week}", className="me-3")]
    if delta:
        unit = f"{delta['currency']} " if delta['currency'] else ""
        parts.append(html.Span(f"Balance {unit}{delta['balance']:,.2f}", className="me-3"))
        ch = delta['change']
        if ch == 'basis_changed':
            parts.append(html.Small("Δ n/a (previous week measured differently)", className="text-muted"))
        elif ch is not None:
            parts.append(html.Span(f"Δ {unit}{ch:+,.2f}" + (f" ({delta['pct']:+.2f}%)" if delta['pct'] is not None
                                                            else ""),
                                   className="text-success" if ch >= 0 else "text-danger"))
        if delta['source'] == 'computed':
            parts.append(html.Small(" (from Compute Balance; saved with the entry)", className="text-muted"))
    else:
        parts.append(html.Small("No balance yet: click Compute Balance on the card to include it.",
                                className="text-muted"))
    return html.Div(parts)


def render_weekly_results(res):
    if not res or not res.get('trades'):
        return html.Small("No trades closed this week.", className="text-muted")
    f = lambda x, suf="R": "—" if x is None else f"{x:+.2f}{suf}"  # noqa: E731
    by = res['by_tactic']
    return html.Div([
        html.Div(f"Trades {res['trades']} · Win {res['win_pct']:g}% · Avg win {f(res['avg_win_r'])} · "
                 f"Avg loss {f(res['avg_loss_r'])} · Expectancy {f(res['expectancy_r'])}", className="small"),
        html.Div([f"Sniper {by['Sniper']['trades']}, exp {f(by['Sniper']['expectancy_r'])} · "
                  f"Pyramid {by['Pyramid']['trades']}, exp {f(by['Pyramid']['expectancy_r'])} · "
                  f"Day {by['Day']['trades']}, exp {f(by['Day']['expectancy_r'])} · ",
                  html.Span(f"Gap losses beyond −1R: {res['gap_losses']}",
                            className="text-danger fw-bold" if res['gap_losses'] else "")], className="small"),
    ])


def render_action_rows(actions, week, since=None):
    if not actions:
        return html.Small("No open actions from earlier coaching notes.", className="text-muted")
    opts = [{'label': '—', 'value': ''}] + [{'label': l, 'value': v} for v, l in ACTION_STATUSES]
    rows = []
    for a in actions:
        missed = sum(1 for h in a.get('history') or [] if h.get('status') == 'not_done'
                     and not in_review_window(h.get('week'), week, since))
        reviewed = reviewed_in_window(a, week, since)
        meta = (f"raised {a['source_week']}" + (f" · not done {missed}×" if missed else "")
                + (f" · reviewed {a['reviewed_week']} in the action register"
                   if reviewed and a.get('reviewed_week') != week else ""))
        current = a['status'] if reviewed else ''
        rows.append(dbc.Row([
            dbc.Col([html.B(action_label(a) + " "), html.Span(a['text'], className="small"),
                     html.Div(([html.Span(a['rules'], className="badge bg-light text-dark border me-2")]
                               if a.get('rules') else []) + [html.Small(meta, className="text-muted")])], width=7),
            dbc.Col(dbc.Select(id={'type': 'wk-action-status', 'index': a['action_id']}, size='sm',
                               options=opts, value=current), width=2),
            dbc.Col(dbc.Input(id={'type': 'wk-action-note', 'index': a['action_id']}, size='sm',
                              placeholder="why (for not done / dropped)",
                              value=a.get('status_note') if reviewed else None), width=3),
        ], className="mb-1 g-1 align-items-center"))
    return html.Div(rows)


def render_lesson_rows(lessons):
    if not lessons:
        return html.Small("No lessons in this week's daily entries.", className="text-muted")
    opts = [{'label': '—', 'value': ''}] + [{'label': l, 'value': v} for v, l in LESSON_DECISIONS]
    return html.Div([dbc.Row([
        dbc.Col([html.Small(l['obs_date'] + " ", className="text-muted"), html.Span(l['lesson'], className="small")],
                width=7),
        dbc.Col(dbc.Select(id={'type': 'wk-lesson-decision', 'index': i}, size='sm', options=opts,
                           value=l.get('decision') or ''), width=2),
        dbc.Col(dbc.Input(id={'type': 'wk-lesson-note', 'index': i}, size='sm', placeholder="note",
                          value=l.get('note')), width=3),
    ], className="mb-1 g-1 align-items-center") for i, l in enumerate(lessons)])


def merge_lessons(saved, current):
    """Saved lesson decisions plus any lesson added to the week's daily
    entries since (keyed on date + text)."""
    seen = {(l['obs_date'], l['lesson']) for l in saved or []}
    return list(saved or []) + [dict(l, decision='', note=None) for l in current
                                if (l['obs_date'], l['lesson']) not in seen]


# ---- Action register: every coaching focus action, current and closed ----
ACTION_REGISTER_VIEWS = [('current', 'Current'), ('closed', 'Closed'), ('all', 'All')]
ACTION_STATUS_LABELS = {'open': ('Open', 'info'), 'not_done': ('Not done, carried', 'warning'),
                        'done': ('Done', 'success'), 'dropped': ('Dropped', 'secondary')}


def action_is_current(action):
    return action.get('status') in ('open', 'not_done')


def action_register_rows(actions, view='current'):
    """The register's rows for a view: 'current' (open or not done, oldest
    raised first, so the longest-running come first), 'closed' (done or
    dropped, most recently closed first) or 'all' (current, then closed).
    Each row adds `times_not_done` and `closed_week` (the week it was marked
    done/dropped, else None)."""
    rows = []
    for a in actions or []:
        hist = sorted(a.get('history') or [], key=lambda h: h.get('week') or '')
        rows.append(dict(a, history=hist,
                         times_not_done=sum(1 for h in hist if h.get('status') == 'not_done'),
                         closed_week=None if action_is_current(a) else a.get('reviewed_week')))
    current = sorted((r for r in rows if action_is_current(r)), key=lambda r: (r['source_week'], r['action_id']))
    closed = sorted((r for r in rows if not action_is_current(r)),
                    key=lambda r: (r['closed_week'] or '', r['action_id']), reverse=True)
    return {'current': current, 'closed': closed}.get(view, current + closed)


def action_register_counts(actions):
    cur = sum(1 for a in actions or [] if action_is_current(a))
    return {'current': cur, 'closed': len(actions or []) - cur, 'all': len(actions or [])}


def action_register_changes(statuses, ids, notes, actions):
    """(changes, problem) from the action register's selects: changes = [(action_id, status,
    note)] for rows whose status or note differs from what's stored (a blank status is no
    change); problem names any not done / dropped without a reason (then nothing is saved)."""
    by_id = {a['action_id']: a for a in actions or []}
    changes, missing = [], []
    for status, ident, note in zip(statuses, ids, notes):
        a = by_id.get(ident['index'])
        if not status or a is None:
            continue
        note = (note or '').strip() or None
        if status == a['status'] and note == (a.get('status_note') or None):
            continue
        if status in ('not_done', 'dropped') and not note:
            missing.append(action_label(a))
        changes.append((a['action_id'], status, note))
    problem = (f"Say why for {', '.join(missing)} (not done or dropped). Nothing was saved."
               if missing else None)
    return changes, problem


def render_action_register(actions, view):
    rows = action_register_rows(actions, view)
    if not rows:
        return html.Small({'current': "No current actions.", 'closed': "No closed actions yet."}
                          .get(view, "No actions yet. They come from a coaching note's FOCUS NEXT WEEK block."),
                          className="text-muted")
    head = html.Thead(html.Tr([html.Th(h) for h in ("ID", "Raised", "Action", "Status", "Review history",
                                                    "Update")]), style=STICKY_HEADER)
    opts = [{'label': '—', 'value': ''}] + [{'label': l, 'value': v} for v, l in ACTION_STATUSES]
    body = []
    for r in rows:
        label, color = ACTION_STATUS_LABELS.get(r['status'], (r['status'], 'secondary'))
        status = [dbc.Badge(label, color=color, text_color='dark' if color == 'warning' else None)]
        if r['times_not_done']:
            status.append(html.Div(f"not done {r['times_not_done']}×", className="small text-danger"))
        hist = [html.Div([html.Span(h.get('week') or '', className="text-muted me-1",
                                    style={'whiteSpace': 'nowrap'}),
                          ACTION_STATUS_LABELS.get(h.get('status'), (h.get('status'),))[0]]
                         + ([html.Span(f": {h['note']}", className="fst-italic")] if h.get('note') else []),
                         className="small") for h in r['history']]
        body.append(html.Tr([
            html.Td(html.B(action_label(r))),
            html.Td(html.Small(r['source_week']), style={'whiteSpace': 'nowrap'}),
            html.Td([html.Div(r['text'], className="small")]
                    + ([html.Span(r['rules'], className="badge bg-light text-dark border")] if r.get('rules') else [])),
            html.Td(status),
            html.Td(hist or html.Small("not reviewed yet", className="text-muted")),
            html.Td([dbc.Select(id={'type': 'ar-status', 'index': r['action_id']}, options=opts, size='sm',
                                value='' if r['status'] == 'open' else r['status']),
                     dbc.Input(id={'type': 'ar-note', 'index': r['action_id']}, size='sm', className="mt-1",
                               placeholder="why (not done / dropped)", value=r.get('status_note') or '')],
                    style={'minWidth': '190px'}),
        ]))
    return html.Div(dbc.Table([head, html.Tbody(body)], size='sm', hover=True, className="mb-0"),
                    style=SCROLL_PANEL)


def action_register_modal():
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Action register")),
        dbc.ModalBody([
            html.Small("Focus actions from the coaching notes. Mark them done, not done or dropped here as "
                       "soon as you've dealt with one, or in the week's entry; either place updates the same "
                       "record. A change made here is dated today and counts toward the next weekly entry, "
                       "which shows it as already reviewed.", className="text-muted d-block mb-2"),
            dbc.RadioItems(id='action-register-view', value='current', inline=True,
                           options=[{'label': l, 'value': v} for v, l in ACTION_REGISTER_VIEWS],
                           className="btn-group mb-2", inputClassName="btn-check",
                           labelClassName="btn btn-outline-primary btn-sm",
                           labelCheckedClassName="active", **PERSIST),
            html.Div(id='action-register-body'),
        ]),
        dbc.ModalFooter([html.Div(id='action-register-status', className="me-auto small"),
                         dbc.Button("Save changes", id='btn-action-register-save', color='primary', size='sm')]),
    ], id='action-register-modal', size='xl', is_open=False)


# ==============================================================
# Structured Daily Trading Journal (2026-10-23)
# One entry per day on a fixed template whose fields follow the Strategy's
# steps (claude-project/Strategy.md). TAPEs computes the Macro Regime's
# 5- and 20-session change, flicker and the effective regime from the
# composites and labels logged in earlier entries, pre-fills portfolio and
# drift fields, and checks the watchlist against plans and holdings. The
# older free-text notes (daily_observations) are kept as legacy entries.
# ==============================================================
REGIME_ORDER = ['Full Risk-Off', 'Partial Risk-Off', 'Neutral', 'Partial Risk-On', 'Full Risk-On']
REGIME_MAX_SCORE = 9.5          # Macro Regime Model v7.5: sum of layer weights
REGIME_DELTA_TRIGGER = 0.95     # 10% of max over 5 sessions = deteriorating/improving (Strategy Step 1)
REGIME_LIMITS = {
    'Full Risk-On':     {'risk': 2.0, 'heat': 6.0, 'max_pos': 10.0, 'tactics': ('sniper', 'pyramid', 'day'),
                         'tactics_text': 'Sniper momentum; Pyramid campaigns; Day trades (half risk)'},
    'Partial Risk-On':  {'risk': 1.5, 'heat': 5.0, 'max_pos': 10.0, 'tactics': ('sniper', 'pyramid', 'day'),
                         'tactics_text': 'Sniper momentum; Pyramid campaigns; Day trades (half risk)'},
    'Neutral':          {'risk': 1.0, 'heat': 4.0, 'max_pos': 7.5, 'tactics': ('pyramid', 'day'),
                         'tactics_text': 'Pyramid campaigns; Day trades (half risk)'},
    'Partial Risk-Off': {'risk': 0.5, 'heat': 2.0, 'max_pos': 5.0, 'tactics': ('probe',),
                         'tactics_text': 'Pyramid probes only (no adds), A-grade setups'},
    'Full Risk-Off':    {'risk': 0.0, 'heat': None, 'max_pos': None, 'tactics': (),
                         'tactics_text': 'No new trades'},
}
REGIME_FLAGS = [('STRESS', 'Stress override'), ('CREDIT', 'Credit divergence'), ('BREADTH', 'Breadth divergence')]
DJ_LAYERS = [('layer_t', 'T', 'Trend'), ('layer_v', 'V', 'Volatility'), ('layer_c', 'C', 'Credit'),
             ('layer_l', 'L', 'Liquidity'), ('layer_m', 'M', 'Macro'), ('layer_s', 'S', 'Sentiment'),
             ('layer_b', 'B', 'Breadth')]
DJ_TACTICS = [('Sniper', 'Sniper momentum'), ('Pyramid', 'Pyramid campaign')]
DJ_WATCH_ROWS = 6
MAX_OPEN_POSITIONS = 10
CANADIAN_EXCHANGE_PREFIXES = {'TSX', 'TSXV', 'TSE', 'NEO', 'CSE'}


def regime_from_composite(composite):
    """The regime band a composite falls in (the indicator's own label can
    differ: it adds hysteresis and the stress override)."""
    if composite in (None, ''):
        return None
    f = float(composite) / REGIME_MAX_SCORE
    if f >= 0.69:
        return 'Full Risk-On'
    if f >= 0.47:
        return 'Partial Risk-On'
    if f <= -0.38:
        return 'Full Risk-Off'
    if f <= -0.16:
        return 'Partial Risk-Off'
    return 'Neutral'


def _as_date(value):
    return pd.to_datetime(value).date()


def _sessions_back(day, n):
    """The date n business days (sessions) before `day`."""
    return (pd.Timestamp(day) - pd.offsets.BDay(n)).date()


def _logged_near(history, target, tolerance=1):
    """(date, entry) of the logged entry with a composite closest to `target`,
    within +/- tolerance sessions -- exact first, then the earlier side (a
    market holiday shifts a session back). None if there isn't one."""
    best = None
    for d, e in history.items():
        if e.get('composite') is None:
            continue
        gap = abs(int(np.busday_count(min(d, target), max(d, target))))
        if gap > tolerance:
            continue
        key = (gap, 0 if d <= target else 1)
        if best is None or key < best[0]:
            best = (key, d, e)
    return (best[1], best[2]) if best else None


def compute_effective_regime(history, obs_date, composite, regime, flags=None, weekly=None,
                             delta5=None, delta20=None, flicker=None):
    """Strategy Step 1 from the journal itself. history: {date: entry} of
    OTHER days' logged entries (composite, regime). delta5/delta20/flicker
    are manual overrides (None = compute). Returns a dict with the deltas
    (and which entry they were measured against), flicker, the direction
    (deteriorating / improving / steady), the effective limits and tactics
    states, the limits themselves, and the reasons."""
    day = _as_date(obs_date)
    hist = {}
    for k, v in (history or {}).items():
        d = _as_date(k)
        if d != day:
            hist[d] = v
    out = {'reasons': [], 'band': regime_from_composite(composite)}

    def delta(n, manual):
        if manual not in (None, ''):
            return round(float(manual), 2), 'manual', None
        if composite in (None, ''):
            return None, None, None
        found = _logged_near(hist, _sessions_back(day, n))
        if not found:
            return None, None, None
        return round(float(composite) - float(found[1]['composite']), 2), 'auto', found[0].isoformat()

    out['delta5'], out['delta5_src'], out['delta5_ref'] = delta(5, delta5)
    out['delta20'], out['delta20_src'], out['delta20_ref'] = delta(20, delta20)

    start = _sessions_back(day, 9)
    labels = sorted((d, e['regime']) for d, e in hist.items() if start <= d < day and e.get('regime') in REGIME_ORDER)
    if regime in REGIME_ORDER:
        labels.append((day, regime))
    if flicker not in (None, ''):
        out['flicker'], out['flicker_src'] = bool(flicker), 'manual'
    elif len(labels) >= 5:
        changes = sum(1 for a, b in zip(labels, labels[1:]) if a[1] != b[1])
        out['flicker'], out['flicker_src'] = changes >= 2, 'auto'
    else:
        out['flicker'], out['flicker_src'] = None, None
    out['labels_logged'] = len(labels)

    flags = [f for f in (flags or []) if f]
    prior5 = [e['regime'] for d, e in hist.items()
              if _sessions_back(day, 5) <= d < day and e.get('regime') in REGIME_ORDER]
    dropped = regime in REGIME_ORDER and any(
        REGIME_ORDER.index(r) > REGIME_ORDER.index(regime) for r in prior5)
    d5, d20 = out['delta5'], out['delta20']
    deteriorating = (d5 is not None and d5 <= -REGIME_DELTA_TRIGGER) or dropped or bool(flags)
    improving = (not deteriorating and d5 is not None and d5 >= REGIME_DELTA_TRIGGER
                 and d20 is not None and d20 >= 0)
    out.update({'dropped': dropped, 'deteriorating': deteriorating, 'improving': improving,
                'direction': 'deteriorating' if deteriorating else 'improving' if improving else 'steady'})
    if regime not in REGIME_ORDER:
        out.update({'limits_state': None, 'tactics_state': None, 'limits': None})
        out['reasons'].append('Enter the regime label to work out the effective regime.')
        return out

    base = REGIME_ORDER.index(regime)
    if out['flicker']:
        worst = min(REGIME_ORDER.index(r) for _, r in labels)
        if worst < base:
            out['reasons'].append(f"Flickering: using the worse state, {REGIME_ORDER[worst]}.")
        base = min(base, worst)
    limits = tactics = base
    if deteriorating:
        why = []
        if d5 is not None and d5 <= -REGIME_DELTA_TRIGGER:
            why.append(f"Δ5 {d5:+.2f}")
        if dropped:
            why.append("dropped a state in the last 5 sessions")
        if flags:
            why.append("flag: " + ", ".join(flags))
        limits = max(0, base - 1)
        out['reasons'].append(f"Deteriorating ({'; '.join(why)}): {REGIME_ORDER[limits]} risk limits.")
    elif improving and base > 0:
        tactics = min(len(REGIME_ORDER) - 1, base + 1)
        out['reasons'].append(f"Improving (Δ5 {d5:+.2f}, Δ20 {d20:+.2f}): "
                              f"{REGIME_ORDER[tactics]} tactics at {REGIME_ORDER[base]} limits.")
    if weekly in REGIME_ORDER and abs(REGIME_ORDER.index(weekly) - REGIME_ORDER.index(regime)) >= 2:
        if REGIME_ORDER.index(weekly) < limits:
            limits = REGIME_ORDER.index(weekly)
            out['reasons'].append(f"Weekly ({weekly}) is 2+ states from daily: its more cautious limits apply.")
    if base == 0 or limits == 0:
        tactics = 0                    # no new trades in Full Risk-Off, and none at 0% risk
    out.update({'limits_state': REGIME_ORDER[limits], 'tactics_state': REGIME_ORDER[tactics],
                'limits': REGIME_LIMITS[REGIME_ORDER[limits]],
                'tactics_text': REGIME_LIMITS[REGIME_ORDER[tactics]]['tactics_text']})
    if out['band'] and out['band'] != regime and not ('STRESS' in flags and regime == 'Partial Risk-Off'):
        out['reasons'].append(f"The label differs from the composite's band ({out['band']}): "
                              f"hysteresis or an override. Check the indicator.")
    return out


def current_posture(daily_entries, weekly_rows, today=None):
    """The limits and tactics in force: the newest saved daily or weekly entry
    with an effective regime, not dated after today. A weekly entry wins a tie
    on date, since it's written after that Friday's daily entry. stale means
    2+ weekdays have passed since its date (holidays aren't known, so one can
    warn early). None if there's no such entry."""
    today = today or dt.date.today().isoformat()
    found = []
    for e in daily_entries or []:
        found.append((str(e.get('obs_date') or '')[:10], 0, 'daily', e.get('effective_limits'),
                      e.get('effective_tactics'), e.get('effective_note')))
    for w in weekly_rows or []:
        if w.get('entry_saved_at'):
            found.append((str(w.get('week_ending') or '')[:10], 1, 'weekly', w.get('posture_limits'),
                          w.get('posture_tactics'), w.get('posture_note')))
    usable = [f for f in found if f[0] and f[0] <= today and f[3] in REGIME_LIMITS and f[4] in REGIME_LIMITS]
    if not usable:
        return None
    as_of, _, source, limits_state, tactics_state, note = max(usable, key=lambda f: (f[0], f[1]))
    sessions_old = int(np.busday_count(as_of, today))
    return {'source': source, 'as_of': as_of, 'limits_state': limits_state, 'tactics_state': tactics_state,
            'limits': REGIME_LIMITS[limits_state], 'tactics_text': REGIME_LIMITS[tactics_state]['tactics_text'],
            'note': note, 'sessions_old': sessions_old, 'stale': sessions_old >= 2}


def posture_caps(posture):
    """Per-trade cap and heat thresholds to measure against. With no posture,
    the Strategy's maximums. Caution keeps the same share of the ceiling as the
    maximums (5 of 6); Full Risk-Off has no ceiling, so any open risk warns."""
    if not posture:
        return {'risk': PER_TRADE_RISK_CAP_PCT, 'caution': PORTFOLIO_RISK_CAUTION_PCT,
                'warning': PORTFOLIO_RISK_WARNING_PCT, 'state': None,
                'max_pos': REGIME_LIMITS['Full Risk-On']['max_pos']}
    limits = posture['limits']
    warning = limits['heat'] or 0.0
    return {'risk': limits['risk'], 'warning': warning, 'state': posture['limits_state'],
            'caution': warning * PORTFOLIO_RISK_CAUTION_PCT / PORTFOLIO_RISK_WARNING_PCT,
            'max_pos': limits['max_pos']}


def tactic_warning(plan_type, posture, has_adds=False):
    """Why the tactics in force don't allow this plan type, or None. A scale-out
    plan is the Sniper tactic and a scale-in plan the Pyramid tactic."""
    if not posture:
        return None
    state = posture['tactics_state']
    allowed = REGIME_LIMITS[state]['tactics']
    if not allowed:
        return f"No new trades under {state} tactics."
    if plan_type == 'day_trade':
        return None if 'day' in allowed else f"Day trades aren't allowed under {state} tactics."
    if plan_type == 'scale_in':
        if 'probe' in allowed and has_adds:
            return f"{state} tactics allow a probe only, with no adds."
        return None
    return None if 'sniper' in allowed else f"Sniper (scale-out) trades aren't allowed under {state} tactics."


def pretrade_position_check(entry, qty, stages, existing_value, portfolio_value, trade_rate, max_pct):
    """The position as a % of the portfolio against the max position limit
    (Strategy 6.2: shares x entry), on top of what's already held. A campaign is
    measured at each stage's trigger and reports its largest, since an add can
    breach the cap the probe fits. max_pct None (Full Risk-Off) or no portfolio
    value returns None."""
    if max_pct is None or not portfolio_value:
        return None
    sizes = ([(st['label'], st['shares'] * st['trigger']) for st in stages['stages']] if stages
             else [(None, float(qty) * float(entry))])
    label, value = max(sizes, key=lambda s: s[1])
    pct = (value * float(trade_rate) + (existing_value or 0.0)) / portfolio_value * 100
    return {'pct': pct, 'label': label, 'max_pct': max_pct, 'over': pct > max_pct}


# ---- Regime adherence (2026-11-06) ----
# Each buy is checked against the effective regime in force at the open of its
# day: the newest daily or weekly entry already saved before 09:30 ET that
# day, as that version stood then (db.regime_versions). Entries are usually
# written after the close, so that's normally the previous evening's entry;
# an edit made after the open doesn't count. Fill timestamps are Eastern wall
# clock even though the export labels them +00:00, so their date is used as is.
# Sells never break a regime rule. Risk is measured as in the pre-trade check
# (to the initial stop, or a campaign's scheduled stop, plus the gap
# allowance) against the latest saved weekly balance in CAD.
MARKET_OPEN_ET = dt.time(9, 30)
REGIME_CHECK_TOLERANCE_PCT = 0.005   # rounding room on the risk and position caps


def _utc_to_et(value):
    """A stored UTC timestamp (naive or aware) as naive Eastern time."""
    ts = pd.Timestamp(value)
    ts = ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')
    return ts.tz_convert(MARKET_CLOSE_TZ).tz_localize(None)


def regime_at_open(versions, day):
    """The effective regime in force at 09:30 ET on `day` (an ISO date): of the
    versions saved before then, the newest entry date (a weekly entry wins a
    tie), as it stood at its latest save before the open. None if none.
    edited_after_open: a seeded version whose entry was edited after that open,
    so the version in force then may have differed (it wasn't kept)."""
    opened = dt.datetime.combine(dt.date.fromisoformat(day), MARKET_OPEN_ET)
    best = {}
    for v in versions or []:
        if str(v.get('obs_date') or '')[:10] > day or v.get('limits_state') not in REGIME_LIMITS \
                or v.get('tactics_state') not in REGIME_LIMITS:
            continue
        saved = _utc_to_et(v['saved_at'])
        if saved >= opened:
            continue
        key = (str(v['obs_date'])[:10], 1 if v.get('source') == 'weekly' else 0)
        if key not in best or saved >= best[key][0]:
            best[key] = (saved, v)
    if not best:
        return None
    (as_of, _), (saved, v) = max(best.items(), key=lambda kv: kv[0])
    edited = v.get('edited_at')
    sessions_old = int(np.busday_count(as_of, day))
    return {'source': v.get('source'), 'as_of': as_of, 'saved_at': saved.isoformat(timespec='minutes'),
            'limits_state': v['limits_state'], 'tactics_state': v['tactics_state'], 'note': v.get('note'),
            'limits': REGIME_LIMITS[v['limits_state']], 'sessions_old': sessions_old, 'stale': sessions_old >= 2,
            'edited_after_open': bool(edited) and _utc_to_et(edited) >= opened}


def _latest_on_or_before(rows, day, date_key):
    found = [r for r in rows or [] if str(r.get(date_key) or '')[:10] <= day]
    return max(found, key=lambda r: str(r[date_key])) if found else None


def regime_buy_tactic_issue(plan_type, stage, tactics_state):
    """Why a buy breaks the tactics in force, or None. stage: 0 = the entry
    (a campaign's probe), k >= 1 = a campaign's add k."""
    allowed = REGIME_LIMITS[tactics_state]['tactics']
    if not allowed:
        return f"No new trades under {tactics_state} tactics."
    if plan_type == 'day_trade':
        return None if 'day' in allowed else f"Day trades aren't allowed under {tactics_state} tactics."
    if plan_type == 'scale_in':
        if stage and 'pyramid' not in allowed:
            return f"Add {stage}: {tactics_state} tactics allow a probe only, with no adds."
        return None
    return None if 'sniper' in allowed else f"Sniper (scale-out) entries aren't allowed under {tactics_state} tactics."


def regime_adherence(row, versions, weekly_rows=None, fx_history=None, gap_k=None):
    """Each buy-day of a journal row checked against the regime in force at
    that day's open (regime_at_open). Returns None for a row with no buys, or
    {checked, days, breaches, unchecked_days}; each day has the governing
    regime, the stages bought, the position's risk and size after that day's
    buys as a % of the portfolio, and breaches/notes. Days before any regime
    was logged aren't checked."""
    plan_type = plan_type_of(row)
    sign = 1 if row.get('direction') == 'BUY' else -1
    campaign = row.get('campaign') if isinstance(row.get('campaign'), dict) else None
    if plan_type == 'scale_in' and campaign:
        buys = [dict(b) for b in campaign.get('buys') or []]
        exits = list(campaign.get('exits') or [])
        schedule = [row.get('stop_loss')] + [a.get('stop_before') for a in (row.get('adds') or [])
                                             if a.get('price') not in (None, '')]
    else:
        buys = [dict(f, stage=0) for f in _fill_list_of(row, 'entry_fill_list')]
        exits = _fill_list_of(row, 'exit_fill_list')
        schedule = [row.get('stop_loss')]
    if not buys:
        return None
    currency = _scalar(row.get('currency')) or 'USD'
    gap = gap_per_share(row.get('atr_at_commit'), row.get('entry_price'), row.get('stop_loss'), gap_k)
    events = sorted([(str(b['time']), 0, b) for b in buys] + [(str(f['time']), 1, f) for f in exits],
                    key=lambda e: (e[0], e[1]))
    shares = cost = 0.0
    reached = 0
    per_day = {}
    for when, is_exit, f in events:
        if is_exit:
            if shares > 1e-9:
                sold = min(float(f['qty']), shares)
                cost -= cost / shares * sold
                shares -= sold
            continue
        shares += float(f['qty'])
        cost += float(f['qty']) * float(f['price'])
        reached = max(reached, int(f.get('stage') or 0))
        stop = schedule[min(reached, len(schedule) - 1)]
        risk = sign * (cost - float(stop) * shares) + gap * shares if stop not in (None, '') else None
        d = per_day.setdefault(when[:10], {'stages': set(), 'buys': 0, 'shares': 0.0, 'risk': None, 'value': 0.0})
        d['stages'].add(int(f.get('stage') or 0))
        d['buys'] += 1
        d['shares'] += float(f['qty'])
        if risk is not None:
            d['risk'] = risk if d['risk'] is None else max(d['risk'], risk)
        d['value'] = max(d['value'], cost)

    fx_by_date = {r['date']: r['rate'] for r in fx_history or []}
    days, unchecked = [], []
    for day in sorted(per_day):
        d = per_day[day]
        gov = regime_at_open(versions, day)
        stages = sorted(d['stages'])
        stage_text = ", ".join('entry' if k == 0 and plan_type != 'scale_in' else 'probe' if k == 0 else f'add {k}'
                               for k in stages)
        item = {'date': day, 'stages': stage_text, 'buys': d['buys'], 'shares': d['shares'], 'governing': gov,
                'risk_pct': None, 'cap_pct': None, 'position_pct': None, 'max_pos_pct': None,
                'breaches': [], 'notes': []}
        if gov is None:            # nothing saved before this open: before the journal began
            unchecked.append(day)
            continue
        for k in stages:
            issue = regime_buy_tactic_issue(plan_type, k, gov['tactics_state'])
            if issue and issue not in item['breaches']:
                item['breaches'].append(issue)
        limits = gov['limits']
        balance = _latest_on_or_before([w for w in weekly_rows or [] if w.get('balance') not in (None, '')
                                        and w.get('balance_currency') == BASE_CURRENCY], day, 'week_ending')
        rate = 1.0
        if currency != BASE_CURRENCY:
            near = [dd for dd in fx_by_date if dd <= day] or sorted(fx_by_date)[:1]
            rate = fx_by_date[max(near)] if near else None
        if limits['heat'] is not None and balance and rate:
            base = float(balance['balance'])
            cap = limits['risk'] * (DAY_TRADE_RISK_FRACTION if plan_type == 'day_trade' else 1.0)
            item['cap_pct'], item['max_pos_pct'] = cap, limits['max_pos']
            if d['risk'] is not None:
                item['risk_pct'] = d['risk'] * rate / base * 100
                if item['risk_pct'] > cap + REGIME_CHECK_TOLERANCE_PCT:
                    item['breaches'].append(f"Risk {item['risk_pct']:.2f}% of the portfolio (stop + gap allowance), "
                                            f"over the {cap:g}% cap under {gov['limits_state']} limits.")
            item['position_pct'] = d['value'] * rate / base * 100
            if item['position_pct'] > limits['max_pos'] + REGIME_CHECK_TOLERANCE_PCT:
                item['breaches'].append(f"Position {item['position_pct']:.1f}% of the portfolio, over the "
                                        f"{limits['max_pos']:g}% max position under {gov['limits_state']} limits.")
            item['notes'].append(f"Portfolio: CAD {base:,.0f} (week ending {balance['week_ending']}).")
        elif limits['heat'] is not None:
            item['notes'].append("Size not checked: no weekly balance in CAD (or FX rate) on or before this day.")
        if gov['stale']:
            item['notes'].append(f"The regime in force was {gov['sessions_old']} sessions old at the open.")
        if gov['edited_after_open']:
            item['notes'].append("That entry was edited after this open, and the earlier version wasn't kept "
                                 "(entries saved before 2026-11-06): checked against the edited version.")
        days.append(item)
    if not days:
        return {'checked': False, 'days': [], 'breaches': 0, 'unchecked_days': len(unchecked),
                'unchecked_dates': unchecked}
    return {'checked': True, 'days': days, 'breaches': sum(len(d['breaches']) for d in days),
            'unchecked_days': len(unchecked), 'unchecked_dates': unchecked}


def regime_check_text(day):
    """One line for a checked buy-day."""
    gov = day['governing']
    head = f"{day['date']} ({day['stages']})"
    src = f"{'weekly' if gov['source'] == 'weekly' else 'daily'} entry {gov['as_of']}"
    sizes = []
    if day['risk_pct'] is not None:
        sizes.append(f"risk {day['risk_pct']:.2f}% / {day['cap_pct']:g}%")
    if day['position_pct'] is not None:
        sizes.append(f"position {day['position_pct']:.1f}% / {day['max_pos_pct']:g}%")
    return (f"{head}: limits {gov['limits_state']}, tactics {gov['tactics_state']} ({src})"
            + (f" -- {', '.join(sizes)}" if sizes else ""))


def render_regime_check(check):
    """The detail view's "Regime at the open" section, or None."""
    if not isinstance(check, dict) or not check.get('checked'):
        return None
    items = []
    for d in check['days']:
        lines = [html.Span(regime_check_text(d))]
        lines += [html.Div("⚠ " + b, className="text-danger") for b in d['breaches']]
        lines += [html.Div(n, className="text-muted small") for n in d['notes']]
        items.append(html.Li(lines, className="mb-1"))
    if check.get('unchecked_days'):
        items.append(html.Li(f"{check['unchecked_days']} earlier buy-day(s) not checked: no regime was saved yet.",
                             className="text-muted small"))
    n = check['breaches']
    return html.Div([
        html.H6(["Regime at the open ",
                 html.Span(f"{n} breach{'es' if n != 1 else ''}" if n else "within the rules",
                           className=f"badge bg-{'danger' if n else 'success'}")], className="mt-3"),
        html.Small("Each buy-day against the effective regime saved before 09:30 ET that day. Risk is to the stop "
                   "plus the gap allowance; the portfolio is the latest saved weekly balance.",
                   className="text-muted d-block mb-1"),
        html.Ul(items),
    ])


def load_regime_context():
    return {'versions': db.fetch_regime_versions(), 'weekly': db.fetch_weekly_reviews(),
            'fx': db.fetch_fx_history(f"USD{BASE_CURRENCY}"), 'gap_k': load_gap_allowance_atr()}


def add_regime_checks(journal_rows, context=None):
    """row['regime_check'] (regime_adherence) and row['regime_breaches'] (its
    count, None when not checked) on every matched row."""
    context = context if context is not None else load_regime_context()
    for row in journal_rows:
        check = None
        if row.get('status') in ('Open (in progress)', 'Closed'):
            check = regime_adherence(row, context['versions'], context['weekly'], context['fx'], context['gap_k'])
        row['regime_check'] = check
        row['regime_breaches'] = check['breaches'] if check and check['checked'] else None
    return journal_rows


# ---- Practices (2026-11-06) ----
# How the Strategy is executed, measured from the Wealthsimple fills and
# reported, never enforced. Each practice counts from its start date, so a
# trade is only scored against practices in force when it was made. Fill times
# are the export's effective_date + effective_time (the actual fill, Eastern
# wall clock); plan timestamps are stored in UTC.
PRACTICES = [
    {'id': 'regime_at_open', 'name': 'Trade within the regime at the open', 'unit': 'buy-days',
     'start': '2026-10-23',
     'help': "Each day a trade was bought fits the effective regime saved before 09:30 ET that day: "
             "the tactic allowed, the per-trade risk cap and the max position."},
    {'id': 'regime_set', 'name': 'Regime set before the open', 'unit': 'trading days',
     'start': '2026-10-23',
     'help': "On each day something was bought, the regime in force at the open came from an entry for the "
             "previous session or that morning (not 2+ sessions old, not missing)."},
    {'id': 'plan_first', 'name': 'Plan committed before the trade', 'unit': 'trades',
     'start': '2026-10-23',
     'help': "The plan was committed before its first fill (a draft doesn't count)."},
    {'id': 'entry_window', 'name': 'No buys before 10:00 ET (day trades 9:45)', 'unit': 'buy-days',
     'start': '2026-10-23',
     'help': "Entries, probes and adds are bought at 10:00 ET or later, once the open's volatility has "
             "settled: never pre-market. A day trade's entry waits until 9:45 ET (Strategy 4.5)."},
    {'id': 'day_trade_flat', 'name': 'Day trades closed by 4:00 pm ET', 'unit': 'day trades',
     'start': '2026-10-23',
     'help': "Every share of a day trade is sold on the day it was bought, by 16:00 ET. One still open "
             "on its own day reads unknown until the close."},
    {'id': 'add_protected', 'name': 'Add protected at its stop', 'unit': 'add days',
     'start': '2026-11-04',
     'help': "After a campaign add, the first stop review (that day, or the next session's) shows every "
             "share, the add included, covered by stop orders at or above the add's stop-before level. "
             "With no review by then, the add reads unknown and isn't counted. Stop reviews began on "
             "Nov 5, so earlier adds can't be checked."},
]
PRACTICE_BY_ID = {p['id']: p for p in PRACTICES}
PRACTICE_RECENT_DAYS = 28
ENTRY_WINDOW_START = dt.time(10, 0)
DAY_TRADE_FLAT_BY = dt.time(16, 0)
DAY_TRADE_ENTRY_START = dt.time(9, 45)


def _naive_et(value):
    """A fill time (Eastern wall clock, whatever offset the export labels it with) as naive."""
    return pd.Timestamp(str(value)[:19])


def _row_buys(row):
    """A journal row's buys, oldest first: [{time (naive ET), qty, price, stage}] -- a campaign's
    staged buys (trims netted), else the entry fills (stage 0)."""
    campaign = row.get('campaign') if isinstance(row.get('campaign'), dict) else None
    fills = campaign['buys'] if campaign and campaign.get('buys') else \
        [dict(f, stage=0) for f in _fill_list_of(row, 'entry_fill_list')]
    return sorted(({'time': _naive_et(f['time']), 'qty': float(f['qty']), 'price': float(f['price']),
                    'stage': int(f.get('stage') or 0)} for f in fills), key=lambda b: b['time'])


def _stage_label(row, stage):
    if stage:
        return f"add {stage}"
    return 'probe' if plan_type_of(row) == 'scale_in' else 'entry'


def plan_first_check(row):
    """The plan-before-the-trade result for a journal row: {ok, first_fill, committed} or None
    when there's no fill or no commit time."""
    buys = _row_buys(row)
    when = _scalar(row.get('committed_at')) or _scalar(row.get('created_at'))
    if not buys or not when:
        return None
    first = buys[0]['time']
    committed = _utc_to_et(when)
    return {'ok': committed <= first, 'first_fill': first, 'committed': committed}


def entry_window_items(row):
    """One item per day the row bought: ok when every buy that day was at ENTRY_WINDOW_START or later
    (DAY_TRADE_ENTRY_START for a day trade)."""
    start = DAY_TRADE_ENTRY_START if is_day_trade(row) else ENTRY_WINDOW_START
    by_day = {}
    for b in _row_buys(row):
        by_day.setdefault(b['time'].date().isoformat(), []).append(b)
    out = []
    for day, buys in sorted(by_day.items()):
        early = [b for b in buys if b['time'].time() < start]
        detail = ("; ".join(f"{_stage_label(row, b['stage'])} bought {b['time']:%H:%M}" for b in early)
                  + f" -- before {start:%H:%M} ET") if early else \
            "bought " + ", ".join(f"{b['time']:%H:%M}" for b in buys)
        out.append({'date': day, 'ok': not early, 'detail': detail})
    return out


def day_trade_flat_item(row, today=None):
    """A day trade's result: ok when every exit is on the entry day before DAY_TRADE_FLAT_BY and
    nothing is left open; None while it's still open on its own day. None for other plan types."""
    if not is_day_trade(row):
        return None
    buys = _row_buys(row)
    if not buys:
        return None
    day = buys[0]['time'].date()
    today = dt.date.fromisoformat(today) if isinstance(today, str) else (today or dt.date.today())
    exits = sorted(_naive_et(f['time']) for f in _fill_list_of(row, 'exit_fill_list'))
    if row.get('status') == 'Open (in progress)':
        if day >= today:
            return {'date': day.isoformat(), 'ok': None, 'detail': "still open today."}
        return {'date': day.isoformat(), 'ok': False, 'detail': f"still open after {day:%b %d} -- carried overnight."}
    last = exits[-1] if exits else None
    if last is None:
        return None
    if last.date() > day:
        return {'date': day.isoformat(), 'ok': False,
                'detail': f"last exit {last:%b %d %H:%M} ET -- carried overnight."}
    if last.time() >= DAY_TRADE_FLAT_BY:
        return {'date': day.isoformat(), 'ok': False, 'detail': f"last exit {last:%H:%M} ET, after 16:00."}
    return {'date': day.isoformat(), 'ok': True, 'detail': f"flat at {last:%H:%M} ET."}


def add_protection_items(row, stop_reviews):
    """One item per day a campaign add was bought. The add is checked against the first stop review
    saved after it, if that review is from the add's day or the next session: the shares held then
    must all be covered by stop sells (for this account, symbol and currency) at or above the stop
    scheduled for the stage reached -- an order's stop, or its limit, which a stop sits at or above.
    ok None = no such review (unknown); a position already sold by then isn't listed."""
    if plan_type_of(row) != 'scale_in':
        return []
    buys = _row_buys(row)
    exits = sorted(({'time': _naive_et(f['time']), 'qty': float(f['qty'])}
                    for f in ((row.get('campaign') or {}).get('exits') or [])), key=lambda f: f['time'])
    schedule = [row.get('stop_loss')] + [a.get('stop_before') for a in (row.get('adds') or [])
                                         if a.get('price') not in (None, '')]
    reviews = sorted(((_utc_to_et(rv['checked_at']), rv) for rv in stop_reviews or []), key=lambda x: x[0])
    acct, sym, cur = _scalar(row.get('account_id')), row['symbol'], _scalar(row.get('currency'))
    add_days = {}
    for b in buys:
        if b['stage']:
            add_days.setdefault(b['time'].date().isoformat(), []).append(b)
    out = []
    for day, adds in sorted(add_days.items()):
        last = max(b['time'] for b in adds)
        labels = ", ".join(sorted({_stage_label(row, b['stage']) for b in adds}))
        deadline = (pd.Timestamp(day) + pd.offsets.BDay(1)).date()
        review = next(((t, rv) for t, rv in reviews if t > last), None)
        if review is None or review[0].date() > deadline:
            out.append({'date': day, 'reported_on': None, 'ok': None,
                        'detail': f"{labels}: no stop review by the next session, so it can't be checked."})
            continue
        when, rv = review
        held = sum(b['qty'] for b in buys if b['time'] <= when) - sum(f['qty'] for f in exits if f['time'] <= when)
        if held <= 1e-9:
            continue
        reached = max(b['stage'] for b in buys if b['time'] <= when)
        need = schedule[min(reached, len(schedule) - 1)]
        if need in (None, ''):
            continue
        need = float(need)
        covered = 0.0
        for o in rv.get('orders') or []:
            if (o.get('symbol') or '').upper() != sym or (o.get('account_id') and acct and o['account_id'] != acct) \
                    or (o.get('currency') and cur and o['currency'] != cur) or not _is_stop_sell(o.get('order_type')):
                continue
            level = _ocr_num(o.get('stop'))
            level = level if level is not None else _ocr_num(o.get('limit'))
            if level is not None and level >= need - STOP_REVIEW_TOLERANCE:
                covered += _ocr_num(o.get('qty')) or 0.0
        ok = covered >= held - 1e-6
        out.append({'date': day, 'reported_on': rv['obs_date'], 'ok': ok,
                    'detail': f"{labels}: stop review {when:%b %d %H:%M} ET shows {covered:g} of {held:g} shares "
                              f"stopped at or above {need:.2f}" + ("." if ok else " -- the rest are unprotected "
                                                                    "or stopped lower.")})
    return out


def practice_items(journal_rows, stop_reviews=None):
    """Every scored instance of each practice since its start: {practice_id: [{date, ok, detail,
    symbols, plan_ids, reported_on}]}. ok is True/False, or None for unknown (not counted).
    reported_on is the day the result became known (an add's stop review), else the date.
    regime_set is one item per trading day (several trades bought the same day share it)."""
    out = {p['id']: [] for p in PRACTICES}
    by_day = {}
    for r in journal_rows or []:
        if r.get('status') not in ('Open (in progress)', 'Closed'):
            continue
        check = r.get('regime_check') if isinstance(r.get('regime_check'), dict) else None
        sym, pid = r['symbol'], r['plan_id']
        flat = day_trade_flat_item(r)
        for pid_key, its in (('entry_window', entry_window_items(r)),
                             ('day_trade_flat', [flat] if flat else []),
                             ('add_protected', add_protection_items(r, stop_reviews))):
            for it in its:
                if it['date'] >= PRACTICE_BY_ID[pid_key]['start']:
                    out[pid_key].append(dict(it, symbols=[sym], plan_ids=[pid]))
        if check:
            for d in check.get('days', []):
                if d['date'] >= PRACTICE_BY_ID['regime_at_open']['start']:
                    out['regime_at_open'].append({
                        'date': d['date'], 'ok': not d['breaches'], 'symbols': [sym], 'plan_ids': [pid],
                        'detail': "; ".join(d['breaches']) or regime_check_text(d)})
                by_day.setdefault(d['date'], {'gov': d['governing'], 'symbols': [], 'plan_ids': []})
                by_day[d['date']]['symbols'].append(sym)
                by_day[d['date']]['plan_ids'].append(pid)
            for day in check.get('unchecked_dates', []):
                by_day.setdefault(day, {'gov': None, 'symbols': [], 'plan_ids': []})
                by_day[day]['symbols'].append(sym)
                by_day[day]['plan_ids'].append(pid)
        pf = plan_first_check(r)
        if pf and pf['first_fill'].date().isoformat() >= PRACTICE_BY_ID['plan_first']['start']:
            out['plan_first'].append({
                'date': pf['first_fill'].date().isoformat(), 'ok': pf['ok'], 'symbols': [sym], 'plan_ids': [pid],
                'detail': (f"committed {pf['committed']:%b %d %H:%M} ET, first fill {pf['first_fill']:%b %d %H:%M} ET"
                           if not pf['ok'] else f"committed {pf['committed']:%b %d %H:%M} ET, before the first fill")})
    for day, d in sorted(by_day.items()):
        if day < PRACTICE_BY_ID['regime_set']['start']:
            continue
        gov = d['gov']
        if gov is None:
            ok, detail = False, "No regime entry saved before the open."
        elif gov['sessions_old'] > 1:
            ok, detail = False, f"The newest entry was {gov['as_of']}, {gov['sessions_old']} sessions old at the open."
        else:
            saved = pd.Timestamp(gov['saved_at'])
            ok, detail = True, f"{gov['source'].capitalize()} entry {gov['as_of']}, saved {saved:%b %d %H:%M} ET."
        out['regime_set'].append({'date': day, 'ok': ok, 'detail': detail,
                                  'symbols': sorted(set(d['symbols'])), 'plan_ids': d['plan_ids']})
    for its in out.values():
        for it in its:
            it.setdefault('reported_on', it['date'])
    return out


def practice_scorecard(items, today=None):
    """Per practice: done / total this week (Monday to today), over the last PRACTICE_RECENT_DAYS days
    and since its start, plus its exceptions newest first and its unknowns (ok None, not counted)."""
    today = dt.date.fromisoformat(today) if isinstance(today, str) else (today or dt.date.today())
    monday = (today - dt.timedelta(days=today.weekday())).isoformat()
    recent = (today - dt.timedelta(days=PRACTICE_RECENT_DAYS - 1)).isoformat()
    end = today.isoformat()
    out = []
    for p in PRACTICES:
        its = [i for i in items.get(p['id'], []) if i['date'] <= end]

        def count(since):
            sel = [i for i in its if i['date'] >= since and i['ok'] is not None]
            return {'ok': sum(1 for i in sel if i['ok']), 'total': len(sel)}
        out.append(dict(p, week=count(monday), recent=count(recent), all=count(p['start']),
                        exceptions=sorted((i for i in its if i['ok'] is False), key=lambda i: i['date'],
                                          reverse=True),
                        unknown=sorted((i for i in its if i['ok'] is None), key=lambda i: i['date'],
                                       reverse=True)))
    return out


def practices_vs_results(journal_rows, items):
    """Closed trades whose first buy was on or after the earliest practice start: those with no
    exception in any practice vs those with one or more. Win rate and average R per group,
    withheld below SUMMARY_MIN_SAMPLE trades."""
    broke = {pid for its in items.values() for i in its if i['ok'] is False for pid in i['plan_ids']}
    scored = {pid for its in items.values() for i in its if i['ok'] is not None for pid in i['plan_ids']}
    groups = {'followed': [], 'broke': []}
    for r in journal_rows or []:
        if r.get('status') != 'Closed' or r['plan_id'] not in scored:
            continue
        rv = pd.to_numeric(r.get('r_multiple'), errors='coerce')
        if pd.isna(rv):
            continue
        groups['broke' if r['plan_id'] in broke else 'followed'].append(float(rv))
    out = {}
    for k, rs in groups.items():
        out[k] = {'n': len(rs)}
        if len(rs) >= SUMMARY_MIN_SAMPLE:
            out[k].update({'win_rate_pct': round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1),
                           'avg_r': round(sum(rs) / len(rs), 2)})
    return out


def _practice_count_text(c):
    return f"{c['ok']} of {c['total']}" if c['total'] else "—"


def render_practices(journal_rows, today=None, stop_reviews=None, entries=None, improvement_list=None):
    """The Practices card body: the encouragement strip, then one row per practice with its counts,
    streak, exceptions and unknowns behind toggles, and practices vs results."""
    items = practice_items(journal_rows, stop_reviews)
    card = practice_scorecard(items, today)
    streaks = practice_streaks(items)
    head = html.Thead(html.Tr([html.Th("Practice"), html.Th("This week"),
                               html.Th(f"Last {PRACTICE_RECENT_DAYS // 7} weeks"), html.Th("Since start"),
                               html.Th(["Streak", help_icon("Followed in a row, up to the latest one (best run in "
                                                            "brackets). An unknown doesn't break it.")])]))
    body = []
    for p in card:
        tone = ('text-success' if p['all']['total'] and not p['exceptions'] else
                'text-danger' if p['exceptions'] else 'text-muted')
        name = [html.Span(p['name']), help_icon(p['help'] + f" Counts {p['unit']} from {p['start']}.")]
        if p['exceptions']:
            name.append(html.Details([
                html.Summary(f"{len(p['exceptions'])} exception{'s' if len(p['exceptions']) != 1 else ''}",
                             className="small text-danger"),
                html.Ul([html.Li(f"{e['date']} {', '.join(e['symbols'])}: {e['detail']}", className="small")
                         for e in p['exceptions']], className="mb-1")]))
        if p['unknown']:
            name.append(html.Details([
                html.Summary(f"{len(p['unknown'])} unknown (not counted)", className="small text-muted"),
                html.Ul([html.Li(f"{e['date']} {', '.join(e['symbols'])}: {e['detail']}", className="small")
                         for e in p['unknown']], className="mb-1")]))
        body.append(html.Tr([html.Td(name), html.Td(_practice_count_text(p['week'])),
                             html.Td(_practice_count_text(p['recent'])),
                             html.Td(_practice_count_text(p['all']), className=f"fw-bold {tone}"),
                             html.Td(f"{streaks[p['id']][0]} (best {streaks[p['id']][1]})"
                                     if streaks[p['id']][1] else "—")]))
    vs = practices_vs_results(journal_rows, items)

    def group(label, g):
        if 'avg_r' not in g:
            return f"{label}: n/a (n={g['n']})"
        return f"{label}: {g['n']} trades, {g['win_rate_pct']:.0f}% wins, avg {g['avg_r']:+.2f}R"
    summary = encouragement_summary(journal_rows, items, entries, improvement_list or [], today)
    return [render_encouragement(summary),
            dbc.Table([head, html.Tbody(body)], size='sm', hover=True, className="mb-1"),
            html.Small(f"Closed trades since the practices started -- {group('followed every practice', vs['followed'])}"
                       f" · {group('broke one or more', vs['broke'])}. Averages need {SUMMARY_MIN_SAMPLE}+ trades "
                       f"per group.", className="text-muted")]


def render_plan_first(row):
    """The detail view's plan-before-the-trade line, or None."""
    if row.get('status') not in ('Open (in progress)', 'Closed'):
        return None
    pf = plan_first_check(row)
    if not pf or pf['first_fill'].date().isoformat() < PRACTICE_BY_ID['plan_first']['start']:
        return None
    text = (f"Plan committed {pf['committed']:%b %d %H:%M} ET; first fill {pf['first_fill']:%b %d %H:%M} ET.")
    return html.Div([html.B("Plan before the trade: "),
                     html.Span("✓ " + text if pf['ok'] else "⚠ " + text + " The plan came after the trade.",
                               className="text-success" if pf['ok'] else "text-danger")], className="mt-2 small")


def practices_for_prompt(journal_rows, week_ending, stop_reviews=None):
    """The weekly prompt's practices: counts for the 7 days to week_ending and since start, and
    that week's exceptions and unknowns."""
    end = pd.to_datetime(week_ending).date()
    start = (end - dt.timedelta(days=6)).isoformat()
    items = practice_items(journal_rows, stop_reviews)
    out = []
    for p in PRACTICES:
        its = [i for i in items[p['id']] if i['date'] <= end.isoformat()]
        week = [i for i in its if i['date'] >= start]

        def count(sel):
            known = [i for i in sel if i['ok'] is not None]
            return f"{sum(1 for i in known if i['ok'])} of {len(known)}"
        out.append({'practice': p['name'], 'unit': p['unit'], 'since': p['start'],
                    'this_week': count(week), 'since_start': count(its),
                    'unknown_this_week': sum(1 for i in week if i['ok'] is None),
                    'exceptions_this_week': [{'date': i['date'], 'symbols': i['symbols'], 'detail': i['detail']}
                                             for i in week if i['ok'] is False]})
    return out


def practice_prefill(journal_rows, day, stop_reviews=None, last_import=None):
    """The daily journal's EXECUTION pre-fill for `day`: the practice exceptions that became known
    that day (an add's result arrives with the stop review after it), or a line saying all were
    followed, or that no buys are on record (with the last import, since TAPEs can't tell a quiet
    day from one not imported yet)."""
    items = practice_items(journal_rows, stop_reviews)
    known = [(p, i) for p in PRACTICES for i in items[p['id']]
             if i.get('reported_on') == day and i['ok'] is not None]
    bad = [f"⚠ {', '.join(i['symbols'])} -- {p['name']}: {i['detail']}" for p, i in known if i['ok'] is False]
    if bad:
        return "\n".join(bad)
    if known:
        return f"Practices: all followed ({len(known)} checks)."
    since = f" (last import: {last_import:%b %d %H:%M} UTC)" if last_import is not None else ""
    return f"No buys on record for {day}{since}. If you traded, import the activity export first."


# ---- Lessons and continuous improvement (2026-11-07) ----
# Lessons are written as two halves, on each trade (lesson_keep / lessons_learned)
# and each daily entry (lesson_keep / lesson): what worked (keep) and what to
# change. Every "what to change" lands in the Improvement register, where it's
# classified and followed through: new -> adopted (as a practice, a Strategy
# rule or a TAPEs feature, with a link) -> verified, or dropped. Encouragement
# comes from the same data: streaks, the changes your own lessons produced, and
# trades that followed the plan whatever their result.
TRIVIAL_LESSONS = {'', 'none', 'n/a', 'na', '-', 'nil', 'nothing', 'nothing to change', '.'}
IMPROVEMENT_CATEGORIES = [('practice', 'Trading practice'), ('strategy', 'Strategy rule'),
                          ('tapes', 'TAPEs feature'), ('observation', 'Observation (no action)')]
IMPROVEMENT_STATUSES = [('new', 'New'), ('adopted', 'Adopted'), ('verified', 'Verified'), ('dropped', 'Dropped')]
IMPROVEMENT_STATUS_COLORS = {'new': 'secondary', 'adopted': 'primary', 'verified': 'success', 'dropped': 'gray-400'}
# What an adopted lesson became (2026-11-07): links to real things, several per lesson.
BECAME_KINDS = [('practice', 'Practice'), ('strategy', 'Strategy version'), ('action', 'Focus action'),
                ('tapes', 'TAPEs feature')]
STRATEGY_VERSION_RE = re.compile(r'^- \*\*(\d+(?:\.\d+)+) \((\d{4}-\d{2}-\d{2})\):\*\*', re.M)


def strategy_versions(path=None):
    """[{version, date}] from Strategy.md's change log, oldest first ([] if unreadable)."""
    try:
        with open(path or STRATEGY_PATH, encoding='utf-8') as f:
            text = f.read()
    except OSError:
        return []
    return [{'version': v, 'date': d} for v, d in STRATEGY_VERSION_RE.findall(text)]


def _action_ref(ref):
    ref = str(ref or '').strip()
    return ref[1:] if ref[:1] in ('A', 'a') else ref


def became_options(actions, versions):
    """The Became picker's options: every practice, Strategy version and focus action
    (TAPEs features are typed, since TAPEs has no list of them)."""
    opts = [{'label': f"Practice: {p['name']}", 'value': f"practice:{p['id']}"} for p in PRACTICES]
    opts += [{'label': f"Strategy v{v['version']} ({v['date']})", 'value': f"strategy:{v['version']}"}
             for v in reversed(versions or [])]
    for a in sorted(actions or [], key=lambda a: -a['action_id']):
        text = a['text'] if len(a['text']) <= 70 else a['text'][:68] + '…'
        opts.append({'label': f"{action_label(a)}: {text}", 'value': f"action:{a['action_id']}"})
    return opts


def became_from_values(selected, tapes_text):
    """The picker's values plus the TAPEs-feature box (several separated by ';') as a
    became list."""
    out = []
    for v in selected or []:
        kind, _, ref = str(v).partition(':')
        if kind in ('practice', 'strategy', 'action') and ref and {'kind': kind, 'ref': ref} not in out:
            out.append({'kind': kind, 'ref': ref})
    for t in re.split(r'\s*;\s*', (tapes_text or '').strip()):
        if t and {'kind': 'tapes', 'ref': t} not in out:
            out.append({'kind': 'tapes', 'ref': t})
    return out


def became_values(became):
    """(picker values, TAPEs-feature text) for a became list."""
    became = became or []
    return ([f"{b['kind']}:{b['ref']}" for b in became if b['kind'] != 'tapes'],
            "; ".join(b['ref'] for b in became if b['kind'] == 'tapes'))


def became_label(b):
    if b['kind'] == 'practice':
        return f"Practice: {PRACTICE_BY_ID.get(b['ref'], {}).get('name', b['ref'])}"
    if b['kind'] == 'strategy':
        return f"Strategy v{b['ref']}"
    if b['kind'] == 'action':
        return f"A{_action_ref(b['ref'])}"
    return f"TAPEs: {b['ref']}"


def became_text(became):
    return " · ".join(became_label(b) for b in became or []) or None


def became_evidence(b, items=None, actions=None, versions=None, since=None):
    """One line on how a link is doing: a practice's results since the lesson was adopted,
    an action's status, a Strategy version's date. None for a TAPEs feature."""
    if b['kind'] == 'practice':
        p = PRACTICE_BY_ID.get(b['ref'])
        if not p:
            return "no longer a practice TAPEs measures."
        its = sorted([i for i in (items or {}).get(b['ref'], []) if i['ok'] is not None
                      and (not since or str(i['date'])[:10] >= since)], key=lambda i: str(i['date']))
        when = f"since {since}" if since else "so far"
        if not its:
            return f"no results {when}."
        ok = sum(1 for i in its if i['ok'])
        cur, _ = run_streaks([i['ok'] for i in its])
        exc = len(its) - ok
        return (f"{ok} of {len(its)} {p['unit']} {when}, {exc} exception{'s' if exc != 1 else ''}; "
                f"current run {cur}.")
    if b['kind'] == 'action':
        a = next((a for a in actions or [] if str(a['action_id']) == _action_ref(b['ref'])), None)
        if not a:
            return "not in the action register."
        label = ACTION_STATUS_LABELS.get(a['status'], (a['status'],))[0]
        missed = sum(1 for h in a.get('history') or [] if h.get('status') == 'not_done')
        return f"{label}" + (f", not done {missed}×" if missed else "") + "."
    if b['kind'] == 'strategy':
        v = next((v for v in versions or [] if v['version'] == b['ref']), None)
        return f"dated {v['date']}." if v else "not in Strategy.md's change log."
    return None
LESSON_PROMPT_START = '2026-10-23'   # closed trades from here on get a "Lesson?" prompt
# NYSE full-day closures, so a holiday doesn't break the daily-lesson streak.
NYSE_HOLIDAYS = ['2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25', '2026-06-19',
                 '2026-07-03', '2026-09-07', '2026-11-26', '2026-12-25',
                 '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31', '2027-06-18',
                 '2027-07-05', '2027-09-06', '2027-11-25', '2027-12-24']


def lesson_text(value):
    """A lesson's text, or None when it's empty or a placeholder like "None"."""
    text = (_scalar(value) or '').strip() if not isinstance(value, str) else value.strip()
    return None if text.lower().rstrip('.') in TRIVIAL_LESSONS else text


def trade_needs_lesson(row):
    """A closed trade (exited on or after LESSON_PROMPT_START) with neither lesson half written."""
    if row.get('status') != 'Closed' or str(row.get('exit_time') or '')[:10] < LESSON_PROMPT_START:
        return False
    return not (lesson_text(row.get('lesson_keep')) or lesson_text(row.get('lessons_learned')))


def closed_without_lesson_text(journal_rows, day):
    """The daily pre-fill's reminder for trades closed on `day` with no lesson yet, or None."""
    symbols = [r['symbol'] for r in journal_rows or []
               if str(r.get('exit_time') or '')[:10] == day and trade_needs_lesson(r)]
    if not symbols:
        return None
    return (f"Closed today without a lesson: {', '.join(symbols)} -- add what worked and what to change "
            f"(Trade Journal -> Edit).")


def render_trade_lessons(row):
    keep, change = lesson_text(row.get('lesson_keep')), lesson_text(row.get('lessons_learned'))
    if not keep and not change:
        return html.P("(not filled in yet -- Edit the trade to add what worked and what to change)",
                      className="text-muted")
    return html.Div([html.Div([html.B("Keep: "), keep or html.Span("—", className="text-muted")]),
                     html.Div([html.B("Change: "), change or html.Span("—", className="text-muted")])])


def improvement_rows(plans, entries, improvements):
    """Every "what to change" lesson, newest first, joined to its saved classification:
    [{key, source, ref, date, label, keep, change, category, status, became, note, adopted_on}]."""
    saved = {(i['source'], i['source_ref']): i for i in improvements or []}
    rows = []

    def add(source, ref, date, label, keep, change):
        s = saved.get((source, ref), {})
        rows.append({'key': f"{source}|{ref}", 'source': source, 'ref': ref, 'date': str(date or '')[:10],
                     'label': label, 'keep': keep, 'change': change, 'category': s.get('category'),
                     'status': s.get('status') or 'new', 'became': s.get('became') or [], 'note': s.get('note'),
                     'adopted_on': s.get('adopted_on')})
    for p in plans or []:
        change = lesson_text(p.get('lessons_learned'))
        if change:
            add('trade', p['plan_id'], p.get('planned_date') or p.get('created_at'), f"Trade {p['symbol']}",
                lesson_text(p.get('lesson_keep')), change)
    for e in entries or []:
        change = lesson_text(e.get('lesson'))
        if change:
            add('day', e['obs_date'], e['obs_date'], "Day", lesson_text(e.get('lesson_keep')), change)
    return sorted(rows, key=lambda r: r['date'], reverse=True)


def improvement_counts(rows):
    counts = {s: 0 for s, _ in IMPROVEMENT_STATUSES}
    for r in rows:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    counts['all'] = len(rows)
    return counts


def render_improvement_register(rows, view='all', options=None, items=None, actions=None, versions=None):
    shown = [r for r in rows if view in (None, 'all') or r['status'] == view]
    if not shown:
        return html.P("Nothing here yet.", className="text-muted")
    cat_opts = [{'label': '— type —', 'value': 'unset'}] + [{'label': l, 'value': v} for v, l in IMPROVEMENT_CATEGORIES]
    status_opts = [{'label': l, 'value': v} for v, l in IMPROVEMENT_STATUSES]
    out = []
    for r in shown:
        idx = r['key']
        picked, tapes = became_values(r['became'])
        evidence = [html.Div([html.Span(became_label(b) + ": ", className="fw-semibold"),
                              became_evidence(b, items, actions, versions, r['adopted_on'])],
                             className="small text-muted")
                    for b in r['became'] if b['kind'] != 'tapes']
        out.append(dbc.Card(dbc.CardBody([
            dbc.Row([
                dbc.Col([html.Small(f"{r['date']} · {r['label']}", className="text-muted d-block"),
                         html.Div([html.B("Change: ", className="small"), html.Span(r['change'], className="small")]),
                         html.Div([html.B("Keep: ", className="small"), html.Span(r['keep'], className="small")],
                                  className="text-muted") if r['keep'] else None], md=5),
                dbc.Col([dbc.Select(id={'type': 'imp-cat', 'index': idx}, options=cat_opts, size='sm',
                                    value=r['category'] or 'unset'),
                         dbc.Select(id={'type': 'imp-status', 'index': idx}, options=status_opts, size='sm',
                                    value=r['status'], className="mt-1")], md=2),
                dbc.Col([dcc.Dropdown(id={'type': 'imp-became', 'index': idx}, options=options or [],
                                      value=picked, multi=True, placeholder="Became… (practice, Strategy version, "
                                      "focus action)", className="small"),
                         dbc.InputGroup([dbc.InputGroupText("TAPEs"),
                                         dbc.Input(id={'type': 'imp-tapes', 'index': idx}, value=tapes,
                                                   placeholder="feature(s), separated by ;")],
                                        size='sm', className="mt-1"),
                         dbc.Input(id={'type': 'imp-note', 'index': idx}, size='sm', value=r['note'] or '',
                                   placeholder="Note", className="mt-1"),
                         html.Div(evidence, className="mt-1") if evidence else None,
                         html.Small(f"Adopted {r['adopted_on']}", className="text-muted")
                         if r['adopted_on'] and r['status'] in ('adopted', 'verified') else None], md=5),
            ], className="g-2"),
        ]), className="mb-2",
            style={'borderLeft': f"4px solid var(--bs-{IMPROVEMENT_STATUS_COLORS[r['status']]})"}))
    return out


def run_streaks(flags):
    """(current, best) runs of True in an ordered list of True/False."""
    best = cur = 0
    for f in flags:
        cur = cur + 1 if f else 0
        best = max(best, cur)
    return cur, best


def practice_streaks(items):
    """{practice_id: (current, best)} over each practice's known results in date order."""
    return {pid: run_streaks([i['ok'] for i in sorted(its, key=lambda i: i['date']) if i['ok'] is not None])
            for pid, its in items.items()}


def lesson_streak(entries, today=None):
    """(current, best) runs of consecutive trading days whose daily entry has a lesson (keep or
    change). Today without an entry yet doesn't break the current run -- it's usually written
    after the close."""
    today = dt.date.fromisoformat(today) if isinstance(today, str) else (today or dt.date.today())
    with_lesson = sorted({_as_date(e['obs_date']) for e in entries or []
                          if lesson_text(e.get('lesson_keep')) or lesson_text(e.get('lesson'))})
    if not with_lesson:
        return 0, 0
    best = cur = 1
    for a, b in zip(with_lesson, with_lesson[1:]):
        cur = cur + 1 if int(np.busday_count(a, b, holidays=NYSE_HOLIDAYS)) == 1 else 1
        best = max(best, cur)
    # Alive while no trading day was missed between the last lesson and today
    # (today itself is still open; weekends and holidays don't count).
    after_last = with_lesson[-1] + dt.timedelta(days=1)
    missed = int(np.busday_count(after_last, today, holidays=NYSE_HOLIDAYS)) if after_last < today else 0
    return (cur if missed == 0 else 0), best


def followed_plan_flags(journal_rows, items):
    """{plan_id: True/False} for closed trades whose buys were all scored by the practices: True
    when no practice exception and the stop wasn't breached -- a good trade whatever its result."""
    broke = {pid for its in items.values() for i in its if i['ok'] is False for pid in i['plan_ids']}
    scored = {pid for its in items.values() for i in its if i['ok'] is not None for pid in i['plan_ids']}
    return {r['plan_id']: r['plan_id'] not in broke and r.get('stop_outcome') != 'breached'
            for r in journal_rows or [] if r.get('status') == 'Closed' and r['plan_id'] in scored}


def encouragement_summary(journal_rows, items, entries, improvement_list, today=None):
    """The figures behind the encouragement strip and the weekly prompt."""
    today = dt.date.fromisoformat(today) if isinstance(today, str) else (today or dt.date.today())
    flags = followed_plan_flags(journal_rows, items)
    pnl = {r['plan_id']: _scalar(r.get('actual_pnl')) for r in journal_rows or []}
    followed = [pid for pid, ok in flags.items() if ok]
    month = today.isoformat()[:7]
    adopted = [i for i in improvement_list if i['status'] in ('adopted', 'verified')]
    cur, best = lesson_streak(entries, today)
    return {'lesson_streak': cur, 'lesson_streak_best': best,
            'followed_plan': len(followed), 'closed_scored': len(flags),
            'followed_plan_losses': sum(1 for pid in followed if (pnl.get(pid) or 0) < 0),
            'changes_adopted': sum(1 for i in adopted if i['status'] == 'adopted'),
            'changes_verified': sum(1 for i in adopted if i['status'] == 'verified'),
            'changes_this_month': sum(1 for i in adopted if str(i.get('adopted_on') or '')[:7] == month),
            'adopted_list': [{'date': i['date'], 'lesson': i['change'], 'became': became_text(i.get('became')),
                              'status': i['status']} for i in adopted],
            'practice_streaks': {PRACTICE_BY_ID.get(k, {}).get('name', k): {'current': c, 'best': b}
                                 for k, (c, b) in practice_streaks(items).items()}}


def render_encouragement(summary):
    s = summary
    bits = []
    if s['lesson_streak_best']:
        bits.append(html.Span([html.B(f"{s['lesson_streak']}"),
                               f" trading day{'s' if s['lesson_streak'] != 1 else ''} in a row with a daily lesson "
                               f"(best {s['lesson_streak_best']})"]))
    if s['closed_scored']:
        bits.append(html.Span([html.B(f"{s['followed_plan']} of {s['closed_scored']}"),
                               " closed trades followed the plan"
                               + (f" -- {s['followed_plan_losses']} of them losses, good trades anyway"
                                  if s['followed_plan_losses'] else "")]))
    changes = s['changes_adopted'] + s['changes_verified']
    bits.append(html.Span([html.B(f"{changes}"), f" change{'s' if changes != 1 else ''} from your own lessons"
                           + (f" ({s['changes_verified']} verified)" if s['changes_verified'] else "")
                           + (f", {s['changes_this_month']} this month" if s['changes_this_month'] else "")]))
    sep = html.Span(" · ", className="text-muted")
    joined = [b for pair in zip(bits, [sep] * len(bits)) for b in pair][:-1]
    return dbc.Alert(joined, color='success', className="py-2 mb-2 small")


def render_posture_banner(posture):
    if not posture:
        return dbc.Alert(
            f"No daily entry with a regime yet, so the risk checks use the Strategy's maximums "
            f"({PER_TRADE_RISK_CAP_PCT:g}% per trade, {PORTFOLIO_RISK_WARNING_PCT:g}% heat).",
            color='warning', className="py-1 mb-2 small")
    limits = posture['limits']
    limit_text = (f"{limits['risk']:g}% per trade · {limits['heat']:g}% heat · {limits['max_pos']:g}% max position"
                  if limits['heat'] is not None else "No new trades")
    day = dt.date.fromisoformat(posture['as_of'])
    source = f"{'weekly entry, week ending' if posture['source'] == 'weekly' else 'daily entry for'} {day:%a %b} {day.day}"
    if posture['stale']:
        when = f"⚠ From the {source}, {posture['sessions_old']} sessions old. Log a new entry or re-read the regime."
    elif posture['as_of'] == dt.date.today().isoformat():
        when = f"From the {source} (today)."
    else:
        when = f"From the {source}. It reflects that close, so re-read the regime before trading."
    return dbc.Alert([
        html.Div([html.B("Limits ", className="me-1"), _regime_chip(posture['limits_state']), limit_text]),
        html.Div([html.B("Tactics ", className="me-1"), _regime_chip(posture['tactics_state']),
                  posture['tactics_text']], className="mt-1"),
        html.Small(" ".join(t for t in (posture['note'], when) if t), className="d-block mt-1"),
    ], color='warning' if posture['stale'] else 'light', className="py-2 mb-2 border")


def check_watchlist(items, plans, openpos, cash_equiv, tactics_state):
    """Each watchlist line checked against plans and holdings: held or not
    (and in which account), a plan or not, a held symbol with no plan, the
    tactic allowed by the effective regime, the listing's currency vs the
    plan's, and news-sourced ideas. Returns [{symbol, level, notes}]."""
    allowed = REGIME_LIMITS[tactics_state]['tactics'] if tactics_state in REGIME_LIMITS else None
    cash_equiv = set(cash_equiv or [])
    out = []
    for it in items or []:
        sym = (it.get('symbol') or '').strip().upper()
        if not sym:
            continue
        exch, _, ticker = sym.rpartition(':')
        notes, level = [], 'ok'
        if not exch:
            notes.append("add the exchange (EXCH:TICKER)")
            level = 'warn'
        held = [p for p in (openpos or []) if p.get('symbol') == ticker]
        live_plans = [p for p in (plans or []) if p.get('symbol') == ticker
                      and p.get('status') in ('committed', 'draft')]
        if held:
            accts = sorted({str(_scalar(p.get('account_id')) or '?') for p in held})
            notes.append("held in " + ", ".join(accts))
            if not live_plans and ticker not in cash_equiv:
                notes.append("held without a plan")
                level = 'warn'
        if live_plans:
            notes.append("plan: " + ", ".join(sorted({p['status'] for p in live_plans})))
            if exch:
                want = 'CAD' if exch in CANADIAN_EXCHANGE_PREFIXES else 'USD'
                if any(_scalar(p.get('currency')) and p['currency'] != want for p in live_plans):
                    notes.append(f"plan currency isn't {want}: check the listing")
                    level = 'warn'
        if not held and not live_plans:
            notes.append("new idea")
        tactic = it.get('tactic')
        if tactic and allowed is not None:
            if tactic == 'Sniper' and 'sniper' not in allowed:
                notes.append(f"Sniper not allowed ({tactics_state})")
                level = 'warn'
            elif tactic == 'Pyramid' and not ({'pyramid', 'probe'} & set(allowed)):
                notes.append(f"no new trades ({tactics_state})")
                level = 'warn'
            elif tactic == 'Pyramid' and 'probe' in allowed:
                notes.append("probe only, no adds")
        if it.get('news'):
            notes.append("news-sourced: discipline check")
            if level == 'ok':
                level = 'warn'
        out.append({'symbol': sym, 'level': level, 'notes': notes})
    return out


def journal_date_gap(obs_date, created_at):
    """Days between the entry's date and when it was first written, when more
    than one day apart (an entry written up later, or ahead); else None."""
    if not obs_date or not created_at:
        return None
    gap = (pd.to_datetime(created_at).date() - _as_date(obs_date)).days
    return gap if abs(gap) > 1 else None


def journal_portfolio_prefill(openpos, staged, cash_equiv, plans, journal, snapshots, executions, accounts,
                              beta_store):
    """PORTFOLIO pre-fill: heat % (needs Refresh Live Prices), positions
    (account + symbol + currency, cash-equivalents excluded; a campaign's
    lots count once) and beta % (needs Compute Market Exposure)."""
    cash_eq = set(cash_equiv or [])
    keys = {(_scalar(p.get('account_id')), p['symbol'], _scalar(p.get('currency')))
            for p in (openpos or []) if p['symbol'] not in cash_eq}
    heat = None
    if staged and staged.get('priced_symbols'):
        pw = portfolio_with_cash(openpos, staged, snapshots, executions, accounts)
        value, fx = pw['value'], pw['fx']
        stops = _compute_ratcheted_stops_by_lot(openpos or [], plans, journal)
        total, _, _ = compute_portfolio_risk(openpos or [], stops, cash_equiv, staged['priced_symbols'], value, fx=fx,
                                             gap_by_lot=gap_allowance_by_lot(openpos, plans, journal))
        heat = round(total / value * 100, 2) if value else None
    beta = beta_store.get('total_exposure_pct') if beta_store else None
    return {'heat_pct': heat, 'positions': len(keys), 'beta_pct': round(beta, 1) if beta is not None else None}


def journal_drift_prefill(drift_results, reconciled):
    """DRIFT REPORT ACTIONS pre-fill from today's Drift Report: stops to raise,
    targets reached, campaign adds triggered, live breaches. A check mark
    means the lot was ticked Reconciled. '' if the report hasn't been run."""
    if not drift_results:
        return ''
    rec = set(reconciled or [])
    lines = []
    for r in drift_results:
        if r.get('error'):
            continue
        acts = []
        if r.get('stop_breached_live'):
            acts.append("STOP BREACHED")
        if r.get('stop_action') == 'raise' and r.get('should_be_stop') is not None:
            acts.append(f"stop → {float(r['should_be_stop']):.2f}")
        nxt = r.get('next_add') if r.get('plan_type') == 'scale_in' else None
        if nxt and r.get('next_add_reached'):
            acts.append(f"{nxt.get('label')} reached: raise stop, buy {float(nxt.get('qty') or 0):.4g}")
        elif r.get('next_target_reached'):
            acts.append("target reached")
        if acts:
            lines.append(f"{r['symbol']}: " + ", ".join(acts) + (" ✓" if r.get('lot_id') in rec else ""))
    return "; ".join(lines) or "none"


# Every field on the template form, in order: (component id, property).
DJ_INPUTS = ([('dj-composite', 'value'), ('dj-regime', 'value')]
             + [(f'dj-{key}', 'value') for key, _, _ in DJ_LAYERS]
             + [('dj-flags', 'value'), ('dj-weekly', 'value'), ('dj-d5', 'value'), ('dj-d20', 'value'),
                ('dj-flicker', 'value'), ('dj-fomo', 'value'), ('dj-fomo-extreme', 'value'),
                ('dj-heat', 'value'), ('dj-positions', 'value'), ('dj-beta', 'value'),
                ('dj-drift', 'value'), ('dj-execution', 'value')]
             + [(f'dj-w{i}-{f}', 'value') for i in range(1, DJ_WATCH_ROWS + 1)
                for f in ('sym', 'tactic', 'trigger', 'news')]
             + [(f"dj-{a['id']}", 'value') for a in PSYCH_AXES]
             + [('dj-lesson-keep', 'value'), ('dj-lesson', 'value'), ('dj-market', 'value')])


def _num_or_none(v):
    v = _scalar(v)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def dj_form_values(entry):
    """The form's values (DJ_INPUTS order) for a stored entry, or blank."""
    e = entry or {}
    vals = {'dj-composite': e.get('composite'), 'dj-regime': e.get('regime'),
            'dj-flags': list(e.get('flags') or []), 'dj-weekly': e.get('weekly_regime'),
            'dj-d5': e.get('delta5') if e.get('delta5_manual') else None,
            'dj-d20': e.get('delta20') if e.get('delta20_manual') else None,
            'dj-flicker': ('Y' if e.get('flicker') else 'N') if e.get('flicker_manual') else 'auto',
            'dj-fomo': e.get('fomo_panic'), 'dj-fomo-extreme': ['Y'] if e.get('fomo_extreme') else [],
            'dj-heat': e.get('heat_pct'), 'dj-positions': e.get('positions'), 'dj-beta': e.get('beta_pct'),
            'dj-drift': e.get('drift_actions'), 'dj-execution': e.get('execution'),
            'dj-lesson-keep': e.get('lesson_keep'), 'dj-lesson': e.get('lesson'), 'dj-market': e.get('market')}
    for key, _, _ in DJ_LAYERS:
        vals[f'dj-{key}'] = e.get(key)
    rows = list(e.get('watchlist') or []) + [{}] * DJ_WATCH_ROWS
    for i, w in enumerate(rows[:DJ_WATCH_ROWS], start=1):
        vals[f'dj-w{i}-sym'] = w.get('symbol')
        vals[f'dj-w{i}-tactic'] = w.get('tactic')
        vals[f'dj-w{i}-trigger'] = w.get('trigger')
        vals[f'dj-w{i}-news'] = ['Y'] if w.get('news') else []
    for a in PSYCH_AXES:
        v = e.get(a['key'])
        vals[f"dj-{a['id']}"] = int(v) if _scalar(v) is not None else 0
    return [vals[cid] for cid, _ in DJ_INPUTS]


def dj_watchlist_from_values(vals):
    v = dict(zip([cid for cid, _ in DJ_INPUTS], vals))
    rows = []
    for i in range(1, DJ_WATCH_ROWS + 1):
        sym = (v.get(f'dj-w{i}-sym') or '').strip().upper()
        if sym:
            rows.append({'symbol': sym, 'tactic': v.get(f'dj-w{i}-tactic') or None,
                         'trigger': (v.get(f'dj-w{i}-trigger') or '').strip() or None,
                         'news': bool(v.get(f'dj-w{i}-news'))})
    return rows


def dj_entry_from_values(obs_date, vals, history):
    """The entry to store from the form's values (DJ_INPUTS order), with the
    deltas, flicker and effective regime computed against `history`. Psych is
    stored only if a slider was moved (an untouched form isn't a reading)."""
    v = dict(zip([cid for cid, _ in DJ_INPUTS], vals))
    flicker_sel = v.get('dj-flicker') or 'auto'
    calc = compute_effective_regime(
        history, obs_date, _num_or_none(v['dj-composite']), v.get('dj-regime'), v.get('dj-flags'),
        v.get('dj-weekly'), _num_or_none(v.get('dj-d5')), _num_or_none(v.get('dj-d20')),
        None if flicker_sel == 'auto' else flicker_sel == 'Y')
    psych = [v.get(f"dj-{a['id']}") for a in PSYCH_AXES]
    moved = any(int(x or 0) != 0 for x in psych)
    text = lambda k: (v.get(k) or '').strip() or None  # noqa: E731
    entry = {'obs_date': obs_date, 'composite': _num_or_none(v['dj-composite']), 'regime': v.get('dj-regime') or None,
             'flags': list(v.get('dj-flags') or []), 'weekly_regime': v.get('dj-weekly') or None,
             'delta5': calc['delta5'], 'delta20': calc['delta20'],
             'delta5_manual': 1 if calc['delta5_src'] == 'manual' else 0,
             'delta20_manual': 1 if calc['delta20_src'] == 'manual' else 0,
             'flicker': None if calc['flicker'] is None else int(calc['flicker']),
             'flicker_manual': 1 if calc['flicker_src'] == 'manual' else 0,
             'effective_limits': calc.get('limits_state'), 'effective_tactics': calc.get('tactics_state'),
             'effective_note': " ".join(calc['reasons']) or None,
             'fomo_panic': text('dj-fomo'), 'fomo_extreme': 1 if v.get('dj-fomo-extreme') else 0,
             'heat_pct': _num_or_none(v.get('dj-heat')),
             'heat_ceiling': (calc['limits'] or {}).get('heat') if calc.get('limits') else None,
             'positions': int(_num_or_none(v.get('dj-positions'))) if _num_or_none(v.get('dj-positions')) is not None else None,
             'beta_pct': _num_or_none(v.get('dj-beta')),
             'drift_actions': text('dj-drift'), 'execution': text('dj-execution'),
             'watchlist': dj_watchlist_from_values(vals),
             'lesson_keep': text('dj-lesson-keep'), 'lesson': text('dj-lesson'), 'market': text('dj-market')}
    for key, _, _ in DJ_LAYERS:
        entry[key] = _num_or_none(v.get(f'dj-{key}'))
    for a, x in zip(PSYCH_AXES, psych):
        entry[a['key']] = int(x or 0) if moved else None
    return entry, calc


def journal_history(entries):
    return {e['obs_date']: e for e in (entries or [])}


def daily_journal_modal():
    """The template form (claude-project/Strategy.md order)."""
    regime_opts = [{'label': '—', 'value': ''}] + [{'label': r, 'value': r} for r in reversed(REGIME_ORDER)]
    num = dict(type='number', size='sm', step='any')

    def lab(t, tip=None):
        return dbc.Label([t, help_icon(tip)] if tip else t, size='sm', className="mb-0 small fw-semibold")

    watch_rows = [dbc.Row([
        dbc.Col(dbc.Input(id=f'dj-w{i}-sym', size='sm', placeholder="EXCH:TICKER"), width=3),
        dbc.Col(dbc.Select(id=f'dj-w{i}-tactic', size='sm',
                           options=[{'label': '—', 'value': ''}] + [{'label': l, 'value': v} for v, l in DJ_TACTICS]),
                width=3),
        dbc.Col(dbc.Input(id=f'dj-w{i}-trigger', size='sm', placeholder="trigger"), width=4),
        dbc.Col(dbc.Checklist(id=f'dj-w{i}-news', options=[{'label': 'News', 'value': 'Y'}], value=[],
                              inline=True, className="small"), width=2),
    ], className="mb-1 g-1") for i in range(1, DJ_WATCH_ROWS + 1)]

    section = lambda title: html.H6(title, className="mt-3 mb-1 text-uppercase small fw-bold text-secondary")  # noqa: E731
    body = [
        dbc.Row([
            dbc.Col([lab("Date"), dcc.DatePickerSingle(id='dj-date', display_format='YYYY-MM-DD',
                                                       date=dt.date.today().isoformat())], width='auto'),
            dbc.Col(html.Div(id='dj-loaded', className="small text-muted mt-4"), width=True),
        ]),
        section("Regime (daily close)"),
        dbc.Row([
            dbc.Col([lab("Composite", "Macro Regime v7.5 composite, out of 9.5."),
                     dbc.Input(id='dj-composite', **num)], width=2),
            dbc.Col([lab("Label"), dbc.Select(id='dj-regime', size='sm', options=regime_opts)], width=3),
            dbc.Col([lab("Flags"), dbc.Checklist(id='dj-flags', inline=True, className="small",
                                                 options=[{'label': k, 'value': k} for k, _ in REGIME_FLAGS],
                                                 value=[])], width=4),
            dbc.Col([lab("Weekly"), dbc.Select(id='dj-weekly', size='sm', options=regime_opts)], width=3),
        ], className="g-2"),
        dbc.Row([dbc.Col([lab(f"{code}", name), dbc.Input(id=f'dj-{key}', **num)], width=True)
                 for key, code, name in DJ_LAYERS], className="g-1 mt-1"),
        dbc.Row([
            dbc.Col([lab("Δ5", "Computed from the entry 5 sessions back. Type a value only if that day "
                               "wasn't logged."), dbc.Input(id='dj-d5', placeholder="auto", **num)], width=2),
            dbc.Col([lab("Δ20", "Computed from the entry 20 sessions back."),
                     dbc.Input(id='dj-d20', placeholder="auto", **num)], width=2),
            dbc.Col([lab("Flicker", "2+ regime changes in the last 10 sessions. Auto needs 5+ logged labels."),
                     dbc.Select(id='dj-flicker', size='sm', value='auto',
                                options=[{'label': 'Auto', 'value': 'auto'}, {'label': 'Yes', 'value': 'Y'},
                                         {'label': 'No', 'value': 'N'}])], width=2),
        ], className="g-2 mt-1"),
        html.Div(id='dj-calc', className="mt-2"),
        section("FOMO / Panic"),
        dbc.Row([dbc.Col(dbc.Input(id='dj-fomo', size='sm', placeholder="e.g. Panic 1.4 NYSE, FOMO 0.7"), width=9),
                 dbc.Col(dbc.Checklist(id='dj-fomo-extreme', options=[{'label': 'Extreme (≥ 2.8)', 'value': 'Y'}],
                                       value=[], className="small"), width=3)], className="g-2"),
        section("Portfolio"),
        dbc.Row([
            dbc.Col([lab("Heat %", "Pre-filled from Refresh Live Prices."), dbc.Input(id='dj-heat', **num)], width=3),
            dbc.Col([lab(f"Positions (of {MAX_OPEN_POSITIONS})"), dbc.Input(id='dj-positions', type='number',
                                                                         size='sm', step=1)], width=3),
            dbc.Col([lab("Beta %", "Pre-filled from Compute Market Exposure."), dbc.Input(id='dj-beta', **num)],
                    width=3),
        ], className="g-2"),
        section("Drift Report actions"),
        dbc.Textarea(id='dj-drift', rows=2, size='sm',
                     placeholder="Stops raised, targets caught up, adds done -- or 'none'"),
        section("Execution"),
        dbc.Textarea(id='dj-execution', rows=2, size='sm',
                     placeholder="Fills vs plan: partials, gaps, wrong qty -- in R"),
        section("Watchlist"),
        *watch_rows,
        html.Div(id='dj-watch-check', className="mt-1"),
        section("Psych"),
        psych_sliders_block('dj', label="Psychological state today (optional)"),
        section("Lessons (one line each; both go to the weekly review)"),
        dbc.Row([dbc.Col([dbc.Label("What worked (keep)", size='sm'),
                          dbc.Input(id='dj-lesson-keep', size='sm',
                                    placeholder="Something you did well today")], md=6),
                 dbc.Col([dbc.Label("What to change / proposal", size='sm'),
                          dbc.Input(id='dj-lesson', size='sm',
                                    placeholder="Something to do differently, or an idea to try")], md=6)]),
        section("Market (one line)"),
        dbc.Input(id='dj-market', size='sm'),
        html.Div(id='dj-status', className="mt-3"),
    ]
    return dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Daily Trading Journal entry")),
        dbc.ModalBody(body),
        dbc.ModalFooter([
            dbc.Button("Delete entry", id='btn-dj-delete', color='outline-danger', size='sm', className="me-auto"),
            dbc.Button("Close", id='btn-dj-close', color='secondary'),
            dbc.Button("Save entry", id='btn-dj-save', color='primary'),
        ]),
    ], id='dj-modal', size='xl', is_open=False, scrollable=True)


def _save_plan_screenshot(plan_id, filename, contents):
    """Decode a dcc.Upload data URI and save it as assets/plan_screenshots/{plan_id}.{ext},
    replacing any previously-attached screenshot for this plan (even one with a
    different extension) so a symbol/entry edit never leaves an orphaned image
    file behind. Returns the path to store on the plan, relative to assets/
    (suitable for app.get_asset_url)."""
    if not contents:
        return None
    _, b64data = contents.split(',', 1)
    ext = os.path.splitext(filename or '')[1] or '.png'
    _remove_plan_screenshot(plan_id)
    rel_path = f"plan_screenshots/{plan_id}{ext}"
    with open(os.path.join(SCREENSHOT_DIR, f"{plan_id}{ext}"), 'wb') as f:
        f.write(base64.b64decode(b64data))
    return rel_path


def _remove_plan_screenshot(plan_id):
    for old in glob.glob(os.path.join(SCREENSHOT_DIR, f'{plan_id}.*')):
        os.remove(old)


def _load_initial_state():
    executions_df = db.fetch_executions()
    plans = db.fetch_plans()
    round_trips_df, open_pos_df = build_round_trips(executions_df, plans)
    _reconcile_closed_lot_matches(open_pos_df, round_trips_df)
    plan_matches = db.fetch_plan_matches()
    plan_lot_matches = db.fetch_plan_lot_matches()
    cash_equiv_symbols = db.fetch_cash_equivalent_symbols()
    dismissed_rts = db.fetch_dismissed_round_trips()
    daily_observations = db.fetch_daily_observations()
    weekly_reviews = db.fetch_weekly_reviews()
    dismissed_holdings = db.fetch_dismissed_holdings_discrepancies()
    # Both checklists default their date picker to today (see the Daily
    # Checklist card and weekly-week-ending's own default) -- load whichever
    # state already exists for today so a same-day reload doesn't lose
    # checked boxes.
    today = dt.date.today().isoformat()
    daily_checklist_checked = db.fetch_daily_checklist_state(today)
    weekly_checklist_checked = db.fetch_weekly_checklist_state(today)
    # The Drift Report is always "today's" (it's checked against live price,
    # not a picked date), so this loads unconditionally against today -- no
    # date picker needed for it, unlike the checklists above.
    drift_reconciled_today = db.fetch_daily_drift_reconciled(today)

    executions_out = executions_df.copy()
    if not executions_out.empty:
        executions_out['timestamp'] = executions_out['timestamp'].astype(str)

    rt_out = round_trips_df.copy()
    if not rt_out.empty:
        rt_out['entry_time'] = rt_out['entry_time'].astype(str)
        rt_out['exit_time'] = rt_out['exit_time'].astype(str)

    op_out = open_pos_df.copy()
    if not op_out.empty:
        op_out['since'] = op_out['since'].astype(str)
        op_out['entry_time'] = op_out['entry_time'].astype(str)

    return (executions_out.to_dict('records'), rt_out.to_dict('records'),
            op_out.to_dict('records'), plans, plan_matches, plan_lot_matches, cash_equiv_symbols, dismissed_rts,
            daily_observations, weekly_reviews, dismissed_holdings,
            daily_checklist_checked, weekly_checklist_checked, drift_reconciled_today)


def labeled(label, component, width=4):
    return dbc.Col([dbc.Label(label), component], width=width)


def help_icon(help_text, placement='right'):
    """A small hover-tooltip info icon carrying instructional copy that would
    otherwise sit as an always-visible paragraph under a card header or
    label, cluttering the page. Each call gets a fresh random id -- safe,
    since it's purely a client-side hover target that no callback ever reads
    -- so this can be dropped into a CardHeader list, next to a Label, or
    anywhere else inline."""
    icon_id = f"help-icon-{uuid.uuid4().hex[:8]}"
    return html.Span([
        html.Span(" ⓘ", id=icon_id, className="text-muted", style={'cursor': 'help', 'fontSize': '0.85rem'}),
        dbc.Tooltip(help_text, target=icon_id, placement=placement),
    ])


def target_rows_form(prefix):
    """Build the 5 optional price-target rows (Price / Qty % / Move Stop To)
    used in both the Add Plan form and the Edit modal. `prefix` is 'plan'
    or 'edit' so the generated component ids don't collide. Each price field
    carries a live '% from entry' readout next to it (see _pct_from_entry_js
    callbacks) -- purely informational, recomputed whenever entry or the
    field itself changes, including after a manual override."""
    # Default R-multiple per target when the field is left untouched -- the
    # exact same spread _auto_apply_stop_targets has always used
    # (np.linspace(1, 6, MAX_TARGETS)), just now editable per-target instead
    # of hardcoded, so a plan that never touches these fields computes
    # identical prices to before this became configurable.
    default_rmults = np.linspace(1, 6, db.MAX_TARGETS)
    rows = [
        html.Div([
            html.Span("Price Targets (up to 5, optional)", className="fw-bold small"),
            help_icon("As each is hit, note where you'll move the protective stop. Qty % is the "
                      "share of your ORIGINAL position size to sell at that target, not the share of "
                      "whatever remains at that point -- a 50% / 50% / 100% remaining-based scale-out "
                      "(sell half, then half of what's left, then the rest) is entered here as "
                      "50 / 25 / 25. The Plan Score's weighted-average target price is only correct if "
                      "these are percentages of the same base and sum to ~100%. R× only matters when "
                      "Method is \"ATR-derived\" below -- it's the ATR multiple this target's price is "
                      "computed from (defaults to the standard 1R-6R spread; edit it to use a different "
                      "spacing for this specific plan). To drop a target, clear its R×."),
        ], className="mb-1"),
        dbc.Row([
            dbc.Col(html.Small("Target"), width=1), dbc.Col(html.Small("R×"), width=2),
            dbc.Col(html.Small("Price (% from entry)"), width=3),
            dbc.Col(html.Small("Qty % (of original)"), width=2),
            dbc.Col(html.Small("Move Stop To (% from entry)"), width=4),
        ], className="mb-1"),
    ]
    for i in range(1, db.MAX_TARGETS + 1):
        rows.append(dbc.Row([
            dbc.Col(html.Div(f"T{i}", className="pt-2"), width=1),
            dbc.Col(dbc.Input(id=f'{prefix}-t{i}-rmult', type='number', size='sm', step=0.1,
                               value=round(float(default_rmults[i - 1]), 2)), width=2),
            dbc.Col(dbc.InputGroup([
                dbc.Input(id=f'{prefix}-t{i}-price', type='number', size='sm'),
                dbc.InputGroupText("—", id=f'{prefix}-t{i}-price-pct'),
            ]), width=3),
            dbc.Col(dbc.Input(id=f'{prefix}-t{i}-qty', type='number', size='sm', min=0, max=100), width=2),
            dbc.Col(dbc.InputGroup([
                dbc.Input(id=f'{prefix}-t{i}-stop', type='number', size='sm'),
                dbc.InputGroupText("—", id=f'{prefix}-t{i}-stop-pct'),
            ]), width=4),
        ], className="mb-1"))
    return html.Div(rows, id=f'{prefix}-targets-section')


def campaign_block(prefix):
    """The scale-in campaign's add ladder (shown instead of the price targets
    when Plan type is "Scale-in campaign"). The probe is the Entry, Quantity
    and Initial Protective Stop above; each add has a trigger, a share count,
    and the stop that must be in place BEFORE buying it."""
    trail_opts = [{'label': 'From the probe', 'value': '0'}] + \
        [{'label': f'After add {k}', 'value': str(k)} for k in range(1, db.MAX_TARGETS + 1)]
    rows = [
        html.Div([
            html.Span("Add ladder (the probe is the entry, quantity and stop above)", className="fw-bold small"),
            help_icon("Each add is bought only once price reaches its trigger -- after the reversal "
                      "confirms, never to average down. Before each add, raise the stop on the WHOLE "
                      "position to that add's \"Stop before\" level. \"Fill defaults\" lays out equal "
                      "adds at 1R, 2R, 3R... beyond the entry with each stop at the previous level, which "
                      "keeps the campaign's risk at or below the probe's. The exit is the trailing stop "
                      "only (Stop method above)."),
        ], className="mb-1"),
        dbc.Row([
            dbc.Col([dbc.Label("Adds", size='sm'),
                     dbc.Select(id=f'{prefix}-adds-count', size='sm', value=DEFAULT_CAMPAIGN_ADDS,
                                options=[{'label': str(k), 'value': k} for k in range(1, db.MAX_TARGETS + 1)])],
                    width=2),
            dbc.Col([dbc.Label("Add size", size='sm'),
                     dbc.Select(id=f'{prefix}-adds-sizing', size='sm', value='equal',
                                options=[{'label': 'Equal', 'value': 'equal'},
                                         {'label': 'Scaled (coming later)', 'value': 'scaled', 'disabled': True}])],
                    width=2),
            dbc.Col([dbc.Label("Per add", size='sm'),
                     dbc.Input(id=f'{prefix}-add-qty', type='number', size='sm',
                               placeholder="= probe quantity")], width=2),
            dbc.Col([dbc.Label(["Trail starts", help_icon(
                        "When the trailing stop starts working. From the probe is fine for the ATR "
                        "trail -- early on it sits below your scheduled stop, so it only ever tightens. "
                        "The EMA(21) trail also waits until price closes above the EMA, so it can never "
                        "put the stop above the market at the start of a reversal.")], size='sm'),
                     dbc.Select(id=f'{prefix}-trail-start-add', size='sm', value='0', options=trail_opts)],
                    width=3),
            dbc.Col(dbc.Button("Fill defaults", id=f'{prefix}-fill-adds', size='sm', color='outline-primary',
                               className="mt-4"), width=3),
        ], className="mb-2 align-items-end"),
        dbc.Row([
            dbc.Col(html.Small("Add"), width=1), dbc.Col(html.Small("Trigger price (% from entry)"), width=4),
            dbc.Col(html.Small("Shares"), width=2),
            dbc.Col(html.Small("Stop before this add (% from entry)"), width=5),
        ], className="mb-1"),
    ]
    for i in range(1, db.MAX_TARGETS + 1):
        rows.append(dbc.Row([
            dbc.Col(html.Div(f"A{i}", className="pt-2"), width=1),
            dbc.Col(dbc.InputGroup([
                dbc.Input(id=f'{prefix}-a{i}-price', type='number', size='sm'),
                dbc.InputGroupText("—", id=f'{prefix}-a{i}-price-pct'),
            ]), width=4),
            dbc.Col(dbc.Input(id=f'{prefix}-a{i}-qty', type='number', size='sm', min=0), width=2),
            dbc.Col(dbc.InputGroup([
                dbc.Input(id=f'{prefix}-a{i}-stop', type='number', size='sm'),
                dbc.InputGroupText("—", id=f'{prefix}-a{i}-stop-pct'),
            ]), width=5),
        ], className="mb-1"))
    return html.Div(rows, id=f'{prefix}-campaign-section', style={'display': 'none'})


def plan_type_block(prefix):
    return html.Div([
        dbc.Label(["Plan type", help_icon(
            "Scale-out: one entry, then sell in stages at price targets. Scale-in campaign: a "
            "small probe position as a trend reverses, adding as it confirms, and exiting on a "
            "trailing stop only. Day trade: a news-catalyst trade opened and closed in one session "
            "(Strategy 4.5).")], className="fw-bold small mb-0 me-2"),
        dbc.RadioItems(id=f'{prefix}-plan-type', inline=True, value='scale_out', className="d-inline-block",
                       options=[{'label': lbl, 'value': v} for v, lbl in PLAN_TYPES]),
    ], className="mb-1")


def daytrade_block(prefix):
    """The day-trade fields (shown only for Plan type = Day trade)."""
    return html.Div([
        dbc.Row([
            dbc.Col([dbc.Label(["Catalyst", help_icon(
                "Required: what happened, when, and the source. It must be fresh -- since the previous "
                "close or within 24 hours. A tip, a post or \"it's moving\" is not a catalyst.")], size='sm'),
                dbc.Textarea(id=f'{prefix}-day-catalyst', rows=2, size='sm',
                             placeholder="What happened, when, and the source")], width=8),
            dbc.Col([dbc.Label("Setup", size='sm'),
                     dbc.Select(id=f'{prefix}-day-setup', size='sm', value='',
                                options=[{'label': '—', 'value': ''}] + [{'label': l, 'value': v}
                                                                         for v, l in DAY_SETUPS])], width=4),
        ], className="g-2"),
        html.Small(f"Strategy 4.5: 5m (or 15m) ATR · stop beyond the setup's level · T1 1R (50%, stop to "
                   f"entry), T2 2R · flat by {DAY_TRADE_TIME_STOP}, never carried overnight · half the "
                   f"per-trade risk · at most {DAY_TRADE_MAX_PER_DAY} a day · not on a symbol you hold.",
                   className="text-muted d-block mt-1"),
    ], id=f'{prefix}-daytrade-section', style={'display': 'none'}, className="mb-2 p-2 border rounded")


def atr_block(prefix):
    """ATR-based stop/target helper, shared by the Add Plan form and Edit modal.

    Fully automatic: entering/changing the symbol fetches ATR (live TradingView
    Desktop, falling back to a Yahoo Finance proxy -- see fetch_atr), and
    entry/ATR/multiplier changes recompute the stop and T1-T5 targets from it
    (1R-6R spread) -- as long as Method is 'ATR-derived'. Switching Method to
    'Manual override' freezes whatever is currently in the stop/target fields:
    the auto-recompute callback checks this as a State and no-ops when it's
    not 'atr', so a deliberate hand-edit (after flipping the radio) persists
    through further symbol/entry changes and through commit.

    On the Add Plan form only, the method choices (Stop ATR x, Method, Stop
    method, trail settings) are remembered between sessions as your defaults.
    The Edit modal's copies aren't -- they're filled from the plan being edited."""
    keep = PERSIST if prefix == 'plan' else {}
    return html.Div([
        html.P("ATR-Based Levels", className="fw-bold small mb-1"),
        dbc.Row([
            dbc.Col([
                dbc.Label(["Timeframe", help_icon(
                    "The bars ATR(14) is read from: 4-hour, daily or weekly (5m/15m for day trades "
                    "only). TradingView switches "
                    "to this timeframe for the read. A weekly ATR is typically about twice the "
                    "daily one, a 4H ATR about half, so stops and targets scale with the trade's "
                    "horizon. An ATR(14) trail uses this timeframe's ATR too; the EMA(21) trail "
                    "always uses daily bars.")], size='sm'),
                dbc.Select(id=f'{prefix}-atr-timeframe', size='sm', value=DEFAULT_ATR_TIMEFRAME,
                           options=[{'label': lbl, 'value': v} for v, lbl in INTRADAY_TIMEFRAMES + ATR_TIMEFRAMES],
                           **keep),
            ], width=2),
            dbc.Col([
                dbc.Label("ATR (14)", size='sm'),
                dbc.Input(id=f'{prefix}-atr', type='number', size='sm'),
            ], width=3),
            dbc.Col([
                dbc.Label("Stop ATR ×", size='sm'),
                dbc.Input(id=f'{prefix}-atr-mult', type='number', size='sm', value=1.5, step=0.1, **keep),
            ], width=3),
            dbc.Col([
                dbc.Label("Method", size='sm'),
                dbc.RadioItems(
                    id=f'{prefix}-target-method', inline=True,
                    options=[{'label': 'ATR-derived', 'value': 'atr'},
                             {'label': 'Manual override', 'value': 'manual_override'}],
                    value='atr', **keep
                ),
            ], width=4),
        ], className="mb-1 align-items-end"),
        html.Div(id=f'{prefix}-atr-status', className="text-muted small mb-2"),
        dbc.Row([
            dbc.Col([
                dbc.Label(["Stop method", help_icon(
                    "Ladder: the stop moves only to each target's \"Move Stop To\" value. "
                    "ATR(14) trail: the stop trails the highest daily high by the ATR multiple, "
                    "using the ATR of the plan's Timeframe (weekly ATR for a 1W plan). "
                    f"EMA(21) trail: the stop follows EMA(21) - {EMA_TRAIL_BUFFER_ATR} x ATR(14), "
                    "only while EMA(21) is above your entry. Either trail starts once the "
                    "\"Trail activates after\" target is hit; until then the ladder's Move Stop To "
                    "values apply. Both trails move the stop to at least your entry once T1 is "
                    "hit, never lower it, and are recomputed once a day from completed bars only -- "
                    "set the new level in Wealthsimple after the close, before the next session. "
                    "The Drift Report shows the level to set.")], size='sm'),
                dbc.RadioItems(id=f'{prefix}-stop-method', inline=True, value='ladder',
                               options=[{'label': lbl, 'value': key} for key, lbl in STOP_METHODS],
                               className="small", **keep),
            ], width=6),
            dbc.Col([
                dbc.Label("Trail activates after", size='sm'),
                dbc.Select(
                    id=f'{prefix}-trailing-activate-after', size='sm',
                    options=[{'label': f'T{i}', 'value': i} for i in range(1, db.MAX_TARGETS + 1)],
                    value=1, **keep,
                ),
            ], width=3),
            dbc.Col([
                dbc.Label("Trail ATR ×", size='sm'),
                dbc.Input(id=f'{prefix}-trailing-atr-mult', type='number', size='sm',
                           value=2.0, step=0.1, **keep),
            ], width=3),
        ], className="mb-1 align-items-end"),
    ])


def psych_sliders_block(prefix, label="Psychological State at Entry (optional)"):
    """Psychological-state sliders (PSYCH_AXES), shared by the Add Plan form,
    Edit modal, AND the Notes/Observations popup (which is used for exit and
    mid-trade readings too, not just entry -- callers there must override
    `label`, since "at Entry" would be actively wrong in that context).
    Numeric -2..+2 scales rather than tags so each reading is directly usable
    in scoring/correlation against Plan Score or R-multiple."""
    rows = [dbc.Label([label, help_icon(
        "Risk emotion and Urgency are best at the center -- either end is a distortion "
        "(Fearful/Overconfident, Complacent/FOMO). Focus is better the higher it goes: "
        "only the Fatigued side counts against you.")], size='sm', className="fw-bold small mb-1")]
    for a in PSYCH_AXES:
        # The words sit in their own row above the slider, not in its marks: a
        # mark is centred on its tick, so an end label like "Overconfident"
        # hangs half outside the slider and overran narrow forms/modals.
        # Three equal columns keep the middle word centred and let long ones wrap.
        scale = html.Div([
            html.Span(a['neg'], style={'flex': 1}),
            html.Span(a['mid'], style={'flex': 1}, className="text-center"),
            html.Span(a['pos'], style={'flex': 1}, className="text-end"),
        ], className="d-flex small text-muted px-2", style={'lineHeight': 1.2})
        rows.append(html.Div([
            dbc.Label(a['title'], size='sm', className="small fw-semibold mb-0"),
            scale,
            # No number box (Dash 4's default): it duplicates the slider and
            # narrows the track, so the words above stop lining up with the ticks.
            dcc.Slider(id=f"{prefix}-{a['id']}", min=-2, max=2, step=1, value=0,
                       marks={-2: '-2', -1: '-1', 0: '0', 1: '+1', 2: '+2'},
                       allow_direct_input=False),
        ], className="mb-2"))
    return html.Div(rows)


def trade_triggers_block(prefix):
    """Entry-only: why the trade was opened. Multi-select, since a revenge
    trade can also be a chased breakout."""
    return html.Div([
        dbc.Label(["Trade Trigger (select all that apply)", help_icon(
            "Why you actually took this trade. \"Planned setup\" is the baseline the "
            "others are compared against in the weekly coaching stats.")],
            size='sm', className="fw-bold small mb-1"),
        dbc.Checklist(id=f'{prefix}-trade-triggers',
                      options=[{'label': lbl, 'value': key} for key, lbl in TRADE_TRIGGERS],
                      value=[], inline=True, className="small"),
        html.Div(id=f'{prefix}-revenge-hint'),
    ])


def screenshot_block(prefix, show_remove=False):
    """Manual chart-screenshot attachment, shared by the Add Plan form and Edit
    modal. Deliberately manual (drag/drop or click-to-browse) rather than an
    automatic TradingView Desktop capture over CDP -- gets the actual value
    (chart context for later review) without a new dependency on that
    connection; automatic capture is a natural later upgrade, not a redesign,
    since fetch_atr_from_tradingview already proves the same CDP connection
    works from this app."""
    children = [
        dbc.Label("Chart Screenshot (optional)", size='sm', className="fw-bold small mb-1"),
        dcc.Upload(
            id=f'{prefix}-screenshot-upload',
            children=html.Div("Drop an image here or click to browse", className="text-muted small"),
            style={'borderWidth': '1px', 'borderStyle': 'dashed', 'borderRadius': '4px',
                   'textAlign': 'center', 'padding': '10px', 'cursor': 'pointer'},
            accept='image/*', multiple=False,
        ),
        html.Div(id=f'{prefix}-screenshot-preview'),
    ]
    if show_remove:
        children.append(dbc.Button("Remove Screenshot", id=f'{prefix}-btn-remove-screenshot', size='sm',
                                    color='outline-danger', className="mt-1"))
    return html.Div(children)


def register_screenshot_callbacks(prefix):
    """Live preview of whatever's in {prefix}-screenshot-upload -- the data URI
    dcc.Upload hands back works directly as an <img src>, so this needs no
    round-trip through the server just to show the user what they dropped."""

    @app.callback(
        Output(f'{prefix}-screenshot-preview', 'children', allow_duplicate=True),
        Input(f'{prefix}-screenshot-upload', 'contents'),
        State(f'{prefix}-screenshot-upload', 'filename'),
        prevent_initial_call=True,
    )
    def _preview(contents, filename, prefix=prefix):
        if not contents:
            raise PreventUpdate
        return html.Div([
            html.Img(src=contents, style={'maxWidth': '300px', 'maxHeight': '200px', 'display': 'block'}),
            html.Small(filename, className="text-muted"),
        ], className="mt-1")
    return _preview


plan_form = dbc.Form([
    dbc.Row([
        labeled("Symbol *", dbc.Input(id='plan-symbol', placeholder="e.g. ARKK", debounce=True), 3),
        labeled("Direction *", dbc.Select(
            id='plan-direction',
            options=[{'label': 'Long (BUY)', 'value': 'BUY'}, {'label': 'Short (SELL)', 'value': 'SELL'}],
            value='BUY'
        ), 3),
        dbc.Col([
            dbc.Label(["Currency *", help_icon(
                "Guessed from your open positions/execution history for this symbol when "
                "you enter it above -- edit it if the guess is wrong or the symbol's never "
                "been traded before. Every price/dollar figure for this plan (entry, stop, "
                "targets, max loss/reward) is denominated in this currency.")]),
            dbc.Select(
                id='plan-currency',
                options=[{'label': 'USD', 'value': 'USD'}, {'label': 'CAD', 'value': 'CAD'}],
                value='USD'
            ),
        ], width=3),
        labeled("Planned Date", dcc.DatePickerSingle(
            id='plan-date', date=dt.date.today(), display_format='YYYY-MM-DD'
        ), 3),
    ], className="mb-2"),
    dbc.Row([
        labeled("Entry Price *", dbc.Input(id='plan-entry', type='number', debounce=True), 6),
        dbc.Col([
            dbc.Label("Initial Protective Stop (optional)"),
            dbc.InputGroup([
                dbc.Input(id='plan-stop', type='number'),
                dbc.InputGroupText("—", id='plan-stop-pct'),
            ]),
        ], width=6),
    ], className="mb-2"),
    dbc.Row([
        labeled("Quantity *", dbc.Input(id='plan-qty', type='number'), 6),
        dbc.Col([
            dbc.Label(["Account *", help_icon(
                "Which account this trade is in. Defaults to the account picked in the "
                "header's Account view, or your most recently used account when viewing All. "
                "A plan only ever matches fills from its own account.")]),
            dbc.Select(id='plan-account', options=[]),
        ], width=6),
    ], className="mb-2"),
    dbc.Row([dbc.Col([html.Div(id='plan-account-balance')], width=12)], className="mb-2"),
    dbc.Row([dbc.Col([html.Div(id='plan-pretrade-risk')], width=12)], className="mb-2"),
    dbc.Row([dbc.Col([plan_type_block('plan'), daytrade_block('plan')], width=12)], className="mb-1"),
    dbc.Row([dbc.Col([atr_block('plan')], width=12)], className="mb-2"),
    dbc.Row([dbc.Col([target_rows_form('plan'), campaign_block('plan'),
                      html.Div(id='plan-risk-reward', className="mt-2")], width=12)], className="mb-2"),
    dbc.Row([
        labeled("Reason / Setup (optional)", dbc.Textarea(id='plan-reason', rows=3,
                 placeholder="Why this trade? What's the thesis / setup?"), 12),
    ], className="mb-2"),
    dbc.Row([
        labeled("Notes (optional)", dbc.Textarea(id='plan-notes', rows=2), 12),
    ], className="mb-2"),
    dbc.Row([dbc.Col([psych_sliders_block('plan')], width=12)], className="mb-2"),
    dbc.Row([dbc.Col([trade_triggers_block('plan')], width=12)], className="mb-2"),
    dbc.Row([dbc.Col([screenshot_block('plan')], width=12)], className="mb-2"),
    html.Div([
        dbc.ButtonGroup([
            dbc.Button("Save as Draft", id='btn-save-draft', color='secondary', outline=True),
            dbc.Button("Commit Plan", id='btn-add-plan', color='primary'),
        ]),
        help_icon("A draft can be freely edited; committing finalizes the plan for execution matching.",
                   placement='top'),
    ]),
    html.Div(id='plan-add-status', className="mt-2"),
])

def serve_layout():
    """Dash calls this fresh for every new browser session (rather than reusing
    a layout object built once at process start), so each session's stores load
    whatever is currently in the database -- not a snapshot frozen from whenever
    the server process happened to start. Without this, a browser reload (no
    process restart) would silently revert Open Positions/executions/plans to
    that startup-time snapshot, discarding anything imported or changed since."""
    (executions, roundtrips, openpos, plans, plan_matches, plan_lot_matches,
     cash_equiv_symbols, dismissed_rts, daily_observations, weekly_reviews, dismissed_holdings,
     daily_checklist_checked, weekly_checklist_checked, drift_reconciled_today) = _load_initial_state()
    checklists = {name: load_checklist(name) for name in CHECKLIST_NAMES}
    coach_setup = coaching_system_prompt()
    placed_orders_today = db.fetch_daily_placed_orders(dt.date.today().isoformat())
    account_list = db.fetch_accounts()
    # fluid=True: this app is data-dense (the journal table alone has 11
    # columns plus a per-row action group) and Bootstrap's default Container
    # caps out around 1140-1320px regardless of screen size, wasting real
    # width on anything wider -- fluid uses the actual browser width instead.
    return dbc.Container([
    dcc.Store(id='store-executions', storage_type='memory', data=executions),
    dcc.Store(id='store-roundtrips', storage_type='memory', data=roundtrips),
    dcc.Store(id='store-openpos', storage_type='memory', data=openpos),
    dcc.Store(id='store-plans', storage_type='memory', data=plans),
    dcc.Store(id='store-accounts', storage_type='memory', data=db.fetch_accounts()),
    dcc.Store(id='store-account-snapshots', storage_type='memory', data=db.fetch_latest_account_snapshots()),
    dcc.Store(id='store-journal', storage_type='memory', data=[]),
    dcc.Store(id='store-unmatched-rts', storage_type='memory', data=[]),
    dcc.Store(id='store-editing-plan-id', storage_type='memory'),
    dcc.Store(id='store-edit-loaded', storage_type='memory'),
    dcc.Store(id='store-pending-delete-plan', storage_type='memory'),
    dcc.Store(id='store-plan-matches', storage_type='memory', data=plan_matches),
    dcc.Store(id='store-plan-lot-matches', storage_type='memory', data=plan_lot_matches),
    dcc.Store(id='store-matching-lot-plan-id', storage_type='memory'),
    dcc.Store(id='store-matching-plan-id', storage_type='memory'),
    dcc.Store(id='store-cash-equiv-symbols', storage_type='memory', data=cash_equiv_symbols),
    dcc.Store(id='store-dismissed-rts', storage_type='memory', data=dismissed_rts),
    dcc.Store(id='store-observing-plan-id', storage_type='memory'),
    dcc.Store(id='store-observation-event-type', storage_type='memory'),
    dcc.Store(id='store-obs-version', storage_type='memory', data=0),
    dcc.Store(id='store-daily-observations', storage_type='memory', data=daily_observations),
    dcc.Store(id='store-editing-daily-obs-id', storage_type='memory'),
    dcc.Store(id='store-daily-journal', storage_type='memory', data=db.fetch_daily_journal_entries()),
    dcc.Store(id='store-dj-nonce', storage_type='memory', data=0),
    dcc.Store(id='store-wk-nonce', storage_type='memory', data=0),
    dcc.Store(id='store-wk-lessons', storage_type='memory'),
    dcc.Store(id='store-weekly-actions', storage_type='memory', data=db.fetch_weekly_actions()),
    dcc.Store(id='store-improvements', storage_type='memory', data=db.fetch_improvements()),
    dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Improvement register")),
        dbc.ModalBody([
            html.P("Every 'what to change' lesson from your trades and daily entries. Give each a type, and "
                   "follow it through: Adopted when it became something real, Verified once the change has "
                   "stuck, or Dropped. An adopted or verified lesson needs at least one 'Became' link: a "
                   "practice, a Strategy version or a focus action (pick any number), and/or a TAPEs feature "
                   "(typed). Practices and actions show how they've gone since the lesson was adopted.",
                   className="small text-muted"),
            dbc.RadioItems(id='improvement-view', value='new', inline=True, className="btn-group mb-2",
                           inputClassName="btn-check", labelClassName="btn btn-outline-secondary btn-sm",
                           labelCheckedClassName="active", options=[], **PERSIST),
            html.Div(id='improvement-body'),
        ]),
        dbc.ModalFooter([html.Div(id='improvement-status', className="me-auto small"),
                         dbc.Button("Save changes", id='btn-improvement-save', color='primary', size='sm')]),
    ], id='improvement-modal', size='xl', scrollable=True, is_open=False),
    dcc.Store(id='store-weekly-reviews', storage_type='memory', data=weekly_reviews),
    dcc.Store(id='store-weekly-balance-staged', storage_type='memory'),
    dcc.Store(id='store-editing-weekly-review-week', storage_type='memory'),
    dcc.Store(id='store-open-positions-prices', storage_type='memory'),
    dcc.Store(id='store-beta-exposure', storage_type='memory'),
    dcc.Store(id='store-plan-drift', storage_type='memory'),
    dcc.Store(id='store-drift-reconciled', storage_type='memory', data=drift_reconciled_today),
    dcc.Store(id='store-placed-orders', storage_type='memory', data=placed_orders_today),
    dcc.Store(id='store-stop-checks', storage_type='memory'),
    dcc.Store(id='store-stop-review-paste', storage_type='memory'),  # written by assets/stop_review_paste.js
    dcc.Store(id='store-edit-screenshot-removed', storage_type='memory', data=False),
    dcc.Store(id='store-dismissed-holdings', storage_type='memory', data=dismissed_holdings),
    dcc.Store(id='store-holdings-diff', storage_type='memory'),
    dcc.Store(id='store-daily-checklist', storage_type='memory', data=daily_checklist_checked),
    dcc.Store(id='store-weekly-checklist', storage_type='memory', data=weekly_checklist_checked),
    dcc.Store(id='store-checklists', storage_type='memory', data=checklists),
    dcc.Store(id='store-checklist-draft', storage_type='memory', data=None),
    dcc.Download(id='download-journal'),

    # ---- Edit-plan modal ----
    dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Edit Trade Plan")),
        dbc.ModalBody([
            dbc.Row([
                labeled("Symbol *", dbc.Input(id='edit-symbol', debounce=True), 3),
                labeled("Direction *", dbc.Select(
                    id='edit-direction',
                    options=[{'label': 'Long (BUY)', 'value': 'BUY'}, {'label': 'Short (SELL)', 'value': 'SELL'}],
                ), 3),
                labeled("Currency *", dbc.Select(
                    id='edit-currency',
                    options=[{'label': 'USD', 'value': 'USD'}, {'label': 'CAD', 'value': 'CAD'}],
                ), 3),
                labeled("Planned Date", dcc.DatePickerSingle(id='edit-date', display_format='YYYY-MM-DD'), 3),
            ], className="mb-2"),
            dbc.Row([
                labeled("Entry Price *", dbc.Input(id='edit-entry', type='number', debounce=True), 6),
                dbc.Col([
                    dbc.Label("Initial Protective Stop"),
                    dbc.InputGroup([
                        dbc.Input(id='edit-stop', type='number'),
                        dbc.InputGroupText("—", id='edit-stop-pct'),
                    ]),
                ], width=6),
            ], className="mb-2"),
            dbc.Row([labeled("Quantity *", dbc.Input(id='edit-qty', type='number'), 6),
                     labeled("Account *", dbc.Select(id='edit-account', options=[]), 6)], className="mb-2"),
            dbc.Row([dbc.Col([plan_type_block('edit'), daytrade_block('edit')], width=12)], className="mb-1"),
            dbc.Row([dbc.Col([atr_block('edit')], width=12)], className="mb-2"),
            dbc.Row([dbc.Col([target_rows_form('edit'), campaign_block('edit'),
                              html.Div(id='edit-risk-reward', className="mt-2")], width=12)], className="mb-2"),
            dbc.Row([labeled("Reason / Setup", dbc.Textarea(id='edit-reason', rows=3), 12)], className="mb-2"),
            dbc.Row([labeled("Notes", dbc.Textarea(id='edit-notes', rows=2), 12)], className="mb-2"),
            dbc.Row([dbc.Col([psych_sliders_block('edit')], width=12)], className="mb-2"),
            dbc.Row([dbc.Col([trade_triggers_block('edit')], width=12)], className="mb-2"),
            dbc.Row([dbc.Col([screenshot_block('edit', show_remove=True)], width=12)], className="mb-2"),
            html.Div([html.B("Lessons", className="small"),
                      html.Small(" -- fill in once the trade is closed. Either box can stay empty; "
                                 "'nothing to change' is a fine answer for a trade that went to plan.",
                                 className="text-muted")], className="mb-1"),
            dbc.Row([labeled("What worked (keep)",
                             dbc.Textarea(id='edit-lesson-keep', rows=2,
                                          placeholder="Something you did well and want to keep doing"), 6),
                      labeled("What to change",
                              dbc.Textarea(id='edit-lessons-learned', rows=2,
                                           placeholder="Something you'd do differently next time"), 6)],
                    className="mb-2"),
            html.Div(id='edit-status'),
        ]),
        dbc.ModalFooter([
            dbc.Button("Cancel", id='btn-edit-cancel', color='secondary'),
            dbc.Button("Save Changes", id='btn-edit-save', color='primary'),
        ]),
    ], id='edit-modal', is_open=False, size='xl', scrollable=True),

    # ---- Manual plan<->round-trip match override modal ----
    dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Match Plan to Executed Round Trip")),
        dbc.ModalBody([
            html.P("The auto-matcher picks the nearest-in-time round trip for the same symbol and "
                   "direction. Pick a different one here to correct it, or clear it to fall back to "
                   "auto-matching.", className="text-muted small"),
            dbc.Select(id='match-rt-select', options=[], value=None),
            html.Div(id='match-status', className="mt-2"),
        ]),
        dbc.ModalFooter([
            dbc.Button("Cancel", id='btn-match-cancel', color='secondary'),
            dbc.Button("Clear Match", id='btn-match-clear', color='outline-danger'),
            dbc.Button("Save Match", id='btn-match-save', color='primary'),
        ]),
    ], id='match-modal', is_open=False),

    # ---- Plan observations modal (dated notes on a still-open plan) ----
    dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Plan Observations", id='observations-modal-title')),
        dbc.ModalBody([
            html.P("Optional dated notes on how this position is developing relative to the plan -- "
                   "not scored, just a running log.", className="text-muted small"),
            html.Div(id='observations-list'),
            html.Hr(),
            dbc.Textarea(id='observation-note', placeholder="What are you seeing today?", rows=2),
            html.Div([psych_sliders_block('observation', label="Psychological State for This Entry (optional)")],
                      className="mt-2"),
            dbc.Button("Add Observation", id='btn-add-observation', color='primary', size='sm',
                       className="mt-2"),
            html.Div(id='observation-status', className="mt-2"),
        ]),
        dbc.ModalFooter([
            dbc.Button("Close", id='btn-observations-close', color='secondary'),
        ]),
    ], id='observations-modal', size='lg', is_open=False),

    # ---- Edit daily-observation modal ----
    dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Edit Daily Journal Entry")),
        dbc.ModalBody([
            dbc.Label("Date"),
            dcc.DatePickerSingle(id='daily-obs-edit-date', display_format='YYYY-MM-DD'),
            dbc.Label("Observation", className="mt-2"),
            dbc.Textarea(id='daily-obs-edit-note', rows=3),
            html.Div(id='daily-obs-edit-status', className="mt-2"),
        ]),
        dbc.ModalFooter([
            dbc.Button("Cancel", id='btn-daily-obs-edit-cancel', color='secondary'),
            dbc.Button("Save Changes", id='btn-daily-obs-edit-save', color='primary'),
        ]),
    ], id='daily-obs-edit-modal', is_open=False),
    daily_journal_modal(),
    weekly_entry_modal(),
    action_register_modal(),

    # ---- Weekly review view/edit modal ----
    dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Weekly Review Entry")),
        dbc.ModalBody([
            dbc.Label("Week Ending"),
            html.Div(id='weekly-review-edit-week-display', className="fw-bold mb-2"),
            dbc.Label("Balance"),
            dbc.Input(id='weekly-review-edit-balance', type='number'),
            dbc.Label("Your Notes", className="mt-2"),
            dbc.Textarea(id='weekly-review-edit-user-note', rows=4),
            dbc.Label("Coaching Note", className="mt-2"),
            dbc.Textarea(id='weekly-review-edit-llm-note', rows=6),
            html.Div(id='weekly-review-edit-status', className="mt-2"),
        ]),
        dbc.ModalFooter([
            dbc.Button("Cancel", id='btn-weekly-review-edit-cancel', color='secondary'),
            dbc.Button("Save Changes", id='btn-weekly-review-edit-save', color='primary'),
        ]),
    ], id='weekly-review-edit-modal', is_open=False, size='lg'),

    # ---- Daily Plan-vs-Actual Drift Report modal -- 'xl' + scrollable for
    # more viewport room than the card it's triggered from could offer,
    # since this is meant to be scanned across several open lots at once.
    dbc.Modal([
        dbc.ModalHeader([dbc.ModalTitle("Daily Plan-vs-Actual Drift Report"),
                          html.Span(id='drift-modal-progress', className="ms-3 text-muted")]),
        dbc.ModalBody([
            html.Div(id='plan-drift-report'),
            html.Hr(),
            html.H6(["Stop review: your Wealthsimple stops", help_icon(
                "Take a screenshot of Wealthsimple's Pending orders list and press Ctrl+V while this "
                "report is open (or drop the image on the box). TAPEs reads the orders with Windows' built-in text "
                "recognition, on this computer. Check the table, fix anything misread, then Check stops. "
                "The orders are saved for today and matching lots are ticked Reconciled.")]),
            dcc.Upload(id='stop-review-upload', accept='image/*', multiple=True, children=html.Div(
                ["Press ", html.B("Ctrl+V"), " to paste a screenshot of your pending orders, or drop or "
                 "choose an image here. Paste several if the list is long."]),
                style={'border': '2px dashed #adb5bd', 'borderRadius': '6px', 'padding': '14px',
                       'textAlign': 'center', 'cursor': 'pointer'},
                className="small text-muted mb-2"),
            html.Div(id='stop-review-status', className="small mb-2"),
            dash_table.DataTable(
                id='stop-review-table',
                columns=[
                    {'name': 'Account', 'id': 'account_id', 'presentation': 'dropdown'},
                    {'name': 'Symbol', 'id': 'symbol'},
                    {'name': 'Currency', 'id': 'currency', 'presentation': 'dropdown'},
                    {'name': 'Shares', 'id': 'qty', 'type': 'numeric'},
                    {'name': 'Stop', 'id': 'stop', 'type': 'numeric'},
                    {'name': 'Limit', 'id': 'limit', 'type': 'numeric'},
                    {'name': 'Order type', 'id': 'order_type'},
                    {'name': 'Shown price', 'id': 'shown_price', 'type': 'numeric'},
                ],
                data=placed_orders_today, editable=True, row_deletable=True,
                dropdown={'account_id': {'options': [
                              {'label': a.get('nickname') or a.get('account_type') or a['account_id'],
                               'value': a['account_id']} for a in account_list]},
                          'currency': {'options': [{'label': c, 'value': c} for c in ('USD', 'CAD')]}},
                style_cell={'textAlign': 'left', 'padding': '4px'}, style_header={'fontWeight': 'bold'},
                css=[{'selector': '.Select-menu-outer', 'rule': 'display: block !important'}],
            ),
            html.Div([
                dbc.Button("Add row", id='btn-stop-review-add', color='link', size='sm'),
                dbc.Button("Clear", id='btn-stop-review-clear', color='link', size='sm'),
                dbc.Button("Check stops", id='btn-stop-review-check', color='primary', size='sm',
                           className="ms-2"),
            ], className="mt-1 mb-2"),
            html.Div(id='stop-review-result'),
        ]),
        dbc.ModalFooter([
            dbc.Button("Close", id='btn-drift-modal-close', color='secondary'),
        ]),
    ], id='drift-modal', is_open=False, size='xl', scrollable=True),

    dcc.ConfirmDialog(id='confirm-delete-plan'),
    dcc.ConfirmDialog(id='confirm-clear-plans',
                       message="This will permanently delete ALL trade plans from the database. This cannot be undone. Continue?"),
    dcc.ConfirmDialog(id='confirm-clear-executions',
                       message="This will permanently delete ALL imported executions (fills) and the round-trip "
                               "trades derived from them. Trade plans are kept. This cannot be undone. Continue?"),

    dbc.Row(
        [dbc.Col([
            html.Img(src=app.get_asset_url('NearNorthCleanRight.png'), height="90px", className="float-start")
        ], width=1),
        dbc.Col([
        html.H1("TAPEs", className="mt-3 mb-0"),
        html.P("Trading Analysis, Performance and Evaluation System — “reading the tape.”",
               className="text-muted fst-italic mb-1"),
        html.P("Log why you took a trade and what your plan was, import your actual fills, "
               "and see how well the plan was executed.", className="text-muted"),
    ], width=6),
        dbc.Col([
            dbc.Label(["Account view", help_icon(
                "All accounts combined (the default), or drill down to one. Filters Open "
                "Positions, the Drift Report, the Trade Journal, Executed Trades Without a Plan, "
                "Draft Plans, Summary Stats and Holdings Reconciliation. Portfolio risk caps "
                "always measure the whole portfolio; per-account risk is shown for information.")],
                size='sm', className="mt-3 mb-0"),
            dcc.Dropdown(id='account-view', value=ALL_ACCOUNTS, clearable=False, searchable=False,
                         options=[{'label': 'All accounts', 'value': ALL_ACCOUNTS}], **PERSIST),
        ], width=3),
        dbc.Col([
            dbc.Button("Help", id='btn-open-help', color='secondary', outline=True,
                       className="float-end mt-3"),
            dbc.Button("Data", id='btn-open-data-management', color='secondary', outline=True,
                       className="float-end mt-3 me-2",
                       title="Data Management: accounts, tracking start dates, bulk deletes"),
        ], width=2),
    ]),

    # Static reference content (tool-fit map + hard risk-framework limits) --
    # see HELP_PANE_MARKDOWN for why this deliberately excludes any
    # point-in-time figure. An Offcanvas (not a Modal) so it stays open
    # alongside the page while working, rather than blocking it.
    dbc.Offcanvas(
        dcc.Markdown(HELP_PANE_MARKDOWN),
        id='help-offcanvas', title="Help — Strategy, Tools & Risk", is_open=False,
        placement='end', style={'width': '600px'},
    ),

    # Data Management lives in a modal (opened from the header's "Data" button)
    # rather than the page, to cut clutter. Component ids are unchanged, so every
    # callback that reads/writes them works as before.
    dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Data Management")),
        dbc.ModalBody([
            html.Div(id='db-counts', className="text-muted small mb-2"),
            html.P(f"Data is stored in {db.DB_PATH}", className="text-muted small"),
            dbc.Button("Delete All Trade Plans", id='btn-clear-plans', color='outline-danger',
                       size='sm', className="me-2"),
            dbc.Button("Delete All Imported Executions & Trades", id='btn-clear-executions',
                       color='outline-danger', size='sm'),
            html.Div(id='clear-status', className="mt-2"),
            html.Hr(),
            html.B(["Accounts", help_icon(
                "Accounts are added automatically the first time an activity export "
                "includes them. The nickname is what the Account view and badges show. "
                "Tracking start: trades WITHOUT a plan that closed before this date are "
                "archived -- hidden everywhere, but kept in the database (and never counted "
                "in performance stats anyway). Planned trades always count. Clear the date "
                "to bring them back.")]),
            html.Div(id='accounts-list', className="mt-2"),
            html.Div(id='accounts-status', className="mt-1"),
            html.Hr(),
            html.B(["Cancelled plans", help_icon(
                "The reasons offered when you cancel a plan (one per line), and how many days a "
                "committed plan can sit with no fills before it's flagged Stale. Plans already "
                "cancelled keep the reason they were given, even if you rename or remove it here.")]),
            dbc.Row([
                dbc.Col([dbc.Label("Cancellation reasons (one per line)", size='sm'),
                         dbc.Textarea(id='cancel-reasons-text', rows=6,
                                      value="\n".join(load_cancel_reasons()))], width=8),
                dbc.Col([dbc.Label("Flag a plan stale after (days)", size='sm'),
                         dbc.Input(id='stale-plan-days', type='number', min=1, step=1,
                                   value=load_stale_plan_days()),
                         dbc.Button("Save", id='btn-save-cancel-settings', size='sm', color='primary',
                                    className="mt-2")], width=4),
            ], className="mt-1"),
            html.Div(id='cancel-settings-status', className="mt-1"),
            html.Hr(),
            html.B(["Stop gap allowance", help_icon(
                "Extra risk per share, in ATRs, added to the stop distance wherever TAPEs counts risk for "
                "planning: the plan's max loss and R:R, the pre-trade check, campaign peak risk and "
                "portfolio heat. It covers a stop that a gap opens through. Closed trades' R is unchanged, "
                "so a gap still shows as a loss beyond -1R. 0 turns it off.")]),
            dbc.Row([
                dbc.Col([dbc.Label("Allowance (× ATR)", size='sm'),
                         dbc.Input(id='stop-gap-atr', type='number', min=0, max=MAX_STOP_GAP_ATR, step=0.05,
                                   value=load_gap_allowance_atr())], width=4),
                dbc.Col(dbc.Button("Save", id='btn-save-gap-settings', size='sm', color='primary',
                                   className="mt-4"), width=2),
                dbc.Col(html.Div(id='gap-settings-status', className="mt-4 small"), width=6),
            ], className="mt-1"),
        ]),
    ], id='data-management-modal', size='lg', is_open=False),

    dcc.Store(id='store-cancel-plan-id'),
    dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Cancel plan", id='cancel-plan-title')),
        dbc.ModalBody([
            html.P("The plan is kept (status Cancelled), out of matching and scoring, and later replayed "
                   "to show what it would have done if kept.", className="text-muted small"),
            dbc.Label("Reason", size='sm'),
            dbc.Select(id='cancel-reason', options=[]),
            dbc.Label("Note (optional)", size='sm', className="mt-2"),
            dbc.Textarea(id='cancel-note', rows=2, placeholder="What changed?"),
            html.Div(psych_sliders_block('cancel', label="Psychological state now (optional)"),
                     className="mt-3"),
            html.Div(id='cancel-plan-status', className="mt-2"),
        ]),
        dbc.ModalFooter([
            dbc.Button("Cancel plan", id='btn-cancel-plan-save', color='warning'),
            dbc.Button("Keep it", id='btn-cancel-plan-close', color='secondary'),
        ]),
    ], id='cancel-plan-modal', is_open=False),

    # Checklist editor (2026-10-09): one modal for both lists; which one is
    # being edited, and the unsaved edits, live in store-checklist-draft.
    dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Edit checklist", id='checklist-editor-title')),
        dbc.ModalBody([
            html.P(["Edit any caption or help text in place. ↑/↓ move a row; a heading moves "
                    "together with everything under it. A ", html.B("Section"), " starts a new "
                    "collapsible group (daily list); a ", html.B("Sub-heading"), " sits inside one. "
                    "Nothing changes until you click Save. Ticks already saved for past days stay "
                    "with their items, even when you rename or move them."],
                   className="text-muted small"),
            html.Div(id='checklist-editor-rows'),
            html.Div([
                dbc.Button("+ Item", id='btn-cl-add-item', size='sm', color='outline-primary', className="me-2"),
                dbc.Button("+ Section", id='btn-cl-add-section', size='sm', color='outline-primary',
                           className="me-2"),
                dbc.Button("+ Sub-heading", id='btn-cl-add-sub', size='sm', color='outline-primary',
                           className="me-2"),
                dbc.Button("Reset to defaults", id='btn-cl-reset', size='sm', color='outline-danger',
                           className="float-end",
                           title="Replace this list with TAPEs's original version (not saved until you click Save)."),
            ], className="mt-2"),
            html.Div(id='checklist-editor-status', className="mt-2"),
        ]),
        dbc.ModalFooter([
            dbc.Button("Cancel", id='btn-cl-cancel', color='secondary'),
            dbc.Button("Save", id='btn-cl-save', color='primary'),
        ]),
    ], id='checklist-editor-modal', size='xl', is_open=False, scrollable=True),

    # Enlarged plan screenshot (2026-10-12), opened from the thumbnail in the
    # trade detail view.
    dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Chart screenshot", id='screenshot-modal-title')),
        dbc.ModalBody(id='screenshot-modal-body'),
    ], id='screenshot-modal', size='xl', is_open=False, centered=True),

    dbc.Row([dbc.Col([html.Div(id='data-freshness-banner', className="mb-2")])]),
    dbc.Row([dbc.Col([html.Div(id='posture-banner')])]),

    # Near the top on purpose: the first thing to read on opening the app.
    dbc.Row([dbc.Col(dbc.Card([
        dbc.CardHeader(dbc.Row([
            dbc.Col(html.Span("Summary Statistics", id='summary-stats-header'), className="pt-1"),
            dbc.Col(html.Div(dbc.Select(id='strategy-view', size='sm', value='all',
                                        options=[{'label': lbl, 'value': v} for v, lbl in STRATEGY_VIEWS],
                                        **PERSIST),
                             title="Show one strategy's trades only (also applies to Performance Over Time)."),
                    width='auto'),
        ], className="g-2 align-items-center")),
        dbc.CardBody([html.Div(id='summary-stats')]),
    ]))]),

    dbc.Row([dbc.Col(dbc.Accordion([
        dbc.AccordionItem(
            html.Div([
                html.Div(dbc.Button("Improvement register", id='btn-improvement-register', size='sm',
                                    color='success', outline=True,
                                    title="Every 'what to change' lesson from your trades and days, and "
                                          "what became of it."), className="mb-2 text-end"),
                html.Div(id='practices-body'),
            ]),
            title=["Practices", help_icon(
                "How you execute the Strategy, measured from your Wealthsimple fills for the current "
                "Account view. Reported, never enforced. Each practice counts from its start date.")],
            item_id='practices'),
    ], id='practices-accordion', active_item='practices', **PERSIST))], className="mt-3"),

    # Collapsed by default, and not remembered open, so its charts are never
    # built on page load -- only once you open it (see update_performance).
    dbc.Row([dbc.Col(dbc.Accordion([
        dbc.AccordionItem(
            html.Div(id='performance-body'),
            title=["Performance Over Time", help_icon(
                "Your closed planned trades in exit order, for the current Account view: "
                "cumulative R and drawdown, rolling expectancy and win rate, the spread of "
                "results, and process discipline by month. R puts USD and CAD trades on one "
                "scale; P&L appears per currency in the monthly table.")],
            item_id='performance'),
    ], id='performance-accordion', start_collapsed=True, active_item=None))], className="mt-3"),

    dbc.Row([dbc.Col([
        dbc.Accordion([
            dbc.AccordionItem([
                dbc.Row([
                    dbc.Col([dbc.Label("Date", size='sm'),
                             dcc.DatePickerSingle(id='daily-checklist-date', display_format='YYYY-MM-DD',
                                                   date=dt.date.today().isoformat()),
                             # A tab left open overnight would otherwise stay on the day it was
                             # loaded (2026-11-09: Check stops ticked today's item while the page
                             # still showed Saturday's list). See roll_daily_checklist_date.
                             dcc.Store(id='store-checklist-day', data=dt.date.today().isoformat()),
                             dcc.Interval(id='checklist-day-tick', interval=60_000)], width=3),
                    dbc.Col(html.Div(_checklist_progress_text(checklist_items(checklists['daily']),
                                                              daily_checklist_checked),
                                      id='daily-checklist-progress', className="mt-4 fw-bold"), width=7),
                    dbc.Col(dbc.Button("Edit list", id={'type': 'btn-edit-checklist', 'index': 'daily'},
                                       size='sm', color='outline-secondary', className="mt-4 float-end"),
                            width=2),
                ], className="mb-2"),
                html.Div([
                    dbc.Label(["To-do", help_icon(
                        "One-off items for the day, such as research or admin. They're separate from the "
                        "checklist's progress count. Unfinished items carry forward until you tick or drop them.")],
                        size='sm', className="fw-bold"),
                    dbc.InputGroup([
                        dbc.Input(id='daily-todo-input', placeholder="Add an item and press Enter", size='sm'),
                        dbc.Button("Add", id='btn-todo-add', color='outline-secondary', size='sm'),
                    ], size='sm', className="mb-2"),
                    html.Div(render_todos(db.fetch_todos(), dt.date.today().isoformat()), id='daily-todo-list'),
                ], className="mb-3"),
                html.Div(_render_daily_checklist_accordion(checklists['daily'], daily_checklist_checked),
                          id='daily-checklist-body'),
            ], title=["Daily Checklist",
                      help_icon("Your daily workflow, start to finish. Checked state is saved "
                                "per date, so a stale day's boxes don't carry over -- pick a "
                                "different date above to review or fill in a past day.")],
                item_id='daily-checklist'),

            dbc.AccordionItem([
                dbc.Row([
                    dbc.Col(dbc.Button("Today's entry", id='btn-dj-new', color='primary', size='sm'),
                            width='auto'),
                    dbc.Col(html.Small("One entry per day on the Strategy template. Older free-text notes "
                                       "are kept below as legacy notes.", className="text-muted"),
                            className="d-flex align-items-center"),
                ], className="mb-2"),
                html.Div(id='daily-obs-status'),
                dbc.Row([
                    dbc.Col(dbc.Select(id='daily-obs-period', size='sm', value='30',
                                       options=[{'label': lbl, 'value': v} for v, lbl in DAILY_OBS_PERIODS],
                                       **PERSIST), width=2),
                    dbc.Col(dbc.Input(id='daily-obs-search', size='sm', type='search', debounce=True,
                                      placeholder="Search notes..."), width=4),
                    dbc.Col(html.Small(id='daily-obs-count', className="text-muted"), width=6,
                            className="d-flex align-items-center"),
                ], className="mb-1 mt-2 g-2"),
                html.Div(id='daily-obs-list', style=SCROLL_PANEL),
                dbc.Pagination(id='daily-obs-page', max_value=1, active_page=1, fully_expanded=False,
                               size='sm', className="mt-2 mb-0"),
            ], title=["Daily Trading Journal",
                      help_icon("One structured entry per trading day, following the Strategy's "
                                "steps: regime, FOMO/Panic, portfolio, drift actions, execution, "
                                "watchlist, psych, a lesson and the market. Anything not tied to one "
                                "specific trade. For a note on how a single open position is "
                                "developing, use \"Observations\" on that position in Open "
                                "Positions below instead.")],
                item_id='daily-trading-journal'),

            dbc.AccordionItem([
                dcc.Upload(
                    id='upload-holdings-csv',
                    children=html.Div([html.H6("Drop holdings CSV here or click to browse"),
                                        html.P("Wealthsimple current-holdings export (positions snapshot, "
                                               "not the activity/fills export)", className="text-muted small")]),
                    style={'borderWidth': '2px', 'borderStyle': 'dashed', 'borderRadius': '6px',
                           'textAlign': 'center', 'padding': '16px', 'cursor': 'pointer'},
                    multiple=False
                ),
                html.Div(id='holdings-recon-status', className="mt-2"),
                html.Div(id='holdings-recon-list', className="mt-2"),
                html.Div([
                    html.Span("Dismissed (acknowledged intentional)", className="fw-bold small"),
                    help_icon("A discrepancy you've confirmed is expected (e.g. a symbol held in an "
                              "account TAPEs doesn't track) -- dismissing it here stops it from "
                              "re-flagging every week without changing any of TAPEs's own data."),
                ], className="mb-1 mt-2"),
                html.Div(id='dismissed-holdings-list'),
            ], title=["Weekly Reconciliation: Wealthsimple Holdings",
                      help_icon("Wealthsimple has no API, so TAPEs's picture of your positions is "
                                "only as current as the last reconciliation. Export a current "
                                "holdings CSV from Wealthsimple (not the activity/fills export used "
                                "above) and upload it here to cross-check it against TAPEs's own "
                                "open positions.")],
                item_id='weekly-reconciliation'),

            dbc.AccordionItem([
                html.Div([
                    html.Span("Weekly Checklist", className="fw-bold"),
                    html.Span(_checklist_progress_text(checklist_items(checklists['weekly']),
                                                       weekly_checklist_checked),
                              id='weekly-checklist-progress', className="ms-2 text-muted"),
                    dbc.Button("Edit list", id={'type': 'btn-edit-checklist', 'index': 'weekly'},
                               size='sm', color='outline-secondary', className="ms-3 py-0"),
                ], className="mb-1"),
                html.Div(_render_checklist_entries(checklists['weekly'], weekly_checklist_checked,
                                                   'weekly-check-item'),
                          id='weekly-checklist-body'),
                html.Hr(),
                dbc.Row([
                    dbc.Col([dbc.Label("Week Ending", size='sm'),
                             dcc.DatePickerSingle(id='weekly-week-ending', display_format='YYYY-MM-DD',
                                                   date=dt.date.today().isoformat())], width=2),
                    dbc.Col([
                        dbc.Label(["Balance", help_icon(
                            "Always computed from TODAY's prices, regardless of which week-ending "
                            "date is selected above -- there's no historical mark-to-market, so "
                            "catching up on a past week's note still uses current prices.")],
                            size='sm'),
                        html.Div([
                            dbc.Button("Compute Balance", id='btn-weekly-compute-balance',
                                       color='secondary', outline=True, size='sm'),
                        ]),
                    ], width=3),
                    dbc.Col(html.Div(id='weekly-balance-display', className="mt-4"), width=7),
                ], className="mb-2 align-items-start"),

                dbc.Row([
                    dbc.Col(dbc.Button("Weekly entry", id='btn-wk-new', color='primary', size='sm'), width='auto'),
                    dbc.Col(html.Small("The weekly template for the week ending above: regime and posture, book, "
                                       "results, last week's coaching actions, lessons, watchlist. Fill it in "
                                       "before generating the prompt.", className="text-muted"),
                            className="d-flex align-items-center"),
                ], className="mb-2"),
                html.Details([
                    html.Summary(f"Coaching chat setup (copy once, and again whenever Strategy.md changes; "
                                 f"includes Strategy.md v{strategy_rules_text()[0] or '?'})",
                                 className="text-muted small", style={'cursor': 'pointer'}),
                    html.Div([
                        dbc.Textarea(value=coach_setup, readOnly=True, rows=6,
                                     style={'fontFamily': 'monospace', 'fontSize': '0.8rem'}),
                        dcc.Clipboard(content=coach_setup, title="Copy system prompt",
                                      className="mt-1"),
                    ], className="mt-1"),
                ], className="mb-2"),

                dbc.Row([
                    dbc.Col([dbc.Label("Your Notes", size='sm'),
                             dbc.Textarea(id='weekly-user-note', rows=4,
                                          placeholder="What happened this week? How did you feel "
                                                       "about your trading?")],
                            width=6),
                    dbc.Col([
                        html.Div([
                            dbc.Label("This Week's Prompt", size='sm', className="me-2"),
                            dbc.Button("Generate Prompt", id='btn-weekly-generate-prompt',
                                       color='info', outline=True, size='sm'),
                        ]),
                        dbc.Textarea(id='weekly-prompt-text', readOnly=True, rows=6,
                                     style={'fontFamily': 'monospace', 'fontSize': '0.8rem'}),
                        dcc.Clipboard(id='weekly-prompt-clipboard', title="Copy this week's prompt",
                                      className="mt-1"),
                        html.Div(id='weekly-prompt-status', className="mt-1"),
                    ], width=6),
                ], className="mb-2"),

                dbc.Row([
                    dbc.Col([dbc.Label("Paste the Coach's Response", size='sm'),
                             dbc.Textarea(id='weekly-llm-note-paste', rows=4,
                                          placeholder="Paste what the dedicated coaching chat said "
                                                       "back here...")],
                            width=12),
                ], className="mb-2"),

                dbc.Button("Save Week", id='btn-weekly-save', color='primary'),
                dbc.Button("Action register", id='btn-action-register', color='secondary', outline=True,
                           className="ms-2"),
                html.Div(id='weekly-save-status', className="mt-2"),
                html.Hr(),
                html.Div(id='weekly-reviews-list'),
            ], title=["Weekly Review",
                      help_icon("Mark-to-market balance of open positions, your own notes, and "
                                "a coaching note from a dedicated LLM chat you keep running "
                                "outside TAPEs -- this card generates what to paste into it "
                                "each week (data + trend context), not an automatic API call.")],
                item_id='weekly-review'),
        ], always_open=True,
           active_item=['daily-checklist', 'daily-trading-journal', 'weekly-reconciliation', 'weekly-review'],
           id='accordion-block-1', **PERSIST),
    ])], className="mt-3"),

    dbc.Row([
        dbc.Col(dbc.Card([
            dbc.CardHeader("1. Add a Trade Plan"),
            dbc.CardBody([plan_form]),
        ]), width=6),
        dbc.Col([
            dbc.Card([
                dbc.CardHeader("2. Import Executions (CSV)"),
                dbc.CardBody([
                    dcc.Upload(
                        id='upload-csv',
                        children=html.Div([html.H5("Drop CSV here or click to browse"),
                                            html.P("Wealthsimple activity export, or a generic trade CSV",
                                                   className="text-muted small")]),
                        style={'borderWidth': '2px', 'borderStyle': 'dashed', 'borderRadius': '6px',
                               'textAlign': 'center', 'padding': '24px', 'cursor': 'pointer'},
                        multiple=False
                    ),
                    dbc.Row([
                        dbc.Col(dbc.Label(["Account for generic CSVs", help_icon(
                            "Wealthsimple activity exports carry their own account on every row, "
                            "so this only applies to a generic (non-Wealthsimple) trade CSV.")],
                            size='sm'), width=5),
                        dbc.Col(dbc.Select(id='import-account', size='sm', options=[]), width=7),
                    ], className="mt-2 align-items-center"),
                    html.Div(id='csv-upload-status', className="mt-2"),
                ])
            ]),
            dbc.Card([
                dbc.CardHeader(["Open Positions (not yet closed)",
                                 help_icon("A position you're currently holding without a plan is live, "
                                           "unmanaged risk -- plan these before working through closed "
                                           "trades below.")]),
                dbc.CardBody([
                    html.Div([
                        dbc.Button("Refresh Live Prices", id='btn-refresh-open-prices', size='sm',
                                   color='secondary', outline=True),
                        help_icon("Fetches a current price per open position (Yahoo Finance) to compute "
                                  "each position's live R:R and the portfolio's total risk below -- "
                                  "manual, not automatic, since fetching several symbols on every page "
                                  "load would be slow."),
                        html.Div(id='open-positions-price-status', className="small mt-1"),
                    ], className="mb-2"),
                    html.Div(id='portfolio-risk-banner', className="mb-2"),
                    html.Div([
                        dbc.Button("Compute Market Exposure", id='btn-compute-beta-exposure', size='sm',
                                   color='secondary', outline=True),
                        help_icon(f"Beta-weighted net market exposure: each position's market value "
                                  f"times its beta (covariance/variance of daily returns vs. "
                                  f"{BETA_BENCHMARK_SYMBOL} over the trailing {BETA_LOOKBACK_DAYS} "
                                  f"trading days), summed across the book. A different question from "
                                  f"the risk banner above -- that's how much you could lose if every "
                                  f"stop got hit; this is whether positions that look diversified by "
                                  f"ticker are actually one large correlated bet on the same market "
                                  f"direction (e.g. several leveraged or single-theme index/sector ETFs "
                                  f"that move almost identically in a risk-off move despite different "
                                  f"tickers). No target/ceiling is enforced here -- purely a "
                                  f"measurement. Requires \"Refresh Live Prices\" above first. Betas are "
                                  f"cached and reused for up to {BETA_CACHE_MAX_AGE_HOURS}h, not "
                                  f"refetched on every click."),
                        html.Div(id='beta-exposure-status', className="small mt-1"),
                    ], className="mb-2"),
                    html.Div(id='beta-exposure-banner', className="mb-2"),
                    html.Div(id='open-positions'),
                    html.Hr(),
                    html.Div([
                        html.Span("Cash-equivalent symbols", className="fw-bold small"),
                        help_icon("Parking positions (e.g. BIL) never need a plan -- add them here to "
                                  "drop the \"No plan\" prompt above. Has no effect on scoring either "
                                  "way: a symbol with no plan was already excluded from every "
                                  "performance metric, this just quiets the prompt."),
                    ], className="mb-1"),
                    dbc.InputGroup([
                        dbc.Input(id='cash-equiv-input', placeholder="e.g. BIL", size='sm'),
                        dbc.Button("Add", id='btn-add-cash-equiv', size='sm', color='outline-secondary'),
                    ], className="mb-2", style={'maxWidth': '300px'}),
                    html.Div(id='cash-equiv-list'),
                ])
            ], className="mt-3"),
            dbc.Card([
                dbc.CardHeader(["Daily Plan-vs-Actual Drift Report",
                                 help_icon("Wealthsimple has no API -- fills only reach TAPEs via manual "
                                           "CSV re-import -- so price can move past a target, or even "
                                           "through the stop, before the corresponding order is placed "
                                           "or filled and shows up here. This checks each open lot's "
                                           "plan against LIVE price (not just what's been executed so "
                                           "far) to catch that drift. Meant to be run each morning "
                                           "before market open. Requires \"Refresh Live Prices\" above "
                                           "first.")]),
                dbc.CardBody([
                    html.Div([
                        dbc.Button("Check Plan Drift", id='btn-check-plan-drift', size='sm',
                                   color='secondary', outline=True),
                        html.Div(id='plan-drift-status', className="small mt-1"),
                    ], className="mb-2"),
                ])
            ], className="mt-3"),
        ], width=6),
    ], className="mt-3"),

    dbc.Row([dbc.Col([
        dbc.Accordion([
            dbc.AccordionItem([
                html.Div(id='draft-plans-list')
            ], title=["Draft Trade Plans (not yet committed)",
                      help_icon("Drafts are excluded from the journal and execution matching "
                                "until committed.")],
                item_id='draft-trade-plans'),

            dbc.AccordionItem([
                dash_table.DataTable(
                    id='unmatched-rt-table',
                    columns=[
                        {'name': 'Account', 'id': 'account'},
                        {'name': 'Symbol', 'id': 'symbol'},
                        {'name': 'Entry', 'id': 'entry_price', 'type': 'numeric', 'format': {'specifier': '.2f'}},
                        {'name': 'Exit', 'id': 'exit_price', 'type': 'numeric', 'format': {'specifier': '.2f'}},
                        {'name': 'Qty', 'id': 'qty', 'type': 'numeric', 'format': {'specifier': '.4~g'}},
                        {'name': 'P&L', 'id': 'realized_pnl', 'type': 'numeric', 'format': {'specifier': '.2f'}},
                        {'name': 'Currency', 'id': 'currency'},
                        {'name': 'Exit Date', 'id': 'exit_time'},
                    ],
                    data=[],
                    row_selectable='single',
                    selected_rows=[],
                    sort_action='native',
                    filter_action='native',
                    page_size=10,
                    # Sort and column filters only: data/selected_rows are callback-driven.
                    persisted_props=['sort_by', 'filter_query'], **PERSIST,
                    style_table={'overflowX': 'auto'},
                    style_cell={'textAlign': 'left', 'padding': '6px'},
                    style_header={'fontWeight': 'bold'},
                    style_data_conditional=[
                        {'if': {'filter_query': '{realized_pnl} >= 0', 'column_id': 'realized_pnl'},
                         'color': '#198754'},
                        {'if': {'filter_query': '{realized_pnl} < 0', 'column_id': 'realized_pnl'},
                         'color': '#dc3545'},
                    ],
                ),
                dbc.Button("Log Plan for Selected Trade", id='btn-log-selected-plan', color='outline-primary',
                           size='sm', className="mt-2 me-2"),
                dbc.Button("Dismiss Selected Trade", id='btn-dismiss-selected-rt', color='outline-danger',
                           size='sm', className="mt-2",
                           title="Hide this trade from this list. It won't touch your imported "
                                 "executions and can be undone below."),
                html.Div(id='unmatched-rt-status', className="mt-2"),
                html.Div([
                    html.Small("Dismissed: ", className="text-muted"),
                    html.Div(id='dismissed-rt-list', style={'display': 'inline-block'}),
                ], className="mt-2"),
            ], title=["Executed Trades Without a Plan",
                      help_icon("These round-trip trades were found in your import but don't have "
                                "a matching plan yet. Select a row and click \"Log Plan for "
                                "Selected Trade\" to pre-fill the form above.")],
                item_id='executed-without-plan'),

            dbc.AccordionItem([
                dbc.Row([
                    dbc.Col([html.P("Filter by Symbol:"),
                             dcc.Dropdown(id='filter-symbol', options=[], value=None, placeholder="All symbols",
                                          **PERSIST)],
                            width=4),
                    dbc.Col([html.P("Filter by Score Range:"),
                             dcc.RangeSlider(id='filter-score', min=-100, max=150, step=10,
                                              marks={i: str(i) for i in range(-100, 151, 50)},
                                              value=[-100, 150], **PERSIST)],
                            width=8),
                ], className="mb-2"),
                dbc.Row([
                    dbc.Col(dbc.RadioItems(
                        id='filter-status', value='active',
                        options=[{'label': lbl, 'value': v} for v, lbl in JOURNAL_TABS],
                        className="btn-group", inputClassName="btn-check",
                        labelClassName="btn btn-outline-secondary btn-sm", labelCheckedClassName="active",
                        # Its own persistence key: the old checklist saved a
                        # list of statuses, this saves one tab.
                        persistence=f"{VIEW_PERSISTENCE_KEY}-status3", persistence_type='local'),
                        width=4, className="mt-3"),
                    dbc.Col([dbc.Label("Closed / cancelled in", size='sm', className="mb-0"),
                             dbc.Select(id='journal-period', size='sm', value='90',
                                        options=[{'label': lbl, 'value': v} for v, lbl in JOURNAL_PERIODS],
                                        **PERSIST)], width=2),
                    dbc.Col([dbc.Label("Sort", size='sm', className="mb-0"),
                             dbc.Select(id='journal-sort', size='sm', value='recent',
                                        options=[{'label': lbl, 'value': v} for v, lbl in JOURNAL_SORTS],
                                        **PERSIST)], width=3),
                    dbc.Col([dbc.Button("Check what would have happened", id='btn-refresh-cancelled', size='sm',
                                        color='secondary', outline=True, className="mt-3",
                                        title="Replay each cancelled plan against price since it went live "
                                              "(fetches daily history; results are saved)."),
                             html.Div(id='cancelled-refresh-status', className="small mt-1")], width=3),
                ], className="mb-2"),
                html.Small(id='journal-count', className="text-muted d-block mb-1"),
                html.Div(html.Div(id='journal-table'), style=SCROLL_PANEL),
                dbc.Pagination(id='journal-page', max_value=1, active_page=1, fully_expanded=False,
                               size='sm', className="mt-2 mb-0"),
            ], title="Trade Journal (Plan + Execution)", item_id='trade-journal'),
        ], always_open=True,
           active_item=['draft-trade-plans', 'executed-without-plan', 'trade-journal'],
           id='accordion-block-2', **PERSIST),
    ])], className="mt-3"),

    dbc.Row([
        dbc.Col(dbc.Card([dbc.CardHeader("Export"),
                           dbc.CardBody([dbc.Button("Download Journal as CSV", id='btn-export', color='secondary')])]),
                width=4),
    ], className="mt-3"),

    dbc.Row([
        dbc.Col(dbc.Card([dbc.CardHeader("Selected Trade Details"),
                           dbc.CardBody([html.Div(id='detail-markdown', style={'whiteSpace': 'pre-wrap'})])]),
                width=6),
        dbc.Col(dbc.Card([dbc.CardHeader("Price Path"),
                           dbc.CardBody([dcc.Graph(id='price-chart')])]),
                width=6),
    ], className="mt-3 mb-5"),

    ], fluid=True)


app.layout = serve_layout


# ==============================================================
# 5. Callbacks
# ==============================================================

@app.callback(
    [Output('store-executions', 'data'),
     Output('store-roundtrips', 'data'),
     Output('store-openpos', 'data'),
     Output('csv-upload-status', 'children'),
     Output('store-plan-matches', 'data', allow_duplicate=True),
     Output('store-plan-lot-matches', 'data', allow_duplicate=True),
     Output('store-accounts', 'data', allow_duplicate=True)],
    Input('upload-csv', 'contents'),
    State('import-account', 'value'),
    prevent_initial_call=True,
)
def import_csv(contents, import_account):
    """Parse the uploaded CSV, persist new fills to SQLite (deduplicated),
    then reload the full execution history from the DB (the source of truth)
    and recompute round trips from that."""
    if not contents:
        raise PreventUpdate

    def fail(alert):
        return no_update, no_update, no_update, alert, no_update, no_update, no_update

    fmt = detect_csv_format(contents)
    try:
        new_exec = parse_wealthsimple_csv(contents) if fmt == 'wealthsimple' else parse_generic_csv(contents)
    except ValueError as ve:
        return fail(dbc.Alert(f"Error processing CSV: {ve}", color='danger'))
    except Exception as e:
        return fail(dbc.Alert(f"Unexpected error: {e}", color='danger'))

    if new_exec.empty:
        return fail(dbc.Alert("No trade rows found in that CSV.", color='warning'))

    # Wealthsimple rows carry their own account; register any not seen before.
    # A generic CSV has none, so its rows go to the account picked beside the upload.
    for _, a in new_exec[['account_id', 'account_type']].drop_duplicates().iterrows():
        if a['account_id']:
            db.upsert_account(a['account_id'], a['account_type'] or None)
    if new_exec['account_id'].isna().any() and not import_account:
        return fail(dbc.Alert("This CSV has no account column -- pick the account it belongs to, "
                              "then upload again.", color='warning'))
    try:
        n_new = db.insert_executions(new_exec, account_id=import_account)
    except ValueError as ve:
        return fail(dbc.Alert(str(ve), color='danger'))
    combined = db.fetch_executions()
    round_trips, open_pos = build_round_trips(combined, db.fetch_plans())
    # A pyramided lot that just closed (this import's fills completed it)
    # may carry a manual plan_lot_matches override that needs migrating to
    # plan_matches now that it's a round trip -- see the function's own
    # docstring. Re-read both stores fresh afterward so the browser reflects
    # whatever this migration just changed in the database.
    _reconcile_closed_lot_matches(open_pos, round_trips)
    plan_matches_out = db.fetch_plan_matches()
    plan_lot_matches_out = db.fetch_plan_lot_matches()

    combined_out = combined.copy()
    if not combined_out.empty:
        combined_out['timestamp'] = combined_out['timestamp'].astype(str)
    rt_out = round_trips.copy()
    if not rt_out.empty:
        rt_out['entry_time'] = rt_out['entry_time'].astype(str)
        rt_out['exit_time'] = rt_out['exit_time'].astype(str)
    op_out = open_pos.copy()
    if not op_out.empty:
        op_out['since'] = op_out['since'].astype(str)
        op_out['entry_time'] = op_out['entry_time'].astype(str)

    n_rt = len(round_trips)
    n_dupe = len(new_exec) - n_new
    dupe_note = f" ({n_dupe} rows already in the database were skipped.)" if n_dupe else ""
    msg = dbc.Alert(
        f"✅ Parsed {len(new_exec)} fills ({fmt} format), {n_new} new saved to the database.{dupe_note} "
        f"Database now has {len(combined)} fills → {n_rt} completed round-trip trades "
        f"({len(open_pos)} positions still open).",
        color='success'
    )
    return (combined_out.to_dict('records'), rt_out.to_dict('records'), op_out.to_dict('records'), msg,
            plan_matches_out, plan_lot_matches_out, db.fetch_accounts())


@app.callback(
    Output('data-freshness-banner', 'children'),
    Input('store-executions', 'data'),
)
def render_data_freshness_banner_cb(executions_data):
    return _render_freshness_banner(executions_data)


@app.callback(
    Output('posture-banner', 'children'),
    [Input('store-daily-journal', 'data'), Input('store-weekly-reviews', 'data')],
)
def render_posture_banner_cb(daily_entries, weekly_rows):
    return render_posture_banner(current_posture(daily_entries, weekly_rows))


@app.callback(
    Output('help-offcanvas', 'is_open'),
    Input('btn-open-help', 'n_clicks'),
    State('help-offcanvas', 'is_open'),
    prevent_initial_call=True,
)
def toggle_help_offcanvas(n_clicks, is_open):
    if not n_clicks:
        raise PreventUpdate
    return not is_open


@app.callback(
    Output('data-management-modal', 'is_open'),
    Input('btn-open-data-management', 'n_clicks'),
    prevent_initial_call=True,
)
def open_data_management(n_clicks):
    # Closing is the modal's own × / backdrop click.
    if not n_clicks:
        raise PreventUpdate
    return True


@app.callback(
    Output('action-register-modal', 'is_open'),
    Input('btn-action-register', 'n_clicks'),
    prevent_initial_call=True,
)
def open_action_register(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return True


@app.callback(
    [Output('action-register-body', 'children'), Output('action-register-view', 'options')],
    [Input('store-weekly-actions', 'data'), Input('action-register-view', 'value')],
)
def render_action_register_cb(actions, view):
    counts = action_register_counts(actions)
    options = [{'label': f"{l} ({counts[v]})", 'value': v} for v, l in ACTION_REGISTER_VIEWS]
    return render_action_register(actions, view or 'current'), options


@app.callback(
    [Output('store-weekly-actions', 'data', allow_duplicate=True),
     Output('action-register-status', 'children')],
    Input('btn-action-register-save', 'n_clicks'),
    [State({'type': 'ar-status', 'index': ALL}, 'value'), State({'type': 'ar-status', 'index': ALL}, 'id'),
     State({'type': 'ar-note', 'index': ALL}, 'value'), State('store-weekly-actions', 'data')],
    prevent_initial_call=True,
)
def save_action_register_cb(n_clicks, statuses, ids, notes, actions):
    """Records each changed action as a review dated today (2026-11-07: the register is
    editable as well as the weekly entry)."""
    if not n_clicks:
        raise PreventUpdate
    changes, problem = action_register_changes(statuses, ids, notes, actions)
    if problem:
        return no_update, html.Span(problem, className="text-danger")
    if not changes:
        return no_update, html.Span("Nothing changed.", className="text-muted")
    today = dt.date.today().isoformat()
    for action_id, status, note in changes:
        db.review_weekly_action(action_id, today, status, note)
    done = ", ".join(f"A{c[0]} {dict(ACTION_STATUSES)[c[1]].lower()}" for c in changes)
    return db.fetch_weekly_actions(), html.Span(f"Saved: {done}.", className="text-success")


# ==============================================================
# Daily/weekly checklists -- see load_checklist / _render_daily_checklist_accordion
# and _render_checklist_rows/_checklist_progress_text above. Each checklist
# gets two callbacks: one that (re)renders the checkbox rows + progress text
# whenever the relevant date changes (daily-checklist-date, or the Weekly
# Review card's own weekly-week-ending), and one that saves to the DB
# whenever a checkbox is toggled. Both callbacks target the same progress
# text Output, hence allow_duplicate=True on the toggle callback's.
# ==============================================================

@app.callback(
    [Output('daily-checklist-body', 'children'), Output('daily-checklist-progress', 'children')],
    [Input('daily-checklist-date', 'date'), Input('store-checklists', 'data')],
    prevent_initial_call=True,
)
def render_daily_checklist_cb(date, checklists):
    if not date:
        raise PreventUpdate
    entries = (checklists or {}).get('daily') or load_checklist('daily')
    checked = db.fetch_daily_checklist_state(date)
    return (_render_daily_checklist_accordion(entries, checked),
            _checklist_progress_text(checklist_items(entries), checked))


def rolled_checklist_date(shown, page_day, today):
    """The date the daily checklist should show: `today` when it's still on the day the
    page was loaded (or that day has passed), else what the user picked. None = no change."""
    if today == page_day:
        return None
    return today if shown in (None, '', page_day) else None


@app.callback(
    [Output('daily-checklist-date', 'date', allow_duplicate=True), Output('store-checklist-day', 'data')],
    Input('checklist-day-tick', 'n_intervals'),
    [State('daily-checklist-date', 'date'), State('store-checklist-day', 'data')],
    prevent_initial_call=True,
)
def roll_daily_checklist_date(_n, shown, page_day):
    today = dt.date.today().isoformat()
    if today == page_day:
        raise PreventUpdate
    new = rolled_checklist_date(shown, page_day, today)
    return (new if new else no_update), today


@app.callback(
    Output('daily-checklist-progress', 'children', allow_duplicate=True),
    Input({'type': 'daily-check-item', 'index': ALL}, 'value'),
    [State({'type': 'daily-check-item', 'index': ALL}, 'id'), State('daily-checklist-date', 'date'),
     State('store-checklists', 'data')],
    prevent_initial_call=True,
)
def toggle_daily_check_cb(values, ids, date, checklists):
    if not date:
        raise PreventUpdate
    entries = (checklists or {}).get('daily') or load_checklist('daily')
    checked_ids = apply_auto_ticks(entries, [i['index'] for i, v in zip(ids, values) if v],
                                   db.fetch_daily_checklist_state(date))
    db.save_daily_checklist_state(date, checked_ids)
    return _checklist_progress_text(checklist_items(entries), checked_ids)


@app.callback(
    [Output('daily-todo-list', 'children'), Output('daily-todo-input', 'value')],
    [Input('daily-checklist-date', 'date'), Input('btn-todo-add', 'n_clicks'), Input('daily-todo-input', 'n_submit'),
     Input({'type': 'todo-check', 'index': ALL}, 'value'), Input({'type': 'todo-drop', 'index': ALL}, 'n_clicks')],
    [State('daily-todo-input', 'value'), State({'type': 'todo-check', 'index': ALL}, 'id')],
    prevent_initial_call=True,
)
def update_todos_cb(date, _add, _submit, check_values, _drops, text, check_ids):
    """Re-rendered checkboxes and buttons can fire this with nothing changed,
    so a tick only counts if it differs from what's saved, and a drop only on
    a real click."""
    if not date:
        raise PreventUpdate
    trigger = callback_context.triggered_id
    kind = trigger.get('type') if isinstance(trigger, dict) else trigger
    clear_input = no_update
    if kind in ('btn-todo-add', 'daily-todo-input'):
        if not (text or '').strip():
            raise PreventUpdate
        db.add_todo(text.strip(), date)
        clear_input = ''
    elif kind == 'todo-check':
        by_id = {t['id']: t for t in db.fetch_todos()}
        changes = [(i['index'], bool(v)) for i, v in zip(check_ids, check_values)
                   if i['index'] in by_id and bool(v) != (todo_state_on(by_id[i['index']], date) == 'done')]
        if not changes:
            raise PreventUpdate
        for todo_id, done in changes:
            db.set_todo_status(todo_id, 'done' if done else 'open', date if done else None)
    elif kind == 'todo-drop':
        if not callback_context.triggered[0]['value']:
            raise PreventUpdate
        todo = next((t for t in db.fetch_todos() if t['id'] == trigger['index']), None)
        if todo is None:
            raise PreventUpdate
        if todo_state_on(todo, date) == 'dropped':
            db.set_todo_status(todo['id'], 'open')
        else:
            db.set_todo_status(todo['id'], 'dropped', date)
    return render_todos(db.fetch_todos(), date), clear_input


@app.callback(
    [Output('weekly-checklist-body', 'children'), Output('weekly-checklist-progress', 'children')],
    [Input('weekly-week-ending', 'date'), Input('store-checklists', 'data')],
    prevent_initial_call=True,
)
def render_weekly_checklist_cb(week_ending, checklists):
    if not week_ending:
        raise PreventUpdate
    entries = (checklists or {}).get('weekly') or load_checklist('weekly')
    checked = db.fetch_weekly_checklist_state(week_ending)
    return (_render_checklist_entries(entries, checked, 'weekly-check-item'),
            _checklist_progress_text(checklist_items(entries), checked))


@app.callback(
    Output('weekly-checklist-progress', 'children', allow_duplicate=True),
    Input({'type': 'weekly-check-item', 'index': ALL}, 'value'),
    [State({'type': 'weekly-check-item', 'index': ALL}, 'id'), State('weekly-week-ending', 'date'),
     State('store-checklists', 'data')],
    prevent_initial_call=True,
)
def toggle_weekly_check_cb(values, ids, week_ending, checklists):
    if not week_ending:
        raise PreventUpdate
    checked_ids = [i['index'] for i, v in zip(ids, values) if v]
    db.save_weekly_checklist_state(week_ending, checked_ids)
    entries = (checklists or {}).get('weekly') or load_checklist('weekly')
    return _checklist_progress_text(checklist_items(entries), checked_ids)


# ---- Checklist editor ----

def _render_checklist_editor_rows(entries):
    """One editable row per entry: headings get a Section/Sub-heading picker
    and a shaded background; items are indented with caption + help fields.
    Every control's pattern id is keyed on the entry's stable id."""
    def controls(eid):
        return dbc.ButtonGroup([
            dbc.Button("↑", id={'type': 'cl-up', 'index': eid}, size='sm', color='outline-secondary',
                       title="Move up"),
            dbc.Button("↓", id={'type': 'cl-down', 'index': eid}, size='sm', color='outline-secondary',
                       title="Move down"),
            dbc.Button("×", id={'type': 'cl-del', 'index': eid}, size='sm', color='outline-danger',
                       title="Delete"),
        ])
    rows = []
    for e in entries:
        if e['kind'] == 'heading':
            rows.append(dbc.Row([
                dbc.Col(dbc.Select(id={'type': 'cl-level', 'index': e['id']}, size='sm', value=str(e['level']),
                                   options=[{'label': 'Section', 'value': '1'},
                                            {'label': 'Sub-heading', 'value': '2'}]), width=2),
                dbc.Col(dbc.Input(id={'type': 'cl-label', 'index': e['id']}, value=e.get('text', ''),
                                  size='sm', placeholder="Heading text", className="fw-bold"), width=8),
                dbc.Col(controls(e['id']), width=2, className="text-end"),
            ], className=("mb-1 py-1 align-items-center rounded bg-light"
                          + ("" if e['level'] == 1 else " ms-3"))))
        else:
            rows.append(dbc.Row([
                dbc.Col(dbc.Input(id={'type': 'cl-label', 'index': e['id']}, value=e.get('label', ''),
                                  size='sm', placeholder="Checklist item caption"), width=6,
                        className="ps-4"),
                dbc.Col(dbc.Input(id={'type': 'cl-help', 'index': e['id']}, value=e.get('help', ''),
                                  size='sm', placeholder="Help text (optional, shown on hover)"), width=4),
                dbc.Col(controls(e['id']), width=2, className="text-end"),
            ], className="mb-1 align-items-center"))
    if not rows:
        rows = [html.P("Empty -- add an item or a heading below.", className="text-muted small")]
    return rows


def _merge_editor_values(entries, label_vals, label_ids, help_vals, help_ids, level_vals, level_ids):
    """The draft entries with whatever is currently typed in the editor --
    applied before any move/add/delete re-renders the rows, so in-progress
    typing is never lost."""
    labels = {i['index']: v for i, v in zip(label_ids or [], label_vals or [])}
    helps = {i['index']: v for i, v in zip(help_ids or [], help_vals or [])}
    levels = {i['index']: v for i, v in zip(level_ids or [], level_vals or [])}
    out = []
    for e in entries or []:
        e = dict(e)
        if e['kind'] == 'heading':
            e['text'] = labels.get(e['id'], e.get('text', ''))
            e['level'] = int(levels.get(e['id'], e['level']) or 1)
        else:
            e['label'] = labels.get(e['id'], e.get('label', ''))
            e['help'] = helps.get(e['id'], e.get('help', '')) or ''
        out.append(e)
    return out


_EDITOR_VALUE_STATES = [
    State({'type': 'cl-label', 'index': ALL}, 'value'), State({'type': 'cl-label', 'index': ALL}, 'id'),
    State({'type': 'cl-help', 'index': ALL}, 'value'), State({'type': 'cl-help', 'index': ALL}, 'id'),
    State({'type': 'cl-level', 'index': ALL}, 'value'), State({'type': 'cl-level', 'index': ALL}, 'id'),
]


@app.callback(
    [Output('checklist-editor-modal', 'is_open'), Output('checklist-editor-title', 'children'),
     Output('store-checklist-draft', 'data'), Output('checklist-editor-rows', 'children'),
     Output('checklist-editor-status', 'children')],
    [Input({'type': 'btn-edit-checklist', 'index': ALL}, 'n_clicks'),
     Input({'type': 'cl-up', 'index': ALL}, 'n_clicks'), Input({'type': 'cl-down', 'index': ALL}, 'n_clicks'),
     Input({'type': 'cl-del', 'index': ALL}, 'n_clicks'),
     Input('btn-cl-add-item', 'n_clicks'), Input('btn-cl-add-section', 'n_clicks'),
     Input('btn-cl-add-sub', 'n_clicks'), Input('btn-cl-reset', 'n_clicks')],
    [State('store-checklist-draft', 'data'), State('store-checklists', 'data')] + _EDITOR_VALUE_STATES,
    prevent_initial_call=True,
)
def checklist_editor_cb(_open, _up, _down, _del, _add_item, _add_section, _add_sub, _reset,
                        draft, checklists, label_vals, label_ids, help_vals, help_ids, level_vals, level_ids):
    """Open the editor on one list, or apply one structural edit to the draft.
    Re-rendering replaces the rows, which re-creates their buttons; Dash can
    fire this callback for those new buttons with n_clicks None, so only a
    real click (a truthy value) does anything."""
    trig = callback_context.triggered_id
    if not trig or not callback_context.triggered[0].get('value'):
        raise PreventUpdate
    if isinstance(trig, dict) and trig.get('type') == 'btn-edit-checklist':
        name = trig['index']
        entries = [dict(e) for e in ((checklists or {}).get(name) or load_checklist(name))]
        return (True, f"Edit {CHECKLIST_NAMES[name]}", {'name': name, 'entries': entries},
                _render_checklist_editor_rows(entries), '')
    if not draft:
        raise PreventUpdate
    name = draft['name']
    entries = _merge_editor_values(draft['entries'], label_vals, label_ids, help_vals, help_ids,
                                   level_vals, level_ids)
    status = ''
    if isinstance(trig, dict):
        if trig['type'] == 'cl-up':
            entries = move_checklist_entry(entries, trig['index'], -1)
        elif trig['type'] == 'cl-down':
            entries = move_checklist_entry(entries, trig['index'], +1)
        elif trig['type'] == 'cl-del':
            entries = [e for e in entries if e['id'] != trig['index']]
    elif trig == 'btn-cl-add-item':
        entries.append({'kind': 'item', 'id': f"item_{uuid.uuid4().hex[:8]}", 'label': '', 'help': ''})
    elif trig in ('btn-cl-add-section', 'btn-cl-add-sub'):
        entries.append({'kind': 'heading', 'id': f"heading_{uuid.uuid4().hex[:8]}", 'text': '',
                        'level': 1 if trig == 'btn-cl-add-section' else 2})
    elif trig == 'btn-cl-reset':
        entries = [dict(e) for e in DEFAULT_CHECKLISTS[name]]
        status = dbc.Alert("Reset to the original list -- click Save to keep it, or Cancel.",
                           color='info', className="py-1 mb-0")
    return no_update, no_update, {'name': name, 'entries': entries}, _render_checklist_editor_rows(entries), status


@app.callback(
    [Output('store-checklists', 'data'), Output('checklist-editor-modal', 'is_open', allow_duplicate=True),
     Output('checklist-editor-status', 'children', allow_duplicate=True)],
    Input('btn-cl-save', 'n_clicks'),
    [State('store-checklist-draft', 'data'), State('store-checklists', 'data')] + _EDITOR_VALUE_STATES,
    prevent_initial_call=True,
)
def save_checklist_cb(n_clicks, draft, checklists, label_vals, label_ids, help_vals, help_ids,
                      level_vals, level_ids):
    if not n_clicks or not draft:
        raise PreventUpdate
    name = draft['name']
    entries = _merge_editor_values(draft['entries'], label_vals, label_ids, help_vals, help_ids,
                                   level_vals, level_ids)
    for e in entries:                                 # tidy stray whitespace
        if e['kind'] == 'heading':
            e['text'] = (e.get('text') or '').strip()
        else:
            e['label'], e['help'] = (e.get('label') or '').strip(), (e.get('help') or '').strip()
    error = validate_checklist(entries)
    if error:
        return no_update, no_update, dbc.Alert(error, color='danger', className="py-1 mb-0")
    if entries == DEFAULT_CHECKLISTS[name]:
        db.reset_checklist_definition(name)           # identical to the default: follow the default
    else:
        db.save_checklist_definition(name, entries)
    checklists = dict(checklists or {})
    checklists[name] = entries
    return checklists, False, ''


@app.callback(
    Output('checklist-editor-modal', 'is_open', allow_duplicate=True),
    Input('btn-cl-cancel', 'n_clicks'),
    prevent_initial_call=True,
)
def cancel_checklist_editor(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return False


# ==============================================================
# Weekly holdings reconciliation -- see parse_wealthsimple_holdings_csv /
# compute_holdings_discrepancies above.
# ==============================================================

@app.callback(
    [Output('store-holdings-diff', 'data'), Output('holdings-recon-status', 'children'),
     Output('store-account-snapshots', 'data'), Output('store-accounts', 'data', allow_duplicate=True)],
    Input('upload-holdings-csv', 'contents'),
    [State('store-openpos', 'data'), State('store-cash-equiv-symbols', 'data'),
     State('store-dismissed-holdings', 'data'), State('store-accounts', 'data')],
    prevent_initial_call=True,
)
def import_holdings_csv_cb(contents, openpos, cash_equiv_symbols, dismissed_keys, accounts):
    if not contents:
        raise PreventUpdate
    try:
        holdings_df = parse_wealthsimple_holdings_csv(contents)
        snapshots = parse_holdings_snapshot(contents)
    except ValueError as ve:
        return no_update, dbc.Alert(f"Error processing CSV: {ve}", color='danger'), no_update, no_update
    except Exception as e:
        return no_update, dbc.Alert(f"Unexpected error: {e}", color='danger'), no_update, no_update

    # Each account's balance (positions + cash) as of this export -- shown on
    # the Add Plan form for sizing. An account seen here first is registered,
    # same as an activity import would.
    types = dict(zip(holdings_df['account_id'], holdings_df['account_type']))
    for acct, snap in snapshots.items():
        db.upsert_account(acct, types.get(acct) or None)
        db.save_account_snapshot(acct, snap['as_of'], snap['positions'], snap['cash'])
    accounts = db.fetch_accounts()
    saved_note = (f" Saved balances for {len(snapshots)} account(s) as of "
                  f"{_fmt_as_of(next(iter(snapshots.values()))['as_of'])}." if snapshots else "")

    discrepancies = compute_holdings_discrepancies(holdings_df, openpos, cash_equiv_symbols, dismissed_keys,
                                                   accounts=accounts)
    if not discrepancies:
        status = dbc.Alert("No discrepancies -- TAPEs's open positions match this holdings export."
                           + saved_note, color='success')
    else:
        status = dbc.Alert(f"{len(discrepancies)} discrepancy(ies) found -- see below." + saved_note,
                           color='warning')
    return discrepancies, status, db.fetch_latest_account_snapshots(), accounts


@app.callback(
    Output('holdings-recon-list', 'children'),
    [Input('store-holdings-diff', 'data'), Input('account-view', 'value')],
)
def render_holdings_diff_cb(discrepancies, view):
    if discrepancies is None:
        return html.P("Upload a holdings CSV above to check for discrepancies.", className="text-muted small")
    discrepancies = _filter_view(discrepancies, view)
    if not discrepancies:
        return html.P("None.", className="text-muted small")
    kind_labels = {
        'untracked_in_tapes': "In Wealthsimple, not in TAPEs",
        'untracked_in_wealthsimple': "In TAPEs, not in Wealthsimple",
        'qty_mismatch': "Quantity mismatch",
    }
    rows = []
    for d in discrepancies:
        rows.append(dbc.Row([
            dbc.Col(html.B(d['symbol']), width=2),
            dbc.Col(html.Span(kind_labels.get(d['kind'], d['kind']), className="badge bg-warning text-dark"),
                    width=3),
            dbc.Col(d['detail'], width=5, className="small"),
            dbc.Col(dbc.Button("Dismiss", size='sm', color='outline-secondary',
                                id={'type': 'btn-dismiss-holding',
                                    'index': d.get('key') or holdings_key(None, d['symbol'])}), width=2),
        ], className="mb-1 align-items-center"))
    return rows


@app.callback(
    [Output('store-dismissed-holdings', 'data', allow_duplicate=True),
     Output('store-holdings-diff', 'data', allow_duplicate=True)],
    Input({'type': 'btn-dismiss-holding', 'index': ALL}, 'n_clicks'),
    [State('store-dismissed-holdings', 'data'), State('store-holdings-diff', 'data')],
    prevent_initial_call=True,
)
def dismiss_holding_discrepancy_cb(n_clicks_list, dismissed, discrepancies):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    key = triggered['index']
    account_id, symbol = key.split('|', 1)
    db.dismiss_holdings_discrepancy(account_id, symbol)
    dismissed = list(dismissed or [])
    if key not in dismissed:
        dismissed.append(key)
    remaining = [d for d in (discrepancies or []) if d.get('key') != key]
    return dismissed, remaining


@app.callback(
    Output('dismissed-holdings-list', 'children'),
    [Input('store-dismissed-holdings', 'data'), Input('store-accounts', 'data')],
)
def render_dismissed_holdings_cb(dismissed_keys, accounts):
    dismissed_keys = list(dismissed_keys or [])
    if not dismissed_keys:
        return html.Span("none", className="text-muted small")

    def label(key):
        account_id, symbol = key.split('|', 1)
        return f"{account_name(accounts, account_id)}: {symbol}" if len(accounts or []) > 1 else symbol
    buttons = [
        dbc.Button([label(key), html.Span(" ×", className="ms-1")], size='sm', color='outline-secondary',
                   id={'type': 'btn-undismiss-holding', 'index': key}, title="Re-check this holding next upload",
                   className="me-1 mb-1")
        for key in dismissed_keys
    ]
    return dbc.ButtonGroup(buttons)


@app.callback(
    Output('store-dismissed-holdings', 'data', allow_duplicate=True),
    Input({'type': 'btn-undismiss-holding', 'index': ALL}, 'n_clicks'),
    State('store-dismissed-holdings', 'data'),
    prevent_initial_call=True,
)
def undismiss_holding_discrepancy_cb(n_clicks_list, current):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    key = triggered['index']
    account_id, symbol = key.split('|', 1)
    db.undismiss_holdings_discrepancy(account_id, symbol)
    return [k for k in (current or []) if k != key]


def _fallback_plan_for(plans, pos):
    """The most-recently-created BUY plan (any status) that fits this open
    position's (account, symbol, currency) -- the fallback when the journal
    hasn't matched a committed plan to the lot (e.g. the plan is still a
    draft). Same "most recent plan" convention as before multi-account
    support, now never crossing accounts or listings."""
    best = None
    for p in (plans or []):
        if p.get('direction') != 'BUY' or not plan_fits_position(
                p, pos.get('account_id'), pos.get('symbol'), pos.get('currency')):
            continue
        if best is None or (p.get('created_at') or '') > (best.get('created_at') or ''):
            best = p
    return best


# ---- Accounts and the header "Account view" ----

def account_name(accounts, account_id):
    """Display name (nickname) for an account id; the id itself if unknown."""
    for a in (accounts or []):
        if a['account_id'] == account_id:
            return a.get('nickname') or a.get('account_type') or account_id
    return account_id or '—'


def account_options(accounts):
    return [{'label': f"{a.get('nickname') or a['account_id']} ({a['account_id']})", 'value': a['account_id']}
            for a in (accounts or [])]


def _in_view(record, view):
    """Does this record (anything with an account_id) belong in the current
    Account view? ALL_ACCOUNTS (or nothing chosen) shows everything."""
    if view in (None, '', ALL_ACCOUNTS):
        return True
    return _scalar(record.get('account_id')) == view


def _filter_view(records, view):
    return [r for r in (records or []) if _in_view(r, view)]


def _is_all_view(view):
    return view in (None, '', ALL_ACCOUNTS)


def is_archived(round_trip, accounts):
    """An UNPLANNED round trip that closed before its account's tracking start
    date -- trades from before plans were written and performance was
    measured. Callers only ever pass unmatched round trips: a trade with a
    plan always counts, whatever its date. Archived trades are hidden, never
    deleted: they're real fills, and account-level history (cash, FX) needs
    them. Re-importing them can't resurface them either, since the rule is
    date-based rather than a per-row flag."""
    acct = _scalar(round_trip.get('account_id'))
    start = next((a.get('tracking_start_date') for a in (accounts or []) if a['account_id'] == acct), None)
    exit_day = str(round_trip.get('exit_time') or '')[:10]
    return bool(start and exit_day) and exit_day < start


def _default_account(accounts, view, current=None, plans=None):
    """Account a new plan/import should default to: the drilled-down account,
    else a still-valid current choice, else the most recent plan's account,
    else the first account."""
    ids = [a['account_id'] for a in (accounts or [])]
    if not ids:
        return None
    if not _is_all_view(view) and view in ids:
        return view
    if current in ids:
        return current
    recent = sorted((p for p in (plans or []) if p.get('account_id') in ids),
                    key=lambda p: p.get('created_at') or '')
    if recent:
        return recent[-1]['account_id']
    return ids[0]


@app.callback(
    [Output('account-view', 'options'),
     Output('plan-account', 'options'), Output('edit-account', 'options'),
     Output('import-account', 'options')],
    Input('store-accounts', 'data'),
)
def populate_account_options(accounts):
    opts = account_options(accounts)
    return [{'label': 'All accounts', 'value': ALL_ACCOUNTS}] + opts, opts, opts, opts


# A holdings-export balance older than this gets a "stale" flag on the form.
ACCOUNT_SNAPSHOT_STALE_DAYS = 7


def _fmt_as_of(as_of):
    """'2026-10-03T13:27:00-04:00' -> '2026-10-03 13:27'."""
    return str(as_of or '')[:16].replace('T', ' ')


def _snapshot_age_days(as_of, now=None):
    ts = pd.Timestamp(as_of)
    now = now or (pd.Timestamp.now(tz=ts.tz) if ts.tz is not None else pd.Timestamp.now())
    return (now - ts).total_seconds() / 86400


def _fmt_ccy_amounts(amounts, signed=False):
    """{currency: amount} -> 'USD 79,893.50 · CAD 1,200.00' (no FX conversion);
    signed=True gives 'USD +1,305.20 · CAD -12.00' for changes."""
    fmt = '{:+,.2f}' if signed else '{:,.2f}'
    parts = [f"{ccy} {fmt.format(amt)}" for ccy, amt in sorted((amounts or {}).items()) if abs(amt) >= 0.005]
    return " · ".join(parts) or ("no change" if signed else "0.00")


def _base_total_note(amounts, fx):
    """' = CAD 113,520.10' when amounts span a foreign currency and every one
    has a rate; '' otherwise (all already CAD, or no usable rate)."""
    if not amounts or set(amounts) <= {BASE_CURRENCY}:
        return ""
    total = to_base(amounts, usable_fx(fx, amounts))
    return f" = {BASE_CURRENCY} {total:,.2f}" if total is not None else ""


def render_account_balance(account_id, accounts, snapshots, staged_prices, openpos, now=None, fx=None):
    """The Add Plan form's sizing reference for the chosen account: its balance
    from the latest holdings export (positions + cash, with its age), and a
    live positions-only value beside it. With fx, each also shows its
    combined total in the base currency at today's rate."""
    if not account_id:
        return None
    name = account_name(accounts, account_id)
    snap = (snapshots or {}).get(account_id)
    lines = []
    if snap:
        total = dict(snap['positions'])
        for ccy, amt in snap['cash'].items():
            total[ccy] = total.get(ccy, 0.0) + amt
        age = _snapshot_age_days(snap['as_of'], now)
        days = round(age)
        age_text = "today" if age < 1 else f"{days} day{'s' if days != 1 else ''} ago"
        stale = (html.Span(" stale -- upload a fresh holdings export", className="badge bg-warning text-dark ms-1")
                 if age > ACCOUNT_SNAPSHOT_STALE_DAYS else None)
        lines.append(html.Div([
            html.B(f"{name} balance: {_fmt_ccy_amounts(total)}{_base_total_note(total, fx)}"),
            html.Span(f"  (cash {_fmt_ccy_amounts(snap['cash'])}; holdings export as of "
                      f"{_fmt_as_of(snap['as_of'])}, {age_text})", className="text-muted"),
            stale]))
    else:
        lines.append(html.Div([html.B(f"{name} balance: "), html.Span(
            "no holdings export yet -- upload one in Weekly Reconciliation to see the balance "
            "including cash.", className="text-muted")]))

    priced = (staged_prices or {}).get('priced_symbols')
    if priced:
        live, unpriced = value_by_currency(openpos, priced, account_id)
        not_priced = f"; not priced: {', '.join(sorted(set(unpriced)))}" if unpriced else ""
        lines.append(html.Div(f"Live positions value: {_fmt_ccy_amounts(live)}{_base_total_note(live, fx)} "
                              f"(positions only, no cash){not_priced}", className="text-muted"))
        if snap:
            change = {c: live.get(c, 0.0) - snap['positions'].get(c, 0.0)
                      for c in set(live) | set(snap['positions'])}
            lines.append(html.Div(f"Change in positions since the export: {_fmt_ccy_amounts(change, signed=True)} "
                                  f"(price moves plus any trades since)", className="text-muted"))
    else:
        lines.append(html.Div("Click \"Refresh Live Prices\" (Open Positions) for a live positions value.",
                              className="text-muted"))
    note = fmt_fx_note(fx)
    if note and any(_base_total_note(a, fx) for a in ([total] if snap else []) +
                    ([live] if priced else [])):
        lines.append(html.Div(f"Converted at {note}.", className="text-muted"))
    return html.Div(lines, className="small border rounded p-2 bg-light")


@app.callback(
    Output('plan-account-balance', 'children'),
    [Input('plan-account', 'value'), Input('store-account-snapshots', 'data'),
     Input('store-open-positions-prices', 'data'), Input('store-openpos', 'data')],
    State('store-accounts', 'data'),
)
def update_plan_account_balance(account_id, snapshots, staged_prices, openpos, accounts):
    fx = (staged_prices or {}).get('fx') or get_fx_rates()    # cached; the Bank at most twice a day
    return render_account_balance(account_id, accounts, snapshots, staged_prices, openpos, fx=fx)


@app.callback(
    Output('accounts-list', 'children'),
    Input('store-accounts', 'data'),
)
def render_accounts_list(accounts):
    if not accounts:
        return html.Span("No accounts yet -- import a Wealthsimple activity export.",
                         className="text-muted small")
    return [dbc.Row([
        dbc.Col(html.Code(a['account_id']), width=3),
        dbc.Col(a.get('account_type') or '—', width=2),
        dbc.Col(dbc.Input(id={'type': 'account-nickname', 'index': a['account_id']},
                          value=a.get('nickname') or '', size='sm', debounce=True), width=4),
        dbc.Col(dcc.DatePickerSingle(id={'type': 'account-tracking-start', 'index': a['account_id']},
                                     date=a.get('tracking_start_date'), clearable=True,
                                     placeholder="Tracking start", display_format='YYYY-MM-DD'),
                width=3),
    ], className="mb-1 align-items-center") for a in accounts]


@app.callback(
    [Output('store-accounts', 'data', allow_duplicate=True),
     Output('accounts-status', 'children', allow_duplicate=True)],
    Input({'type': 'account-tracking-start', 'index': ALL}, 'date'),
    State('store-accounts', 'data'),
    prevent_initial_call=True,
)
def save_account_tracking_start(_dates, accounts):
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    account_id = triggered['index']
    start = callback_context.triggered[0]['value']
    current = next((a for a in (accounts or []) if a['account_id'] == account_id), None)
    if current is None or (start or None) == (current.get('tracking_start_date') or None):
        raise PreventUpdate
    db.set_account_tracking_start(account_id, start)
    msg = (f"Tracking start set to {start[:10]}: unplanned trades closed before it are archived."
           if start else "Tracking start cleared: nothing is archived for this account.")
    return db.fetch_accounts(), html.Span(msg, className="text-success small")


@app.callback(
    [Output('store-accounts', 'data', allow_duplicate=True), Output('accounts-status', 'children')],
    Input({'type': 'account-nickname', 'index': ALL}, 'value'),
    State('store-accounts', 'data'),
    prevent_initial_call=True,
)
def save_account_nickname(_values, accounts):
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    account_id = triggered['index']
    nickname = callback_context.triggered[0]['value']
    current = next((a for a in (accounts or []) if a['account_id'] == account_id), None)
    if current is None or (nickname or '').strip() == (current.get('nickname') or ''):
        raise PreventUpdate
    db.set_account_nickname(account_id, nickname)
    return db.fetch_accounts(), html.Span("Saved.", className="text-success small")


@app.callback(
    [Output('plan-account', 'value'), Output('import-account', 'value')],
    [Input('account-view', 'value'), Input('store-accounts', 'data')],
    [State('plan-account', 'value'), State('import-account', 'value'), State('store-plans', 'data')],
)
def default_account_selection(view, accounts, plan_current, import_current, plans):
    return (_default_account(accounts, view, plan_current, plans),
            _default_account(accounts, view, import_current, plans))


def _guess_symbol_currency(symbol, openpos=None, executions=None):
    """Best-effort currency guess for a symbol that hasn't been committed to a
    plan yet -- there's no authoritative source until an execution actually
    exists (plans.currency is a plain user-editable field, not derived), so
    this only ever pre-fills the Currency picker; the user can always
    override it. Checks the currently open position for this symbol first
    (most current data), then the most recent past execution for it, else
    returns None (genuinely unknown -- a symbol that's never been traded
    before) so the caller can fall back to a UI default instead of silently
    asserting a guess as fact."""
    if not symbol:
        return None
    symbol = symbol.strip().upper()
    for pos in (openpos or []):
        if pos.get('symbol') == symbol and pos.get('currency'):
            return pos['currency']
    # executions is a list of dicts (as staged in store-executions), newest
    # import order not guaranteed -- sort by timestamp to get the most recent.
    matches = [e for e in (executions or []) if e.get('symbol') == symbol and e.get('currency')]
    if matches:
        return sorted(matches, key=lambda e: e.get('timestamp') or '')[-1]['currency']
    return None


def _compute_ratcheted_stops_by_lot(data, plans, journal):
    """Current, ratcheted stop per open lot (after any target already hit has
    moved it), replayed via the same analyze_targets_and_stop logic used for
    closed trades and the Journal's "Open (in progress)" rows. Keyed by
    lot_id, not symbol -- two pyramided lots can have genuinely different
    ratcheted stops. Reused by render_open_positions's portfolio-risk banner
    and the pre-trade risk check's "existing total risk" figure, so both
    readings of current portfolio risk stay consistent with each other rather
    than being two independent computations of the same thing.
    """
    plan_by_lot_id = {row['lot_id']: row for row in (journal or []) if row.get('lot_id')}

    def _resolve_plan(r):
        return plan_by_lot_id.get(r['lot_id']) or _fallback_plan_for(plans, r)

    ratcheted_stops = {}
    for pos in data:
        plan = _resolve_plan(pos)
        if not plan:
            continue
        if is_campaign(plan):
            # A campaign's stop follows its add schedule (raised before each add).
            ratcheted_stops[pos['lot_id']] = analyze_campaign(
                plan, _fill_list_of(pos, 'entry_fill_list'), _fill_list_of(pos, 'exit_fill_list'))['scheduled_stop']
            continue
        _, _, _, final_stop, _, _, _ = analyze_targets_and_stop(
            plan['direction'], plan.get('entry_price'), plan.get('stop_loss'),
            plan.get('targets') or [], pos.get('exit_fill_list') or [])
        ratcheted_stops[pos['lot_id']] = final_stop
    return ratcheted_stops


def gap_allowance_by_lot(data, plans, journal, k=None):
    """{lot_id: gap allowance per share} for open lots with a plan, from the
    plan's atr_at_commit (see gap_per_share). Same lot -> plan resolution as
    _compute_ratcheted_stops_by_lot."""
    plan_by_lot_id = {row['lot_id']: row for row in (journal or []) if row.get('lot_id')}
    k = load_gap_allowance_atr() if k is None else k
    out = {}
    for pos in data or []:
        plan = plan_by_lot_id.get(pos['lot_id']) or _fallback_plan_for(plans, pos)
        if plan:
            out[pos['lot_id']] = gap_per_share(plan.get('atr_at_commit'), plan.get('entry_price'),
                                               plan.get('stop_loss'), k)
    return out


@app.callback(
    [Output('open-positions', 'children'), Output('portfolio-risk-banner', 'children')],
    [Input('store-openpos', 'data'), Input('store-plans', 'data'), Input('store-cash-equiv-symbols', 'data'),
     Input('store-open-positions-prices', 'data'), Input('store-executions', 'data'),
     Input('store-journal', 'data'), Input('account-view', 'value'), Input('store-accounts', 'data'),
     Input('store-account-snapshots', 'data'), Input('store-daily-journal', 'data'),
     Input('store-weekly-reviews', 'data')],
)
def render_open_positions(data, plans, cash_equiv_symbols, staged_prices, executions_data, journal,
                          view, accounts, snapshots=None, daily_entries=None, weekly_rows=None):
    """Rows follow the header Account view; the portfolio risk banner always
    measures the whole portfolio (the caps are portfolio-wide), with
    per-account lines underneath for information only."""
    if not data:
        return html.P("None.", className="text-muted"), None
    shown = _filter_view(data, view)
    all_view = _is_all_view(view)
    df = pd.DataFrame(shown)
    cash_equiv = set(cash_equiv_symbols or [])
    # Which specific plan matches which specific LOT -- store-journal already
    # resolved this correctly (deterministic bucketing where possible, the
    # fuzzy date/price matcher otherwise -- see match_plans_to_roundtrips),
    # so this reuses that instead of re-guessing "most recent plan for this
    # symbol" a second, independent way. Only covers COMMITTED plans (the
    # journal excludes drafts); a lot with only a draft plan (no committed
    # plan matched yet) falls back to _fallback_plan_for, the most recent plan
    # for the same account/symbol/currency -- there's no better signal for a
    # draft, since matching only operates on commits.
    plan_by_lot_id = {row['lot_id']: row for row in (journal or []) if row.get('lot_id')}
    priced_symbols = (staged_prices or {}).get('priced_symbols', {})

    def _resolve_plan(r):
        return plan_by_lot_id.get(r['lot_id']) or _fallback_plan_for(plans, r)

    # Two different stops matter here, for two different questions (see the
    # brainstorm this came out of): "Current R:R" always measures against the
    # ORIGINAL planned risk (entry to initial stop) -- the standard R-multiple
    # convention (Van Tharp), used consistently whether a position is up or
    # down, and matching how closed-trade r_multiple already works elsewhere
    # in this app. Portfolio risk and the "Risk-Free" badge instead need the
    # CURRENT, ratcheted stop (after any targets already hit have moved it) --
    # that's the stop that would actually trigger a sell right now, computed
    # via the same analyze_targets_and_stop replay used for closed trades and
    # the Journal's "Open (in progress)" rows, just not previously wired into
    # this card. Keyed by lot_id, not symbol -- two pyramided lots can have
    # genuinely different ratcheted stops.
    ratcheted_stops = _compute_ratcheted_stops_by_lot(data, plans, journal)
    risk_free_lots = set()
    for pos in data:
        plan = _resolve_plan(pos)
        final_stop = ratcheted_stops.get(pos['lot_id'])
        if not plan or final_stop is None:
            continue
        sign = 1 if plan['direction'] == 'BUY' else -1
        if plan.get('entry_price') is not None \
                and sign * (final_stop - plan['entry_price']) >= 0:
            risk_free_lots.add(pos['lot_id'])

    if df.empty:
        return html.P("None in this account.", className="text-muted"), \
            _render_portfolio_risk_banner(data, plans, journal, cash_equiv, staged_prices,
                                          executions_data, view, accounts, ratcheted_stops, snapshots)

    header = dbc.Row([
        dbc.Col(html.B("Account" if all_view else "Symbol"), width=2), dbc.Col(html.B("Qty Open"), width=1),
        dbc.Col(html.B("Avg Price"), width=2), dbc.Col(html.B("Since"), width=1),
        dbc.Col(html.B("Plan?"), width=2),
        dbc.Col(html.B(["Current R:R", help_icon(
            "Unrealized R-multiple: current price vs. entry, always measured against "
            "the ORIGINAL planned risk (entry to initial stop) -- the standard "
            "R-multiple convention, whether the position is currently up or down. "
            "\"Risk-Free\" means the stop has since ratcheted to breakeven or better "
            "(after a target hit), so there's no downside left on this position from "
            "here. Click \"Refresh Live Prices\" above to compute -- not automatic, "
            "since fetching every symbol on every page load would be slow.")]), width=2),
        dbc.Col(html.B(""), width=2),
    ])
    rows = [header]
    for _, r in df.iterrows():
        since = pd.to_datetime(r['since']).strftime('%Y-%m-%d') if pd.notna(r['since']) else '-'
        # Only long positions are auto-detected (see build_round_trips), so a
        # plan "covers" a position if any BUY plan -- draft or committed -- fits
        # its account/symbol/currency. Deliberately more urgent than "Executed
        # Trades Without a Plan": an unplanned open position is live risk.
        if r['symbol'] in cash_equiv:
            plan_badge = html.Span("Cash equivalent", className="badge bg-secondary")
            action = None
        elif _fallback_plan_for(plans, r) is not None:
            plan_badge = html.Span("Planned", className="badge bg-success")
            action = dbc.Button("Observations", size='sm', color='outline-info',
                                 id={'type': 'btn-open-observations', 'index': r['lot_id']})
        else:
            plan_badge = html.Span("No plan", className="badge bg-danger")
            action = dbc.Button("Log Plan", size='sm', color='outline-warning',
                                 id={'type': 'btn-log-open-plan', 'index': r['lot_id']})

        plan = _resolve_plan(r)
        if r['symbol'] in cash_equiv:
            rr_disp = html.Span("—", className="text-muted")
        elif price_key(r) not in priced_symbols:
            rr_disp = html.Span("—", className="text-muted", title="Click \"Refresh Live Prices\"")
        else:
            initial_stop = plan.get('stop_loss') if plan else None
            current_r = compute_current_r_multiple(
                r.get('direction', 'BUY'), r['avg_price'], initial_stop,
                priced_symbols[price_key(r)]['price'])
            if current_r is None:
                rr_disp = html.Span("—", className="text-muted", title="No stop on record")
            else:
                color = 'success' if current_r >= 0 else 'danger'
                rr_disp = html.Span(f"{current_r:+.2f}R", className=f"badge bg-{color}")
            if r['lot_id'] in risk_free_lots:
                rr_disp = html.Div([rr_disp, html.Br(),
                                     html.Span("Risk-Free", className="badge bg-info mt-1")])

        symbol_cell = (html.Div([html.Span(account_name(accounts, r.get('account_id')),
                                           className="badge bg-light text-dark me-1"), r['symbol']])
                       if all_view else r['symbol'])
        rows.append(dbc.Row([
            dbc.Col(symbol_cell, width=2), dbc.Col(f"{r['qty_open']:.4g}", width=1),
            dbc.Col(f"{r['currency']} {r['avg_price']:.2f}", width=2), dbc.Col(since, width=1),
            dbc.Col(plan_badge, width=2), dbc.Col(rr_disp, width=2), dbc.Col(action, width=2),
        ], className="mb-1 align-items-center"))

    return rows, _render_portfolio_risk_banner(data, plans, journal, cash_equiv, staged_prices,
                                               executions_data, view, accounts, ratcheted_stops, snapshots,
                                               current_posture(daily_entries, weekly_rows))


def _render_portfolio_risk_banner(data, plans, journal, cash_equiv, staged_prices, executions_data,
                                  view, accounts, ratcheted_stops, snapshots=None, posture=None):
    """Colour-coded heat for the WHOLE portfolio (caps are portfolio-wide, in
    every view), then one plain line per account -- dollars at risk and % of
    that account's value -- for information only. A single-account view shows
    just that account's line. Portfolio and account values include each
    account's cash from its latest holdings export (portfolio_with_cash)."""
    priced_symbols = (staged_prices or {}).get('priced_symbols', {})
    risk_banner = None
    if staged_prices and priced_symbols:
        # In CAD whenever there's a rate for every currency held: a % of a
        # USD+CAD portfolio is only right with both sides in one currency.
        # Cash counts toward the portfolio (2026-10-17): heat is risk as a
        # share of everything in the accounts, not just what's invested.
        pw = portfolio_with_cash(data, staged_prices, snapshots, executions_data, accounts)
        portfolio_value, fx, by_currency = pw['value'], pw['fx'], pw['positions']
        # Portfolio risk uses the ratcheted stop, not the initial one -- this
        # is "how much could I actually still lose from here," which should
        # reflect the real, current protective level, not a stale original
        # one a target hit has already moved past.
        gap_k = load_gap_allowance_atr()
        total_risk, total_risk_pct, per_lot = compute_portfolio_risk(
            data, ratcheted_stops, cash_equiv, priced_symbols, portfolio_value, fx=fx,
            gap_by_lot=gap_allowance_by_lot(data, plans, journal, gap_k))
        if total_risk_pct is not None:
            caps = posture_caps(posture)
            if total_risk_pct > caps['warning']:
                color, label = 'danger', "⚠ Warning"
            elif total_risk_pct > caps['caution']:
                color, label = 'warning', "⚠ Caution"
            else:
                color, label = 'success', "OK"
            # This banner is exactly the feature staleness makes most
            # dangerous -- a stale "OK" reading gives false confidence right
            # when accuracy matters most (Wealthsimple has no API; TAPEs only
            # knows what's been manually re-imported). Surface how old the
            # underlying execution data actually is directly on the reading
            # itself, not just as a separate page-level notice.
            freshness_note = ""
            freshness = _compute_data_freshness(executions_data)
            if freshness is not None:
                _, hours_ago = freshness
                freshness_note = (f" Based on execution data imported {_format_age(hours_ago)} ago -- "
                                   f"re-import if you've traded since.")
            acct_values = pw['account_values']
            acct_risk = risk_by_account(data, per_lot, fx=fx)
            money = (lambda v: f"{BASE_CURRENCY} {v:,.2f}") if fx else (lambda v: f"{v:,.2f}")
            acct_ids = sorted(set(acct_values) | set(acct_risk), key=lambda a: account_name(accounts, a) or '')
            if not _is_all_view(view):
                acct_ids = [a for a in acct_ids if a == view]
            acct_lines = []
            for acct in acct_ids:
                value, risk = acct_values.get(acct, 0.0), acct_risk.get(acct, 0.0)
                pct = f"{risk / value * 100:.1f}% of its value" if value else "n/a"
                acct_lines.append(html.Li(f"{account_name(accounts, acct)}: {money(risk)} at risk ({pct})"))
            per_account = html.Div([
                html.Small("Per account (information only -- caps apply to the whole portfolio; "
                           "each account's value includes its cash):", className="text-muted"),
                html.Ul(acct_lines, className="small mb-0"),
            ], className="mt-1") if acct_lines else None
            cash = pw['cash']
            cash_part = (f" + cash {_fmt_ccy_amounts(cash['by_currency'])} ({cash_dates_text(cash)})"
                         if cash['by_currency'] else " (no cash: no holdings export yet)")
            if fx:
                money_note = (f" Portfolio: positions {_fmt_ccy_amounts(by_currency)}{cash_part}, converted at "
                              f"{fmt_fx_note(fx, pw['combined'])}.")
            else:
                missing = ", ".join(sorted((staged_prices.get('fx') or {}).get('errors', {}))) or "a currency"
                money_note = (f" Figures are summed across currencies as-is (no FX rate for {missing}), "
                              f"so treat them as approximate.")
            risk_banner = dbc.Alert([
                html.Div(
                    f"{label}: total portfolio risk is {total_risk_pct:.1f}% of portfolio value"
                    + (f" against a {caps['warning']:g}% ceiling ({caps['state']} limits)" if caps['state'] else "")
                    + f" ({money(portfolio_value)} portfolio across all accounts, {money(total_risk)} at "
                    f"risk if every position hit its (current, ratcheted) stop from here"
                    + (f", plus a gap allowance of {gap_k:g} × ATR per share" if gap_k else "")
                    + f") -- excludes cash-equivalent positions.{money_note}{freshness_note}"),
                *[html.Div(w, className="small mt-1") for w in cash_warnings(pw['cash'], accounts)],
                per_account,
            ], color=color, className="py-2 mb-0")
    return risk_banner


def _render_observations_list(observations, readonly=False):
    """Render one plan's observation log, newest first (see db.fetch_plan_observations).
    Shared between the modal-open callback and the add/delete callbacks below (all
    interactive, readonly=False, each row getting a delete control) AND the static
    View detail panel (readonly=True, no delete control -- a delete button there
    would target the Notes modal's own list container, not this panel, since that's
    what store-observing-plan-id tracks)."""
    if not observations:
        return html.P("No observations logged yet.", className="text-muted small")
    rows = []
    for o in observations:
        when = o['observed_at'][:16].replace('T', ' ')
        psych_bits = []
        psych_text = psych_summary_text(o)
        if psych_text:
            psych_bits.append(html.Div(psych_text, className="text-muted small"))
        if o.get('event_type') == 'deviation':
            psych_bits.insert(0, html.Span("Deviation", className="badge bg-danger me-1"))
        cols = [
            dbc.Col(html.Small(when, className="text-muted"), width=3),
            dbc.Col([o['note']] + psych_bits, width=9 if readonly else 7),
        ]
        if not readonly:
            cols.append(dbc.Col(dbc.Button("×", size='sm', color='outline-danger',
                                            id={'type': 'btn-delete-observation', 'index': o['obs_id']}),
                                 width=2))
        rows.append(dbc.Row(cols, className="mb-1"))
    return html.Div(rows)


@app.callback(
    [Output('observations-modal', 'is_open'),
     Output('store-observing-plan-id', 'data'),
     Output('store-observation-event-type', 'data'),
     Output('observations-modal-title', 'children'),
     Output('observations-list', 'children'),
     Output('observation-note', 'value'),
     Output('observation-status', 'children')] + _psych_outputs('observation'),
    Input({'type': 'btn-open-observations', 'index': ALL}, 'n_clicks'),
    [State('store-plans', 'data'), State('store-journal', 'data'), State('store-openpos', 'data')],
    prevent_initial_call=True,
)
def open_observations_modal(n_clicks_list, plans, journal, openpos):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    lot_id_clicked = triggered['index']
    pos = next((p for p in (openpos or []) if p.get('lot_id') == lot_id_clicked), None)
    if not pos:
        raise PreventUpdate
    symbol = pos['symbol']
    # The journal already resolved which specific committed plan matches
    # this specific lot (deterministic bucketing where possible, the fuzzy
    # matcher otherwise -- see match_plans_to_roundtrips), so this is used
    # ahead of any symbol-level guess. Falls back to "most recently created
    # BUY plan for this symbol" only when no committed plan is matched to
    # this lot yet (e.g. it's still a draft) -- the same guess used before
    # pyramiding support existed, since there's no better signal for a draft.
    journal_row = next((r for r in (journal or []) if r.get('lot_id') == lot_id_clicked), None)
    plan_id = journal_row['plan_id'] if journal_row else None
    if not plan_id:
        fallback = _fallback_plan_for(plans, pos)
        if not fallback:
            raise PreventUpdate
        plan_id = fallback['plan_id']
    observations = db.fetch_plan_observations(plan_id)
    return (True, plan_id, None, f"Plan Observations — {symbol}",
            _render_observations_list(observations), '', '') + (0,) * len(PSYCH_AXES)


@app.callback(
    [Output('observations-modal', 'is_open', allow_duplicate=True),
     Output('store-observing-plan-id', 'data', allow_duplicate=True),
     Output('store-observation-event-type', 'data', allow_duplicate=True),
     Output('observations-modal-title', 'children', allow_duplicate=True),
     Output('observations-list', 'children', allow_duplicate=True),
     Output('observation-note', 'value', allow_duplicate=True),
     Output('observation-status', 'children', allow_duplicate=True)]
    + _psych_outputs('observation', allow_duplicate=True),
    [Input({'type': 'btn-log-deviation', 'index': ALL}, 'n_clicks'),
     Input({'type': 'btn-log-observation-plan', 'index': ALL}, 'n_clicks')],
    State('store-plans', 'data'),
    prevent_initial_call=True,
)
def open_plan_observations_modal_by_id(deviation_clicks, general_clicks, plans):
    """Two entry points into the same Observations modal, both keyed directly
    by plan_id (not a symbol lookup, since the journal row already has it) --
    unlike the Open Positions "Observations" button, both work on a CLOSED
    trade too, since that's exactly where a manual reading is most often
    needed and where the auto-detected prompt doesn't reach:

    - "⚠ Off-Plan Exit" (auto-detected, only on a stop_breached row) tags
      whatever gets added as event_type='deviation'.
    - "Notes" (every journal row, any status) is the general manual
      supplement -- covers a voluntary early exit or anything else the
      stop-breach check can't catch, tagged as an ordinary note (event_type
      stays None)."""
    if not any(deviation_clicks or []) and not any(general_clicks or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    plan_id = triggered['index']
    plan = next((p for p in (plans or []) if p['plan_id'] == plan_id), None)
    if not plan:
        raise PreventUpdate
    observations = db.fetch_plan_observations(plan_id)
    if triggered['type'] == 'btn-log-deviation':
        return (True, plan_id, 'deviation', f"Log Deviation — {plan['symbol']}",
                _render_observations_list(observations),
                '', dbc.Alert("This exit breached your stop -- what was going on?", color='warning')
                ) + (0,) * len(PSYCH_AXES)
    return (True, plan_id, None, f"Plan Observations — {plan['symbol']}",
            _render_observations_list(observations), '', '') + (0,) * len(PSYCH_AXES)


@app.callback(
    Output('observations-modal', 'is_open', allow_duplicate=True),
    Input('btn-observations-close', 'n_clicks'),
    prevent_initial_call=True,
)
def close_observations_modal(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return False


@app.callback(
    [Output('observations-list', 'children', allow_duplicate=True),
     Output('observation-note', 'value', allow_duplicate=True),
     Output('observation-status', 'children', allow_duplicate=True),
     Output('store-obs-version', 'data', allow_duplicate=True)]
    + _psych_outputs('observation', allow_duplicate=True),
    Input('btn-add-observation', 'n_clicks'),
    [State('observation-note', 'value'), State('store-observing-plan-id', 'data'),
     State('store-observation-event-type', 'data'), State('store-obs-version', 'data')]
    + _psych_states('observation'),
    prevent_initial_call=True,
)
def add_observation(n_clicks, note, plan_id, event_type, obs_version, *psych_values):
    if not n_clicks or not plan_id:
        raise PreventUpdate
    if not note or not note.strip():
        return (no_update, no_update, dbc.Alert("Enter a note first.", color='warning'), no_update) \
            + (no_update,) * len(PSYCH_AXES)
    db.add_plan_observation(plan_id, note, event_type=event_type, **_psych_fields(psych_values))
    return (_render_observations_list(db.fetch_plan_observations(plan_id)), '', '',
            (obs_version or 0) + 1) + (0,) * len(PSYCH_AXES)


@app.callback(
    [Output('observations-list', 'children', allow_duplicate=True),
     Output('store-obs-version', 'data', allow_duplicate=True)],
    Input({'type': 'btn-delete-observation', 'index': ALL}, 'n_clicks'),
    [State('store-observing-plan-id', 'data'), State('store-obs-version', 'data')],
    prevent_initial_call=True,
)
def delete_observation(n_clicks_list, plan_id, obs_version):
    if not any(n_clicks_list or []) or not plan_id:
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    db.delete_plan_observation(triggered['index'])
    return _render_observations_list(db.fetch_plan_observations(plan_id)), (obs_version or 0) + 1


@app.callback(
    Output('cash-equiv-list', 'children'),
    Input('store-cash-equiv-symbols', 'data'),
)
def render_cash_equiv_list(symbols):
    if not symbols:
        return html.P("None added yet.", className="text-muted small")
    return dbc.ButtonGroup([
        dbc.Button([sym, html.Span(" ×", className="ms-1")], size='sm', color='outline-secondary',
                   id={'type': 'btn-remove-cash-equiv', 'index': sym}, title=f"Remove {sym}")
        for sym in symbols
    ])


@app.callback(
    [Output('store-cash-equiv-symbols', 'data', allow_duplicate=True),
     Output('cash-equiv-input', 'value')],
    Input('btn-add-cash-equiv', 'n_clicks'),
    [State('cash-equiv-input', 'value'), State('store-cash-equiv-symbols', 'data')],
    prevent_initial_call=True,
)
def add_cash_equiv_symbol(n_clicks, symbol, current):
    if not n_clicks or not symbol or not symbol.strip():
        raise PreventUpdate
    symbol = symbol.strip().upper()
    db.add_cash_equivalent_symbol(symbol)
    current = list(current or [])
    if symbol not in current:
        current.append(symbol)
        current.sort()
    return current, ''


@app.callback(
    Output('store-cash-equiv-symbols', 'data', allow_duplicate=True),
    Input({'type': 'btn-remove-cash-equiv', 'index': ALL}, 'n_clicks'),
    State('store-cash-equiv-symbols', 'data'),
    prevent_initial_call=True,
)
def remove_cash_equiv_symbol(n_clicks_list, current):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    symbol = triggered['index']
    db.remove_cash_equivalent_symbol(symbol)
    return [s for s in (current or []) if s != symbol]


# ---- Long lists (2026-10-20): show recent entries first, the rest a click away ----
# Display only: every stat, the weekly prompt and the MCP tools still use all data.
def paginate(items, page, page_size):
    """(items on that page, number of pages, the page actually shown) --
    page clamped to 1..pages, so a filter that shrinks the list never lands
    on an empty page."""
    pages = max(1, -(-len(items) // page_size))
    page = min(max(1, int(page or 1)), pages)
    return items[(page - 1) * page_size: page * page_size], pages, page


def _period_cutoff(period, today):
    if period in (None, '', 'all'):
        return None
    if period == 'ytd':
        return pd.Timestamp(today.year, 1, 1)
    return pd.Timestamp(today) - pd.Timedelta(days=int(period))


def _naive_ts(v):
    ts = pd.to_datetime(_scalar(v), errors='coerce', utc=True)
    return ts.tz_convert(None) if pd.notna(ts) else pd.NaT


def daily_obs_view(observations, period='30', search='', page=1, page_size=DAILY_OBS_PAGE_SIZE, today=None):
    """The Daily Trading Journal entries to show: within the period (by
    obs_date), matching the search text (case-insensitive, in the note or the
    date), newest first, one page. Returns (rows, matching, pages, page)."""
    cutoff = _period_cutoff(period, today or dt.date.today())
    needle = (search or '').strip().lower()
    rows = [o for o in (observations or [])
            if (cutoff is None or _naive_ts(o.get('obs_date')) >= cutoff)
            and (not needle or needle in (o.get('note') or '').lower() or needle in (o.get('obs_date') or ''))]
    rows.sort(key=lambda o: (o.get('obs_date') or '', o.get('created_at') or ''), reverse=True)
    shown, pages, page = paginate(rows, page, page_size)
    return shown, len(rows), pages, page


def journal_activity_date(row):
    """When a journal row last happened: exit (closed), cancellation,
    entry (open), else when the plan went live."""
    for key in ('exit_time', 'cancelled_at', 'entry_time', 'committed_at', 'planned_date', 'created_at'):
        ts = _naive_ts(row.get(key))
        if pd.notna(ts):
            return ts
    return pd.NaT


def journal_page_view(records, tab='active', period='90', sort='recent', page=1,
                      page_size=JOURNAL_PAGE_SIZE, today=None):
    """The Trade Journal rows to show. tab: active (Open + Planned) / closed /
    cancelled / all. period limits only finished rows (Closed by exit date,
    Cancelled by cancel date) -- live ones always show. sort: most recent
    activity first, or Plan Score (high first; no score last). Returns
    {'rows', 'total', 'pages', 'page', 'counts' (per tab, before the period),
    'hidden_by_period'}."""
    wanted = JOURNAL_TAB_STATUSES.get(tab)
    counts = {t: sum(1 for r in records if JOURNAL_TAB_STATUSES.get(t) is None
                     or r.get('status') in JOURNAL_TAB_STATUSES[t]) for t, _ in JOURNAL_TABS}
    rows = [r for r in records if wanted is None or r.get('status') in wanted]
    cutoff = _period_cutoff(period, today or dt.date.today())
    before = len(rows)
    if cutoff is not None:
        rows = [r for r in rows if r.get('status') not in ('Closed', 'Cancelled')
                or (pd.notna(journal_activity_date(r)) and journal_activity_date(r) >= cutoff)]
    if sort == 'score':
        rows.sort(key=lambda r: (pd.notna(r.get('score')) and r.get('score') is not None,
                                 r.get('score') if pd.notna(r.get('score')) and r.get('score') is not None
                                 else 0), reverse=True)
    else:
        rows.sort(key=lambda r: (pd.notna(journal_activity_date(r)),
                                 journal_activity_date(r) if pd.notna(journal_activity_date(r))
                                 else pd.Timestamp.min), reverse=True)
    shown, pages, page = paginate(rows, page, page_size)
    return {'rows': shown, 'total': len(rows), 'pages': pages, 'page': page, 'counts': counts,
            'hidden_by_period': before - len(rows)}


def _showing_text(page, page_size, shown, total, noun, extra=''):
    if not total:
        return f"No {noun} match{extra}."
    first = (page - 1) * page_size + 1
    return f"Showing {first}–{first + shown - 1} of {total} {noun}{extra}"


def journal_list_items(entries, legacy):
    """Structured entries and legacy notes as one list for daily_obs_view
    (which filters on obs_date and searches 'note'): a structured entry's
    'note' is its searchable text."""
    items = []
    for e in entries or []:
        parts = [e.get('regime'), e.get('effective_limits'), e.get('market'), e.get('lesson_keep'), e.get('lesson'),
                 e.get('execution'), e.get('drift_actions'), e.get('fomo_panic')]
        parts += [w.get('symbol') for w in e.get('watchlist') or []]
        items.append({'kind': 'entry', 'obs_date': e['obs_date'], 'created_at': e.get('created_at') or '',
                      'note': " ".join(str(x) for x in parts if x), 'entry': e})
    for o in legacy or []:
        items.append({'kind': 'legacy', **o})
    return items


def _regime_chip(state, prefix=""):
    if not state:
        return None
    color = {'Full Risk-On': 'success', 'Partial Risk-On': 'success', 'Neutral': 'secondary',
             'Partial Risk-Off': 'warning', 'Full Risk-Off': 'danger'}[state]
    return html.Span(prefix + state, className=f"badge bg-{color} me-1"
                     + (" text-dark" if color == 'warning' else ""))


def _render_journal_entry_row(e):
    eff = e.get('effective_limits')
    tac = e.get('effective_tactics')
    chips = [_regime_chip(e.get('regime'))]
    if e.get('composite') is not None:
        chips.append(html.Span(f"{e['composite']:+.2f}", className="me-2 small"))
    if e.get('delta5') is not None:
        chips.append(html.Span(f"Δ5 {e['delta5']:+.2f}", className="me-2 small text-muted"))
    if eff:
        chips.append(_regime_chip(eff, "EFF "))
        if tac and tac != eff:
            chips.append(html.Small(f"tactics {tac}", className="me-2 text-muted"))
    for f in e.get('flags') or []:
        chips.append(html.Span(f, className="badge bg-warning text-dark me-1"))
    if e.get('heat_pct') is not None:
        ceiling = e.get('heat_ceiling')
        over = ceiling is not None and e['heat_pct'] > ceiling
        chips.append(html.Small(f"heat {e['heat_pct']:.2f}%" + (f" / {ceiling:g}%" if ceiling is not None else ""),
                                className="me-2 " + ("text-danger fw-bold" if over else "text-muted")))
    if e.get('positions') is not None:
        chips.append(html.Small(f"{e['positions']}/{MAX_OPEN_POSITIONS} pos", className="me-2 text-muted"))
    gap = journal_date_gap(e['obs_date'], e.get('created_at'))
    if gap:
        chips.append(html.Span(f"written {abs(gap)}d {'later' if gap > 0 else 'ahead'}",
                               className="badge bg-light text-dark border me-1",
                               title="The entry was first written more than a day from its date."))
    lines = [html.Div(chips)]
    for label, key in (("Market", 'market'), ("Keep", 'lesson_keep'), ("Change", 'lesson'), ("Execution", 'execution')):
        if e.get(key):
            lines.append(html.Div([html.B(label + ": ", className="small"), html.Span(e[key], className="small")]))
    if e.get('watchlist'):
        lines.append(html.Div(html.Small("Watch: " + ", ".join(
            w['symbol'] + (f" ({w['tactic']})" if w.get('tactic') else "") for w in e['watchlist']),
            className="text-muted")))
    return dbc.Row([
        dbc.Col(html.B(e['obs_date']), width=2),
        dbc.Col(lines, width=8),
        dbc.Col(dbc.Button("Open", size='sm', color='outline-primary',
                           id={'type': 'btn-dj-edit', 'index': e['obs_date']}), width=2),
    ], className="mb-2 pb-1 border-bottom")


def _render_daily_observations_list(items):
    if not items:
        return html.P("No entries yet.", className="text-muted small mt-2")
    rows = [dbc.Row([
        dbc.Col(html.B("Date"), width=2), dbc.Col(html.B("Entry"), width=8),
        dbc.Col(html.B(""), width=2),
    ], className="mb-1 mx-0", style=STICKY_HEADER)]
    for o in items:
        if o.get('kind') == 'entry':
            rows.append(_render_journal_entry_row(o['entry']))
            continue
        rows.append(dbc.Row([
            dbc.Col(o['obs_date'], width=2),
            dbc.Col([html.Span("legacy", className="badge bg-light text-muted border me-2"), o['note']], width=8),
            dbc.Col(dbc.ButtonGroup([
                dbc.Button("Edit", size='sm', color='outline-primary',
                           id={'type': 'btn-edit-daily-obs', 'index': o['obs_id']}),
                dbc.Button("×", size='sm', color='outline-danger',
                           id={'type': 'btn-delete-daily-obs', 'index': o['obs_id']}),
            ]), width=2),
        ], className="mb-1"))
    return html.Div(rows)


@app.callback(
    [Output('daily-obs-edit-modal', 'is_open'),
     Output('store-editing-daily-obs-id', 'data'),
     Output('daily-obs-edit-date', 'date'),
     Output('daily-obs-edit-note', 'value'),
     Output('daily-obs-edit-status', 'children')],
    Input({'type': 'btn-edit-daily-obs', 'index': ALL}, 'n_clicks'),
    State('store-daily-observations', 'data'),
    prevent_initial_call=True,
)
def open_daily_obs_edit_modal(n_clicks_list, observations):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    obs_id = triggered['index']
    entry = next((o for o in (observations or []) if o['obs_id'] == obs_id), None)
    if not entry:
        raise PreventUpdate
    return True, obs_id, entry['obs_date'], entry['note'], ''


@app.callback(
    Output('daily-obs-edit-modal', 'is_open', allow_duplicate=True),
    Input('btn-daily-obs-edit-cancel', 'n_clicks'),
    prevent_initial_call=True,
)
def cancel_daily_obs_edit(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return False


@app.callback(
    [Output('store-daily-observations', 'data', allow_duplicate=True),
     Output('daily-obs-edit-modal', 'is_open', allow_duplicate=True),
     Output('daily-obs-edit-status', 'children', allow_duplicate=True)],
    Input('btn-daily-obs-edit-save', 'n_clicks'),
    [State('store-editing-daily-obs-id', 'data'), State('daily-obs-edit-date', 'date'),
     State('daily-obs-edit-note', 'value'), State('store-daily-observations', 'data')],
    prevent_initial_call=True,
)
def save_daily_obs_edit(n_clicks, obs_id, obs_date, note, current):
    if not n_clicks or obs_id is None:
        raise PreventUpdate
    if not note or not note.strip():
        return no_update, True, dbc.Alert("Observation can't be empty.", color='danger')
    db.update_daily_observation(obs_id, note=note, obs_date=obs_date)
    updated = []
    for o in (current or []):
        if o['obs_id'] == obs_id:
            o = dict(o, obs_date=obs_date, note=note.strip())
        updated.append(o)
    return updated, False, ''


@app.callback(
    [Output('daily-obs-list', 'children'), Output('daily-obs-count', 'children'),
     Output('daily-obs-page', 'max_value'), Output('daily-obs-page', 'active_page'),
     Output('daily-obs-page', 'style')],
    [Input('store-daily-observations', 'data'), Input('daily-obs-period', 'value'),
     Input('daily-obs-search', 'value'), Input('daily-obs-page', 'active_page'),
     Input('store-daily-journal', 'data')],
)
def render_daily_observations(observations, period, search, page, entries=None):
    observations = journal_list_items(entries, observations)
    # A new filter or entry starts again at page 1; only the pager itself pages.
    if callback_context.triggered_id != 'daily-obs-page':
        page = 1
    shown, matching, pages, page = daily_obs_view(observations, period, search, page)
    total = len(observations or [])
    if not total:
        count = ''
    elif not matching:
        count = f"No entries match ({total} in all)."
    else:
        count = _showing_text(page, DAILY_OBS_PAGE_SIZE, len(shown), matching, "entries",
                              f" ({total} in all)" if matching != total else "")
    body = _render_daily_observations_list(shown) if shown else \
        html.P("Nothing in this period -- widen it or clear the search." if total else "No entries yet.",
               className="text-muted small mt-2")
    return body, count, pages, page, ({'display': 'none'} if pages <= 1 else {})


@app.callback(
    [Output('dj-modal', 'is_open'), Output('dj-date', 'date'), Output('store-dj-nonce', 'data')],
    [Input('btn-dj-new', 'n_clicks'), Input({'type': 'btn-dj-edit', 'index': ALL}, 'n_clicks')],
    State('store-dj-nonce', 'data'),
    prevent_initial_call=True,
)
def open_daily_journal(_new, _edit, nonce):
    # Re-rendered list buttons can fire with n_clicks 0/None: only a real click opens.
    trig = callback_context.triggered_id
    if not trig or not callback_context.triggered[0].get('value'):
        raise PreventUpdate
    day = trig['index'] if isinstance(trig, dict) else dt.date.today().isoformat()
    return True, day, (nonce or 0) + 1


@app.callback(
    [Output(cid, prop) for cid, prop in DJ_INPUTS] + [Output('dj-loaded', 'children'),
                                                      Output('dj-status', 'children')],
    [Input('dj-date', 'date'), Input('store-dj-nonce', 'data')],
    [State('store-daily-journal', 'data'), State('store-openpos', 'data'),
     State('store-open-positions-prices', 'data'), State('store-cash-equiv-symbols', 'data'),
     State('store-plans', 'data'), State('store-journal', 'data'), State('store-account-snapshots', 'data'),
     State('store-executions', 'data'), State('store-accounts', 'data'), State('store-beta-exposure', 'data'),
     State('store-plan-drift', 'data'), State('store-drift-reconciled', 'data'),
     State('store-stop-checks', 'data')],
    prevent_initial_call=True,
)
def load_daily_journal(day, _nonce, entries, openpos, staged, cash_equiv, plans, journal, snapshots,
                       executions, accounts, beta, drift, reconciled, stop_checks):
    """Fill the form for the chosen date: the stored entry if there is one,
    otherwise a blank template with PORTFOLIO and DRIFT pre-filled (today
    only -- the live figures don't describe a past day)."""
    if not day:
        raise PreventUpdate
    existing = next((e for e in (entries or []) if e['obs_date'] == day), None)
    vals = dj_form_values(existing)
    if existing:
        note = f"Editing the entry for {day} (first written {str(existing.get('created_at'))[:16]} UTC)."
        return vals + [note, '']
    if day == dt.date.today().isoformat():
        idx = {cid: i for i, (cid, _) in enumerate(DJ_INPUTS)}
        pf = journal_portfolio_prefill(openpos, staged, cash_equiv, plans, journal, snapshots, executions,
                                       accounts, beta)
        vals[idx['dj-heat']], vals[idx['dj-positions']], vals[idx['dj-beta']] = \
            pf['heat_pct'], pf['positions'], pf['beta_pct']
        vals[idx['dj-drift']] = ". ".join(t for t in (journal_drift_prefill(drift, reconciled),
                                                     placed_stop_summary(stop_checks)) if t) or None
        fresh = _compute_data_freshness(executions)
        vals[idx['dj-execution']] = "\n".join(t for t in (
            practice_prefill(journal, day, db.fetch_stop_reviews(), fresh[0] if fresh else None),
            closed_without_lesson_text(journal, day)) if t)
        missing =[n for n, v in (("heat (Refresh Live Prices)", pf['heat_pct']),
                                  ("beta (Compute Market Exposure)", pf['beta_pct'])) if v is None]
        note = "New entry for today. Portfolio, drift and practices pre-filled" + (
            f"; not available yet: {', '.join(missing)}." if missing else ".")
        return vals + [note, '']
    return vals + [f"New entry for {day} (no live pre-fill for a past date).", '']


@app.callback(
    [Output('dj-calc', 'children'), Output('dj-watch-check', 'children')],
    [Input('dj-date', 'date'), Input('dj-composite', 'value'), Input('dj-regime', 'value'),
     Input('dj-flags', 'value'), Input('dj-weekly', 'value'), Input('dj-d5', 'value'), Input('dj-d20', 'value'),
     Input('dj-flicker', 'value'), Input('dj-heat', 'value'), Input('dj-positions', 'value')]
    + [Input(f'dj-w{i}-{f}', 'value') for i in range(1, DJ_WATCH_ROWS + 1) for f in ('sym', 'tactic', 'news')],
    [State('store-daily-journal', 'data'), State('store-plans', 'data'), State('store-openpos', 'data'),
     State('store-cash-equiv-symbols', 'data')],
    prevent_initial_call=True,
)
def calc_daily_journal(day, composite, regime, flags, weekly, d5, d20, flicker, heat, positions, *rest):
    watch_vals, (entries, plans, openpos, cash_equiv) = rest[:3 * DJ_WATCH_ROWS], rest[3 * DJ_WATCH_ROWS:]
    if not day:
        raise PreventUpdate
    calc = compute_effective_regime(journal_history(entries), day, _num_or_none(composite), regime, flags, weekly,
                                    _num_or_none(d5), _num_or_none(d20),
                                    None if (flicker or 'auto') == 'auto' else flicker == 'Y')
    return render_regime_calc(calc, _num_or_none(heat), _num_or_none(positions)), \
        render_watch_check(check_watchlist(
            [{'symbol': watch_vals[3 * i], 'tactic': watch_vals[3 * i + 1], 'news': bool(watch_vals[3 * i + 2])}
             for i in range(DJ_WATCH_ROWS)], plans, openpos, cash_equiv, calc.get('tactics_state')))


def render_regime_calc(calc, heat=None, positions=None):
    def fmt_delta(n):
        v, src, ref = calc[f'delta{n}'], calc[f'delta{n}_src'], calc[f'delta{n}_ref']
        if v is None:
            return f"Δ{n} n/a (no entry ~{n} sessions back -- type it if you know it)"
        return f"Δ{n} {v:+.2f}" + (f" (vs {ref})" if src == 'auto' else " (typed)")
    flick = calc['flicker']
    flick_txt = ("Flicker: " + ("YES" if flick else "no") + (" (typed)" if calc['flicker_src'] == 'manual' else "")
                 if flick is not None else f"Flicker: unknown ({calc['labels_logged']} of 5 labels logged in 10 sessions)")
    lines = [html.Div(f"{fmt_delta(5)} · {fmt_delta(20)} · {flick_txt} · direction: {calc['direction']}",
                      className="small")]
    if calc.get('limits_state'):
        lim = calc['limits']
        head = [html.B("EFFECTIVE → "), _regime_chip(calc['limits_state'], "limits ")]
        if calc['tactics_state'] != calc['limits_state']:
            head.append(_regime_chip(calc['tactics_state'], "tactics "))
        lines.append(html.Div(head, className="mt-1"))
        lim_txt = (f"Risk {lim['risk']:g}% · heat ceiling {lim['heat']:g}% · max position {lim['max_pos']:g}% · "
                   if lim['heat'] is not None else "")
        lines.append(html.Div(lim_txt + "Tactics: " + calc['tactics_text'], className="small"))
        if heat is not None and lim['heat'] is not None and heat > lim['heat']:
            lines.append(html.Div(f"Heat {heat:.2f}% is over the {lim['heat']:g}% ceiling: no new trades or adds.",
                                  className="small text-danger fw-bold"))
    if positions is not None and positions >= MAX_OPEN_POSITIONS:
        lines.append(html.Div(f"{int(positions)} open positions: at the {MAX_OPEN_POSITIONS}-position limit.",
                              className="small text-danger fw-bold"))
    for r in calc['reasons']:
        lines.append(html.Div(r, className="small text-muted"))
    return dbc.Alert(lines, color='light', className="py-2 mb-0 border")


def render_watch_check(results):
    if not results:
        return None
    return html.Ul([html.Li([html.B(r['symbol'] + ": "), "; ".join(r['notes']) or "ok"],
                            className="small " + ("text-danger" if r['level'] == 'warn' else "text-muted"))
                    for r in results], className="mb-0")


@app.callback(
    [Output('store-daily-journal', 'data', allow_duplicate=True), Output('dj-status', 'children', allow_duplicate=True),
     Output('dj-loaded', 'children', allow_duplicate=True)],
    Input('btn-dj-save', 'n_clicks'),
    [State('dj-date', 'date')] + [State(cid, prop) for cid, prop in DJ_INPUTS] + [State('store-daily-journal', 'data')],
    prevent_initial_call=True,
)
def save_daily_journal(n_clicks, day, *rest):
    if not n_clicks:
        raise PreventUpdate
    vals, entries = list(rest[:len(DJ_INPUTS)]), rest[len(DJ_INPUTS)]
    if not day:
        return no_update, dbc.Alert("Pick a date.", color='danger', className="py-1"), no_update
    entry, calc = dj_entry_from_values(day, vals, journal_history(entries))
    if entry['composite'] is None or not entry['regime']:
        return no_update, dbc.Alert("REGIME needs at least the composite and the label.", color='danger',
                                    className="py-1"), no_update
    saved = db.save_daily_journal_entry(entry)
    new_entries = [e for e in (entries or []) if e['obs_date'] != day] + [saved]
    new_entries.sort(key=lambda e: e['obs_date'], reverse=True)
    msgs = [f"Saved {day}. Effective: limits {calc['limits_state']}, tactics {calc['tactics_state']}."]
    gap = journal_date_gap(day, saved.get('created_at'))
    if gap:
        msgs.append(f"Note: this entry was first written {abs(gap)} days {'after' if gap > 0 else 'before'} "
                    f"its date -- it's marked in the list.")
    return new_entries, dbc.Alert(" ".join(msgs), color='warning' if gap else 'success', className="py-1"), \
        f"Editing the entry for {day} (first written {str(saved.get('created_at'))[:16]} UTC)."


@app.callback(
    [Output('store-daily-journal', 'data', allow_duplicate=True), Output('dj-modal', 'is_open', allow_duplicate=True)],
    Input('btn-dj-delete', 'n_clicks'),
    [State('dj-date', 'date'), State('store-daily-journal', 'data')],
    prevent_initial_call=True,
)
def delete_daily_journal(n_clicks, day, entries):
    if not n_clicks or not day or not any(e['obs_date'] == day for e in (entries or [])):
        raise PreventUpdate
    db.delete_daily_journal_entry(day)
    return [e for e in entries if e['obs_date'] != day], False


@app.callback(
    Output('dj-modal', 'is_open', allow_duplicate=True),
    Input('btn-dj-close', 'n_clicks'),
    prevent_initial_call=True,
)
def close_daily_journal(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return False


@app.callback(
    [Output('wk-modal', 'is_open'), Output('weekly-week-ending', 'date', allow_duplicate=True),
     Output('store-wk-nonce', 'data')],
    [Input('btn-wk-new', 'n_clicks'), Input({'type': 'btn-wk-open', 'index': ALL}, 'n_clicks')],
    [State('weekly-week-ending', 'date'), State('store-wk-nonce', 'data')],
    prevent_initial_call=True,
)
def open_weekly_entry(_new, _open, week, nonce):
    trig = callback_context.triggered_id
    if not trig or not callback_context.triggered[0].get('value'):
        raise PreventUpdate
    week = trig['index'] if isinstance(trig, dict) else week
    if not week:
        raise PreventUpdate
    return True, week, (nonce or 0) + 1


@app.callback(
    [Output(cid, prop) for cid, prop in WK_INPUTS]
    + [Output('wk-head', 'children'), Output('wk-loaded', 'children'), Output('wk-results', 'children'),
       Output('wk-actions', 'children'), Output('wk-lessons', 'children'), Output('store-wk-lessons', 'data'),
       Output('wk-status', 'children')],
    Input('store-wk-nonce', 'data'),
    [State('weekly-week-ending', 'date'), State('store-weekly-reviews', 'data'),
     State('store-weekly-balance-staged', 'data'), State('store-daily-journal', 'data'),
     State('store-weekly-actions', 'data'), State('store-journal', 'data'),
     State('store-openpos', 'data'), State('store-open-positions-prices', 'data'),
     State('store-cash-equiv-symbols', 'data'), State('store-plans', 'data'),
     State('store-account-snapshots', 'data'), State('store-executions', 'data'),
     State('store-accounts', 'data'), State('store-beta-exposure', 'data')],
    prevent_initial_call=True,
)
def load_weekly_entry(_nonce, week, reviews, staged_balance, daily_entries, actions, journal, openpos, staged,
                      cash_equiv, plans, snapshots, executions, accounts, beta):
    """Fill the weekly form: the saved entry if there is one, otherwise a
    template pre-filled from the week's last daily journal entry and (for
    the current week) the live portfolio figures."""
    if not week:
        raise PreventUpdate
    row = next((r for r in reviews or [] if r['week_ending'] == week), None)
    saved = db.decode_weekly_entry(row) if row and row.get('entry_saved_at') else None
    lessons_now = weekly_lessons(daily_entries, week)
    if saved:
        vals = wk_form_values(saved)
        lessons = merge_lessons(saved.get('lesson_decisions'), lessons_now)
        note = f"Editing the saved entry (saved {str(saved['entry_saved_at'])[:16]} UTC)."
    else:
        daily = latest_daily_entry(daily_entries, week) or {}
        pre = {'weekly_regime': daily.get('weekly_regime'), 'daily_regime': daily.get('regime'),
               'daily_composite': daily.get('composite'), 'flags': daily.get('flags'),
               'watchlist': daily.get('watchlist')}
        notes = [f"New entry. Regime and watchlist pre-filled from the daily entry of {daily['obs_date']}."
                 if daily else "New entry. No daily journal entry this week to pre-fill the regime from."]
        if (dt.date.today() - _as_date(week)).days < 7:
            pf = journal_portfolio_prefill(openpos, staged, cash_equiv, plans, journal, snapshots, executions,
                                           accounts, beta)
            pre.update({'heat_pct': pf['heat_pct'], 'positions': pf['positions'], 'beta_pct': pf['beta_pct']})
            missing = [n for n, v in (("heat (Refresh Live Prices)", pf['heat_pct']),
                                      ("beta (Compute Market Exposure)", pf['beta_pct'])) if v is None]
            notes.append("Book pre-filled from today's figures" + (f"; not available yet: {', '.join(missing)}."
                                                                    if missing else "."))
        vals = wk_form_values(pre)
        lessons = merge_lessons([], lessons_now)
        note = " ".join(notes)
    return vals + [render_weekly_head(week, weekly_balance_delta(reviews, week, staged_balance)), note,
                   render_weekly_results(weekly_results(journal, week)),
                   render_action_rows(actions_for_week(actions, week, action_window_start(reviews, week)), week,
                                      action_window_start(reviews, week)),
                   render_lesson_rows(lessons), lessons, '']


@app.callback(
    [Output('wk-posture', 'children'), Output('wk-book-check', 'children'), Output('wk-watch-check', 'children')],
    [Input('wk-weekly-regime', 'value'), Input('wk-daily-regime', 'value'), Input('wk-daily-composite', 'value'),
     Input('wk-d20', 'value'), Input('wk-flags', 'value'), Input('wk-heat', 'value'),
     Input('wk-positions', 'value'), Input('wk-beta', 'value'), Input('wk-theme-pct', 'value')]
    + [Input(f'wk-w{i}-{f}', 'value') for i in range(1, WK_WATCH_ROWS + 1) for f in ('sym', 'tactic')],
    [State('weekly-week-ending', 'date'), State('store-daily-journal', 'data'), State('store-plans', 'data'),
     State('store-openpos', 'data'), State('store-cash-equiv-symbols', 'data')],
    prevent_initial_call=True,
)
def calc_weekly_entry(weekly, daily, composite, d20, flags, heat, positions, beta, theme_pct, *rest):
    watch, (week, daily_entries, plans, openpos, cash_equiv) = rest[:2 * WK_WATCH_ROWS], rest[2 * WK_WATCH_ROWS:]
    if not week:
        raise PreventUpdate
    v = {'wk-weekly-regime': weekly, 'wk-daily-regime': daily, 'wk-daily-composite': composite, 'wk-d20': d20,
         'wk-flags': flags}
    posture = wk_posture(week, v, daily_entries)
    book = []
    beta_cap = {'Full Risk-On': 100, 'Partial Risk-On': 100, 'Neutral': 60, 'Partial Risk-Off': 30}.get(
        posture.get('limits_state'))
    beta, theme_pct = _num_or_none(beta), _num_or_none(theme_pct)
    if beta is not None and beta_cap is not None and beta > beta_cap:
        book.append(f"Beta {beta:g}% is above the {beta_cap}% soft ceiling for {posture['limits_state']} (S6.6).")
    if theme_pct is not None and theme_pct > 25:
        book.append(f"Largest theme {theme_pct:g}% is above the 25% concentration limit (S6.5).")
    return (render_regime_calc(posture, _num_or_none(heat), _num_or_none(positions)),
            html.Ul([html.Li(b, className="small text-danger") for b in book], className="mb-0") if book else None,
            render_watch_check(check_watchlist(
                [{'symbol': watch[2 * i], 'tactic': watch[2 * i + 1]} for i in range(WK_WATCH_ROWS)],
                plans, openpos, cash_equiv, posture.get('tactics_state'))))


@app.callback(
    [Output('store-weekly-reviews', 'data', allow_duplicate=True),
     Output('store-weekly-actions', 'data', allow_duplicate=True),
     Output('wk-status', 'children', allow_duplicate=True)],
    Input('btn-wk-save', 'n_clicks'),
    [State('weekly-week-ending', 'date')] + [State(cid, prop) for cid, prop in WK_INPUTS]
    + [State({'type': 'wk-action-status', 'index': ALL}, 'value'),
       State({'type': 'wk-action-status', 'index': ALL}, 'id'),
       State({'type': 'wk-action-note', 'index': ALL}, 'value'),
       State({'type': 'wk-lesson-decision', 'index': ALL}, 'value'),
       State({'type': 'wk-lesson-note', 'index': ALL}, 'value'),
       State('store-wk-lessons', 'data'), State('store-daily-journal', 'data'), State('store-journal', 'data'),
       State('store-weekly-balance-staged', 'data'), State('store-weekly-reviews', 'data')],
    prevent_initial_call=True,
)
def save_weekly_entry(n_clicks, week, *rest):
    if not n_clicks or not week:
        raise PreventUpdate
    vals = list(rest[:len(WK_INPUTS)])
    (a_status, a_ids, a_notes, l_decisions, l_notes, lessons, daily_entries, journal, staged_balance,
     reviews) = rest[len(WK_INPUTS):]
    missing_why = [f"A{i['index']}" for s_, i, n in zip(a_status, a_ids, a_notes)
                   if s_ in ('not_done', 'dropped') and not (n or '').strip()]
    if missing_why:
        return no_update, no_update, dbc.Alert(f"Say why for {', '.join(missing_why)} (not done or dropped).",
                                               color='danger', className="py-1")
    decisions = [dict(l, decision=d or '', note=(n or '').strip() or None)
                 for l, d, n in zip(lessons or [], l_decisions, l_notes)]
    entry, posture = wk_entry_from_values(week, vals, daily_entries, journal, decisions)
    fields = db.weekly_entry_fields(entry)
    fields['entry_saved_at'] = db.utc_now().isoformat(timespec='seconds')
    fields.update(_staged_balance_fields(staged_balance))
    row = db.save_weekly_review(week, **fields)
    reviewed = 0
    since = action_window_start(reviews, week)
    current = {a['action_id']: a for a in db.fetch_weekly_actions()}
    for status, ident, note in zip(a_status, a_ids, a_notes):
        if status:
            note = (note or '').strip() or None
            a = current.get(ident['index'], {})
            if not (reviewed_in_window(a, week, since) and a.get('status') == status
                    and (a.get('status_note') or None) == note):
                db.review_weekly_action(ident['index'], week, status, note)
            reviewed += 1
    reviews = sorted([r for r in reviews or [] if r['week_ending'] != week] + [row],
                     key=lambda r: r['week_ending'], reverse=True)
    pending = sum(1 for s_ in a_status if not s_)
    msg = (f"Saved the entry for {week}. Posture: limits {posture.get('limits_state') or '—'}, "
           f"tactics {posture.get('tactics_state') or '—'}. {reviewed} action(s) reviewed"
           + (f", {pending} still to review." if pending else "."))
    return reviews, db.fetch_weekly_actions(), dbc.Alert(msg, color='warning' if pending else 'success',
                                                        className="py-1")


@app.callback(
    Output('wk-modal', 'is_open', allow_duplicate=True),
    Input('btn-wk-close', 'n_clicks'),
    prevent_initial_call=True,
)
def close_weekly_entry(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return False


def cash_from_snapshots(snapshots, executions, account_ids, now=None):
    """Each account's cash from its latest holdings export (the only cash
    source until a cash ledger exists), with what makes it less trustworthy:
    - missing: accounts with no export at all (their cash isn't counted);
    - traded_since: {account: fills after its export} -- that cash may already
      be spent (or new sale proceeds not in it yet);
    - stale: accounts whose export is over ACCOUNT_SNAPSHOT_STALE_DAYS old.
    by_currency sums every account's cash per currency (no FX)."""
    out = {'by_currency': {}, 'by_account': {}, 'as_of': {}, 'missing': [], 'traded_since': {}, 'stale': []}
    fills = pd.DataFrame(executions or [])
    if not fills.empty:
        fills['ts'] = pd.to_datetime(fills['timestamp'], utc=True, errors='coerce')
    for acct in account_ids:
        snap = (snapshots or {}).get(acct)
        if not snap:
            out['missing'].append(acct)
            continue
        out['by_account'][acct] = dict(snap['cash'])
        out['as_of'][acct] = snap['as_of']
        for ccy, amt in snap['cash'].items():
            out['by_currency'][ccy] = out['by_currency'].get(ccy, 0.0) + amt
        as_of = pd.Timestamp(snap['as_of'])
        as_of = as_of.tz_localize(MARKET_CLOSE_TZ) if as_of.tz is None else as_of
        if not fills.empty and 'account_id' in fills:
            n = int(((fills['account_id'] == acct) & (fills['ts'] > as_of.tz_convert('UTC'))).sum())
            if n:
                out['traded_since'][acct] = n
        if _snapshot_age_days(snap['as_of'], now) > ACCOUNT_SNAPSHOT_STALE_DAYS:
            out['stale'].append(acct)
    return out


def portfolio_with_cash(openpos, staged, snapshots, executions, accounts):
    """The portfolio's value with cash (2026-10-17): open positions at the
    staged live prices plus each account's cash from its latest holdings
    export, in CAD when every currency has a rate. The one definition of
    "portfolio value" used by the weekly balance, portfolio heat, the
    pre-trade check and beta exposure, so all of them stay comparable.
    Returns {value, fx (None = summed as-is), positions, combined (per
    currency), cash (cash_from_snapshots' result), account_values (per
    account, positions + cash)}."""
    staged = staged or {}
    positions = staged.get('by_currency') or {}
    ids = [a['account_id'] for a in (accounts or [])] or list((snapshots or {}).keys())
    cash = cash_from_snapshots(snapshots, executions, ids)
    combined = dict(positions)
    for ccy, amt in cash['by_currency'].items():
        combined[ccy] = combined.get(ccy, 0.0) + amt
    fx = usable_fx(staged.get('fx'), combined) if combined else None
    value = to_base(combined, fx) if fx else sum(combined.values())
    acct_values = account_values(openpos, staged.get('priced_symbols') or {}, fx=fx)
    for acct, amounts in cash['by_account'].items():
        acct_values[acct] = acct_values.get(acct, 0.0) + (to_base(amounts, fx) if fx else sum(amounts.values()))
    return {'value': value, 'fx': fx, 'positions': positions, 'combined': combined, 'cash': cash,
            'account_values': acct_values}


def cash_warnings(cash, accounts):
    """Plain-language reasons the included cash may be off (see cash_from_snapshots)."""
    out = []
    if cash['missing']:
        out.append(f"No holdings export for {', '.join(account_name(accounts, a) for a in cash['missing'])} "
                   f"-- its cash isn't included. Upload one in Weekly Reconciliation.")
    if cash['traded_since']:
        out.append("Traded since the holdings export in "
                   + ", ".join(f"{account_name(accounts, a)} ({n} fill{'s' if n != 1 else ''})"
                               for a, n in cash['traded_since'].items())
                   + " -- its cash may be out of date. Upload a fresh holdings export.")
    if cash['stale']:
        out.append(f"Holdings export over {ACCOUNT_SNAPSHOT_STALE_DAYS} days old for "
                   f"{', '.join(account_name(accounts, a) for a in cash['stale'])} -- its cash may be out of date.")
    return out


def cash_dates_text(cash):
    dates = sorted(set(_fmt_as_of(a) for a in cash['as_of'].values()))
    return f"holdings export {', '.join(dates)}" if dates else "no holdings export"


def balance_basis(row):
    """What a weekly balance was measured as: (currency or None for the old
    mixed sum, whether it includes cash). Week-over-week change is only
    computed between two rows on the same basis."""
    try:
        detail = json.loads(row.get('balance_detail_json') or '{}')
    except (TypeError, ValueError):
        detail = {}
    return (row.get('balance_currency') or None, isinstance(detail, dict) and 'cash' in detail)


def weekly_balance_changes(reviews_oldest_first):
    """{week_ending: change vs. the previous week with a balance}, or
    'basis_changed' when the two were saved on a different basis (see
    balance_basis: converted vs. summed as-is, or with vs. without cash) --
    that difference would be the method, not a real change."""
    out, prev = {}, None
    for r in reviews_oldest_first:
        if r.get('balance') is None:
            continue
        if prev is not None:
            same = balance_basis(prev) == balance_basis(r)
            out[r['week_ending']] = r['balance'] - prev['balance'] if same else 'basis_changed'
        prev = r
    return out


def _render_weekly_reviews_list(reviews):
    if not reviews:
        return html.P("No weeks logged yet.", className="text-muted small")
    # reviews arrive newest-first (db.fetch_weekly_reviews); diff each week's
    # balance against the one immediately before it chronologically, then
    # display newest-first as usual.
    by_date_asc = sorted(reviews, key=lambda r: r['week_ending'])
    change_by_week = weekly_balance_changes(by_date_asc)

    header = dbc.Row([
        dbc.Col(html.B("Week Ending"), width=2),
        dbc.Col(html.B(["Balance", help_icon(
            f"Mark-to-market total across all open positions (no cash), converted to "
            f"{BASE_CURRENCY} at the Bank of Canada daily rate. Weeks saved before "
            f"2026-10-08 are marked (mixed): they summed USD and CAD as-is. Change is only "
            f"shown between weeks on the same basis.")]), width=2),
        dbc.Col(html.B("Change"), width=1), dbc.Col(html.B("Your Note"), width=3),
        dbc.Col(html.B("LLM Note"), width=2), dbc.Col(html.B(""), width=2),
    ], className="mb-1")
    rows = [header]
    for r in reviews:
        change = change_by_week.get(r['week_ending'])
        if change == 'basis_changed':
            change_disp = html.Span("n/a", className="text-muted",
                                    title="The previous week was measured differently (mixed "
                                          "currencies vs. converted, or without vs. with cash), so "
                                          "the difference wouldn't be a real change.")
        elif change is None:
            change_disp = html.Span("—", className="text-muted")
        else:
            color = 'success' if change >= 0 else 'danger'
            change_disp = html.Span(f"{change:+,.2f}", className=f"badge bg-{color}")
        if r.get('balance') is None:
            balance_disp = "—"
        elif r.get('balance_currency'):
            balance_disp = (f"{r['balance_currency']} {r['balance']:,.2f}" if balance_basis(r)[1] else
                            html.Span([f"{r['balance_currency']} {r['balance']:,.2f} ",
                                       html.Small("(no cash)", className="text-muted")]))
        else:
            balance_disp = html.Span([f"{r['balance']:,.2f} ", html.Small("(mixed)", className="text-muted")])
        rows.append(dbc.Row([
            dbc.Col(r['week_ending'], width=2),
            dbc.Col(balance_disp, width=2),
            dbc.Col(change_disp, width=1),
            # Previews only -- full text (and edit) is behind the "View/Edit"
            # button, since a weekly note or a pasted coaching response can
            # run well past what's readable truncated to 80 chars.
            dbc.Col(html.Small((r.get('user_note') or '')[:80], className="text-muted"), width=3),
            dbc.Col(html.Small((r.get('llm_note') or '')[:80], className="text-muted"), width=2),
            dbc.Col(dbc.ButtonGroup([
                dbc.Button("Entry", size='sm', color='success' if r.get('entry_saved_at') else 'outline-success',
                           id={'type': 'btn-wk-open', 'index': r['week_ending']},
                           title="Weekly template" + (" (saved)" if r.get('entry_saved_at') else " (not filled in)")),
                dbc.Button("View/Edit", size='sm', color='outline-primary',
                           id={'type': 'btn-edit-weekly-review', 'index': r['week_ending']}),
                dbc.Button("×", size='sm', color='outline-danger',
                           id={'type': 'btn-delete-weekly-review', 'index': r['week_ending']}),
            ]), width=2),
        ], className="mb-1"))
    return html.Div(rows)


@app.callback(
    Output('weekly-reviews-list', 'children'),
    Input('store-weekly-reviews', 'data'),
)
def render_weekly_reviews_list(reviews):
    return _render_weekly_reviews_list(reviews)


@app.callback(
    Output('store-weekly-reviews', 'data', allow_duplicate=True),
    Input({'type': 'btn-delete-weekly-review', 'index': ALL}, 'n_clicks'),
    State('store-weekly-reviews', 'data'),
    prevent_initial_call=True,
)
def delete_weekly_review_cb(n_clicks_list, reviews):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    week_ending = triggered['index']
    db.delete_weekly_review(week_ending)
    return [r for r in (reviews or []) if r['week_ending'] != week_ending]


@app.callback(
    [Output('weekly-review-edit-modal', 'is_open'),
     Output('store-editing-weekly-review-week', 'data'),
     Output('weekly-review-edit-week-display', 'children'),
     Output('weekly-review-edit-balance', 'value'),
     Output('weekly-review-edit-user-note', 'value'),
     Output('weekly-review-edit-llm-note', 'value'),
     Output('weekly-review-edit-status', 'children')],
    Input({'type': 'btn-edit-weekly-review', 'index': ALL}, 'n_clicks'),
    State('store-weekly-reviews', 'data'),
    prevent_initial_call=True,
)
def open_weekly_review_edit_modal(n_clicks_list, reviews):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    week_ending = triggered['index']
    entry = next((r for r in (reviews or []) if r['week_ending'] == week_ending), None)
    if not entry:
        raise PreventUpdate
    return (True, week_ending, week_ending, entry.get('balance'),
            entry.get('user_note') or '', entry.get('llm_note') or '', '')


@app.callback(
    Output('weekly-review-edit-modal', 'is_open', allow_duplicate=True),
    Input('btn-weekly-review-edit-cancel', 'n_clicks'),
    prevent_initial_call=True,
)
def cancel_weekly_review_edit(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return False


@app.callback(
    [Output('store-weekly-reviews', 'data', allow_duplicate=True),
     Output('weekly-review-edit-modal', 'is_open', allow_duplicate=True),
     Output('weekly-review-edit-status', 'children', allow_duplicate=True),
     Output('store-weekly-actions', 'data', allow_duplicate=True),
     Output('weekly-save-status', 'children', allow_duplicate=True)],
    Input('btn-weekly-review-edit-save', 'n_clicks'),
    [State('store-editing-weekly-review-week', 'data'), State('weekly-review-edit-balance', 'value'),
     State('weekly-review-edit-user-note', 'value'), State('weekly-review-edit-llm-note', 'value'),
     State('store-weekly-reviews', 'data')],
    prevent_initial_call=True,
)
def save_weekly_review_edit(n_clicks, week_ending, balance, user_note, llm_note, reviews):
    if not n_clicks or not week_ending:
        raise PreventUpdate
    fields = {
        'balance': float(balance) if balance not in (None, '') else None,
        'user_note': user_note or '',
        'llm_note': llm_note or '',
    }
    updated_row = db.save_weekly_review(week_ending, **fields)
    reviews = [updated_row if r['week_ending'] == week_ending else r for r in (reviews or [])]
    actions, msg = store_focus_actions(week_ending, llm_note)
    status = dbc.Alert(f"Week ending {week_ending} updated. {msg}", color='info') if msg else no_update
    return reviews, False, '', actions, status


@app.callback(
    [Output('weekly-balance-display', 'children'),
     Output('store-weekly-balance-staged', 'data')],
    Input('btn-weekly-compute-balance', 'n_clicks'),
    [State('store-openpos', 'data'), State('store-accounts', 'data'),
     State('store-account-snapshots', 'data'), State('store-executions', 'data')],
    prevent_initial_call=True,
)
def compute_weekly_balance_cb(n_clicks, openpos, accounts, snapshots, executions):
    """Balance = open positions at live prices + each account's cash from its
    latest holdings export (2026-10-17; before this, cash wasn't counted),
    converted to CAD when there's a rate for every currency."""
    if not n_clicks:
        raise PreventUpdate
    total, priced, errors = compute_mark_to_market_balance(openpos)
    staged = stage_prices(openpos, total, priced, errors)
    pw = portfolio_with_cash(openpos, staged, snapshots, executions, accounts)
    value, fx, positions, combined, cash = pw['value'], pw['fx'], pw['positions'], pw['combined'], pw['cash']
    by_account = pw['account_values']
    unit = f"{BASE_CURRENCY} " if fx else ""
    content = [html.Span(f"{unit}{value:,.2f}", className="fw-bold fs-5"),
               html.Div(f"Positions {_fmt_ccy_amounts(positions)} + cash {_fmt_ccy_amounts(cash['by_currency'])} "
                        f"({cash_dates_text(cash)})", className="text-muted small")]
    if fx:
        content.append(html.Div(f"Converted at {fmt_fx_note(fx, combined)}", className="text-muted small"))
    else:
        content.append(html.Div("Mixed currencies, not FX-converted (no rate available).",
                                className="text-muted small"))
    if len(by_account) > 1:
        content.append(html.Div(" · ".join(f"{account_name(accounts, a)}: {unit}{v:,.2f}"
                                           for a, v in sorted(by_account.items())),
                                className="text-muted small"))
    warnings = cash_warnings(cash, accounts)
    if errors:
        warnings.append(f"Couldn't price: {', '.join(errors.keys())} (excluded from total)")
    content.extend(html.Div(w, className="text-warning small mt-1") for w in warnings)
    return html.Div(content), {'total': value, 'currency': BASE_CURRENCY if fx else None,
                               'by_currency': combined, 'positions_by_currency': positions,
                               'cash_by_currency': cash['by_currency'], 'cash_as_of': cash['as_of'],
                               'cash_missing_accounts': cash['missing'], 'fx': fx, 'priced_symbols': priced,
                               'errors': errors, 'by_account': by_account}


@app.callback(
    [Output('store-open-positions-prices', 'data'), Output('open-positions-price-status', 'children')],
    Input('btn-refresh-open-prices', 'n_clicks'),
    State('store-openpos', 'data'),
    prevent_initial_call=True,
)
def refresh_open_position_prices_cb(n_clicks, openpos):
    """Same underlying fetch as the Weekly Review's "Compute Balance" (reuses
    compute_mark_to_market_balance), triggered independently from the Open
    Positions card so refreshing one doesn't require touching the other."""
    if not n_clicks:
        raise PreventUpdate
    total, priced, errors = compute_mark_to_market_balance(openpos)
    status = ''
    if errors:
        status = dbc.Alert(f"Couldn't price: {', '.join(errors.keys())} (excluded from Current R:R "
                            f"and total risk)", color='warning', className="py-1 mb-0")
    return stage_prices(openpos, total, priced, errors), status


def render_beta_exposure(total, total_pct, per_symbol, per_account, portfolio_value, fx, accounts,
                         freshness_note=""):
    """The Compute Market Exposure banner: the % of portfolio as the headline,
    then each account's and each position's beta-weighted exposure with its %
    of the portfolio (2026-10-31: the % used to sit mid-sentence, and
    positions weren't shown at all)."""
    money = (lambda v: f"{BASE_CURRENCY} {v:,.2f}") if fx else (lambda v: f"{v:,.2f}")
    pct = lambda v: f"{v / portfolio_value * 100:+.1f}%" if portfolio_value else "n/a"  # noqa: E731
    rows = []
    for key, d in (per_symbol or {}).items():
        sym, _, ccy = str(key).partition('|')
        rate = (fx_rate(fx, ccy) or 1.0) if fx else 1.0
        weighted = d['market_value'] * d['beta'] * rate
        rows.append((sym, ccy, d['beta'], d['market_value'], weighted))
    rows.sort(key=lambda r: abs(r[4]), reverse=True)
    table = dbc.Table(
        [html.Thead(html.Tr([html.Th("Position"), html.Th("Beta", className="text-end"),
                             html.Th("Market value", className="text-end"),
                             html.Th("Beta-weighted", className="text-end"),
                             html.Th("% of portfolio", className="text-end")])),
         html.Tbody([html.Tr([html.Td(sym), html.Td(f"{b:.2f}", className="text-end"),
                              html.Td(f"{ccy} {mv:,.2f}", className="text-end"),
                              html.Td(money(w), className="text-end"), html.Td(pct(w), className="text-end")])
                     for sym, ccy, b, mv, w in rows])],
        size='sm', borderless=True, className="small mb-1 mt-2", style={'--bs-table-bg': 'transparent'}) \
        if rows else None
    acct_lines = [html.Li(f"{account_name(accounts, a)}: {money(v)} ({pct(v)} of portfolio)")
                  for a, v in sorted((per_account or {}).items(), key=lambda kv: account_name(accounts, kv[0]) or '')]
    headline = (f"Beta exposure: {total_pct:.1f}% of portfolio" if total_pct is not None
                else "Beta exposure: n/a (no portfolio value)")
    return dbc.Alert([
        html.Div([html.B(headline, className="fs-6"),
                  html.Span(f"  ({money(total)} beta-weighted, all accounts)")]),
        html.Ul(acct_lines, className="small mb-0 mt-1") if len(acct_lines) > 1 else None,
        table,
        html.Small(
            f"Each position: market value × beta vs. {BETA_BENCHMARK_SYMBOL} over the trailing "
            f"{BETA_LOOKBACK_DAYS} trading days. % is of the whole portfolio (positions + cash, all accounts). "
            f"Cash-equivalents are left out."
            + (f" Converted at {fmt_fx_note(fx)}." if fx else
               " Summed across currencies as-is (no FX rate available), so approximate.")
            + freshness_note, className="d-block text-muted"),
    ], color='info', className="py-2 mb-0")


@app.callback(
    [Output('store-beta-exposure', 'data'), Output('beta-exposure-banner', 'children'),
     Output('beta-exposure-status', 'children')],
    Input('btn-compute-beta-exposure', 'n_clicks'),
    [State('store-openpos', 'data'), State('store-open-positions-prices', 'data'),
     State('store-cash-equiv-symbols', 'data'), State('store-accounts', 'data'),
     State('store-account-snapshots', 'data'), State('store-executions', 'data')],
    prevent_initial_call=True,
)
def compute_beta_exposure_cb(n_clicks, openpos, staged_prices, cash_equiv_symbols, accounts,
                             snapshots=None, executions=None):
    """Manual-trigger, same "requires a live-prices refresh first" guard as
    the portfolio-risk banner uses implicitly -- beta exposure needs the same
    priced_symbols/portfolio_value staged_prices already provides, reused
    rather than re-fetched. No color-coded threshold here (unlike the risk
    banner above): no concentration ceiling has been agreed on, so this is
    presented as a neutral measurement, not a warning."""
    if not n_clicks:
        raise PreventUpdate
    if not staged_prices or not staged_prices.get('priced_symbols'):
        return no_update, no_update, dbc.Alert(
            'Click "Refresh Live Prices" above first.', color='warning', className="py-1 mb-0")

    pw = portfolio_with_cash(openpos, staged_prices, snapshots, executions, accounts)
    portfolio_value, fx = pw['value'], pw['fx']
    total_exposure, total_exposure_pct, per_symbol, errors, betas_as_of, per_account = \
        compute_portfolio_beta_exposure(openpos, staged_prices['priced_symbols'], cash_equiv_symbols,
                                        portfolio_value, fx=fx)

    freshness_note = ""
    if betas_as_of:
        computed_at = pd.to_datetime(betas_as_of, utc=True, errors='coerce')
        if pd.notna(computed_at):
            age_hours = (pd.Timestamp.now(tz='UTC') - computed_at).total_seconds() / 3600
            freshness_note = f" Betas as of {_format_age(age_hours)} ago (oldest contributor)."

    banner = render_beta_exposure(total_exposure, total_exposure_pct, per_symbol, per_account, portfolio_value,
                                  fx, accounts, freshness_note)

    status = ''
    if errors:
        status = dbc.Alert(
            f"Excluded from exposure: {', '.join(f'{sym} ({msg})' for sym, msg in errors.items())}",
            color='warning', className="py-1 mb-0")

    return {'total_exposure': total_exposure, 'total_exposure_pct': total_exposure_pct,
            'currency': BASE_CURRENCY if fx else None,
            'per_symbol': per_symbol, 'errors': errors, 'betas_as_of': betas_as_of,
            'per_account': {str(k): v for k, v in per_account.items()}}, banner, status


# ---- Stop review: the pending orders on a Wealthsimple screenshot vs the drift report ----
# Wealthsimple has no API, and TAPEs otherwise can't see the stop actually placed.
# The user pastes a screenshot of the "Pending orders" list; Windows' built-in OCR
# reads it (tools/winocr.ps1 -- no new dependency or service), the orders land in
# an editable table to correct, and Check compares them with the report's
# "Set Stop To". Long only (Strategy F-rules), so every stop is a sell stop.

WINOCR_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools', 'winocr.ps1')
STOP_REVIEW_TOLERANCE = 0.005  # half a cent: a placed stop this close to the plan's counts as equal
# Not anchored at the end: OCR sometimes joins the order type onto the same line
# ("44 X $43.59 USD Stop limit sell") and splits a currency ("U SD").
_ORDER_LINE_RE = re.compile(r'^\s*([\d,]+(?:\.\d+)?)\s*[x×X]\s*\$?\s*([\d,]+(?:\.\d+)?)\s*'
                            r'(U\s?S\s?D|C\s?A\s?D)?\b', re.I)
_ORDER_SYMBOL_RE = re.compile(r'^\s*([A-Z][A-Z0-9.\-]{0,9})\s+\$\s*([\d,]+(?:\.\d+)?)')
_ORDER_TYPE_RE = re.compile(r'\b(stop|limit|market)\b.*\b(buy|sell)\b', re.I)


def ocr_image_lines(image_bytes: bytes):
    """Text lines in an image as [{text, x, y, w, h}], via Windows' OCR engine.
    Returns (lines, error); error is a message, never raised."""
    import subprocess
    import tempfile
    if not os.path.exists(WINOCR_SCRIPT):
        return [], "OCR script tools/winocr.ps1 is missing."
    fd, path = tempfile.mkstemp(suffix='.png')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(image_bytes)
        proc = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                               WINOCR_SCRIPT, '-Path', path], capture_output=True, timeout=60)
        out = proc.stdout.decode('utf-8', errors='replace').strip()
        if proc.returncode != 0 or not out:
            err = proc.stderr.decode('utf-8', errors='replace').strip().splitlines()
            return [], f"Windows OCR failed: {err[0] if err else 'no output'}"
        lines = json.loads(out).get('lines') or []
        return ([lines] if isinstance(lines, dict) else lines), None
    except FileNotFoundError:
        return [], "Windows OCR isn't available here (needs Windows PowerShell)."
    except Exception as e:
        return [], f"Couldn't read the image: {e}"
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _ocr_num(s):
    try:
        return float(str(s).replace(',', ''))
    except (TypeError, ValueError):
        return None


def parse_pending_orders(lines):
    """Orders from the OCR lines of a Wealthsimple "Pending orders" list. Each
    order is a row: "SYM $price (chg%)" with the account label under it on the
    left, "Q x $P CUR" and the order type on the right. Lines are grouped into
    rows by the symbol lines' vertical centres. Returns [{symbol, shown_price,
    account_label, qty, stop, limit, currency, order_type}] in screen order; a
    row whose order line wasn't read still comes back (qty/stop None) to fill
    in. The list shows one price per order: the stop for a stop market order,
    but the LIMIT for a stop limit order (its stop isn't shown), so that goes
    in `limit` with `stop` left None."""
    def centre(l):
        return float(l['y']) + float(l.get('h') or 0) / 2
    symbols = sorted((l for l in lines if _ORDER_SYMBOL_RE.match(l.get('text') or '')), key=centre)
    if not symbols:
        return []
    centres = [centre(l) for l in symbols]
    gaps = [b - a for a, b in zip(centres, centres[1:])]
    gap = sorted(gaps)[len(gaps) // 2] if gaps else 80.0
    bounds = [c - gap / 2 for c in centres] + [centres[-1] + gap / 2]
    for i in range(1, len(centres)):
        bounds[i] = (centres[i - 1] + centres[i]) / 2
    orders = []
    for i, sym_line in enumerate(symbols):
        m = _ORDER_SYMBOL_RE.match(sym_line['text'])
        lo, hi = bounds[i], bounds[i + 1]
        inside = [l for l in lines if l is not sym_line and lo <= centre(l) < hi]
        order = {'symbol': m.group(1).upper(), 'shown_price': _ocr_num(m.group(2)), 'account_label': None,
                 'qty': None, 'stop': None, 'limit': None, 'currency': None, 'order_type': None}
        for l in inside:
            text = (l.get('text') or '').strip()
            om = _ORDER_LINE_RE.match(text)
            if om and order['qty'] is None:
                order['qty'], order['stop'] = _ocr_num(om.group(1)), _ocr_num(om.group(2))
                order['currency'] = re.sub(r'\s', '', om.group(3)).upper() if om.group(3) else None
                text = text[om.end():].strip()
            tm = _ORDER_TYPE_RE.search(text)
            if tm and order['order_type'] is None:
                order['order_type'] = text[tm.start():].strip()
        if 'limit' in str(order['order_type'] or '').lower():
            order['limit'], order['stop'] = order['stop'], None
        labels =[l for l in inside if centre(l) > centre(sym_line)
                  and abs(float(l['x']) - float(sym_line['x'])) < 20
                  and not _ORDER_LINE_RE.match(l.get('text') or '')
                  and not _ORDER_TYPE_RE.search(l.get('text') or '')]
        if labels:
            order['account_label'] = min(labels, key=centre)['text'].strip()
        orders.append(order)
    return orders


def _norm_label(s):
    return re.sub(r'[^a-z0-9]', '', str(s or '').lower())


def resolve_order_account(label, symbol, currency, accounts, openpos):
    """The account an order's label ("TFSA", "RRSP", "LIRA") names. Matches the
    nickname, the type, or the start of the nickname; when that leaves more than
    one (two LIRAs), the one holding the symbol wins. None if still unclear."""
    key = _norm_label(label)
    if not key:
        cands = list(accounts or [])
    else:
        cands = [a for a in (accounts or [])
                 if key in (_norm_label(a.get('nickname')), _norm_label(a.get('account_type')),
                            _norm_label(a.get('account_id')))
                 or _norm_label(a.get('nickname')).startswith(key)]
    if len(cands) == 1 and key:
        return cands[0]['account_id']
    holding = {a['account_id'] for a in cands
               for p in (openpos or [])
               if p.get('account_id') == a['account_id'] and p.get('symbol') == symbol
               and (not currency or p.get('currency') == currency) and (p.get('qty_open') or 0) > 0}
    return next(iter(holding)) if len(holding) == 1 else None


def orders_from_screenshot(lines, accounts, openpos):
    """parse_pending_orders plus each order's account and, where the screen
    didn't show it, its currency (from the position held)."""
    out = []
    for o in parse_pending_orders(lines):
        o['account_id'] = resolve_order_account(o['account_label'], o['symbol'], o['currency'], accounts, openpos)
        if not o['currency']:
            curs = {p.get('currency') for p in (openpos or []) if p.get('symbol') == o['symbol']
                    and (o['account_id'] is None or p.get('account_id') == o['account_id'])}
            o['currency'] = next(iter(curs)) if len(curs) == 1 else None
        out.append(o)
    return out


def merge_placed_orders(existing, new):
    """Table rows plus newly read orders, skipping exact repeats (the same
    screenshot pasted twice, or two screenshots that overlap)."""
    def sig(o):
        return (o.get('account_id') or _norm_label(o.get('account_label')), o.get('symbol'), o.get('currency'),
                _ocr_num(o.get('qty')), _ocr_num(o.get('stop')), _ocr_num(o.get('limit')))
    rows = list(existing or [])
    seen = {sig(o) for o in rows}
    for o in new:
        if sig(o) not in seen:
            rows.append(o)
            seen.add(sig(o))
    return rows


def _is_stop_sell(order_type):
    t = str(order_type or '').lower()
    return not t or ('stop' in t and 'sell' in t)


def compare_placed_stops(orders, drift_results, openpos, cash_equiv=None, view=None, planned_lots=None):
    """Each pending order checked against the drift report, per account +
    symbol + currency. Issues, worst first in each row:
    - 'missing': a position (not a cash-equivalent) with no stop order;
    - 'orphan': a stop order with no position behind it;
    - 'low': the stop is below the report's Set Stop To (raise it);
    - 'qty': the order's shares differ from the shares held;
    - 'account': the order's account couldn't be told from the screen.
    - a position with a plan (planned_lots: lot ids matched to a committed
      plan) that's missing from the report, e.g. no live price -- its stop
      couldn't be checked.
    Notes, which still count as matched: a stop above the plan's (tighter), a
    stop at or above the shown price (it fills at the open), a pyramided
    position (checked against its highest lot stop), no plan to check against.

    A stop limit order whose stop wasn't entered (the list shows only its
    limit) is judged by the limit: a sell's stop is at or above its limit, so
    a limit at or above Set Stop To is a match, and one below it can't be
    told and asks for the stop. Every stop limit carries a note that it won't
    sell if price gaps below the limit.
    Returns [{account_id, symbol, currency, held, order_qty, order_stop,
    order_limit, stop_is_bound (order_stop is only the limit), plan_stop,
    diff, price, issues: [(level, text)], ok, lot_ids}]."""
    cash_equiv = set(cash_equiv or [])
    tol = STOP_REVIEW_TOLERANCE

    def key(r):
        return (r.get('account_id'), r.get('symbol'), r.get('currency'))
    held, lots, planned = {}, {}, set()
    planned_lots = set(planned_lots or [])
    for p in (openpos or []):
        if p.get('symbol') in cash_equiv or (p.get('qty_open') or 0) <= 1e-9 or not _in_view(p, view):
            continue
        held[key(p)] = held.get(key(p), 0) + float(p['qty_open'])
        if p.get('lot_id') in planned_lots:
            planned.add(key(p))
    for r in (drift_results or []):
        if r.get('should_be_stop') is not None and not r.get('error'):
            lots.setdefault(key(r), []).append(r)
    stops, others = {}, []
    for o in (orders or []):
        if o.get('symbol') in cash_equiv:
            continue
        if not _is_stop_sell(o.get('order_type')):
            others.append(o)
            continue
        stops.setdefault(key(o), []).append(o)

    checks = []
    for k in sorted(set(held) | set(stops), key=lambda k: (str(k[1]), str(k[0]))):
        account_id, symbol, currency = k
        issues = []
        its = stops.get(k, [])
        qty_held = held.get(k)
        rlots = lots.get(k, [])
        plan_stop = max((float(r['should_be_stop']) for r in rlots), default=None)
        price = next((_ocr_num(o.get('shown_price')) for o in its if _ocr_num(o.get('shown_price'))), None)
        if price is None and rlots:
            price = rlots[0].get('current_price')
        order_qty = sum(_ocr_num(o.get('qty')) or 0 for o in its) if its else None
        # Each order's stop, or failing that its limit as a floor under the stop; the lowest decides.
        levels = [(_ocr_num(o.get('stop')), False) if _ocr_num(o.get('stop')) is not None
                  else (_ocr_num(o.get('limit')), True) for o in its
                  if _ocr_num(o.get('stop')) is not None or _ocr_num(o.get('limit')) is not None]
        order_stop, stop_is_bound = min(levels) if levels else (None, False)
        order_limit = min((_ocr_num(o.get('limit')) for o in its if _ocr_num(o.get('limit')) is not None),
                          default=None)
        diff = (order_stop - plan_stop) if order_stop is not None and plan_stop is not None else None
        if account_id is None:
            issues.append(('danger', "Account not recognised: pick it in the table."))
        if not its:
            issues.append(('danger', "No stop order for this position."))
        elif qty_held is None:
            issues.append(('danger', "Stop order with no position behind it: cancel it if the position was sold."))
        else:
            if order_stop is None:
                issues.append(('danger', "Stop price not read: fill it in."))
            elif plan_stop is None and k in planned:
                issues.append(('danger', "Not in today's drift report (no live price?): run it again."))
            elif plan_stop is None:
                issues.append(('info', "No plan to check the stop against."))
            elif stop_is_bound and diff < -tol:
                issues.append(('danger', f"The limit ({order_stop:.2f}) is below Set Stop To ({plan_stop:.2f}) "
                                         "and the list doesn't show the stop: type the stop from the order's "
                                         "details into the Stop column."))
            elif stop_is_bound:
                issues.append(('info', f"Its stop is at or above its {order_stop:.2f} limit, so at or above "
                                       "Set Stop To."))
            elif diff < -tol:
                issues.append(('danger', f"Raise the stop by {-diff:.2f} to {plan_stop:.2f}."))
            elif diff > tol:
                issues.append(('info', f"{diff:.2f} above the plan's stop (tighter than the plan)."))
            qty_off = order_qty is not None and abs(order_qty - qty_held) > 1e-6
            if qty_off:
                issues.append(('danger', f"Order is for {order_qty:.4g} shares; {qty_held:.4g} held."))
            if len(rlots) > 1:
                issues.append(('info', f"{len(rlots)} lots: checked against the highest lot stop."))
            if order_limit is not None and price is not None and price < order_limit - tol:
                issues.append(('warning', f"Price {price:.2f} is below the {order_limit:.2f} limit: if the stop "
                                          "triggers it won't fill unless price recovers to the limit."))
            elif not stop_is_bound and order_stop is not None and price is not None and price <= order_stop + tol:
                issues.append(('warning', f"Price {price:.2f} is at or below the stop: it likely sells at the open."))
            if order_limit is not None:
                issues.append(('info', f"Stop limit: if price gaps below {order_limit:.2f} it won't sell, but "
                                       "the gap allowance in your planned risk assumes it does."))
        # A gap-through warning doesn't make the stop wrong: it's where the plan says.
        ok = bool(its) and qty_held is not None and not any(lvl == 'danger' for lvl, _ in issues)
        checks.append({'account_id': account_id, 'symbol': symbol, 'currency': currency, 'held': qty_held,
                       'order_qty': order_qty, 'order_stop': order_stop, 'order_limit': order_limit,
                       'stop_is_bound': stop_is_bound, 'plan_stop': plan_stop, 'diff': diff,
                       'price': price, 'issues': issues, 'ok': ok,
                       'lot_ids': [r['lot_id'] for r in rlots if r.get('lot_id')]})
    for o in others:
        checks.append({'account_id': o.get('account_id'), 'symbol': o.get('symbol'), 'currency': o.get('currency'),
                       'held': held.get(key(o)), 'order_qty': _ocr_num(o.get('qty')),
                       'order_stop': _ocr_num(o.get('stop')), 'order_limit': _ocr_num(o.get('limit')),
                       'stop_is_bound': False, 'plan_stop': None, 'diff': None, 'price': None,
                       'issues': [('secondary', f"Not checked: {o.get('order_type')} (only stop sells are).")],
                       'ok': None, 'lot_ids': []})
    return checks


def stop_review_all_matched(checks):
    """True when every stop check passed (orders TAPEs doesn't check, like a
    buy, don't count). An empty book is matched."""
    return all(c['ok'] for c in (checks or []) if c['ok'] is not None)


def placed_stop_summary(checks):
    """One line for the daily journal's drift field: how today's placed stops
    compared with the report."""
    if not checks:
        return ''
    stop_checks = [c for c in checks if c['ok'] is not None]
    bad = [c for c in stop_checks if not c['ok']]
    if not bad:
        return f"Wealthsimple stops checked: all {len(stop_checks)} match the plan."
    parts = [f"{c['symbol']}: " + next(t for lvl, t in c['issues'] if lvl == 'danger').rstrip('.').lower()
             for c in bad]
    return (f"Wealthsimple stops checked: {len(stop_checks) - len(bad)} of {len(stop_checks)} match; "
            + "; ".join(parts))


def render_stop_review(checks, accounts):
    if not checks:
        return html.P("No stop orders or open positions to check.", className="text-muted small mb-0")
    n_ok = sum(1 for c in checks if c['ok'])
    n_stop = sum(1 for c in checks if c['ok'] is not None)
    header = html.Thead(html.Tr([html.Th(h) for h in
                                 ("", "Account", "Symbol", "Placed", "Set Stop To", "Shares (order / held)",
                                  "Check")]))
    body = []
    for c in sorted(checks, key=lambda c: (c['ok'] is not False, c['ok'] is None, str(c['symbol']))):
        cur = c.get('currency') or ''
        mark = (html.Span("✓", className="text-success fw-bold") if c['ok']
                else html.Span("—", className="text-muted") if c['ok'] is None
                else html.Span("⚠", className="text-danger fw-bold"))
        notes = [html.Div(t, className={'danger': 'text-danger', 'warning': 'text-warning fw-bold',
                                        'info': 'text-muted', 'secondary': 'text-muted'}[lvl])
                 for lvl, t in c['issues']] or [html.Span("Matches the plan.", className="text-success")]
        body.append(html.Tr([
            html.Td(mark),
            html.Td(account_name(accounts, c['account_id']) if c['account_id'] else "?"),
            html.Td(c['symbol']),
            html.Td(_placed_text(c, cur) or "—"),
            html.Td(f"{cur} {c['plan_stop']:.2f}" if c['plan_stop'] is not None else "—"),
            html.Td(f"{c['order_qty']:.4g} / {c['held']:.4g}" if c['order_qty'] is not None
                    and c['held'] is not None else
                    f"{c['order_qty']:.4g} / —" if c['order_qty'] is not None else
                    f"— / {c['held']:.4g}" if c['held'] is not None else "—"),
            html.Td(notes, className="small"),
        ]))
    summary = (f"{n_ok} of {n_stop} stops match the plan." if n_stop else "Nothing to check.")
    return html.Div([
        html.Div(summary, className="fw-bold mb-1 " + ("text-success" if n_ok == n_stop else "text-danger")),
        dbc.Table([header, html.Tbody(body)], size='sm', bordered=False, hover=True, className="mb-1"),
        html.Div("Matching lots were ticked Reconciled.", className="small text-muted") if n_ok else None,
    ])


def _placed_text(c, cur=''):
    """'USD 43.59', 'USD 43.70 (limit 43.59)', or 'limit USD 43.59' when only a
    stop limit's limit is known."""
    cur = f"{cur} " if cur else ''
    if c.get('order_stop') is None:
        return ''
    if c.get('stop_is_bound'):
        return f"limit {cur}{c['order_stop']:.2f}"
    limit = f" (limit {c['order_limit']:.2f})" if c.get('order_limit') is not None else ''
    return f"{cur}{c['order_stop']:.2f}{limit}"


def _placed_stop_cell(r, checks):
    """The placed stop under a drift row's Set Stop To, from today's stop review."""
    c = next((c for c in (checks or []) if r.get('lot_id') in c.get('lot_ids', [])), None)
    if not c or c.get('order_stop') is None:
        return None
    cls = "text-success" if c['ok'] else "text-danger fw-bold"
    return html.Div(f"placed {_placed_text(c)}" + ("" if c['ok'] else " ⚠"), className=f"small {cls}",
                    title="Your Wealthsimple stop, from today's stop review.")


def _drift_progress_text(results, reconciled_lot_ids):
    result_lot_ids = {r['lot_id'] for r in (results or [])}
    n_done = len(result_lot_ids & set(reconciled_lot_ids or []))
    return f"{n_done}/{len(result_lot_ids)} reconciled"


def _render_drift_rows(results, reconciled_lot_ids, stop_checks=None):
    """Shared by the initial compute and the reconciled-checkbox toggle (which
    re-renders after a save so a checked row's styling updates immediately) --
    one dbc.Row per lot plus a header, in drift-priority order (an actionable
    finding -- a live stop breach, or price already at the next target --
    sorts first and is color-coded; quiet, on-plan rows sort last), with a
    "Reconciled" checkbox per row (pattern-matching id keyed on lot_id, same
    convention as every other per-row action button in this app). "Set Stop
    To" is should_be_stop, with a "raise" badge when it improves on the current
    stop and an "ATR trail"/"EMA(21) trail" badge when a trailing level is
    what set it (see compute_plan_drift)."""
    if not results:
        return html.P("No open, priced, plan-matched lots to check.", className="text-muted small")

    reconciled = set(reconciled_lot_ids or [])

    def _row_priority(r):
        return (0 if r['stop_breached_live'] or r.get('day_trade_overdue')
                else (1 if r['next_target_reached'] else 2))
    results_sorted = sorted(results, key=_row_priority)
    # Account badge on each row only when the report spans more than one account.
    multi_account = len({r.get('account_id') for r in results}) > 1

    header = dbc.Row([
        dbc.Col(html.B("Reconciled"), width=1), dbc.Col(html.B("Symbol"), width=1),
        dbc.Col(html.B("Price"), width=1),
        dbc.Col(html.B(["ATR(14)", help_icon(
            "14-day ATR from completed daily bars (Yahoo Finance). It's always daily, whatever "
            "timeframe the plan uses, so it can differ from the plan's ATR at commit.")]), width=1),
        dbc.Col(html.B("Actual Stop"), width=1),
        dbc.Col(html.B(["Set Stop To", help_icon(
            "Where your Wealthsimple stop should sit for the next session. Trailing levels are "
            "computed from completed daily closes only, so run this after the close (16:00 ET) or "
            "before the open -- an in-progress day never moves the stop.")]), width=2),
        dbc.Col(html.B("Next Target / Add"), width=2),
        dbc.Col(html.B("Should-Be Qty"), width=1), dbc.Col(html.B("Actual Qty"), width=1),
        dbc.Col(html.B("Qty Drift"), width=1),
    ])
    rows = [header]
    for r in results_sorted:
        is_reconciled = r['lot_id'] in reconciled
        currency = r.get('currency') or ''
        if r['stop_breached_live']:
            row_color = 'danger'
        elif r['next_target_reached']:
            row_color = 'warning'
        else:
            row_color = None
        stop_disp = f"{currency} {r['should_be_stop']:.2f}" if r['should_be_stop'] not in (None, '') else "—"
        if r['stop_breached_live']:
            stop_disp = html.Span([stop_disp, " ⚠"], className="text-danger fw-bold")
        if r.get('stop_action') == 'raise':
            stop_disp = html.Span([stop_disp, " ", html.Span(
                "↑ raise", className="badge bg-primary",
                title="Higher than the current stop -- move your Wealthsimple stop here before the next session.")])
        trail_binding = (r.get('trailing_active') and r.get('trail_level') is not None
                         and r['should_be_stop'] is not None
                         and abs(r['trail_level'] - r['should_be_stop']) < 1e-9)
        if trail_binding:
            label = trail_label(r) or 'trailing'
            stop_disp = html.Span([stop_disp, " ", html.Span(
                label, className="badge bg-info",
                title="Daily trailing level from completed bars (never lower than the ladder stop).")])
        next_target = r.get('next_target')
        next_add = r.get('next_add') if r.get('plan_type') == 'scale_in' else None
        if next_add and next_add.get('price') not in (None, ''):
            add_text = (f"{next_add['label']}: buy {float(next_add.get('qty') or 0):.4g} at {currency} "
                        f"{float(next_add['price']):.2f}")
            if r['next_target_reached']:
                target_disp = html.Span([f"⚠ {next_add['label']} reached: raise the stop to {currency} "
                                         f"{float(next_add['stop_before']):.2f}, then buy "
                                         f"{float(next_add.get('qty') or 0):.4g}"], className="text-warning fw-bold")
            else:
                target_disp = html.Span(add_text, title=f"Raise the stop to {float(next_add['stop_before']):.2f} "
                                                        "before buying.")
        elif r.get('plan_type') == 'scale_in':
            target_disp = html.Span("all adds in", className="text-muted")
        elif r.get('day_trade_overdue'):
            target_disp = html.Span("⚠ Day trade carried overnight: sell it", className="text-danger fw-bold",
                                    title="A day trade is flat by the close of its session (Strategy 4.5).")
        elif next_target:
            target_disp = f"{currency} {next_target['price']:.2f} ({next_target.get('qty_pct') or 0:.0f}%)"
            if r['next_target_reached']:
                target_disp = html.Span([target_disp, " ⚠ reached"], className="text-warning fw-bold")
        else:
            target_disp = html.Span("—", className="text-muted")
        qty_drift = r['qty_drift']
        if abs(qty_drift) < 1e-6:
            drift_disp = html.Span("on plan", className="text-muted")
        elif r.get('plan_type') == 'scale_in' and qty_drift < 0:
            drift_disp = html.Span(f"buy {abs(qty_drift):.4g}", className="text-warning fw-bold",
                                   title="Price has reached an add's trigger: raise the stop first, then buy.")
        else:
            drift_disp = html.Span(f"{qty_drift:+.4g}",
                                    className="text-danger" if qty_drift > 0 else "text-muted",
                                    title="Positive: holding more than the plan says you should by now.")
        row_children = [
            dbc.Col(dbc.Checkbox(id={'type': 'drift-reconciled-check', 'index': r['lot_id']},
                                  value=is_reconciled), width=1),
            dbc.Col(html.Div([html.Span(r['account_label'], className="badge bg-light text-dark d-block mb-1"),
                              r['symbol']]) if multi_account and r.get('account_label') else r['symbol'], width=1),
            dbc.Col(f"{currency} {r['current_price']:.2f}", width=1),
            dbc.Col(f"{currency} {r['atr_14d']:.2f}" if r.get('atr_14d') is not None else "—", width=1),
            dbc.Col(f"{currency} {r['actual_stop']:.2f}" if r['actual_stop'] not in (None, '') else "—", width=1),
            dbc.Col([stop_disp, _placed_stop_cell(r, stop_checks)], width=2), dbc.Col(target_disp, width=2),
            dbc.Col(f"{r['should_be_qty_open']:.4g}", width=1), dbc.Col(f"{r['actual_qty_open']:.4g}", width=1),
            dbc.Col(drift_disp, width=1),
        ]
        row_kwargs = {'className': "mb-1 align-items-center py-1"}
        if is_reconciled:
            row_kwargs['className'] += " opacity-50"
        elif row_color:
            row_kwargs['className'] += f" border-start border-{row_color} border-3"
        rows.append(dbc.Row(row_children, **row_kwargs))

    return rows


@app.callback(
    [Output('store-plan-drift', 'data'), Output('plan-drift-report', 'children'),
     Output('plan-drift-status', 'children'), Output('drift-modal', 'is_open'),
     Output('drift-modal-progress', 'children'), Output('store-stop-checks', 'data'),
     Output('stop-review-result', 'children')],
    Input('btn-check-plan-drift', 'n_clicks'),
    [State('store-openpos', 'data'), State('store-journal', 'data'),
     State('store-open-positions-prices', 'data'), State('store-cash-equiv-symbols', 'data'),
     State('store-drift-reconciled', 'data'), State('account-view', 'value'), State('store-accounts', 'data'),
     State('store-placed-orders', 'data')],
    prevent_initial_call=True,
)
def compute_plan_drift_cb(n_clicks, openpos, journal, staged_prices, cash_equiv_symbols, reconciled_lot_ids,
                          view, accounts, placed_orders):
    """Manual-trigger, same "requires a live-prices refresh first" guard used
    throughout Open Positions -- reuses store-open-positions-prices rather
    than fetching its own (the MCP tool get_plan_drift_report fetches fresh
    instead, since it has no browser session/staged store to read from).
    Opens the results in a large ('xl', scrollable) modal rather than inline
    in the card -- meant to be scanned across several open lots at once,
    which the card's own width can't comfortably show."""
    if not n_clicks:
        raise PreventUpdate
    priced_symbols = (staged_prices or {}).get('priced_symbols', {})
    if not staged_prices or not priced_symbols:
        return no_update, no_update, dbc.Alert(
            'Click "Refresh Live Prices" above first.', color='warning', className="py-1 mb-0"), False, \
            no_update, no_update, no_update

    cash_equiv = set(cash_equiv_symbols or [])
    op_by_lot ={p['lot_id']: p for p in (openpos or []) if p.get('lot_id')}
    results = []
    price_errors = {}
    trailing_errors = {}
    atr_cache = {}
    atr_errors = {}
    for row in (journal or []):
        lot_id = row.get('lot_id')
        if not lot_id or lot_id not in op_by_lot:
            continue  # not matched to a still-open lot -- nothing live to check against
        pos = op_by_lot[lot_id]
        symbol = pos['symbol']
        if symbol in cash_equiv or not _in_view(pos, view):
            continue
        if price_key(pos) not in priced_symbols:
            price_errors[symbol] = 'no current price'
            continue
        current_price = priced_symbols[price_key(pos)]['price']
        trailing, trailing_error = _build_trailing_for_row(row, pos)
        if trailing_error:
            trailing_errors[symbol] = trailing_error
        atr_14d, atr_error = drift_daily_atr(pos, atr_cache)
        if atr_error:
            atr_errors[symbol] = atr_error
        drift = _drift_for_row(row, pos, current_price, trailing)
        results.append({'lot_id': lot_id, 'symbol': symbol, 'currency': pos.get('currency'),
                         'account_id': pos.get('account_id'),
                         'account_label': account_name(accounts, pos.get('account_id')),
                         'stop_method': stop_method_of(row), 'atr_timeframe': atr_timeframe_of(row),
                         'current_price': current_price, 'atr_14d': atr_14d,
                         'stop_action': _stop_action(row['direction'], drift['actual_stop'],
                                                     drift['should_be_stop']), **drift})

    status_parts = []
    if price_errors:
        status_parts.append(dbc.Alert(f"No current price for: {', '.join(price_errors)} (skipped)",
                                      color='warning', className="py-1 mb-1"))
    if trailing_errors:
        status_parts.append(dbc.Alert(
            f"Couldn't fetch daily history for: {', '.join(trailing_errors)} -- trailing level not "
            f"computed; stop shown is the ladder/entry floor only.", color='warning', className="py-1 mb-0"))
    if atr_errors:
        status_parts.append(dbc.Alert(f"No 14-day ATR for: {', '.join(atr_errors)} (shown as —)",
                                      color='warning', className="py-1 mb-0"))
    status = html.Div(status_parts) if status_parts else ''

    # Today's saved stop review (if any), re-checked against this fresh report.
    checks = compare_placed_stops(placed_orders, results, openpos, cash_equiv, view) if placed_orders else None
    rows = _render_drift_rows(results, reconciled_lot_ids, checks)
    progress = _drift_progress_text(results, reconciled_lot_ids)
    return (results, rows, status, True, progress, checks,
            render_stop_review(checks, accounts) if checks else '')


@app.callback(
    Output('drift-modal', 'is_open', allow_duplicate=True),
    Input('btn-drift-modal-close', 'n_clicks'),
    prevent_initial_call=True,
)
def close_drift_modal(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return False


@app.callback(
    [Output('store-drift-reconciled', 'data'), Output('drift-modal-progress', 'children', allow_duplicate=True),
     Output('plan-drift-report', 'children', allow_duplicate=True)],
    Input({'type': 'drift-reconciled-check', 'index': ALL}, 'value'),
    [State({'type': 'drift-reconciled-check', 'index': ALL}, 'id'), State('store-plan-drift', 'data'),
     State('store-stop-checks', 'data')],
    prevent_initial_call=True,
)
def toggle_drift_reconciled_cb(values, ids, results, stop_checks):
    """Persisted per DAY (db.daily_drift_reconciled), not per lot alone --
    the report is checked against live price fresh each morning, so
    yesterday's checkmark on a still-open lot shouldn't silently carry over
    and hide today's re-check of the same position. Re-renders the rows too
    (not just the store/progress) so a checked row's dimmed styling applies
    immediately rather than only on the next full recompute."""
    checked_lot_ids = [i['index'] for i, v in zip(ids, values) if v]
    today = dt.date.today().isoformat()
    db.save_daily_drift_reconciled(today, checked_lot_ids)
    return (checked_lot_ids, _drift_progress_text(results, checked_lot_ids),
            _render_drift_rows(results, checked_lot_ids, stop_checks))


@app.callback(
    [Output('stop-review-table', 'data'), Output('stop-review-status', 'children')],
    [Input('stop-review-upload', 'contents'), Input('store-stop-review-paste', 'data'),
     Input('btn-stop-review-add', 'n_clicks'), Input('btn-stop-review-clear', 'n_clicks')],
    [State('stop-review-table', 'data'), State('store-accounts', 'data'), State('store-openpos', 'data')],
    prevent_initial_call=True,
)
def read_order_screenshots(uploads, pasted, _add, _clear, rows, accounts, openpos):
    """A pasted or uploaded screenshot is read (ocr_image_lines) and its orders
    are added to the table, skipping repeats. Nothing is saved until Check."""
    trigger = callback_context.triggered_id
    rows = list(rows or [])
    if trigger == 'btn-stop-review-clear':
        return [], ''
    if trigger == 'btn-stop-review-add':
        return rows + [{'account_id': None, 'symbol': '', 'currency': None, 'qty': None, 'stop': None,
                        'limit': None, 'order_type': 'Stop market sell', 'shown_price': None}], ''
    images = [pasted['content']] if trigger == 'store-stop-review-paste' and pasted else (uploads or [])
    if not images:
        raise PreventUpdate
    found, errors = [], []
    for content in images:
        try:
            data = base64.b64decode(content.split(',', 1)[1])
        except Exception:
            errors.append("Not an image.")
            continue
        lines, error = ocr_image_lines(data)
        if error:
            errors.append(error)
            continue
        found.extend(orders_from_screenshot(lines, accounts, openpos))
    merged = merge_placed_orders(rows, found)
    added = len(merged) - len(rows)
    parts = []
    if found:
        parts.append(html.Span(f"Read {len(found)} order(s), {added} new. Check the table, then Check stops.",
                               className="text-success"))
        unclear = [o['symbol'] for o in found if not o.get('account_id')]
        if unclear:
            parts.append(html.Div(f"Pick the account for: {', '.join(unclear)}.", className="text-warning"))
        unread = [o['symbol'] for o in found if o.get('stop') is None or o.get('qty') is None]
        if unread:
            parts.append(html.Div(f"Shares or stop not read for: {', '.join(unread)}.", className="text-warning"))
    elif not errors:
        errors.append("No orders found in that image. Is it the Pending orders list?")
    parts += [html.Div(e, className="text-danger") for e in errors]
    return merged, html.Div(parts)


@app.callback(
    [Output('stop-review-result', 'children', allow_duplicate=True),
     Output('store-stop-checks', 'data', allow_duplicate=True),
     Output('store-placed-orders', 'data'),
     Output('store-drift-reconciled', 'data', allow_duplicate=True),
     Output('drift-modal-progress', 'children', allow_duplicate=True),
     Output('plan-drift-report', 'children', allow_duplicate=True),
     Output('daily-checklist-body', 'children', allow_duplicate=True),
     Output('daily-checklist-progress', 'children', allow_duplicate=True)],
    Input('btn-stop-review-check', 'n_clicks'),
    [State('stop-review-table', 'data'), State('store-plan-drift', 'data'), State('store-openpos', 'data'),
     State('store-cash-equiv-symbols', 'data'), State('store-drift-reconciled', 'data'),
     State('account-view', 'value'), State('store-accounts', 'data'), State('store-journal', 'data'),
     State('store-checklists', 'data'), State('daily-checklist-date', 'date')],
    prevent_initial_call=True,
)
def check_stops_cb(n_clicks, rows, results, openpos, cash_equiv, reconciled, view, accounts, journal,
                   checklists, checklist_date):
    """Compare the table with the drift report and save it as today's placed
    orders. Ticks Reconciled for each lot whose stop matches (never unticks:
    a tick the user made by hand stays), and ticks today's self-ticking
    checklist item when everything matched -- or unticks it when not. The
    item only counts a review of every account (the "All accounts" view)."""
    if not n_clicks:
        raise PreventUpdate
    today = dt.date.today().isoformat()
    orders = [{**r, 'symbol': str(r.get('symbol') or '').strip().upper(), 'qty': _ocr_num(r.get('qty')),
               'stop': _ocr_num(r.get('stop')), 'limit': _ocr_num(r.get('limit')),
               'shown_price': _ocr_num(r.get('shown_price'))}
              for r in (rows or []) if str(r.get('symbol') or '').strip()]
    planned_lots = [r['lot_id'] for r in (journal or []) if r.get('lot_id')]
    checks = compare_placed_stops(orders, results, openpos, cash_equiv, view, planned_lots)
    all_matched = stop_review_all_matched(checks) and view in (None, '', ALL_ACCOUNTS)
    db.save_daily_placed_orders(today, orders, all_matched, placed_stop_summary(checks))
    ticked = sorted(set(reconciled or []) | {lot for c in checks if c['ok'] for lot in c['lot_ids']})
    if ticked != sorted(set(reconciled or [])):
        db.save_daily_drift_reconciled(today, ticked)

    entries = (checklists or {}).get('daily') or load_checklist('daily')
    auto = {e['id'] for e in checklist_items(entries) if is_auto_item(e)}
    body = progress = no_update
    if auto:
        saved = set(db.fetch_daily_checklist_state(today))
        new = (saved | auto) if all_matched else (saved - auto)
        if new != saved:
            db.save_daily_checklist_state(today, sorted(new))
        if checklist_date == today:
            body = _render_daily_checklist_accordion(entries, new)
            progress = _checklist_progress_text(checklist_items(entries), new)

    notes = []
    if not results:
        notes.append(dbc.Alert("The drift report found no plan-matched lots, so stops can only be checked "
                               "for missing or leftover orders.", color='warning', className="py-1 mb-1"))
    if auto and view not in (None, '', ALL_ACCOUNTS):
        notes.append(dbc.Alert("Showing one account: the checklist item only ticks itself after a review "
                               "of all accounts.", color='info', className="py-1 mb-1"))
    elif auto:
        elsewhere = (f" (on today's checklist, {today}; the checklist on the page is showing {checklist_date})"
                     if checklist_date and checklist_date != today else "")
        notes.append(html.Div("Checklist: \"" + next(e['label'] for e in checklist_items(entries)
                                                     if e['id'] in auto) + "\" "
                              + ("ticked" if all_matched else "not ticked until every stop matches")
                              + elsewhere + ".",
                              className="small " + ("text-success" if all_matched else "text-muted")))
    return (html.Div(notes + [render_stop_review(checks, accounts)]), checks, orders, ticked,
            _drift_progress_text(results, ticked), _render_drift_rows(results, ticked, checks), body, progress)


@app.callback(
    [Output('weekly-prompt-text', 'value'),
     Output('weekly-prompt-clipboard', 'content'),
     Output('weekly-prompt-status', 'children')],
    Input('btn-weekly-generate-prompt', 'n_clicks'),
    [State('weekly-week-ending', 'date'), State('store-weekly-balance-staged', 'data'),
     State('store-weekly-reviews', 'data'), State('store-openpos', 'data'),
     State('store-cash-equiv-symbols', 'data'), State('store-accounts', 'data')],
    prevent_initial_call=True,
)
def generate_weekly_prompt_cb(n_clicks, week_ending, staged_balance, reviews, openpos, cash_equiv_symbols,
                              accounts):
    if not n_clicks:
        raise PreventUpdate
    if not staged_balance:
        return no_update, no_update, dbc.Alert('Click "Compute Balance" first.', color='warning')
    blocker = weekly_prompt_blocker(week_ending, reviews, db.fetch_weekly_actions())
    if blocker:
        return no_update, no_update, dbc.Alert(blocker, color='warning')

    closed_this_week = _this_week_journal_rows(week_ending)
    for row in closed_this_week:
        row['account'] = account_name(accounts, _scalar(row.pop('account_id', None)))
    prior_weeks = sorted(
        [r for r in (reviews or []) if r['week_ending'] < week_ending and r.get('balance') is not None],
        key=lambda r: r['week_ending'])
    prior = prior_weeks[-1] if prior_weeks else None
    staged_basis = (staged_balance.get('currency'), 'cash_by_currency' in staged_balance)
    same_basis = prior is not None and balance_basis(prior) == staged_basis
    change = (staged_balance['total'] - prior['balance']) if same_basis else None

    # Trimmed to just what a holdings reconciliation needs (symbol/qty/price),
    # not the full open-position record (exit_fill_list etc.) -- same "don't
    # dump more than needed into the prompt" principle as the rest of this
    # design. cash_equivalent is flagged so a parking position like BIL reads
    # as expected, not as a surprise.
    cash_equiv = set(cash_equiv_symbols or [])
    open_positions_summary = [
        {
            'account': account_name(accounts, _scalar(p.get('account_id'))),
            'symbol': p['symbol'],
            'qty_open': round(p['qty_open'], 4) if p.get('qty_open') is not None else None,
            'avg_price': round(p['avg_price'], 4) if p.get('avg_price') is not None else None,
            'currency': p.get('currency'),
            'cash_equivalent': p['symbol'] in cash_equiv,
        }
        for p in (openpos or [])
    ]

    week_summary = {
        'week_ending': week_ending,
        'strategy_version': strategy_rules_text()[0],
        'balance': round(staged_balance['total'], 2),
        'balance_currency': staged_balance.get('currency') or 'mixed (not FX-converted)',
        'balance_by_currency': {c: round(v, 2) for c, v in (staged_balance.get('by_currency') or {}).items()},
        'balance_includes_cash': 'cash_by_currency' in staged_balance,
        'cash_by_currency': {c: round(v, 2) for c, v in (staged_balance.get('cash_by_currency') or {}).items()},
        'fx_rates_used': fmt_fx_note(staged_balance.get('fx')) or None,
        'balance_change_from_prior_week': round(change, 2) if change is not None else None,
        'balance_by_account': {account_name(accounts, a): round(v, 2)
                               for a, v in (staged_balance.get('by_account') or {}).items()},
        'open_positions': open_positions_summary,
        'closed_trades_this_week': closed_this_week,
        'cancelled_plans_this_week': _cancelled_this_week(week_ending),
        'regime_checks_this_week': regime_checks_for_week(_journal_from_db(), week_ending),
        'practices': practices_for_prompt(_journal_from_db(), week_ending, db.fetch_stop_reviews()),
        'lessons_and_improvement': lessons_for_prompt(_journal_from_db(), week_ending, db.fetch_stop_reviews(),
                                                      db.fetch_daily_journal_entries(), db.fetch_plans(),
                                                      db.fetch_improvements(), db.fetch_weekly_actions()),
        'daily_journal_this_week': _daily_journal_this_week(week_ending),
        'weekly_entry': weekly_entry_for_prompt(next((r for r in reviews or []
                                                      if r['week_ending'] == week_ending), None)),
        'focus_actions': actions_for_prompt(db.fetch_weekly_actions(), week_ending,
                                            action_window_start(reviews, week_ending)),
        'stale_planned_plans': _stale_plans_summary(),
        'cancellation_record': {k: v for k, v in _compute_summary_stats(_journal_from_db()).items()
                                if k.startswith('cancel')},
    }
    trend_context = compute_weekly_trend_context()
    prompt = _build_weekly_review_prompt(week_ending, week_summary, trend_context)
    return prompt, prompt, ''


def store_focus_actions(week_ending, llm_note):
    """Turn the coaching note's FOCUS NEXT WEEK block into actions for the week
    (items carrying an existing action by id aren't re-created). Returns
    (all actions, a status line)."""
    if not (llm_note or '').strip():
        return db.fetch_weekly_actions(), None
    items = parse_focus_items(llm_note)
    if not items:
        if any(FOCUS_HEADER.match(ln) for ln in llm_note.splitlines()):
            return db.fetch_weekly_actions(), ("The FOCUS NEXT WEEK heading was found but no items under it, so no "
                                               "actions were created. Put each item on its own line below it.")
        return db.fetch_weekly_actions(), ("No FOCUS NEXT WEEK block found in the coaching note, so no actions "
                                           "were created. Ask the coach to end its reply with one.")
    new = [i for i in items if i['ref'] is None]
    saved = db.replace_focus_actions(week_ending, new)
    carried = [f"A{i['ref']}" for i in items if i['ref'] is not None]
    msg = (f"{len(saved)} focus action(s) for this week: " + ", ".join(action_label(a) for a in saved)
           + (f"; carried: {', '.join(carried)}" if carried else "") + ".")
    return db.fetch_weekly_actions(), msg


def _staged_balance_fields(staged_balance):
    fields = {}
    if staged_balance and staged_balance.get('total') is not None:
        fields['balance'] = staged_balance['total']
        fields['balance_currency'] = staged_balance.get('currency')
        if 'cash_by_currency' in staged_balance:
            fields['balance_detail_json'] = json.dumps({
                'positions': staged_balance.get('positions_by_currency') or {},
                'cash': staged_balance.get('cash_by_currency') or {},
                'cash_as_of': staged_balance.get('cash_as_of') or {}})
        else:
            fields['balance_detail_json'] = json.dumps(staged_balance.get('by_currency') or {})
    return fields


@app.callback(
    [Output('store-weekly-reviews', 'data', allow_duplicate=True),
     Output('weekly-save-status', 'children'),
     Output('store-weekly-actions', 'data', allow_duplicate=True)],
    Input('btn-weekly-save', 'n_clicks'),
    [State('weekly-week-ending', 'date'), State('store-weekly-balance-staged', 'data'),
     State('weekly-user-note', 'value'), State('weekly-llm-note-paste', 'value'),
     State('store-weekly-reviews', 'data')],
    prevent_initial_call=True,
)
def save_weekly_review_cb(n_clicks, week_ending, staged_balance, user_note, llm_note, reviews):
    if not n_clicks or not week_ending:
        raise PreventUpdate
    fields = _staged_balance_fields(staged_balance)
    if user_note is not None:
        fields['user_note'] = user_note
    if llm_note is not None:
        fields['llm_note'] = llm_note
    row = db.save_weekly_review(week_ending, **fields)
    reviews = [r for r in (reviews or []) if r['week_ending'] != week_ending]
    reviews.append(row)
    reviews.sort(key=lambda r: r['week_ending'], reverse=True)
    actions, msg = store_focus_actions(week_ending, llm_note)
    found = msg is None or not msg.startswith("No FOCUS")
    return reviews, dbc.Alert(f"Saved week ending {week_ending}." + (f" {msg}" if msg else ""),
                              color='success' if found else 'warning'), actions


@app.callback(
    Output('store-daily-observations', 'data', allow_duplicate=True),
    Input({'type': 'btn-delete-daily-obs', 'index': ALL}, 'n_clicks'),
    State('store-daily-observations', 'data'),
    prevent_initial_call=True,
)
def delete_daily_observation(n_clicks_list, current):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    obs_id = triggered['index']
    db.delete_daily_observation(obs_id)
    return [o for o in (current or []) if o['obs_id'] != obs_id]


def build_targets_list(*flat_vals):
    """flat_vals is (price1, qty1, stop1, rmult1, price2, qty2, stop2, rmult2, ...)
    for up to MAX_TARGETS targets. Rows with no price are skipped. rmult (the
    ATR multiple this target's price was computed from, when Method is
    "ATR-derived") is carried through purely as provenance/for re-editing --
    it plays no role in scoring or matching, unlike price/qty_pct/stop_after."""
    targets = []
    for i in range(0, len(flat_vals), 4):
        price, qty_pct, stop_after, rmult = flat_vals[i], flat_vals[i + 1], flat_vals[i + 2], flat_vals[i + 3]
        if price in (None, ''):
            continue
        targets.append({
            'price': float(price),
            'qty_pct': float(qty_pct) if qty_pct not in (None, '') else None,
            'stop_after': float(stop_after) if stop_after not in (None, '') else None,
            'r_multiple': float(rmult) if rmult not in (None, '') else None,
        })
    return targets


def _validate_target_qty_pct(targets):
    """Every target with a price set represents a scale-out step, so its Qty %
    (share of the ORIGINAL position size to exit there) must be filled in, and
    across every such target they must sum to exactly 100 -- the position
    should be fully accounted for by its own scale-out ladder, with no gap
    left un-planned and no double-counted overlap. Returns an error message
    string if invalid, or None if there are no priced targets at all (a plan
    doesn't need a scale-out ladder) or the ladder is valid."""
    active = targets or []
    if not active:
        return None
    if any(t.get('qty_pct') in (None, '') for t in active):
        return "Every target with a price needs a Qty % -- fill one in, or clear that target's price."
    total = sum(t['qty_pct'] for t in active)
    if abs(total - 100) > 0.01:
        return f"Target Qty % values must sum to 100 (currently {total:g}%)."
    return None


def targets_to_row_values(targets):
    """Inverse of build_targets_list: pad/truncate a plan's targets list to
    MAX_TARGETS rows of (price, qty_pct, stop_after, r_multiple) for populating
    the edit form."""
    targets = (targets or [])[:db.MAX_TARGETS]
    out = []
    for i in range(db.MAX_TARGETS):
        if i < len(targets):
            t = targets[i]
            out += [t.get('price'), t.get('qty_pct'), t.get('stop_after'), t.get('r_multiple')]
        else:
            out += [None, None, None, None]
    return out


def _fmt_pct(entry, value):
    """'% from entry' display string for one price field, or '—' if either
    side is missing/unparseable. Plain (value-entry)/entry -- not a
    favorable/adverse transform -- since this is just describing distance,
    not judging it."""
    try:
        if entry in (None, '') or value in (None, ''):
            return "—"
        entry_f, value_f = float(entry), float(value)
        if entry_f == 0:
            return "—"
        return f"{(value_f - entry_f) / entry_f * 100:+.1f}%"
    except (TypeError, ValueError):
        return "—"


def _num_key(value):
    return None if value in (None, '') else float(value)


def edit_form_loaded_values(plan, prices, rmults):
    """What the Edit modal was just filled with, so its ATR callbacks can tell
    that load from a real change."""
    return {'symbol': plan['symbol'], 'currency': plan.get('currency') or 'USD',
            'timeframe': atr_timeframe_of(plan), 'entry': plan['entry_price'], 'direction': plan['direction'],
            'atr': plan.get('atr_at_commit'), 'mult': plan.get('atr_multiplier') or 1.5,
            'prices': list(prices), 'rmults': list(rmults)}


def edit_form_unchanged(loaded, entry, direction, atr, mult, rmults, prices):
    """Does the form still hold what was loaded? Prices count too, so going back
    to the saved ATR after trying another one still recomputes."""
    if not loaded:
        return False
    return (direction == loaded['direction']
            and [_num_key(v) for v in (entry, atr, mult)]
            == [_num_key(loaded[k]) for k in ('entry', 'atr', 'mult')]
            and [_num_key(v) for v in rmults] == [_num_key(v) for v in loaded['rmults']]
            and [_num_key(v) for v in prices] == [_num_key(v) for v in loaded['prices']])


def auto_target_prices(entry, sign, atr, rmults, prices, changed, whole, keep_blank):
    """Each target's price when Method is ATR-derived. `changed` holds the rows
    whose R× just changed; `whole` means the entry, direction, ATR or multiplier
    changed, which moves every row. A row with no R× has no ATR price: it empties
    if the R× was just cleared, and otherwise keeps what's there. keep_blank (Edit
    modal) also leaves an emptied price empty, so a removed target stays removed;
    the Add Plan form starts with empty prices, so it can't tell."""
    out = []
    for i, (r, price) in enumerate(zip(rmults, prices)):
        just_changed = i in changed
        if r in (None, ''):
            out.append(None if just_changed else price)
        elif not just_changed and (not whole or (keep_blank and price in (None, ''))):
            out.append(price)
        else:
            out.append(round(entry + sign * float(r) * atr, 4))
    return out


def register_atr_callbacks(prefix):
    """Wire the automatic ATR/target behavior of atr_block(prefix) and the
    live '% from entry' readouts of target_rows_form(prefix), for either the
    Add Plan form ('plan') or the Edit modal ('edit') -- both use the same id
    scheme. Two reactive stages, chained through the ATR field:

    1. Symbol changes (debounced on the input) -> fetch ATR automatically.
    2. Entry / ATR value / ATR multiplier changes -> recompute stop + T1-T5
       from ATR, but ONLY while Method is 'atr' -- checked as a State, so it
       never fires from its own writes, and a user who flips Method to
       'Manual override' first can hand-edit stop/targets without them being
       silently recomputed out from under the edit.

    Filling the Edit modal writes these same fields. store-edit-loaded lets both
    stages ignore that write: opening a plan must never fetch a new ATR or change
    its stop and targets (found 2026-11-03 on EWZ).

    A third, independent callback keeps the '% from entry' labels current
    for every price field, including hand-edited ones.
    """

    @app.callback(
        [Output(f'{prefix}-atr', 'value', allow_duplicate=True),
         Output(f'{prefix}-atr-status', 'children', allow_duplicate=True)],
        [Input(f'{prefix}-symbol', 'value'), Input(f'{prefix}-currency', 'value'),
         Input(f'{prefix}-atr-timeframe', 'value')],
        [State('store-edit-loaded', 'data'), State('edit-atr', 'value')] if prefix == 'edit' else [],
        prevent_initial_call=True,
    )
    def _auto_fetch_atr(symbol, currency, timeframe, *loaded_state, prefix=prefix):
        # Currency and timeframe are Inputs too: currency picks the listing
        # (US vs TSX) and timeframe the bars, so changing either re-fetches.
        if not symbol or not symbol.strip():
            raise PreventUpdate
        if loaded_state and loaded_state[0]:
            loaded, atr = loaded_state
            if (symbol, currency, timeframe) == (loaded['symbol'], loaded['currency'], loaded['timeframe']) \
                    and _num_key(atr) == _num_key(loaded['atr']):
                raise PreventUpdate
        try:
            atr_value, source_note = fetch_atr(symbol.strip().upper(), currency=currency, timeframe=timeframe)
        except Exception as e:
            return no_update, dbc.Alert(f"Couldn't fetch ATR: {e}. Enter it manually.", color='warning')
        # 'Yahoo Finance proxy' = fell all the way back (flag it); 'Changed'
        # = TradingView was corrected to fetch this (worth noticing, not alarming);
        # otherwise the chart was already in the right state.
        if 'Yahoo Finance proxy' in source_note:
            color = 'warning'
        elif 'Changed' in source_note:
            color = 'success'
        else:
            color = 'info'
        note = (f"ATR({ATR_PERIOD}, {ATR_TIMEFRAME_LABELS.get(timeframe, '1D')}) = {currency or ''} "
                f"{atr_value:.4f} -- source: {source_note}")
        return round(atr_value, 4), dbc.Alert(note, color=color)

    @app.callback(
        [Output(f'{prefix}-stop', 'value', allow_duplicate=True)] +
        [Output(f'{prefix}-t{i}-price', 'value', allow_duplicate=True) for i in range(1, db.MAX_TARGETS + 1)],
        [Input(f'{prefix}-entry', 'value'), Input(f'{prefix}-direction', 'value'),
         Input(f'{prefix}-atr', 'value'), Input(f'{prefix}-atr-mult', 'value')] +
        [Input(f'{prefix}-t{i}-rmult', 'value') for i in range(1, db.MAX_TARGETS + 1)],
        [State(f'{prefix}-target-method', 'value')] +
        [State(f'{prefix}-t{i}-price', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
        ([State('store-edit-loaded', 'data')] if prefix == 'edit' else []),
        prevent_initial_call=True,
    )
    def _auto_apply_stop_targets(entry, direction, atr_value, mult, *rest, prefix=prefix):
        n = db.MAX_TARGETS
        rmults, target_method, prices = rest[:n], rest[n], rest[n + 1:2 * n + 1]
        loaded = rest[2 * n + 1] if len(rest) > 2 * n + 1 else None
        if target_method != 'atr' or entry in (None, '') or atr_value in (None, ''):
            raise PreventUpdate
        if edit_form_unchanged(loaded, entry, direction, atr_value, mult, rmults, prices):
            raise PreventUpdate
        try:
            entry_f, atr_f = float(entry), float(atr_value)
        except (TypeError, ValueError):
            raise PreventUpdate
        mult = float(mult) if mult not in (None, '') else 1.5
        sign = 1 if direction == 'BUY' else -1

        ids = {t['prop_id'] for t in callback_context.triggered}
        rmult_ids = {f'{prefix}-t{i + 1}-rmult.value': i for i in range(n)}
        changed = {rmult_ids[p] for p in ids if p in rmult_ids}
        whole = not ids or bool(ids - set(rmult_ids))
        # Changing one R× moves that target only, not the stop or the other targets.
        stop = round(entry_f - sign * atr_f * mult, 4) if whole else no_update
        return [stop] + auto_target_prices(entry_f, sign, atr_f, rmults, prices, changed, whole, prefix == 'edit')

    @app.callback(
        [Output(f'{prefix}-stop-pct', 'children')] +
        [Output(f'{prefix}-t{i}-price-pct', 'children') for i in range(1, db.MAX_TARGETS + 1)] +
        [Output(f'{prefix}-t{i}-stop-pct', 'children') for i in range(1, db.MAX_TARGETS + 1)],
        [Input(f'{prefix}-entry', 'value'), Input(f'{prefix}-stop', 'value')] +
        [Input(f'{prefix}-t{i}-price', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
        [Input(f'{prefix}-t{i}-stop', 'value') for i in range(1, db.MAX_TARGETS + 1)],
    )
    def _update_pct_labels(entry, stop, *target_vals, prefix=prefix):
        prices = target_vals[0:db.MAX_TARGETS]
        stops = target_vals[db.MAX_TARGETS:2 * db.MAX_TARGETS]
        return ([_fmt_pct(entry, stop)] +
                [_fmt_pct(entry, p) for p in prices] +
                [_fmt_pct(entry, s) for s in stops])

    @app.callback(
        Output(f'{prefix}-risk-reward', 'children'),
        [Input(f'{prefix}-entry', 'value'), Input(f'{prefix}-direction', 'value'),
         Input(f'{prefix}-stop', 'value'), Input(f'{prefix}-qty', 'value'),
         Input(f'{prefix}-currency', 'value'), Input(f'{prefix}-plan-type', 'value'),
         Input(f'{prefix}-atr', 'value')] +
        [Input(f'{prefix}-t{i}-price', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
        [Input(f'{prefix}-t{i}-qty', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
        [Input(f'{prefix}-t{i}-stop', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
        [Input(f'{prefix}-a{i}-{f}', 'value') for f in ('price', 'qty', 'stop')
         for i in range(1, db.MAX_TARGETS + 1)],
    )
    def _update_risk_reward(entry, direction, stop, qty, currency, plan_type, atr, *vals, prefix=prefix):
        n = db.MAX_TARGETS
        gap = gap_per_share(atr, entry, stop)
        if plan_type == 'scale_in':
            adds = adds_from_form(vals[3 * n:])
            return render_campaign_stages(compute_campaign_stages(direction, entry, stop, qty, adds, gap),
                                          currency, validate_campaign(direction, entry, stop, adds, 'atr_trail'))
        targets = [{'price': p, 'qty_pct': q, 'stop_after': s} for p, q, s in
                   zip(vals[:n], vals[n:2 * n], vals[2 * n:3 * n])]
        return render_plan_ladder(compute_plan_ladder(direction, entry, stop, targets, qty, gap), currency)


def render_plan_ladder(ladder, currency):
    """The Add Plan / Edit summary: max loss, the planned reward and R:R
    following the scale-out ladder, and the stop-out-at-each-stage table."""
    if not ladder or (ladder['max_loss'] is None and ladder['reward'] is None):
        return html.P("Enter entry, quantity, a stop, and at least one target to see max loss/reward.",
                      className="text-muted small mb-0")
    ccy = f"{currency} " if currency else ""
    if ladder['reward'] is None:
        reward = "Planned reward: n/a (no targets set)"
    elif ladder['reward_basis'] == 'ladder':
        reward = f"Planned reward (full scale-out ladder): {ccy}{ladder['reward']:,.2f}"
    else:
        reward = (f"Max reward: {ccy}{ladder['reward']:,.2f} (no Qty % set, so this assumes the whole "
                  f"position exits at the furthest target)")
    gap_loss = ladder.get('max_loss_gap')
    parts = [(f"Max loss: {ccy}{ladder['max_loss']:,.2f}"
              + (f" ({ccy}{gap_loss:,.2f} with gap allowance)" if gap_loss is not None else ""))
             if ladder['max_loss'] is not None else "Max loss: n/a (no stop set)", reward]
    rr = ladder['rr']
    if rr is not None:
        color = 'success' if rr >= 2 else ('warning' if rr >= 1 else 'danger')
        parts.append(f"R:R = {rr:.2f} : 1" + (f" ({ladder['rr_gap']:.2f} : 1 with gap allowance)"
                                             if ladder.get('rr_gap') is not None else ""))
    else:
        color = 'secondary'
    # Each figure stays on one line ("1.94 : 1" never splits); the row wraps between them.
    children = [html.Div([html.Span(p, className="text-nowrap me-3") for p in parts])]
    if gap_loss is not None:
        children.append(html.Small(
            f"Gap allowance: {ccy}{ladder['gap_per_share']:,.2f}/share ({load_gap_allowance_atr():g} × ATR) for a "
            f"stop that a gap jumps. Size so the max loss with the allowance fits your per-trade risk.",
            className="d-block"))
    if ladder['reward_basis'] == 'ladder' and not ladder['ladder_complete']:
        children.append(html.Small(f"Qty % adds up to {ladder['assigned_pct']:g}%, not 100% -- the reward "
                                   f"counts only the shares assigned to a target.", className="d-block"))
    if ladder['scenarios']:
        rows = [html.Tr([html.Td(sc['label']),
                         html.Td(f"{sc['stop']:,.2f}" if sc['stop'] is not None else "—"),
                         html.Td(f"{ccy}{sc['pnl']:+,.2f}", className="text-end"),
                         html.Td(f"{sc['r']:+.2f}R" if sc['r'] is not None else "—", className="text-end")])
                for sc in ladder['scenarios']]
        children.append(dbc.Table(
            [html.Thead(html.Tr([html.Th("If stopped out…"), html.Th("Stop then"),
                                 html.Th("Result", className="text-end"), html.Th("R", className="text-end")])),
             html.Tbody(rows)],
            size='sm', className="mb-0 mt-2 small", borderless=True,
            style={'--bs-table-bg': 'transparent'}))
        if ladder['best_target_reward'] is not None and ladder['reward'] is not None:
            children.append(html.Small(
                f"For comparison, the whole position exiting at the furthest target would make "
                f"{ccy}{ladder['best_target_reward']:,.2f}; the ladder sells most of it earlier. "
                f"\"Stop then\" is each hit target's Move Stop To value (the floor, for a trailing plan).",
                className="d-block text-muted mt-1"))
    return dbc.Alert(children, color=color, className="py-2 mb-0")


def adds_from_form(vals):
    """Add rows from the form's (price1..5, qty1..5, stop1..5) values; rows
    without a trigger price are dropped."""
    n = db.MAX_TARGETS
    adds = []
    for p, q, st in zip(vals[:n], vals[n:2 * n], vals[2 * n:3 * n]):
        if p not in (None, ''):
            adds.append({'price': p, 'qty': q, 'stop_before': st})
    return adds


def render_campaign_stages(result, currency, error=None):
    """The campaign's risk at each stage (assuming every add fills at its
    trigger), with the peak-risk stage marked -- the figure the 2% cap and
    the campaign's R use."""
    if not result:
        return html.P("Enter entry, quantity and an initial stop to see the campaign's risk by stage.",
                      className="text-muted small mb-0")
    ccy = f"{currency} " if currency else ""
    peak = result['peak_risk']
    rows = []
    for st in result['stages']:
        is_peak = peak > 0 and abs(-st['pnl_if_stopped'] - peak) < 1e-9
        rows.append(html.Tr([
            html.Td(st['label'] + (" (peak risk)" if is_peak else ""), className="fw-bold" if is_peak else None),
            html.Td(f"{st['trigger']:,.2f}"), html.Td(f"{st['shares']:g}"), html.Td(f"{st['avg']:,.2f}"),
            html.Td(f"{st['stop']:,.2f}"),
            html.Td(f"{ccy}{st['pnl_if_stopped']:+,.2f}", className="text-end"),
        ]))
    color = 'danger' if error else 'info'
    children = [
        html.Div([html.Span(f"Peak risk: {ccy}{peak:,.2f}", className="text-nowrap me-3 fw-bold"),
                  *([html.Span(f"With gap allowance: {ccy}{result['peak_risk_gap']:,.2f}",
                               className="text-nowrap me-3")] if result.get('gap_per_share') else []),
                  html.Span(f"Probe risk: {ccy}{result['probe_risk']:,.2f}", className="text-nowrap me-3"),
                  html.Span("Exit: trailing stop", className="text-nowrap")]),
        dbc.Table([html.Thead(html.Tr([html.Th("Stage"), html.Th("Buy at"), html.Th("Shares"), html.Th("Avg cost"),
                                       html.Th("Stop in place"), html.Th("If stopped", className="text-end")])),
                   html.Tbody(rows)],
                  size='sm', borderless=True, className="mb-0 mt-2 small", style={'--bs-table-bg': 'transparent'}),
        html.Small("Assumes each add fills at its trigger, with the stop raised first. The 2% per-trade "
                   "cap applies to the peak-risk stage, with the gap allowance on every share held.",
                   className="d-block text-muted mt-1"),
    ]
    if error:
        children.insert(0, html.Div(error, className="fw-bold mb-1"))
    return dbc.Alert(children, color=color, className="py-2 mb-0")


def register_campaign_callbacks(prefix):
    """Show the add ladder instead of the targets for a campaign, fill the
    default ladder, and keep each add row's '% from entry' readouts live."""

    @app.callback(
        [Output(f'{prefix}-targets-section', 'style'), Output(f'{prefix}-campaign-section', 'style'),
         Output(f'{prefix}-stop-method', 'value', allow_duplicate=True)],
        Input(f'{prefix}-plan-type', 'value'),
        State(f'{prefix}-stop-method', 'value'),
        prevent_initial_call='initial_duplicate',
    )
    def _toggle_plan_type(plan_type, stop_method, prefix=prefix):
        campaign = plan_type == 'scale_in'
        # A campaign exits on a trail only: switch a ladder choice to the ATR trail.
        # A day trade uses the Ladder only (Strategy 4.5). Set here, not in the
        # day-trade defaults callback: two callbacks on the same Input can't both
        # write this field (Dash rejects the duplicate output).
        if campaign:
            new_method = 'atr_trail' if stop_method in (None, 'ladder') else no_update
        else:
            new_method = 'ladder' if plan_type == 'day_trade' and stop_method != 'ladder' else no_update
        return ({'display': 'none'} if campaign else {}), ({} if campaign else {'display': 'none'}), new_method

    @app.callback(
        [Output(f'{prefix}-a{i}-{f}', 'value', allow_duplicate=True) for f in ('price', 'qty', 'stop')
         for i in range(1, db.MAX_TARGETS + 1)],
        Input(f'{prefix}-fill-adds', 'n_clicks'),
        [State(f'{prefix}-direction', 'value'), State(f'{prefix}-entry', 'value'), State(f'{prefix}-stop', 'value'),
         State(f'{prefix}-qty', 'value'), State(f'{prefix}-adds-count', 'value'), State(f'{prefix}-add-qty', 'value')],
        prevent_initial_call=True,
    )
    def _fill_default_adds(n_clicks, direction, entry, stop, qty, count, add_qty, prefix=prefix):
        if not n_clicks:
            raise PreventUpdate
        adds = default_campaign_adds(direction, entry, stop, add_qty if add_qty not in (None, '') else qty,
                                     int(count or DEFAULT_CAMPAIGN_ADDS))
        if not adds:
            raise PreventUpdate
        n = db.MAX_TARGETS
        pad = adds + [{}] * (n - len(adds))
        return ([a.get('price') for a in pad] + [a.get('qty') for a in pad] +
                [a.get('stop_before') for a in pad])

    @app.callback(
        [Output(f'{prefix}-a{i}-{f}-pct', 'children') for f in ('price', 'stop')
         for i in range(1, db.MAX_TARGETS + 1)],
        [Input(f'{prefix}-entry', 'value')] +
        [Input(f'{prefix}-a{i}-{f}', 'value') for f in ('price', 'stop') for i in range(1, db.MAX_TARGETS + 1)],
    )
    def _update_add_pct_labels(entry, *vals, prefix=prefix):
        return [_fmt_pct(entry, v) for v in vals]


register_atr_callbacks('plan')
register_atr_callbacks('edit')
register_campaign_callbacks('plan')
register_campaign_callbacks('edit')


def register_day_trade_callbacks(prefix):
    """Show the day-trade fields for a day trade. On the Add Plan form only,
    switching to Day trade also fills its defaults (5m ATR, stop 1 x ATR,
    Ladder, T1 1R / T2 2R at 50% each), and switching away puts the timeframe
    back to daily; the Edit modal keeps the plan's own values."""

    @app.callback(Output(f'{prefix}-daytrade-section', 'style'), Input(f'{prefix}-plan-type', 'value'))
    def _toggle_day_section(plan_type, prefix=prefix):
        return {} if plan_type == 'day_trade' else {'display': 'none'}

    if prefix != 'plan':
        return
    n = db.MAX_TARGETS

    @app.callback(
        [Output('plan-atr-timeframe', 'value', allow_duplicate=True),
         Output('plan-atr-mult', 'value', allow_duplicate=True)] +
        [Output(f'plan-t{i}-rmult', 'value', allow_duplicate=True) for i in range(1, n + 1)] +
        [Output(f'plan-t{i}-qty', 'value', allow_duplicate=True) for i in range(1, n + 1)],
        Input('plan-plan-type', 'value'),
        State('plan-atr-timeframe', 'value'),
        prevent_initial_call=True,
    )
    def _day_trade_defaults(plan_type, timeframe):
        if plan_type == 'day_trade':
            return ([DAY_TRADE_DEFAULT_TIMEFRAME, DAY_TRADE_DEFAULT_STOP_ATR] + [1, 2] + [None] * (n - 2)
                    + [50, 50] + [None] * (n - 2))
        if timeframe in dict(INTRADAY_TIMEFRAMES):
            return ([DEFAULT_ATR_TIMEFRAME, 1.5] + [round(float(r), 2) for r in np.linspace(1, 6, n)]
                    + [None] * n)
        raise PreventUpdate


register_day_trade_callbacks('plan')
register_day_trade_callbacks('edit')
register_screenshot_callbacks('plan')
register_screenshot_callbacks('edit')


@app.callback(
    Output('plan-currency', 'value'),
    Input('plan-symbol', 'value'),
    [State('store-openpos', 'data'), State('store-executions', 'data')],
    prevent_initial_call=True,
)
def _auto_guess_plan_currency(symbol, openpos, executions):
    """Add Plan form only -- pre-fills the Currency picker from the symbol's
    own open position or trade history (see _guess_symbol_currency), same
    "auto-fetch but always editable, never silently trusted" precedent as the
    ATR auto-fetch above. A no-op (PreventUpdate) when nothing can be guessed
    (a brand-new symbol never traded before) rather than resetting the field
    to some default, since the user may have already set it by hand. The Edit
    modal does NOT get this: an existing plan's currency comes from the plan
    itself (see open_edit_modal), never re-guessed on symbol edit."""
    guess = _guess_symbol_currency(symbol, openpos, executions)
    if guess is None:
        raise PreventUpdate
    return guess


def compute_revenge_hint(roundtrips, cash_equiv_symbols, now=None,
                         lookback_days=REVENGE_HINT_LOOKBACK_DAYS):
    """Context for the "Revenge" trigger, derived from existing data rather
    than self-reported: if the most recently closed (non-cash-equivalent)
    round trip was a loss within lookback_days, return
    {'symbol', 'pnl', 'currency', 'days_ago', 'loss_streak'}; else None.
    Informational only -- it never ticks the Revenge box on the user's behalf."""
    cash_equiv = set(cash_equiv_symbols or [])
    closed = [rt for rt in (roundtrips or [])
              if rt.get('symbol') not in cash_equiv and rt.get('exit_time')
              and rt.get('realized_pnl') is not None]
    if not closed:
        return None
    closed.sort(key=lambda rt: pd.to_datetime(rt['exit_time'], utc=True))
    last = closed[-1]
    if last['realized_pnl'] >= 0:
        return None
    now = now or pd.Timestamp.now(tz='UTC')
    days_ago = (now - pd.to_datetime(last['exit_time'], utc=True)).total_seconds() / 86400
    if days_ago > lookback_days:
        return None
    loss_streak = 0
    for rt in reversed(closed):
        if rt['realized_pnl'] >= 0:
            break
        loss_streak += 1
    return {'symbol': last['symbol'], 'pnl': last['realized_pnl'], 'currency': last.get('currency'),
            'days_ago': days_ago, 'loss_streak': loss_streak}


@app.callback(
    Output('plan-revenge-hint', 'children'),
    [Input('store-roundtrips', 'data'), Input('store-cash-equiv-symbols', 'data')],
)
def _render_revenge_hint(roundtrips, cash_equiv_symbols):
    hint = compute_revenge_hint(roundtrips, cash_equiv_symbols)
    if not hint:
        return None
    when = "today" if hint['days_ago'] < 1 else f"{hint['days_ago']:.0f} day(s) ago"
    streak = f" -- {hint['loss_streak']} losses in a row" if hint['loss_streak'] > 1 else ""
    return dbc.Alert(
        f"Heads up: your last closed trade ({hint['symbol']}, {hint['currency'] or ''} "
        f"{hint['pnl']:,.2f}) was a loss {when}{streak}. Worth an honest check on whether "
        f"\"Revenge\" applies here.", color='warning', className="py-1 mt-1 mb-0 small")


@app.callback(
    Output('plan-pretrade-risk', 'children'),
    [Input('plan-entry', 'value'), Input('plan-direction', 'value'),
     Input('plan-stop', 'value'), Input('plan-qty', 'value'), Input('plan-currency', 'value'),
     Input('plan-account', 'value'), Input('plan-plan-type', 'value'), Input('plan-atr', 'value')] +
    [Input(f'plan-a{i}-{f}', 'value') for f in ('price', 'qty', 'stop') for i in range(1, db.MAX_TARGETS + 1)],
    [State('store-openpos', 'data'), State('store-open-positions-prices', 'data'),
     State('store-cash-equiv-symbols', 'data'), State('store-plans', 'data'),
     State('store-journal', 'data'), State('store-accounts', 'data'),
     State('store-account-snapshots', 'data'), State('store-executions', 'data'),
     State('store-daily-journal', 'data'), State('store-weekly-reviews', 'data'), State('plan-symbol', 'value')],
)
def _update_pretrade_risk(entry, direction, stop, qty, currency, account_id, plan_type, atr, *rest):
    """Informational-only pre-trade risk check on the Add Plan form -- never
    disables Save Draft/Commit, matching the existing portfolio-risk banner's
    own non-blocking precedent on Open Positions (see compute_pretrade_risk_check).
    A separate callback rather than folded into _update_risk_reward: that one is
    prefix-generic (shared with the Edit modal), but "portfolio impact of adding
    a NEW trade" doesn't cleanly apply to editing an already-open position.
    Reuses the same ratcheted-stop-per-lot figure the Open Positions risk banner
    shows (_compute_ratcheted_stops_by_lot) so "existing portfolio risk" means
    the same thing in both places rather than two independent computations.

    Caps are portfolio-wide (all accounts) and follow the limits in force
    (current_posture). The trade's % of its own account is one extra plain line,
    information only (it excludes cash, so it reads high)."""
    add_vals, rest = rest[:3 * db.MAX_TARGETS], rest[3 * db.MAX_TARGETS:]
    (openpos, staged_prices, cash_equiv_symbols, plans, journal, accounts,
     snapshots, executions, daily_entries, weekly_rows, symbol) = (list(rest) + [None] * 5)[:11]
    if entry in (None, '') or stop in (None, '') or qty in (None, ''):
        return html.P("Enter entry, stop, and quantity to see this trade's risk impact.",
                       className="text-muted small mb-0")
    if not staged_prices or not staged_prices.get('priced_symbols'):
        return dbc.Alert(
            'Click "Refresh Live Prices" on Open Positions above first to see this '
            "trade's impact on portfolio risk.", color='warning', className="py-1 mb-0")

    pw = portfolio_with_cash(openpos, staged_prices, snapshots, executions, accounts)
    portfolio_value, fx = pw['value'], pw['fx']
    # The new trade's currency needs a rate too; without one, fall back to the
    # unconverted figures rather than mixing a converted portfolio with a raw trade.
    if fx and fx_rate(fx, currency) is None:
        fx = None
        portfolio_value = sum(pw['combined'].values())
    priced_symbols = staged_prices.get('priced_symbols', {})
    ratcheted_stops = _compute_ratcheted_stops_by_lot(openpos, plans, journal)
    existing_total_risk, _, _ = compute_portfolio_risk(
        openpos, ratcheted_stops, cash_equiv_symbols, priced_symbols, portfolio_value, fx=fx,
        gap_by_lot=gap_allowance_by_lot(openpos, plans, journal))

    campaign = plan_type == 'scale_in'
    gap = gap_per_share(atr, entry, stop)
    stages = compute_campaign_stages(direction, entry, stop, qty, adds_from_form(add_vals), gap) if campaign else None
    trade_rate = fx_rate(fx, currency) if fx else 1.0
    posture = current_posture(daily_entries, weekly_rows)
    caps = posture_caps(posture)
    check = compute_pretrade_risk_check(direction, entry, stop, qty, portfolio_value,
                                         existing_total_risk, trade_fx_rate=trade_rate,
                                         max_loss_override=stages['peak_risk_gap'] if stages else None,
                                         gap_per_share=gap,
                                         cap_pct=caps['risk'] * (DAY_TRADE_RISK_FRACTION
                                                                 if plan_type == 'day_trade' else 1.0),
                                         caution_pct=caps['caution'], warning_pct=caps['warning'])
    if check is None:
        return html.P("Enter entry, stop, and quantity to see this trade's risk impact.",
                       className="text-muted small mb-0")

    # What's already held in this account adds to the position (a pyramid lot).
    wanted = (symbol or '').strip().upper()
    held = sum(float(p['qty_open']) * priced_symbols[price_key(p)]['price'] for p in (openpos or [])
               if wanted and p.get('symbol') == wanted and p.get('account_id') == account_id
               and p.get('currency') == currency and price_key(p) in priced_symbols)
    position = pretrade_position_check(entry, qty, stages, held * trade_rate, portfolio_value, trade_rate,
                                       caps['max_pos'])
    tactic_issue = tactic_warning(plan_type, posture, bool(stages and len(stages['stages']) > 1))

    if check['over_per_trade_cap'] or check['projected_over_warning'] or tactic_issue or (position or {}).get('over'):
        color = 'danger'
    elif check['projected_over_caution']:
        color = 'warning'
    else:
        color = 'success'

    parts = [
        f"{'This campaign at its peak' if campaign else 'This trade'}'s risk"
        + (" incl. gap allowance" if gap else "") + ": "
        f"{currency or ''} {check['trade_risk_dollars']:,.2f} "
        f"({check['trade_risk_pct']:.2f}% of portfolio, cap {check['cap_pct']:g}%"
        + (f", {caps['state']} limits" if caps['state'] else "")
        + ("; half-size for a day trade" if plan_type == 'day_trade' else "") + ")"
        + (" ⚠ over per-trade cap" if check['over_per_trade_cap'] else ""),
        f"Portfolio heat if added: {check['existing_total_risk_pct']:.2f}% → "
        f"{check['projected_total_risk_pct']:.2f}% (ceiling {caps['warning']:g}%, all accounts"
        + (f"; in {BASE_CURRENCY} at {fmt_fx_note(fx)})" if fx else "; mixed currencies, not FX-converted)")
        + (" ⚠ Warning" if check['projected_over_warning']
           else (" ⚠ Caution" if check['projected_over_caution'] else "")),
    ]
    if position:
        parts.append(f"Position{' with what you hold' if held else ''}"
                     + (f" at {position['label']}" if campaign else "") + ": "
                     f"{position['pct']:.2f}% of portfolio (max {position['max_pct']:g}%)"
                     + (" ⚠ over the max position" if position['over'] else ""))
    tactic_line = html.Div(f"⚠ {tactic_issue}", className="fw-bold mb-1") if tactic_issue else None
    # Account values in the same units as the trade: CAD when converting, else
    # as-is (re-derived without rates when the trade's currency had none).
    acct_values = pw['account_values'] if fx or not pw['fx'] else portfolio_with_cash(
        openpos, {**staged_prices, 'fx': None}, snapshots, executions, accounts)['account_values']
    acct_value = acct_values.get(account_id) if account_id else None
    acct_line = None
    if acct_value:
        trade_risk = check['trade_risk_dollars'] * (fx_rate(fx, currency) if fx else 1.0)
        acct_line = html.Small(
            f"Information only: {trade_risk / acct_value * 100:.2f}% of "
            f"{account_name(accounts, account_id)}'s value "
            f"({(BASE_CURRENCY + ' ') if fx else ''}{acct_value:,.2f} incl. cash).",
            className="d-block mt-1")
    size_line = None
    try:
        per_share = abs(float(entry) - float(stop)) + gap
    except (TypeError, ValueError):
        per_share = 0
    if not campaign and per_share > 0 and portfolio_value:
        max_shares = int(check['cap_pct'] / 100 * portfolio_value / (per_share * trade_rate))
        size_line = html.Small(
            f"Most shares within the {check['cap_pct']:g}% cap: {max_shares:,} "
            f"({currency or ''} {per_share:,.2f}/share = stop distance"
            + (f" + {gap:,.2f} gap allowance" if gap else "") + ")."
            + (f" The {position['max_pct']:g}% position cap allows "
               f"{max(0, int((position['max_pct'] / 100 * portfolio_value - held * trade_rate) / (float(entry) * trade_rate))):,}"
               " shares: use the smaller." if position else "")
            + ("" if caps['state'] else " No daily entry yet, so this is the Strategy's maximum."),
            className="d-block mt-1")
    day_lines = []
    if plan_type == 'day_trade':
        today = dt.date.today().isoformat()
        todays = [p for p in (plans or []) if is_day_trade(p) and p.get('status') == 'committed'
                  and str(_scalar(p.get('planned_date')) or '')[:10] == today]
        losses = sum(min(0.0, _num_or_none(r.get('actual_pnl')) or 0.0)
                     * ((fx_rate(fx, _scalar(r.get('currency'))) or 1.0) if fx else 1.0)
                     for r in (journal or []) if is_day_trade(r) and r.get('status') == 'Closed'
                     and str(r.get('exit_time') or '')[:10] == today)
        limit = DAY_TRADE_DAILY_LOSS_PCT / 100 * portfolio_value
        too_many = len(todays) >= DAY_TRADE_MAX_PER_DAY
        too_much = -losses >= limit > 0
        day_lines = [
            html.Div(f"Day trades today: {len(todays)} of {DAY_TRADE_MAX_PER_DAY}"
                     + (" ⚠ limit reached -- no more day trades today" if too_many else ""),
                     className="small" + (" fw-bold" if too_many else "")),
            html.Div(f"Day-trade losses today: {(BASE_CURRENCY + ' ') if fx else ''}{-losses:,.2f} of the "
                     f"{DAY_TRADE_DAILY_LOSS_PCT:g}% limit ({limit:,.2f})"
                     + (" ⚠ limit reached -- no more day trades today" if too_much else ""),
                     className="small" + (" fw-bold" if too_much else "")),
        ]
        if too_many or too_much:
            color = 'danger'
    return dbc.Alert([tactic_line, html.Div(" | ".join(parts)), size_line, *day_lines, acct_line], color=color,
                     className="py-2 mb-0")


@app.callback(
    [Output('store-plans', 'data'), Output('plan-add-status', 'children'),
     Output('plan-symbol', 'value'), Output('plan-entry', 'value'),
     Output('plan-stop', 'value'), Output('plan-qty', 'value'),
     Output('plan-reason', 'value'), Output('plan-notes', 'value'),
     Output('plan-atr', 'value'), Output('plan-atr-status', 'children'),
     Output('plan-screenshot-upload', 'contents'),
     Output('plan-screenshot-upload', 'filename'), Output('plan-screenshot-preview', 'children')] +
    [Output(f'plan-t{i}-price', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [Output(f'plan-t{i}-qty', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [Output(f'plan-t{i}-stop', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [Output(f'plan-t{i}-rmult', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [Output('plan-trade-triggers', 'value')] + _psych_outputs('plan') +
    [Output(f'plan-a{i}-{f}', 'value', allow_duplicate=True) for f in ('price', 'qty', 'stop')
     for i in range(1, db.MAX_TARGETS + 1)],
    [Input('btn-add-plan', 'n_clicks'), Input('btn-save-draft', 'n_clicks')],
    [State('plan-symbol', 'value'), State('plan-direction', 'value'),
     State('plan-date', 'date'), State('plan-entry', 'value'),
     State('plan-stop', 'value'), State('plan-qty', 'value'),
     State('plan-reason', 'value'), State('plan-notes', 'value'),
     State('plan-atr', 'value'), State('plan-atr-mult', 'value'), State('plan-atr-timeframe', 'value'),
     State('plan-target-method', 'value'),
     State('plan-stop-method', 'value'), State('plan-trailing-activate-after', 'value'),
     State('plan-trailing-atr-mult', 'value'), State('plan-currency', 'value'),
     State('plan-trade-triggers', 'value')] + _psych_states('plan') +
    [State('plan-screenshot-upload', 'contents'),
     State('plan-screenshot-upload', 'filename'),
     State('store-plans', 'data'), State('plan-account', 'value')] +
    [State(f'plan-t{i}-price', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [State(f'plan-t{i}-qty', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [State(f'plan-t{i}-stop', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [State(f'plan-t{i}-rmult', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [State('plan-plan-type', 'value'), State('plan-trail-start-add', 'value')] +
    [State(f'plan-a{i}-{f}', 'value') for f in ('price', 'qty', 'stop') for i in range(1, db.MAX_TARGETS + 1)] +
    [State('plan-day-catalyst', 'value'), State('plan-day-setup', 'value')],
    prevent_initial_call=True,
)
def add_plan(n_add, n_draft, symbol, direction, planned_date, entry, stop, qty, reason, notes,
             atr_value, atr_mult, atr_timeframe, target_method, stop_method, trailing_activate_after,
             trailing_atr_mult, currency, trade_triggers, psych_risk, psych_urgency, psych_focus,
             screenshot_contents, screenshot_filename, plans, account_id, *target_vals):
    if not n_add and not n_draft:
        raise PreventUpdate
    plans = plans or []
    n_blank_outputs = 13 + 4 * db.MAX_TARGETS + 1 + len(PSYCH_AXES) + 3 * db.MAX_TARGETS
    plan_status = 'draft' if callback_context.triggered_id == 'btn-save-draft' else 'committed'
    # target_vals = the 4 target State groups, then plan type, trail start, and the add rows.
    n4 = 4 * db.MAX_TARGETS
    plan_type, trail_start_add = target_vals[n4], target_vals[n4 + 1]
    adds = adds_from_form(target_vals[n4 + 2:])
    day_catalyst, day_setup = (target_vals[-2], target_vals[-1]) \
        if len(target_vals) >= n4 + 2 + 3 * db.MAX_TARGETS + 2 else (None, None)
    target_vals = target_vals[:n4]
    campaign = plan_type == 'scale_in'
    day = plan_type == 'day_trade'

    if not symbol or not direction or entry in (None, '') or qty in (None, '') or not account_id:
        status = dbc.Alert("Account, Symbol, Direction, Entry Price and Quantity are required.", color='danger')
        return (no_update, status) + (no_update,) * (n_blank_outputs - 2)

    # target_vals arrives as (price1..price5, qty1..qty5, stop1..stop5, rmult1..rmult5)
    # since that's the State ordering above; rezip into (price_i, qty_i, stop_i, rmult_i)
    # quadruples.
    prices = target_vals[0:db.MAX_TARGETS]
    qtys = target_vals[db.MAX_TARGETS:2 * db.MAX_TARGETS]
    stops = target_vals[2 * db.MAX_TARGETS:3 * db.MAX_TARGETS]
    rmults = target_vals[3 * db.MAX_TARGETS:4 * db.MAX_TARGETS]
    flat = []
    for p, q, s, r in zip(prices, qtys, stops, rmults):
        flat += [p, q, s, r]
    targets = [] if campaign else build_targets_list(*flat)
    if day:
        targets = day_trade_targets(direction, entry, targets)
    error = (((validate_campaign(direction, entry, stop, adds, stop_method) or validate_swing_timeframe(atr_timeframe))
              if campaign
              else (validate_day_trade(direction, entry, stop, targets, stop_method, atr_timeframe, day_catalyst,
                                       day_setup) if day else validate_swing_timeframe(atr_timeframe))
              or _validate_target_qty_pct(targets))
             or _campaign_conflict(plans, account_id, symbol, currency, campaign or day, _closed_plan_ids()))
    error = error or (_held_conflict(account_id, symbol, currency) if day else None)
    if error:
        status = dbc.Alert(error, color='danger')
        return (no_update, status) + (no_update,) * (n_blank_outputs - 2)

    symbol = symbol.strip().upper()
    now = db.utc_now().isoformat()
    plan_id = str(uuid.uuid4())[:8]
    plan = {
        'plan_id': plan_id,
        'symbol': symbol,
        'direction': direction,
        'planned_date': planned_date,
        'created_at': now,
        'entry_price': float(entry),
        'stop_loss': float(stop) if stop not in (None, '') else None,
        'targets': targets,
        'qty': float(qty),
        'reason': reason or '',
        'notes': notes or '',
        'status': plan_status,
        'committed_at': now if plan_status == 'committed' else None,
        'target_method': target_method,
        'atr_at_commit': float(atr_value) if atr_value not in (None, '') else None,
        'atr_multiplier': float(atr_mult) if atr_mult not in (None, '') else None,
        'atr_timeframe': atr_timeframe if atr_timeframe in ATR_TIMEFRAME_LABELS else DEFAULT_ATR_TIMEFRAME,
        'stop_method': stop_method if stop_method in dict(STOP_METHODS) else 'ladder',
        'trailing_stop_enabled': 0 if stop_method in (None, 'ladder') else 1,
        'trailing_activate_after_target': int(trailing_activate_after) if trailing_activate_after not in (None, '') else None,
        'trailing_atr_multiplier': float(trailing_atr_mult) if trailing_atr_mult not in (None, '') else None,
        'account_id': account_id,
        'currency': currency or 'USD',
        **_psych_fields([psych_risk, psych_urgency, psych_focus]),
        'trade_triggers': list(trade_triggers or []),
        'screenshot_path': _save_plan_screenshot(plan_id, screenshot_filename, screenshot_contents),
        'lessons_learned': '',
        'lesson_keep': '',
        'plan_type': 'scale_in' if campaign else ('day_trade' if day else 'scale_out'),
        'day_catalyst': ((day_catalyst or '').strip() or None) if day else None,
        'day_setup': (day_setup or None) if day else None,
        'adds': adds if campaign else [],
        'trail_start_add': int(trail_start_add or 0) if campaign else None,
    }
    db.insert_plan(plan)
    plan['adds'] = db._clean_adds(plan['adds'])
    plans = plans + [plan]
    verb = "saved as a draft" if plan_status == 'draft' else "committed and saved to the database"
    status = dbc.Alert(f"✅ Plan for {symbol} ({direction}) {verb}.", color='success')
    return ((plans, status, '', None, None, None, '', '', None, '', None, None, '')
            + (None,) * (4 * db.MAX_TARGETS) + ([],) + (0,) * len(PSYCH_AXES) + (None,) * (3 * db.MAX_TARGETS))


def _closed_plan_ids():
    """Plan ids whose trade has closed (from a fresh journal build) -- a closed
    plan never blocks a new campaign in the same symbol."""
    return {r['plan_id'] for r in _journal_from_db() if r.get('status') == 'Closed'}


def _campaign_conflict(plans, account_id, symbol, currency, new_is_campaign, closed_ids,
                       exclude_plan_id=None):
    """One campaign per account + symbol at a time: a campaign owns every fill
    of its symbol in its account, so it can't share that position with any
    other open plan -- and no other plan can start while a campaign is open.
    An error message, or None."""
    sym = (symbol or '').strip().upper()
    for p in (plans or []):
        if p.get('plan_id') == exclude_plan_id or p.get('plan_id') in (closed_ids or set()):
            continue
        if p.get('symbol') != sym or _scalar(p.get('account_id')) != account_id:
            continue
        if not plan_fits_position(p, account_id, sym, currency):
            continue
        if new_is_campaign or is_campaign(p) or is_day_trade(p):
            kind = ("a scale-in campaign" if is_campaign(p) else "a day trade" if is_day_trade(p) else "a plan")
            return (f"There's already {kind} open for {sym} in this account ({p['plan_id']}). A scale-in "
                    f"campaign or a day trade owns every fill of its symbol in its account, so it can't share "
                    f"it with another open plan -- close, cancel or delete that one first.")
    return None


@app.callback(
    [Output('plan-symbol', 'value', allow_duplicate=True),
     Output('plan-direction', 'value', allow_duplicate=True),
     Output('plan-currency', 'value', allow_duplicate=True),
     Output('plan-entry', 'value', allow_duplicate=True),
     Output('plan-qty', 'value', allow_duplicate=True),
     Output('plan-notes', 'value', allow_duplicate=True),
     Output('unmatched-rt-status', 'children'),
     Output('plan-account', 'value', allow_duplicate=True)],
    Input('btn-log-selected-plan', 'n_clicks'),
    [State('unmatched-rt-table', 'derived_virtual_data'),
     State('unmatched-rt-table', 'derived_virtual_selected_rows')],
    prevent_initial_call=True,
)
def prefill_plan_from_roundtrip(n_clicks, virtual_data, selected_rows):
    if not n_clicks:
        raise PreventUpdate
    if not selected_rows or not virtual_data:
        return (no_update, no_update, no_update, no_update, no_update, no_update,
                dbc.Alert("Select a row in the table first.", color='warning'), no_update)
    # derived_virtual_* reflects the table's current sort/filter view, so the
    # selected index maps correctly even when the user has sorted or filtered it.
    rt = virtual_data[selected_rows[0]]
    currency = rt.get('currency') or ''
    note = (f"Logged retroactively from import. Actual entry {currency} {rt['entry_price']:.2f}, "
            f"exit {currency} {rt['exit_price']:.2f}, qty {rt['qty']:.4g}. Set your intended stop/target below.")
    return (rt['symbol'], rt['direction'], rt.get('currency') or 'USD',
            round(rt['entry_price'], 4), round(rt['qty'], 4), note, '', rt.get('account_id') or no_update)


@app.callback(
    [Output('plan-symbol', 'value', allow_duplicate=True),
     Output('plan-direction', 'value', allow_duplicate=True),
     Output('plan-currency', 'value', allow_duplicate=True),
     Output('plan-entry', 'value', allow_duplicate=True),
     Output('plan-qty', 'value', allow_duplicate=True),
     Output('plan-notes', 'value', allow_duplicate=True),
     Output('plan-account', 'value', allow_duplicate=True)],
    Input({'type': 'btn-log-open-plan', 'index': ALL}, 'n_clicks'),
    State('store-openpos', 'data'),
    prevent_initial_call=True,
)
def prefill_plan_from_open_position(n_clicks_list, openpos):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    lot_id_clicked = triggered['index']
    op = next((p for p in (openpos or []) if p.get('lot_id') == lot_id_clicked), None)
    if not op:
        raise PreventUpdate
    currency = op.get('currency') or ''
    note = (f"Logged for a currently-open position (no plan existed before it was executed). "
            f"Avg price {currency} {op['avg_price']:.2f}, qty {op['qty_open']:.4g}. Set your intended stop/target below.")
    return (op['symbol'], 'BUY', op.get('currency') or 'USD',
            round(op['avg_price'], 4), round(op['qty_open'], 4), note, op.get('account_id') or no_update)


@app.callback(
    Output('draft-plans-list', 'children'),
    [Input('store-plans', 'data'), Input('account-view', 'value'), Input('store-accounts', 'data')],
)
def render_draft_plans(plans, view, accounts):
    drafts = [p for p in _filter_view(plans, view) if p.get('status') == 'draft']
    show_account = _is_all_view(view) and len(accounts or []) > 1
    if not drafts:
        return html.P("None.", className="text-muted")
    header = dbc.Row([
        dbc.Col(html.B("Symbol"), width=2), dbc.Col(html.B("Dir"), width=1),
        dbc.Col(html.B("Entry"), width=2), dbc.Col(html.B("Qty"), width=2),
        dbc.Col(html.B("Method"), width=2), dbc.Col(html.B(""), width=3),
    ], className="mb-1")
    rows = [header]
    for p in drafts:
        actions = dbc.ButtonGroup([
            dbc.Button("Edit", size='sm', color='primary', outline=True,
                       id={'type': 'btn-edit', 'index': p['plan_id']}),
            dbc.Button("Commit", size='sm', color='success', outline=True,
                       id={'type': 'btn-draft-commit', 'index': p['plan_id']}),
            dbc.Button("Discard", size='sm', color='danger', outline=True,
                       id={'type': 'btn-delete', 'index': p['plan_id']}),
        ])
        rows.append(dbc.Row([
            dbc.Col([p['symbol'], html.Div(account_name(accounts, p.get('account_id')),
                                           className="text-muted small")] if show_account else p['symbol'],
                    width=2), dbc.Col(p['direction'], width=1),
            dbc.Col(f"{p.get('currency') or ''} {p['entry_price']:.2f}", width=2), dbc.Col(f"{p['qty']:.4g}", width=2),
            dbc.Col(p.get('target_method') or '-', width=2), dbc.Col(actions, width=3),
        ], className="mb-1 align-items-center"))
    return rows


@app.callback(
    [Output('store-plans', 'data', allow_duplicate=True),
     Output('plan-add-status', 'children', allow_duplicate=True)],
    Input({'type': 'btn-draft-commit', 'index': ALL}, 'n_clicks'),
    State('store-plans', 'data'),
    prevent_initial_call=True,
)
def commit_draft_plan(n_clicks_list, plans):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    plan_id = triggered['index']
    now = db.utc_now().isoformat()
    if not db.update_plan(plan_id, {'status': 'committed', 'committed_at': now}):
        raise PreventUpdate

    new_plans = []
    for p in (plans or []):
        if p['plan_id'] == plan_id:
            p = dict(p)
            p['status'] = 'committed'
            p['committed_at'] = now
        new_plans.append(p)
    status = dbc.Alert("✅ Draft committed -- now eligible for execution matching.", color='success')
    return new_plans, status


def _build_journal_and_unmatched(plans, rt_df, op_df, plan_matches, plan_lot_matches=None, accounts=None,
                                 regime_context=None, locks=None):
    """Core journal-building logic, shared between the compute_journal Dash
    callback (fed from dcc.Store data already loaded in the browser) and the
    get_journal MCP tool (fed from a fresh direct read of the database).
    Drafts aren't finalized yet -- keep them out of the scored journal and out
    of execution matching until committed (see the Draft Trade Plans card)."""
    all_plans = plans or []
    plans = [p for p in all_plans if p.get('status') == 'committed']
    journal_rows, unmatched_rt_ids = match_plans_to_roundtrips(
        plans, rt_df, op_df, plan_matches, manual_lot_matches=plan_lot_matches,
        accounts=accounts if accounts is not None else db.fetch_accounts(),
        locks=locks if locks is not None else db.fetch_plan_locks())
    # Stale: a committed plan with no fills, older than the threshold -- cancel
    # it or keep it, but don't let it linger (it can still be auto-matched to
    # a later, unrelated trade).
    stale_after = load_stale_plan_days()
    for row in journal_rows:
        age = plan_age_days(row) if row.get('status') == 'Planned (no execution matched yet)' else None
        row['age_days'] = age
        row['stale'] = age is not None and age >= stale_after
    journal_rows += [cancelled_journal_row(p) for p in all_plans if is_cancelled(p)]
    add_regime_checks(journal_rows, regime_context)
    reviews = regime_context.get('stop_reviews') if regime_context is not None else db.fetch_stop_reviews()
    flags = followed_plan_flags(journal_rows, practice_items(journal_rows, reviews))
    for row in journal_rows:
        row['followed_plan'] = flags.get(row['plan_id'])

    for row in journal_rows:
        if row.get('entry_time') is not None:
            row['entry_time'] = str(row['entry_time'])
        if row.get('exit_time') is not None:
            row['exit_time'] = str(row['exit_time'])

    unmatched = rt_df[rt_df['rt_id'].isin(unmatched_rt_ids)].copy() \
        if rt_df is not None and not rt_df.empty else pd.DataFrame()
    if not unmatched.empty:
        unmatched['entry_time'] = unmatched['entry_time'].astype(str)
        unmatched['exit_time'] = unmatched['exit_time'].astype(str)

    return journal_rows, unmatched.to_dict('records')


@app.callback(
    [Output('store-journal', 'data'), Output('store-unmatched-rts', 'data')],
    [Input('store-plans', 'data'), Input('store-roundtrips', 'data'),
     Input('store-openpos', 'data'), Input('store-plan-matches', 'data'),
     Input('store-plan-lot-matches', 'data')],
)
def compute_journal(plans, roundtrips, openpos, plan_matches, plan_lot_matches):
    rt_df = pd.DataFrame(roundtrips) if roundtrips else pd.DataFrame()
    if not rt_df.empty:
        rt_df['entry_time'] = pd.to_datetime(rt_df['entry_time'], utc=True)
        rt_df['exit_time'] = pd.to_datetime(rt_df['exit_time'], utc=True)

    op_df = pd.DataFrame(openpos) if openpos else pd.DataFrame()
    if not op_df.empty:
        op_df['entry_time'] = pd.to_datetime(op_df['entry_time'], utc=True)

    locks = db.fetch_plan_locks()
    rows, unmatched = _build_journal_and_unmatched(plans, rt_df, op_df, plan_matches, plan_lot_matches,
                                                   locks=locks)
    sync_plan_locks(rows, locks)  # lock trades that have just closed (2026-11-09)
    return rows, unmatched


@app.callback(
    Output('store-plan-matches', 'data', allow_duplicate=True),
    Input({'type': 'btn-accept-lock', 'index': ALL}, 'n_clicks'),
    State('store-plan-matches', 'data'),
    prevent_initial_call=True,
)
def accept_lock_cb(clicks, plan_matches):
    """Accept a locked trade's new fills or recalculated result: the lock is dropped and
    the next journal build locks the trade again as it stands now."""
    trigger = callback_context.triggered_id
    if not isinstance(trigger, dict) or not callback_context.triggered[0]['value']:
        raise PreventUpdate
    db.delete_plan_lock(trigger['index'])
    return dict(plan_matches or {})  # re-sent so the journal rebuilds


@app.callback(
    Output('practices-body', 'children'),
    [Input('store-journal', 'data'), Input('account-view', 'value'), Input('store-daily-journal', 'data'),
     Input('store-improvements', 'data'), Input('store-plans', 'data')],
)
def render_practices_cb(journal, view, entries, improvements, plans):
    return render_practices(_filter_view(journal or [], view), stop_reviews=db.fetch_stop_reviews(),
                            entries=entries, improvement_list=improvement_rows(plans, entries, improvements))


@app.callback(
    Output('improvement-modal', 'is_open'),
    Input('btn-improvement-register', 'n_clicks'),
    prevent_initial_call=True,
)
def open_improvement_register(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return True


@app.callback(
    [Output('improvement-body', 'children'), Output('improvement-view', 'options')],
    [Input('improvement-modal', 'is_open'), Input('improvement-view', 'value'), Input('store-improvements', 'data')],
    [State('store-plans', 'data'), State('store-daily-journal', 'data'), State('store-journal', 'data'),
     State('store-weekly-actions', 'data')],
)
def render_improvement_register_cb(is_open, view, improvements, plans, entries, journal, actions):
    rows = improvement_rows(plans, entries, improvements)
    counts = improvement_counts(rows)
    options = [{'label': f"{label} ({counts.get(v, 0)})", 'value': v}
               for v, label in IMPROVEMENT_STATUSES + [('all', 'All')]]
    if not is_open:
        return no_update, options
    versions = strategy_versions()
    items = practice_items(journal or [], db.fetch_stop_reviews())
    return render_improvement_register(rows, view or 'new', became_options(actions, versions), items,
                                       actions, versions), options


@app.callback(
    [Output('store-improvements', 'data'), Output('improvement-status', 'children')],
    Input('btn-improvement-save', 'n_clicks'),
    [State({'type': 'imp-cat', 'index': ALL}, 'value'), State({'type': 'imp-cat', 'index': ALL}, 'id'),
     State({'type': 'imp-status', 'index': ALL}, 'value'), State({'type': 'imp-became', 'index': ALL}, 'value'),
     State({'type': 'imp-tapes', 'index': ALL}, 'value'), State({'type': 'imp-note', 'index': ALL}, 'value'),
     State('store-improvements', 'data'), State('store-plans', 'data'), State('store-daily-journal', 'data')],
    prevent_initial_call=True,
)
def save_improvements_cb(n_clicks, cats, ids, statuses, picked, tapes, notes, improvements, plans, entries):
    """Saves every row on screen whose values differ from what's stored (a row never saved
    differs once it's given a type, a status other than New, a link or a note). An adopted or
    verified lesson must say what it became; if one doesn't, nothing is saved."""
    if not n_clicks:
        raise PreventUpdate
    saved = {(i['source'], i['source_ref']): i for i in improvements or []}
    labels = {r['key']: f"{r['date']} {r['label']}" for r in improvement_rows(plans, entries, improvements)}
    writes, missing = [], []
    for cat, ident, status, sel, tp, note in zip(cats, ids, statuses, picked, tapes, notes):
        source, _, ref = ident['index'].partition('|')
        new = {'category': None if cat in (None, '', 'unset') else cat, 'status': status or 'new',
               'became': became_from_values(sel, tp), 'note': (note or '').strip() or None}
        if new['status'] in ('adopted', 'verified') and not new['became']:
            missing.append(labels.get(ident['index'], ident['index']))
        old = saved.get((source, ref), {})
        old = {'category': old.get('category'), 'status': old.get('status') or 'new',
               'became': old.get('became') or [], 'note': old.get('note')}
        if any((old[k] or None) != (v or None) for k, v in new.items()):
            writes.append((source, ref, new))
    if missing:
        return no_update, html.Span("Say what it became for each adopted or verified lesson: "
                                    + "; ".join(missing) + ". Nothing was saved.", className="text-danger")
    for source, ref, new in writes:
        db.save_improvement(source, ref, **new)
    changed = len(writes)
    if not changed:
        return no_update, html.Span("Nothing changed.", className="text-muted")
    return db.fetch_improvements(), html.Span(f"Saved {changed} lesson{'s' if changed != 1 else ''}.",
                                              className="text-success")


@app.callback(
    [Output('unmatched-rt-table', 'data'), Output('unmatched-rt-table', 'selected_rows')],
    [Input('store-unmatched-rts', 'data'), Input('store-dismissed-rts', 'data'),
     Input('account-view', 'value'), Input('store-accounts', 'data')],
)
def render_unmatched_roundtrips(data, dismissed_ids, view, accounts):
    data = [r for r in _filter_view(data, view) if not is_archived(r, accounts)]
    if not data:
        return [], []
    dismissed = set(dismissed_ids or [])
    df = pd.DataFrame(data)
    # Per-trade dismissal is purely a display filter on this already-derived,
    # already-unmatched list -- it never touches executions, plans, or the
    # open-positions matching logic (see dismissed_round_trips in db.py).
    df = df[~df['rt_id'].isin(dismissed)]
    if df.empty:
        return [], []
    df = df.sort_values('exit_time', ascending=False)
    df['account'] = [account_name(accounts, _scalar(a)) for a in df.get('account_id', pd.Series([None] * len(df)))]
    df['exit_time'] = df['exit_time'].str[:10]  # date only, full timestamp is noise in the table
    # DataTable's `data` prop requires every cell to be a string/number/boolean,
    # so every column holding a list or dict is dropped -- exit_fill_list,
    # entry_fill_list, entry_fill_ids and any added later (the 2026-10-19
    # campaign work added entry_fill_list and broke this table when the drop
    # list named columns one by one). Neither the "Log Plan" prefill nor
    # Dismiss reads them.
    nested = [c for c in df.columns if df[c].map(lambda v: isinstance(v, (list, dict, tuple))).any()]
    df = df.drop(columns=nested)
    # rt_id rides along in each record for the "Log Plan" callback below, even
    # though it's not in the table's `columns` (DataTable only *displays*
    # listed columns; unlisted keys stay on the record and survive sort/filter).
    return df.to_dict('records'), []


@app.callback(
    [Output('store-dismissed-rts', 'data', allow_duplicate=True),
     Output('unmatched-rt-status', 'children', allow_duplicate=True)],
    Input('btn-dismiss-selected-rt', 'n_clicks'),
    [State('unmatched-rt-table', 'derived_virtual_data'),
     State('unmatched-rt-table', 'derived_virtual_selected_rows'),
     State('store-dismissed-rts', 'data')],
    prevent_initial_call=True,
)
def dismiss_selected_roundtrip(n_clicks, virtual_data, selected_rows, dismissed):
    if not n_clicks:
        raise PreventUpdate
    if not selected_rows or not virtual_data:
        return no_update, dbc.Alert("Select a row in the table first.", color='warning')
    rt = virtual_data[selected_rows[0]]
    db.dismiss_round_trip(rt['rt_id'])
    dismissed = list(dismissed or [])
    if rt['rt_id'] not in dismissed:
        dismissed.append(rt['rt_id'])
    status = dbc.Alert(
        f"Dismissed {rt['symbol']} (exit {rt['exit_time']}) from this list -- your imported "
        "executions are untouched. Undo any time from the \"Dismissed\" list below.",
        color='success')
    return dismissed, status


@app.callback(
    Output('dismissed-rt-list', 'children'),
    [Input('store-dismissed-rts', 'data'), Input('store-unmatched-rts', 'data'),
     Input('store-accounts', 'data')],
)
def render_dismissed_rt_list(dismissed_ids, unmatched_data, accounts):
    # store-unmatched-rts still carries every truly-unmatched round trip
    # regardless of dismissal (dismissal is filtered only at render time above),
    # so it's a reliable lookup for a human-readable label here. Archived
    # trades (before the account's tracking start date) get no button.
    lookup = {r['rt_id']: r for r in (unmatched_data or [])}
    archived = {rt_id for rt_id, r in lookup.items() if is_archived(r, accounts)}
    dismissed_ids = [i for i in (dismissed_ids or []) if i not in archived]
    note = html.Small(f" ({len(archived)} archived before the tracking start date -- see Data Management)",
                      className="text-muted") if archived else None
    if not dismissed_ids:
        return [html.Span("none", className="text-muted small"), note]
    buttons = []
    for rt_id in dismissed_ids:
        r = lookup.get(rt_id)
        label = f"{r['symbol']} ({str(r['exit_time'])[:10]})" if r else rt_id[:8]
        buttons.append(dbc.Button(
            [label, html.Span(" ×", className="ms-1")], size='sm', color='outline-secondary',
            id={'type': 'btn-undismiss-rt', 'index': rt_id}, title="Restore to the list above",
            className="me-1 mb-1"))
    return [dbc.ButtonGroup(buttons), note]


@app.callback(
    Output('store-dismissed-rts', 'data', allow_duplicate=True),
    Input({'type': 'btn-undismiss-rt', 'index': ALL}, 'n_clicks'),
    State('store-dismissed-rts', 'data'),
    prevent_initial_call=True,
)
def undismiss_roundtrip(n_clicks_list, current):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    rt_id = triggered['index']
    db.undismiss_round_trip(rt_id)
    return [r for r in (current or []) if r != rt_id]


@app.callback(
    Output('filter-symbol', 'options'),
    [Input('store-journal', 'data'), Input('account-view', 'value')],
)
def populate_symbol_filter(data, view):
    data = _filter_view(data, view)
    if not data:
        return []
    symbols = sorted({row['symbol'] for row in data if row.get('symbol')})
    return [{'label': s, 'value': s} for s in symbols]


@app.callback(
    [Output('filter-score', 'min'), Output('filter-score', 'max'),
     Output('filter-score', 'value'), Output('filter-score', 'marks')],
    Input('store-journal', 'data'),
    [State('filter-score', 'value'), State('filter-score', 'min'), State('filter-score', 'max')],
)
def update_score_filter_range(data, value, cur_min, cur_max):
    """The Score Range slider used to have a hard-coded [-100, 150] range --
    Plan Score is actual_pnl / planned_reward_total * 100, unbounded above
    when the actual gain far exceeds a modest planned target, so a
    legitimately high (or low) score had no way to be brought into view: the
    slider itself couldn't select beyond its own fixed max/min, so this
    wasn't a narrow filter setting, it was a silent, permanent exclusion.
    Confirmed live 2026-08-31: a real IBB trade scored 391.65% and vanished
    from the journal table entirely with no way to reveal it. Now the
    slider's own min/max expand to cover whatever's actually in the data
    (with headroom beyond it), so nothing can be permanently invisible.

    The selection is remembered between sessions (PERSIST), and Dash drops a
    remembered value whenever a callback writes it -- so the value is only
    rewritten when the slider currently spans its whole range (nothing
    narrowed), expanding it to the new full range. A narrowed, remembered
    selection is left alone."""
    df = pd.DataFrame(data) if data else pd.DataFrame()
    scores = df['score'].dropna() if not df.empty and 'score' in df.columns else pd.Series(dtype=float)
    lo = min(-100, int(scores.min()) - 10) if not scores.empty else -100
    hi = max(150, int(scores.max()) + 10) if not scores.empty else 150
    span = hi - lo
    step = 50 if span <= 400 else (100 if span <= 1000 else max(1, span // 10))
    marks = {i: str(i) for i in range(lo, hi + 1, step)}
    marks[hi] = str(hi)
    spans_all = (not value or len(value) != 2
                 or (cur_min is not None and cur_max is not None
                     and value[0] <= cur_min and value[1] >= cur_max))
    return lo, hi, ([lo, hi] if spans_all else no_update), marks


@app.callback(
    [Output('journal-table', 'children'), Output('journal-count', 'children'),
     Output('journal-page', 'max_value'), Output('journal-page', 'active_page'),
     Output('journal-page', 'style'), Output('filter-status', 'options')],
    [Input('store-journal', 'data'), Input('filter-symbol', 'value'), Input('filter-score', 'value'),
     Input('filter-status', 'value'), Input('store-obs-version', 'data'),
     Input('account-view', 'value'), Input('store-accounts', 'data'),
     Input('journal-period', 'value'), Input('journal-sort', 'value'), Input('journal-page', 'active_page')],
)
def render_journal(data, symbol_filter, score_range, tab, _obs_version, view, accounts,
                   period='90', sort='recent', page=1):
    # A new filter or tab starts again at page 1; only the pager itself pages.
    if callback_context.triggered_id != 'journal-page':
        page = 1
    data = _filter_view(data, view)
    show_account = _is_all_view(view) and len(accounts or []) > 1
    tab_options = [{'label': lbl, 'value': v} for v, lbl in JOURNAL_TABS]
    if not data:
        return (html.Div("No trade plans yet — add one above, or import a CSV and log a plan for an "
                         "existing trade.", className="text-muted"), '', 1, 1, {'display': 'none'}, tab_options)
    # _obs_version is a pure cache-buster (see add/delete_observation) so this
    # table re-renders -- and the "Off-Plan Exit" prompt below disappears --
    # right after logging a deviation, with no page reload needed.
    deviation_logged = db.fetch_deviation_logged_plan_ids()
    df = pd.DataFrame(data)
    if symbol_filter:
        df = df[df['symbol'] == symbol_filter]
    if score_range:
        lo, hi = score_range
        has_score = df['score'].notna()
        df = df[~has_score | (df['score'].between(lo, hi))]
    n_stale = int(df['stale'].fillna(False).astype(bool).sum()) if 'stale' in df else 0
    view_ = journal_page_view(df.to_dict('records'), tab or 'active', period, sort, page)
    tab_options = [{'label': f"{lbl} ({view_['counts'][v]})", 'value': v} for v, lbl in JOURNAL_TABS]
    hidden = view_['hidden_by_period']
    count = _showing_text(view_['page'], JOURNAL_PAGE_SIZE, len(view_['rows']), view_['total'], "trades",
                          f" · {hidden} older closed/cancelled hidden by the period" if hidden else "")
    pager = (view_['pages'], view_['page'], {'display': 'none'} if view_['pages'] <= 1 else {})
    if not view_['rows']:
        return (html.Div("No trades match the current filters" + (" -- try a longer period." if hidden else "."),
                         className="text-muted"), count, *pager, tab_options)
    df = pd.DataFrame(view_['rows'])

    # Percentage-based flex widths instead of Bootstrap's 12-unit integer grid --
    # the Actions column needs real room for 4 outline buttons, which a 1/12
    # share never gave it (buttons overlapped/clipped their neighbors). Header
    # and body rows share this single list so they can't drift out of sync.
    JOURNAL_COL_PCTS = [6, 5, 15, 7, 9, 6, 7, 6, 7, 10, 22]  # sums to 100

    def _jcol(content, i):
        pct = JOURNAL_COL_PCTS[i]
        return dbc.Col(content, style={'flex': f'0 0 {pct}%', 'maxWidth': f'{pct}%'})

    header = dbc.Row([
        _jcol(html.B("Symbol"), 0), _jcol(html.B("Dir"), 1),
        _jcol(html.B("Entry (plan→actual)"), 2), _jcol(html.B("Exit"), 3),
        _jcol(html.B("Actual P&L"), 4), _jcol(html.B("R-mult"), 5),
        _jcol(html.B("Plan R:R"), 6),
        _jcol(html.B("Score"), 7), _jcol(html.B("Targets"), 8),
        _jcol(html.B("Status"), 9), _jcol(html.B(""), 10),
    ], className="mb-1 mx-0", style=STICKY_HEADER)

    # Full status text (plus manual-match note) moves into a hover tooltip so
    # the cell itself can stay a short, non-wrapping badge -- the old plain-text
    # rendering wrapped mid-word ("Open (in progres|s)") in a narrow column.
    STATUS_BADGE = {'Closed': ('Closed', 'success'), 'Open (in progress)': ('Open', 'info'),
                     'Planned (no execution matched yet)': ('Planned', 'secondary'),
                     'Cancelled': ('Cancelled', 'dark')}

    rows = [header]
    if n_stale and (tab or 'active') in ('active', 'all'):
        rows.insert(0, dbc.Alert(
            f"{n_stale} planned trade{'s are' if n_stale != 1 else ' is'} {load_stale_plan_days()}+ days old with "
            f"no fills (marked Stale) -- cancel or keep {'them' if n_stale != 1 else 'it'}.",
            color='warning', className="py-1 mb-2 small"))
    for _, r in df.iterrows():
        currency = r['currency'] or ''
        entry_disp = (f"{currency} {r['entry_price']:.2f} → {r['entry_actual']:.2f}" if pd.notna(r['entry_actual'])
                      else f"{currency} {r['entry_price']:.2f} → (open)")
        exit_disp = f"{currency} {r['exit_actual']:.2f}" if pd.notna(r['exit_actual']) else "-"
        pnl_disp = f"{currency} {r['actual_pnl']:.2f}" if pd.notna(r['actual_pnl']) else "-"
        r_disp = f"{r['r_multiple']:.2f}R" if pd.notna(r['r_multiple']) else "-"
        score_disp = html.Span(f"{r['score']:.0f}%", className=f"badge bg-{r['color']}") if pd.notna(r['score']) \
            else html.Span("N/A", className="badge bg-secondary")
        targets_total = r.get('targets_total') or 0
        campaign = r.get('campaign') if isinstance(r.get('campaign'), dict) else None
        if campaign:
            done, planned = campaign.get('adds_done') or 0, campaign.get('adds_planned') or 0
            targets_disp = html.Span(f"Adds {done}/{planned}", title="Scale-in campaign: adds made / planned",
                                     className=f"badge bg-{'success' if planned and done == planned else 'info'}")
        elif targets_total:
            targets_hit = r.get('targets_hit') or 0
            t_color = 'success' if targets_hit == targets_total else ('warning' if targets_hit else 'secondary')
            targets_disp = html.Span(f"{targets_hit}/{targets_total}", className=f"badge bg-{t_color}")
        else:
            targets_disp = html.Span("—", className="text-muted")
        rr = r.get('risk_reward_ratio')
        rr_disp = html.Span(f"{rr:.1f}:1", className=f"badge bg-{'success' if rr >= 2 else 'warning' if rr >= 1 else 'danger'}") \
            if pd.notna(rr) else html.Span("—", className="text-muted")
        actions = dbc.ButtonGroup([
            dbc.Button("View", size='sm', color='secondary', outline=True,
                       id={'type': 'btn-view', 'index': r['plan_id']}),
            dbc.Button("Notes", size='sm', color='info', outline=True,
                       id={'type': 'btn-log-observation-plan', 'index': r['plan_id']},
                       title="Log a dated note (optionally with a psych reading) for this trade -- "
                             "works on closed trades too, unlike Open Positions' Observations button."),
            dbc.Button("Reinstate", size='sm', color='success', outline=True,
                       id={'type': 'btn-reinstate-plan', 'index': r['plan_id']},
                       title="Back to a committed plan (the cancellation and its replay are cleared).")
            if r['status'] == 'Cancelled' else
            dbc.Button("Match", size='sm', color='info', outline=True,
                       id={'type': 'btn-match', 'index': r['plan_id']}),
            dbc.Button("Edit", size='sm', color='primary', outline=True,
                       id={'type': 'btn-edit', 'index': r['plan_id']}),
            *([dbc.Button("Cancel", size='sm', color='warning', outline=True,
                          id={'type': 'btn-cancel-plan', 'index': r['plan_id']},
                          title="Abandon this plan before any fill. It's kept, with a reason, and replayed "
                                "to show what it would have done.")]
              if r['status'] == 'Planned (no execution matched yet)' else []),
            dbc.Button("Del", size='sm', color='danger', outline=True,
                       id={'type': 'btn-delete', 'index': r['plan_id']}),
        ])
        label, color = STATUS_BADGE.get(r['status'], (r['status'], 'secondary'))
        tooltip = r['status'] + (' (manual match)' if r.get('manual_match') == True else '')
        lk = r.get('lock') if isinstance(r.get('lock'), dict) else None
        if r.get('lock_state') == 'locked' and lk:
            tooltip += (f"\nLocked {str(lk.get('locked_at'))[:10]}: this trade keeps its fills and match even if "
                        "later plans or imports would pick differently.")
        status_children = [html.Span(label + (' *' if r.get('manual_match') == True else '')
                                     + (' 🔒' if r.get('lock_state') == 'locked' else ''),
                                      className=f"badge bg-{color}", title=tooltip)]
        changes = r.get('lock_changes') if isinstance(r.get('lock_changes'), list) else []
        if r.get('lock_state') == 'broken' or changes:
            what = ("⚠ Fills changed since it closed" if r.get('lock_state') == 'broken'
                    else "⚠ Result recalculated")
            detail = "\n".join(f"{lab}: {fmt_lock_value(was)} at close, now {fmt_lock_value(now)}"
                               for lab, was, now in changes)
            tip = ((f"The trade locked on {str(lk.get('locked_at'))[:10]} no longer matches the imported fills "
                    "(a corrected or late fill, or it reopened). It's shown as the fills read now.\n"
                    if r.get('lock_state') == 'broken' and lk else
                    "The figures differ from when the trade closed, usually after a rule or plan change.\n")
                   + detail + "\nAccept to lock it as it reads now.")
            status_children.append(html.Div([
                html.Span(what, className="small text-warning fw-bold", title=tip),
                dbc.Button("Accept", size='sm', color='warning', outline=True, className="ms-1",
                           id={'type': 'btn-accept-lock', 'index': r['plan_id']}, title=tip,
                           style={'fontSize': '0.7rem', 'padding': '0.1rem 0.35rem'})], className="mt-1"))
        # Auto-detected "exited contrary to plan" prompt: stop_breached is
        # computed purely from the reconciled Wealthsimple fills vs. the plan
        # (see analyze_targets_and_stop), not a self-reported flag -- once a
        # deviation observation exists for this plan, the prompt retires
        # itself in favor of a quiet acknowledgment.
        if r['status'] == 'Closed' and r.get('stop_breached'):
            if r['plan_id'] in deviation_logged:
                status_children.append(html.Div(
                    html.Small("Deviation logged", className="text-muted"), className="mt-1"))
            else:
                status_children.append(html.Div(
                    dbc.Button("⚠ Off-Plan Exit", size='sm', color='danger', outline=True,
                               id={'type': 'btn-log-deviation', 'index': r['plan_id']},
                               style={'fontSize': '0.7rem', 'padding': '0.1rem 0.35rem'}),
                    className="mt-1"))
        elif r['status'] == 'Planned (no execution matched yet)' and r.get('stale') == True:  # noqa: E712 (numpy bool / NaN)
            status_children.append(html.Div(html.Span(
                f"Stale {int(r['age_days'])}d", className="badge bg-warning text-dark",
                title="No fills since this plan went live. Cancel it, or keep it if it's still valid -- "
                      "a lingering plan can be auto-matched to a later, unrelated trade."), className="mt-1"))
        elif r['status'] == 'Cancelled':
            cf = r.get('counterfactual') if isinstance(r.get('counterfactual'), dict) else None
            if cf and cf.get('outcome'):
                short = ("never filled" if cf['outcome'] == 'never_filled' else
                         (f"{cf['r']:+.2f}R" if cf.get('r') is not None else "filled") +
                         f" ({CANCEL_OUTCOME_TEXT[cf['outcome']]})")
                tone = ("text-muted" if cf['outcome'] == 'never_filled' or cf.get('r') is None
                        else "text-success" if cf['r'] > 0 else "text-danger")
                status_children.append(html.Div(html.Small(
                    f"If kept: {short}" + ("" if cf.get('final') else " so far"), className=tone,
                    title=counterfactual_text(cf, currency)), className="mt-1"))
            else:
                status_children.append(html.Div(html.Small("If kept: not checked", className="text-muted"),
                                                className="mt-1"))
        elif r['status'] == 'Closed' and r.get('stop_outcome') == 'slippage':
            status_children.append(html.Div(
                html.Small("Stopped out (slippage)", className="text-muted",
                           title="Exit filled slightly past the stop, within tolerance -- not a breach."),
                className="mt-1"))
        n_regime = r.get('regime_breaches')
        if n_regime is not None and pd.notna(n_regime):
            check = r.get('regime_check') if isinstance(r.get('regime_check'), dict) else {}
            tip = "\n".join(b for d in check.get('days', []) for b in d['breaches']) or \
                "Every buy fit the regime in force at that day's open."
            status_children.append(html.Div(html.Span(
                f"⚠ Regime {int(n_regime)}" if n_regime else "✓ Regime",
                className="badge bg-danger" if n_regime else "small text-success", title=tip), className="mt-1"))
        if r.get('followed_plan') == True:  # noqa: E712 (numpy bool / NaN)
            status_children.append(html.Div(html.Span(
                "✓ Followed the plan", className="small text-success",
                title="No practice exception and the stop wasn't breached -- a good trade, whatever the result."),
                className="mt-1"))
        if trade_needs_lesson(r):
            status_children.append(html.Div(html.Span(
                "Lesson?", className="badge bg-info text-dark",
                title="Add what worked (keep) and what to change: Edit this trade."), className="mt-1"))
        if r['status'] in ('Open (in progress)', 'Closed'):
            pf = plan_first_check(r)
            if pf and not pf['ok'] and pf['first_fill'].date().isoformat() >= PRACTICE_BY_ID['plan_first']['start']:
                status_children.append(html.Div(html.Span(
                    "⚠ Plan after fill", className="badge bg-danger",
                    title=f"Committed {pf['committed']:%b %d %H:%M} ET, after the first fill "
                          f"({pf['first_fill']:%b %d %H:%M} ET)."), className="mt-1"))
        status_disp = html.Div(status_children)
        rows.append(dbc.Row([
            _jcol([r['symbol'], html.Div(account_name(accounts, _scalar(r.get('account_id'))),
                                         className="text-muted small")] if show_account else r['symbol'], 0),
            _jcol(r['direction'], 1),
            _jcol(entry_disp, 2),
            _jcol(exit_disp, 3),
            _jcol(pnl_disp, 4),
            _jcol(r_disp, 5),
            _jcol(rr_disp, 6),
            _jcol(score_disp, 7),
            _jcol(targets_disp, 8),
            _jcol(status_disp, 9),
            _jcol(actions, 10),
        ], className="mb-1 align-items-center"))
    return rows, count, *pager, tab_options


def _cancel_stats(rows):
    """Cancelled plans: count by reason, and the "what would have happened"
    record -- how many would have filled, how many would have won, average R
    (including results still open, which is said where shown)."""
    if not rows:
        return {}
    reasons = {}
    for r in rows:
        key = _scalar(r.get('cancel_reason')) or 'No reason given'
        reasons[key] = reasons.get(key, 0) + 1
    cfs = [r.get('counterfactual') for r in rows if isinstance(r.get('counterfactual'), dict)
           and r['counterfactual'].get('outcome')]
    filled = [c for c in cfs if c['outcome'] != 'never_filled']
    rs = [c['r'] for c in filled if c.get('r') is not None]
    return {'cancel_reasons': reasons, 'cancel_checked': len(cfs),
            'cancel_never_filled': len(cfs) - len(filled), 'cancel_filled': len(filled),
            'cancel_would_have_won': sum(1 for x in rs if x > 0),
            'cancel_avg_r': round(sum(rs) / len(rs), 2) if rs else None,
            'cancel_not_final': sum(1 for c in cfs if not c.get('final'))}


def _cancel_summary_text(stats):
    n = stats.get('cancelled_count') or 0
    if not n:
        return '—'
    if not stats.get('cancel_checked'):
        return f"{n} · not checked yet"
    parts = [f"{n}"]
    if stats.get('cancel_filled'):
        parts.append(f"if kept: {stats['cancel_would_have_won']} of {stats['cancel_filled']} filled would have "
                     f"won, avg {stats['cancel_avg_r']:+.2f}R" if stats.get('cancel_avg_r') is not None
                     else f"if kept: {stats['cancel_filled']} would have filled")
    if stats.get('cancel_never_filled'):
        parts.append(f"{stats['cancel_never_filled']} never filled")
    if stats.get('cancel_not_final'):
        parts.append(f"{stats['cancel_not_final']} still running")
    return " · ".join(parts)


def _compute_summary_stats(journal_rows):
    """Core summary-stats arithmetic, shared between update_summary (renders it
    as HTML for the page) and the get_summary_stats MCP tool (returns it as
    plain data). Returns a dict; keys beyond plan_count/closed_count are only
    present once there's enough data to compute them."""
    if not journal_rows:
        return {'plan_count': 0, 'closed_count': 0, 'cancelled_count': 0}
    df = pd.DataFrame(journal_rows)
    cancelled = df[df['status'] == 'Cancelled']
    df = df[df['status'] != 'Cancelled']
    closed = df[df['status'] == 'Closed']
    stats = {'plan_count': len(df), 'closed_count': len(closed), 'cancelled_count': len(cancelled)}
    stats.update(_cancel_stats(cancelled.to_dict('records')))
    # Regime adherence (open and closed trades whose buys could be checked).
    breaches = pd.to_numeric(df['regime_breaches'], errors='coerce').dropna() \
        if 'regime_breaches' in df else pd.Series(dtype=float)
    stats['regime_checked_count'] = len(breaches)
    stats['regime_breach_trades'] = int((breaches > 0).sum())
    stats['regime_breach_count'] = int(breaches.sum())
    if closed.empty:
        return stats

    scored = closed[closed['score'].notna()]
    stats['win_rate_pct'] = round((closed['actual_pnl'] > 0).mean() * 100, 1)
    stats['total_actual_pnl'] = round(closed['actual_pnl'].sum(), 2)    # mixed; kept for MCP callers
    # Per currency, never converted: each trade closed at its own day's rate,
    # so today's rate would misstate it (historical conversion is step-2 work).
    ccy = closed['currency'].fillna('?') if 'currency' in closed else pd.Series('?', index=closed.index)
    stats['pnl_by_currency'] = {c: round(v, 2) for c, v in closed.groupby(ccy)['actual_pnl'].sum().items()}
    if not scored.empty:
        best = scored.loc[scored['score'].idxmax()]
        worst = scored.loc[scored['score'].idxmin()]
        stats['avg_plan_score_pct'] = round(scored['score'].mean(), 1)
        stats['median_plan_score_pct'] = round(scored['score'].median(), 1)
        stats['best_executed'] = {'symbol': best['symbol'], 'score_pct': best['score']}
        stats['worst_executed'] = {'symbol': worst['symbol'], 'score_pct': worst['score']}
    with_targets = closed[closed['targets_total'] > 0]
    if not with_targets.empty:
        stats['avg_pct_targets_hit'] = round(
            (with_targets['targets_hit'] / with_targets['targets_total']).mean() * 100, 0)

    # Everything below is in exit order: streaks and drawdown are sequences.
    closed = closed.assign(_exit=pd.to_datetime(closed['exit_time'], utc=True, errors='coerce'))
    closed = closed.sort_values('_exit', kind='stable')
    stats.update(_streak_stats((closed['actual_pnl'] > 0).tolist()))
    breached = closed.get('stop_breached', pd.Series(False, index=closed.index)) == True
    slipped = closed.get('stop_outcome', pd.Series(None, index=closed.index)) == 'slippage'
    stats['stop_breach_count'] = int(breached.sum())
    stats['stop_breach_rate_pct'] = round(breached.mean() * 100, 1)
    stats['stop_slippage_count'] = int(slipped.sum())
    stats['stop_slippage_rate_pct'] = round(slipped.mean() * 100, 1)
    rs = (pd.to_numeric(closed['r_multiple'], errors='coerce').dropna()
          if 'r_multiple' in closed else pd.Series(dtype=float))
    stats.update(_r_stats(rs))
    return stats


# R-based summary stats (expectancy, payoff, profit factor, drawdown) are
# withheld below this many trades with an R -- a handful of trades swings
# them too much to mean anything. Same idea as PSYCH_MIN_SAMPLE.
SUMMARY_MIN_SAMPLE = 5


def _r_stats(rs):
    """R-multiple stats over closed trades in exit order. R is per unit of each
    trade's own initial risk, so it ignores currency and position size --
    unlike P&L, which is mixed-currency until FX conversion exists. A trade
    with no initial stop has no R and isn't counted (r_trade_count says how
    many were). Expectancy is the mean R, which equals win% x avg win R -
    loss% x avg loss R. Drawdown is peak-to-trough of cumulative R, starting
    from 0; current_drawdown_r is how far below that peak the latest trade is."""
    out = {'r_trade_count': len(rs)}
    if len(rs) < SUMMARY_MIN_SAMPLE:
        out['r_insufficient_sample'] = True
        return out
    wins, losses = rs[rs > 0], rs[rs < 0]
    gross_loss = -losses.sum()
    cum = rs.cumsum()
    drawdown = cum.cummax().clip(lower=0) - cum
    out.update({
        'expectancy_r': round(rs.mean(), 2),
        'avg_win_r': round(wins.mean(), 2) if not wins.empty else None,
        'avg_loss_r': round(losses.mean(), 2) if not losses.empty else None,
        # None when there are no losing trades (infinite; not valid JSON).
        'profit_factor': round(wins.sum() / gross_loss, 2) if gross_loss > 0 else None,
        'max_drawdown_r': round(drawdown.max(), 2),
        'current_drawdown_r': round(drawdown.iloc[-1], 2),
    })
    return out


def _streak_stats(won):
    """Longest losing streak and the current streak, from a win/loss sequence
    in exit order (a win is actual P&L > 0, same as the win rate)."""
    longest = run = 0
    for w in won:
        run = 0 if w else run + 1
        longest = max(longest, run)
    current = 0
    for w in reversed(won):
        if w != won[-1]:
            break
        current += 1
    return {'longest_losing_streak': longest,
            'current_streak': {'kind': 'win' if won[-1] else 'loss', 'length': current}}


def _summary_rows(stats):
    """(label, value) pairs for one account's (or the combined) summary --
    the rows of both the All-accounts breakout table and the single-account
    list. '—' where there's nothing to compute yet; R-based rows read
    "n/a (n=…)" until SUMMARY_MIN_SAMPLE trades have an R."""
    def pct(key, fmt='{:.1f}%'):
        return fmt.format(stats[key]) if key in stats else '—'

    def extreme(key):
        e = stats.get(key)
        return f"{e['symbol']} ({e['score_pct']:.0f}%)" if e else '—'

    def r_stat(render):
        if stats.get('r_insufficient_sample'):
            return f"n/a (n={stats.get('r_trade_count', 0)})"
        return render() if 'expectancy_r' in stats else '—'

    def r_val(key):
        v = stats.get(key)
        return f"{v:+.2f}R" if v is not None else '—'

    def streaks():
        if 'current_streak' not in stats:
            return '—'
        cur = stats['current_streak']
        one = cur['length'] == 1
        kind = ('win' if one else 'wins') if cur['kind'] == 'win' else ('loss' if one else 'losses')
        return f"{stats['longest_losing_streak']} longest · now {cur['length']} {kind}"

    def stop_exits():
        if 'stop_breach_rate_pct' not in stats:
            return '—'
        return (f"{stats['stop_breach_rate_pct']:.1f}% breached ({stats['stop_breach_count']}) · "
                f"{stats['stop_slippage_rate_pct']:.1f}% slippage ({stats['stop_slippage_count']})")

    def regime():
        n = stats.get('regime_checked_count')
        if not n:
            return '—'
        bad = stats['regime_breach_trades']
        return (f"{n - bad} of {n} within the rules" if not bad else
                f"{bad} of {n} trades broke it ({stats['regime_breach_count']} breach"
                f"{'es' if stats['regime_breach_count'] != 1 else ''})")

    def label(text, tip):
        return html.Span([text, help_icon(tip)])

    return [
        ("Closed trades", f"{stats['closed_count']} of {stats['plan_count']} plans"),
        (label("Cancelled plans", "Committed plans abandoned before any fill, and what they would have "
                                  "done if kept (Check what would have happened, in the Trade Journal)."),
         _cancel_summary_text(stats)),
        ("Win rate", pct('win_rate_pct')),
        (label("Expectancy", "Average result per closed trade, in R (multiples of each trade's "
                             "initial risk). Positive means the approach makes money on average."),
         r_stat(lambda: r_val('expectancy_r'))),
        (label("Avg win / avg loss", "Average R of winning trades / of losing trades. Together "
                                     "with the win rate, these explain the expectancy."),
         r_stat(lambda: f"{r_val('avg_win_r')} / {r_val('avg_loss_r')}")),
        (label("Profit factor", "Total R won ÷ total R lost. Above 1.5 is solid; below 1 is losing."),
         r_stat(lambda: 'no losses' if stats.get('profit_factor') is None
                else f"{stats['profit_factor']:.2f}")),
        (label("Max drawdown", "Largest peak-to-trough fall in cumulative R, closed trades in exit "
                               "order. 'Now' is how far below the peak the latest trade leaves you."),
         r_stat(lambda: f"{stats['max_drawdown_r']:.2f}R (now {stats['current_drawdown_r']:.2f}R)")),
        (label("Losing streaks", "Longest run of consecutive losing trades, and the current run "
                                 "of wins or losses, in exit order."), streaks()),
        (label("Stop exits", "Share of closed trades that exited past the stop beyond the "
                             "slippage tolerance (breached), or within it (slippage)."), stop_exits()),
        (label("Regime at the open", "Open and closed trades whose buys were checked against the effective "
                                     "regime saved before 09:30 ET that day: the tactic allowed, the per-trade "
                                     "risk cap and the max position."), regime()),
        ("Total actual P&L", _fmt_ccy_amounts(stats['pnl_by_currency'], signed=True)
         if stats.get('pnl_by_currency') else '—'),
        ("Average Plan Score", pct('avg_plan_score_pct')),
        ("Median Plan Score", pct('median_plan_score_pct')),
        ("Best-executed", extreme('best_executed')),
        ("Worst-executed", extreme('worst_executed')),
        ("Avg % of planned targets hit", pct('avg_pct_targets_hit', '{:.0f}%')),
    ]


def summary_stats_by_account(journal_rows, accounts):
    """{account_id: stats} for every known account, plus anything in the
    journal under an account that isn't registered (shouldn't happen, but
    never drop rows silently)."""
    ids = [a['account_id'] for a in (accounts or [])]
    ids += sorted({_scalar(r.get('account_id')) for r in (journal_rows or [])} - set(ids) - {None})
    return {acct: _compute_summary_stats(_filter_view(journal_rows, acct)) for acct in ids}


def _render_summary_breakout(data, accounts):
    """All-accounts view: one column per account, plus a combined column when
    there's more than one account. Same metrics as the single-account list."""
    per_account = summary_stats_by_account(data, accounts)
    columns = [(account_name(accounts, a), s) for a, s in per_account.items()]
    if len(columns) > 1:
        columns.append(("All accounts", _compute_summary_stats(data)))
    rows = [list(zip(*_summary_rows(s)))[1] for _, s in columns]
    labels = [label for label, _ in _summary_rows(columns[0][1])]
    header = html.Thead(html.Tr([html.Th("")] + [html.Th(name) for name, _ in columns]))
    body = html.Tbody([
        html.Tr([html.Td(label)] + [html.Td(r[i], className="fw-bold" if j == len(rows) - 1 and len(rows) > 1
                                            else None) for j, r in enumerate(rows)])
        for i, label in enumerate(labels)])
    return [dbc.Table([header, body], size='sm', bordered=False, hover=True, className="mb-1"),
            _summary_footnote()]


@app.callback(
    [Output('summary-stats', 'children'), Output('summary-stats-header', 'children')],
    [Input('store-journal', 'data'), Input('account-view', 'value'), Input('store-accounts', 'data'),
     Input('strategy-view', 'value')],
)
def update_summary(data, view, accounts, strategy='all'):
    data, suffix = _strategy_filter(data, strategy), _strategy_suffix(strategy)
    if _is_all_view(view):
        return _render_summary_breakout(data, accounts), f"Summary Statistics — all accounts{suffix}"
    return html.Ul(_render_summary_list(_filter_view(data, view)), className="list-unstyled"), \
        f"Summary Statistics — {account_name(accounts, view)}{suffix}"


def _render_summary_list(data):
    stats = _compute_summary_stats(data)
    if stats['plan_count'] == 0:
        return [html.Li("No trades yet.")]
    if stats['closed_count'] == 0:
        return [html.Li(f"{stats['plan_count']} plan(s) logged, none matched to a closed trade yet.")]
    items = [html.Li([label, ": ", value]) for label, value in _summary_rows(stats)]
    items.append(html.Li(_summary_footnote(), className="mt-1"))
    return items


def _summary_footnote():
    return html.Small(f"R-based figures need at least {SUMMARY_MIN_SAMPLE} closed trades with an "
                      f"initial stop. P&L is shown per currency: each trade closed at its own "
                      f"day's exchange rate, so it isn't converted at today's.",
                      className="text-muted")


# ==============================================================
# Performance over time (2026-10-09): how the closed planned trades evolve,
# in exit order. R-based (one scale across USD and CAD, like the summary);
# P&L appears only per currency in the monthly table. Colours are the
# validated light-mode chart palette (dataviz skill): one blue series, a
# blue/red pair for gain vs. loss, and three categorical slots for the
# process rates -- whose aqua is under 3:1 contrast, so the monthly table
# doubles as the required table view.
# ==============================================================
PERF_ROLLING_WINDOW = 10
PERF_SMALL_SAMPLE = 30
PERF_COLORS = {'blue': '#2a78d6', 'red': '#e34948', 'orange': '#eb6834', 'aqua': '#1baf7a',
               'surface': '#fcfcfb', 'grid': '#e6e5e0', 'zero': '#a8a7a2',
               'text': '#0b0b0b', 'muted': '#52514e'}


def performance_frame(journal_rows):
    """Closed planned trades in exit order, one row each, with what the
    charts need: n (1-based trade number), exit, r, cum_r, drawdown (<= 0,
    from the running peak of cumulative R, starting at 0), rolling
    expectancy and win rate over the last PERF_ROLLING_WINDOW trades (NaN
    until there are that many), plan score, % of targets hit, stop outcome,
    month. Trades without an R (no initial stop) are dropped from the R
    columns' running sums but kept for the monthly table, flagged has_r."""
    df = pd.DataFrame(journal_rows or [])
    if df.empty or 'status' not in df:
        return pd.DataFrame()
    c = df[df['status'] == 'Closed'].copy()
    c['exit'] = pd.to_datetime(c.get('exit_time'), utc=True, errors='coerce')
    c = c.dropna(subset=['exit']).sort_values('exit', kind='stable').reset_index(drop=True)
    if c.empty:
        return c
    c['r'] = pd.to_numeric(c.get('r_multiple'), errors='coerce')
    c['has_r'] = c['r'].notna()
    c['win'] = pd.to_numeric(c['actual_pnl'], errors='coerce') > 0
    total = pd.to_numeric(c.get('targets_total'), errors='coerce')
    hit = pd.to_numeric(c.get('targets_hit'), errors='coerce')
    c['targets_pct'] = (hit / total * 100).where(total > 0)
    c['score'] = pd.to_numeric(c.get('score'), errors='coerce')
    c['month'] = c['exit'].dt.strftime('%Y-%m')
    c['n'] = range(1, len(c) + 1)
    r = c['r'].fillna(0.0)
    c['cum_r'] = r.cumsum()
    c['drawdown'] = c['cum_r'] - c['cum_r'].cummax().clip(lower=0)
    w = PERF_ROLLING_WINDOW
    c['rolling_expectancy'] = c['r'].rolling(w, min_periods=w).mean()
    c['rolling_win_rate'] = c['win'].astype(float).rolling(w, min_periods=w).mean() * 100
    return c


def monthly_performance(frame):
    """One row per exit month: trades, win rate, expectancy (R), average Plan
    Score, % of targets hit, stop-breach and slippage rates, and realized P&L
    per currency."""
    rows = []
    for month, g in frame.groupby('month', sort=True):
        outcome = g.get('stop_outcome', pd.Series(None, index=g.index))
        ccy = g['currency'].fillna('?') if 'currency' in g else pd.Series('?', index=g.index)
        rows.append({
            'month': month, 'trades': len(g),
            'win_rate_pct': g['win'].mean() * 100,
            'expectancy_r': g['r'].mean() if g['has_r'].any() else None,
            'avg_plan_score_pct': g['score'].mean() if g['score'].notna().any() else None,
            'targets_hit_pct': g['targets_pct'].mean() if g['targets_pct'].notna().any() else None,
            'stop_breach_pct': (outcome == 'breached').mean() * 100,
            'stop_slippage_pct': (outcome == 'slippage').mean() * 100,
            'pnl_by_currency': {c: round(v, 2) for c, v in
                                pd.to_numeric(g['actual_pnl'], errors='coerce').groupby(ccy).sum().items()},
        })
    return rows


def _perf_style(fig, height, ytitle=None, xtitle=None, legend=False):
    """The shared quiet chart look: surface background, hairline recessive
    grid, text-token ink, no mode bar clutter."""
    fig.update_layout(
        height=height, margin=dict(l=56, r=24, t=36, b=40), showlegend=legend,
        plot_bgcolor=PERF_COLORS['surface'], paper_bgcolor=PERF_COLORS['surface'],
        font=dict(color=PERF_COLORS['muted'], size=12), hovermode='closest',
        hoverlabel=dict(bgcolor='white', font=dict(color=PERF_COLORS['text'])),
        legend=dict(orientation='h', yanchor='bottom', y=1.02, x=0, font=dict(color=PERF_COLORS['text'])),
        title_font=dict(color=PERF_COLORS['text'], size=14), title_x=0, bargap=0.25)
    fig.update_xaxes(showgrid=False, linecolor=PERF_COLORS['grid'], ticks='', zeroline=False)
    fig.update_yaxes(gridcolor=PERF_COLORS['grid'], gridwidth=1, zeroline=False, ticks='')
    if ytitle:
        fig.update_yaxes(title_text=ytitle, title_font=dict(color=PERF_COLORS['muted']))
    if xtitle:
        fig.update_xaxes(title_text=xtitle, title_font=dict(color=PERF_COLORS['muted']))
    return fig


def _left_panel_titles(fig):
    """Subplot titles left-aligned above their panel, in text ink -- matching
    the single charts' titles, and clear of the plot area below."""
    for ann in fig.layout.annotations:
        if ann.xref == 'x domain' or (ann.xref == 'paper' and ann.showarrow is False and ann.yanchor == 'bottom'):
            ann.update(x=0, xanchor='left', yshift=10, font=dict(color=PERF_COLORS['text'], size=14))
    return fig


def _trade_hover(f):
    return [f"Trade #{n} · {sym} ({acct})<br>Exit {d:%Y-%m-%d}<br>This trade: {r:+.2f}R"
            for n, sym, acct, d, r in zip(f['n'], f['symbol'], f['account'], f['exit'], f['r'].fillna(0))]


def fig_cumulative_r(f):
    """Cumulative R by trade, with the drawdown from its running peak below
    it -- two panels sharing the trade axis (never two y-scales on one)."""
    from plotly.subplots import make_subplots
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.66, 0.34], vertical_spacing=0.16,
                        subplot_titles=("Cumulative R", "Drawdown from peak (R)"))
    hover = [f"{h}<br>Cumulative: {c:+.2f}R" for h, c in zip(_trade_hover(f), f['cum_r'])]
    fig.add_trace(go.Scatter(x=f['n'], y=f['cum_r'], mode='lines+markers',
                             line=dict(color=PERF_COLORS['blue'], width=2),
                             marker=dict(size=8, color=PERF_COLORS['blue'],
                                         line=dict(color=PERF_COLORS['surface'], width=2)),
                             hovertext=hover, hoverinfo='text', name='Cumulative R'), row=1, col=1)
    dd_hover = [f"{h}<br>Drawdown: {d:.2f}R" for h, d in zip(_trade_hover(f), f['drawdown'])]
    fig.add_trace(go.Scatter(x=f['n'], y=f['drawdown'], mode='lines', fill='tozeroy',
                             line=dict(color=PERF_COLORS['red'], width=2), fillcolor='rgba(227,73,72,0.10)',
                             hovertext=dd_hover, hoverinfo='text', name='Drawdown'), row=2, col=1)
    for row in (1, 2):
        fig.add_hline(y=0, line=dict(color=PERF_COLORS['zero'], width=1), row=row, col=1)
    last = f.iloc[-1]
    fig.add_annotation(x=last['n'], y=last['cum_r'], text=f"{last['cum_r']:+.2f}R", showarrow=False,
                       xanchor='left', xshift=8, font=dict(color=PERF_COLORS['text']), row=1, col=1)
    _left_panel_titles(fig)
    _perf_style(fig, 460)
    fig.update_xaxes(title_text="Closed trade # (exit order)", row=2, col=1)
    fig.update_xaxes(dtick=1 if len(f) <= 30 else None)
    return fig


def fig_rolling(f):
    """Rolling expectancy (R) and rolling win rate over the last
    PERF_ROLLING_WINDOW trades, as two panels on the same trade axis."""
    from plotly.subplots import make_subplots
    w = PERF_ROLLING_WINDOW
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.2,
                        subplot_titles=(f"Expectancy, last {w} trades (R)", f"Win rate, last {w} trades (%)"))
    g = f.dropna(subset=['rolling_expectancy'])
    hover_e = [f"Trades #{n - w + 1}–#{n}<br>Expectancy: {v:+.2f}R" for n, v in zip(g['n'], g['rolling_expectancy'])]
    hover_w = [f"Trades #{n - w + 1}–#{n}<br>Win rate: {v:.0f}%" for n, v in zip(g['n'], g['rolling_win_rate'])]
    marker = dict(size=8, color=PERF_COLORS['blue'], line=dict(color=PERF_COLORS['surface'], width=2))
    fig.add_trace(go.Scatter(x=g['n'], y=g['rolling_expectancy'], mode='lines+markers', marker=marker,
                             line=dict(color=PERF_COLORS['blue'], width=2), hovertext=hover_e,
                             hoverinfo='text'), row=1, col=1)
    fig.add_trace(go.Scatter(x=g['n'], y=g['rolling_win_rate'], mode='lines+markers', marker=marker,
                             line=dict(color=PERF_COLORS['blue'], width=2), hovertext=hover_w,
                             hoverinfo='text'), row=2, col=1)
    fig.add_hline(y=0, line=dict(color=PERF_COLORS['zero'], width=1), row=1, col=1)
    fig.add_hline(y=50, line=dict(color=PERF_COLORS['zero'], width=1), row=2, col=1)
    _left_panel_titles(fig)
    _perf_style(fig, 400)
    fig.update_yaxes(range=[0, 100], row=2, col=1)
    fig.update_xaxes(title_text="Closed trade # (window ends here)", dtick=1, row=2, col=1)
    return fig


def fig_r_distribution(f):
    """The spread of results as a horizontal violin (R on the x axis), with
    every trade drawn as its own dot on top -- gains blue, losses red -- and
    the average win and average loss marked. With few trades the violin's
    smoothed outline is partly guesswork, so the dots carry the real data and
    the outline stops at the best and worst actual results (spanmode 'hard')
    rather than implying results that never happened. The box inside shows
    the median and the middle 50%."""
    g = f[f['has_r']].reset_index(drop=True)
    fig = go.Figure(go.Violin(
        x=g['r'], y0=0, orientation='h', name='All trades', spanmode='hard', points=False,
        line=dict(color=PERF_COLORS['blue'], width=2), fillcolor='rgba(42,120,214,0.10)',
        box=dict(visible=True, width=0.18, fillcolor=PERF_COLORS['surface'],
                 line=dict(color=PERF_COLORS['muted'], width=1)),
        meanline=dict(visible=False), hoverinfo='skip', showlegend=False))
    # A fixed, evenly spread vertical offset per trade (not random), so dots
    # don't stack on each other and the chart is identical on every render.
    jitter = [((i * 0.618034) % 1 - 0.5) * 0.36 for i in range(len(g))]
    hovers = _trade_hover(g.assign(account=g.get('account', '')))
    for name, mask, color in (("Gain", g['r'] >= 0, PERF_COLORS['blue']),
                              ("Loss", g['r'] < 0, PERF_COLORS['red'])):
        idx = [i for i, m in enumerate(mask) if m]
        fig.add_trace(go.Scatter(
            x=[g['r'][i] for i in idx], y=[jitter[i] for i in idx], mode='markers', name=name,
            marker=dict(size=9, color=color, line=dict(color=PERF_COLORS['surface'], width=2)),
            hovertext=[hovers[i] for i in idx], hoverinfo='text'))
    wins, losses = g[g['r'] > 0]['r'], g[g['r'] < 0]['r']
    for val, name in ((wins.mean(), "avg win"), (losses.mean(), "avg loss")):
        if pd.notna(val):
            fig.add_vline(x=val, line=dict(color=PERF_COLORS['muted'], width=1))
            fig.add_annotation(x=val, y=1, yref='paper', text=f"{name} {val:+.2f}R", showarrow=False,
                               yanchor='bottom', font=dict(color=PERF_COLORS['text'], size=11))
    fig.add_vline(x=0, line=dict(color=PERF_COLORS['zero'], width=1))
    _perf_style(fig, 380, xtitle="Result per trade (R)", legend=True)
    fig.update_layout(title_text="Distribution of results", margin=dict(t=72),
                      legend=dict(x=1, xanchor='right', y=1.14, yanchor='bottom'))
    fig.update_yaxes(visible=False, range=[-0.5, 0.5])
    fig.update_xaxes(showgrid=True, gridcolor=PERF_COLORS['grid'])
    return fig


def fig_plan_score_by_month(months):
    x = [m['month'] for m in months]
    y = [m['avg_plan_score_pct'] if m['avg_plan_score_pct'] is not None else 0 for m in months]
    colors = [PERF_COLORS['blue'] if v >= 0 else PERF_COLORS['red'] for v in y]
    hover = [f"{m['month']}: average Plan Score {v:+.0f}% over {m['trades']} trade(s)" for m, v in zip(months, y)]
    fig = go.Figure(go.Bar(x=x, y=y, marker=dict(color=colors, cornerradius=4), width=0.4,
                           hovertext=hover, hoverinfo='text', text=[f"{v:+.0f}%" for v in y],
                           textposition='outside', textfont=dict(color=PERF_COLORS['text'])))
    fig.add_hline(y=0, line=dict(color=PERF_COLORS['zero'], width=1))
    _perf_style(fig, 320, ytitle="Average Plan Score (%)")
    fig.update_layout(title_text="Plan Score by month")
    fig.update_xaxes(type='category')
    return fig


def fig_process_rates_by_month(months):
    """Targets hit, stop breaches and slippage as % of the month's trades --
    three series, so a legend; grouped, thin bars."""
    x = [m['month'] for m in months]
    fig = go.Figure()
    for key, name, color in (('targets_hit_pct', 'Targets hit', PERF_COLORS['blue']),
                             ('stop_breach_pct', 'Stop breached', PERF_COLORS['orange']),
                             ('stop_slippage_pct', 'Stop slippage', PERF_COLORS['aqua'])):
        vals = [m[key] if m[key] is not None else 0 for m in months]
        fig.add_trace(go.Bar(x=x, y=vals, name=name, marker=dict(color=color, cornerradius=4),
                             hovertext=[f"{m['month']} · {name}: {v:.0f}% ({m['trades']} trade(s))"
                                        for m, v in zip(months, vals)], hoverinfo='text'))
    _perf_style(fig, 320, ytitle="% of the month's trades", legend=True)
    fig.update_layout(title_text="Process discipline by month", barmode='group', bargroupgap=0.12,
                      margin=dict(t=72))
    fig.update_yaxes(range=[0, 100])
    fig.update_xaxes(type='category')
    return fig


def _monthly_table(months):
    def pct(v):
        return f"{v:.0f}%" if v is not None else "—"
    head = html.Thead(html.Tr([html.Th(h) for h in (
        "Month", "Trades", "Win rate", "Expectancy", "Avg Plan Score", "Targets hit",
        "Stop breached / slippage", "Realized P&L")]))
    body = html.Tbody([html.Tr([
        html.Td(m['month']), html.Td(m['trades']), html.Td(pct(m['win_rate_pct'])),
        html.Td(f"{m['expectancy_r']:+.2f}R" if m['expectancy_r'] is not None else "—"),
        html.Td(f"{m['avg_plan_score_pct']:+.0f}%" if m['avg_plan_score_pct'] is not None else "—"),
        html.Td(pct(m['targets_hit_pct'])),
        html.Td(f"{m['stop_breach_pct']:.0f}% / {m['stop_slippage_pct']:.0f}%"),
        html.Td(_fmt_ccy_amounts(m['pnl_by_currency'], signed=True)),
    ]) for m in months])
    return dbc.Table([head, body], size='sm', hover=True, responsive=True, className="mb-0")


def render_performance(journal_rows, view=None, accounts=None):
    """The Performance Over Time section for the current Account view."""
    f = performance_frame(_filter_view(journal_rows, view))
    if f.empty:
        return html.P("No closed planned trades yet in this view.", className="text-muted")
    f['account'] = [account_name(accounts, _scalar(a)) for a in f.get('account_id', [None] * len(f))]
    graph = dict(config={'displayModeBar': False, 'responsive': True})
    n, span = len(f), f"{f['exit'].iloc[0]:%Y-%m-%d} to {f['exit'].iloc[-1]:%Y-%m-%d}"
    note = [html.B(f"{n} closed planned trade{'s' if n != 1 else ''}"), f", exits {span}."]
    if n < PERF_SMALL_SAMPLE:
        note.append(f" A small sample: read trends as early signals, not conclusions.")
    if not f['has_r'].all():
        note.append(f" {int((~f['has_r']).sum())} trade(s) without an initial stop have no R and count "
                    f"as 0R in the curves.")
    months = monthly_performance(f)
    rolling = (dcc.Graph(figure=fig_rolling(f), **graph) if f['rolling_expectancy'].notna().any() else
               html.P(f"Rolling expectancy and win rate need {PERF_ROLLING_WINDOW} closed trades "
                      f"({n} so far).", className="text-muted small mt-4"))
    distribution = (dcc.Graph(figure=fig_r_distribution(f), **graph) if f['has_r'].any() else
                    html.P("No trades with an R yet.", className="text-muted small"))
    return html.Div([
        html.P(note, className="small text-muted mb-2"),
        dcc.Graph(figure=fig_cumulative_r(f), **graph),
        dbc.Row([dbc.Col(rolling, md=6), dbc.Col(distribution, md=6)], className="mt-2"),
        dbc.Row([dbc.Col(dcc.Graph(figure=fig_plan_score_by_month(months), **graph), md=6),
                 dbc.Col(dcc.Graph(figure=fig_process_rates_by_month(months), **graph), md=6)],
                className="mt-2"),
        html.H6("By month", className="mt-3"),
        _monthly_table(months),
    ])


@app.callback(
    Output('performance-body', 'children'),
    [Input('performance-accordion', 'active_item'), Input('store-journal', 'data'),
     Input('account-view', 'value'), Input('store-accounts', 'data'), Input('strategy-view', 'value')],
)
def update_performance(active_item, journal, view, accounts, strategy='all'):
    # Built only while the section is open -- it's collapsed by default so
    # the charts never slow a page load. Follows the Summary Statistics
    # strategy picker as well as the Account view.
    if not active_item:
        raise PreventUpdate
    return render_performance(_strategy_filter(journal, strategy), view, accounts)


@app.callback(
    [Output('screenshot-modal', 'is_open'), Output('screenshot-modal-title', 'children'),
     Output('screenshot-modal-body', 'children')],
    Input({'type': 'btn-enlarge-screenshot', 'index': ALL}, 'n_clicks'),
    State('store-plans', 'data'),
    prevent_initial_call=True,
)
def enlarge_screenshot(_clicks, plans):
    # The thumbnail is re-created every time a trade is viewed, which can fire
    # this with n_clicks 0 -- only a real click opens the window.
    trig = callback_context.triggered_id
    if not trig or not callback_context.triggered[0].get('value'):
        raise PreventUpdate
    plan = next((p for p in (plans or []) if p['plan_id'] == trig['index']), None)
    url = screenshot_url(plan.get('screenshot_path')) if plan else None
    if not url:
        raise PreventUpdate
    title = f"{plan['symbol']} -- chart at planning ({plan.get('planned_date') or ''})"
    body = [html.Img(src=url, style={'width': '100%', 'height': 'auto'}),
            html.A("Open full size in a new tab", href=url, target='_blank', className="d-block mt-2 small")]
    return True, title, body


def _atr_and_stop_method_line(r, currency):
    """The trade detail view's line naming the ATR's timeframe and the stop method."""
    atr = _scalar(r.get('atr_at_commit'))
    tf = ATR_TIMEFRAME_LABELS[atr_timeframe_of(r)]
    atr_text = (f"ATR({ATR_PERIOD}, {tf}) at commit: {currency} {float(atr):.4f}"
                if atr not in (None, '') else f"ATR timeframe: {tf}")
    if is_day_trade(r):
        return (f"Day trade · Setup: {day_setup_label(r.get('day_setup')) or '—'} · Catalyst: "
                f"{_scalar(r.get('day_catalyst')) or '—'}  |  {atr_text}  |  Time stop: flat by {DAY_TRADE_TIME_STOP}")
    return f"{atr_text}  |  Stop method: {trail_label(r) or 'Ladder'}"


@app.callback(
    [Output('detail-markdown', 'children'), Output('price-chart', 'figure')],
    Input({'type': 'btn-view', 'index': ALL}, 'n_clicks'),
    State('store-journal', 'data'),
    prevent_initial_call=True,
)
def show_detail(n_clicks_list, data):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    plan_id = triggered['index']

    df = pd.DataFrame(data or [])
    row = df[df['plan_id'] == plan_id]
    if row.empty:
        return "", go.Figure()
    r = row.iloc[0]

    is_open_in_progress = r['status'] == 'Open (in progress)'
    currency = r['currency'] or ''
    summary_lines = [
        f"Symbol: {r['symbol']}  |  Direction: {r['direction']}  |  Status: {r['status']}",
        "",
        f"Planned entry: {currency} {r['entry_price']}",
        f"Initial protective stop: {currency} {r['stop_loss']}" if r['stop_loss'] is not None else "Initial protective stop: not set",
        f"Planned qty: {r['qty']}",
        _atr_and_stop_method_line(r, currency),
        "",
        f"Max potential loss (full position stopped out at initial stop): "
        f"{currency} {r['max_loss']}" if r.get('max_loss') is not None else
        "Max potential loss: n/a (no initial stop set)",
        (f"Planned reward (full scale-out ladder): {currency} {r['max_reward']}"
         if _scalar(r.get('reward_basis')) == 'ladder' else
         f"Max potential reward (no Qty % set: full position exiting at the furthest target): "
         f"{currency} {r['max_reward']}") if _scalar(r.get('max_reward')) is not None else
        "Planned reward: n/a (no targets set)",
        f"Plan risk/reward ratio: {r['risk_reward_ratio']:.2f} : 1" if _scalar(r.get('risk_reward_ratio')) is not None else
        "Plan risk/reward ratio: n/a (need both a stop and at least one target)",
        "",
        f"Actual entry: {currency} {r['entry_actual']}" if r['entry_actual'] is not None else "Actual entry: n/a (not executed yet)",
        (f"Actual qty (position still open): {r['qty_actual']}" if is_open_in_progress and r['qty_actual'] is not None
         else f"Actual (weighted avg) exit: {currency} {r['exit_actual']}" if r['exit_actual'] is not None else None),
        f"Actual qty: {r['qty_actual']}" if not is_open_in_progress and r['qty_actual'] is not None else None,
        (f"P&L realized on shares sold so far (position still open): {currency} {r['actual_pnl']}"
         if is_open_in_progress and r['actual_pnl'] is not None else
         f"Actual P&L: {currency} {r['actual_pnl']}" if r['actual_pnl'] is not None else None),
        f"Actual return: {r['actual_return_pct']}%" if r['actual_return_pct'] is not None else "Actual return: n/a",
        f"R-multiple (vs. initial stop): {r['r_multiple']}" if r['r_multiple'] is not None else "R-multiple: n/a (no initial stop set)",
        f"Plan score (vs. blended targets): {r['score']}%" if r['score'] is not None else "Plan score: n/a (no targets set)",
        f"Stop level in effect at final exit: {currency} {r['final_effective_stop']}" if r['final_effective_stop'] is not None else "",
    ]
    summary_text = "\n".join(l for l in summary_lines if l is not None)

    if r['status'] == 'Cancelled':
        summary_text = summary_text.replace("Actual entry: n/a (not executed yet)",
                                            "Actual entry: n/a (cancelled before any fill)")
        if _cancel_psych_text(r):
            summary_text += f"\nPsychological state at cancel: {_cancel_psych_text(r)}"
    campaign = r.get('campaign') if isinstance(r.get('campaign'), dict) else None
    if campaign:
        summary_text = summary_text.replace(
            "Max potential loss (full position stopped out at initial stop)",
            "Planned peak risk (the riskiest stage of the add ladder)").replace(
            "Plan score: n/a (no targets set)", "Plan score: n/a (a campaign has no targets)").replace(
            "Planned reward: n/a (no targets set)", "Planned reward: open-ended (the exit is the trailing stop)").replace(
            "Plan risk/reward ratio: n/a (need both a stop and at least one target)",
            "Plan risk/reward ratio: n/a (no fixed target)").replace(
            "R-multiple (vs. initial stop)", "R-multiple (vs. peak risk taken)").replace(
            "Stop level in effect at final exit", "Scheduled stop at the end")
    target_results = r.get('target_results') or []
    target_table = _campaign_detail_table(campaign, currency) if campaign else None
    if target_results:
        header = dbc.Row([
            dbc.Col(html.B("#"), width=1), dbc.Col(html.B("Planned Price"), width=2),
            dbc.Col(html.B("Qty % (of original)"), width=1), dbc.Col(html.B("Move Stop To"), width=2),
            dbc.Col(html.B("Hit?"), width=1), dbc.Col(html.B("Actual Fill"), width=3),
            dbc.Col(html.B("Stop After"), width=2),
        ], className="mb-1")
        trows = [header]
        for i, t in enumerate(target_results, start=1):
            hit_badge = html.Span("Hit", className="badge bg-success") if t['hit'] \
                else html.Span("Not reached", className="badge bg-secondary")
            fill_disp = (f"{currency} {t['matched_price']:.2f} × {t['matched_qty']:.4g} @ {str(t['matched_time'])[:19]}"
                         if t['hit'] else "-")
            trows.append(dbc.Row([
                dbc.Col(f"T{i}", width=1),
                dbc.Col(f"{currency} {t['price']:.2f}", width=2),
                dbc.Col(f"{t['qty_pct']:.0f}%" if t['qty_pct'] is not None else "-", width=1),
                dbc.Col(f"{currency} {t['stop_after']:.2f}" if t['stop_after'] is not None else "-", width=2),
                dbc.Col(hit_badge, width=1),
                dbc.Col(fill_disp, width=3),
                dbc.Col(f"{currency} {t['stop_after_hit']:.2f}" if t['stop_after_hit'] is not None else "-", width=2),
            ], className="mb-1 align-items-center"))
        target_table = html.Div([html.H6("Target Ladder", className="mt-3"), html.Div(trows)])

    notes_block = html.Div([
        html.H6("Execution Notes", className="mt-3"),
        html.Ul([html.Li(n) for n in (r['exec_notes'] or [])]) if r['exec_notes'] else html.P("—", className="text-muted"),
        render_regime_check(r.get('regime_check')),
        render_plan_first(r),
    ])
    reason_block = html.Div([
        html.H6("Reason", className="mt-3"), html.P(r['reason'] or '(none given)'),
        html.H6("Notes"), html.P(r['notes'] or '(none given)'),
    ])

    context_block = html.Div([
        html.H6("Psychological State at Entry", className="mt-3"),
        html.P(psych_summary_text(r) or "Not rated"),
        html.P(f"Distortion index: {psych_distortion_index(r)} / 6") if has_psych_v2(r) else None,
        html.H6("Trade Trigger", className="mt-3"),
        html.P(trade_triggers_text(r.get('trade_triggers')) or "Not recorded"),
        html.H6("Chart Screenshot (at entry)", className="mt-3"),
        html.Div([
            html.Img(src=screenshot_url(r.get('screenshot_path')), n_clicks=0,
                     id={'type': 'btn-enlarge-screenshot', 'index': r['plan_id']},
                     title="Click to enlarge",
                     style={'maxWidth': '100%', 'maxHeight': '400px', 'cursor': 'zoom-in'}),
            html.Small("Click the chart to enlarge it.", className="d-block text-muted"),
        ]) if screenshot_url(r.get('screenshot_path'))
        else html.P("(none attached)", className="text-muted"),
        html.H6("Lessons", className="mt-3"),
        render_trade_lessons(r),
    ])

    # Read-only: a delete button here would target the Notes modal's own list
    # container (keyed by store-observing-plan-id), not this static panel --
    # use the "Notes" button on the journal row to add/remove entries.
    observations_block = html.Div([
        html.H6(["Observation Log",
                 help_icon("Dated notes logged via the \"Notes\" button below (entry, exit, or anything "
                           "in between) -- add or remove entries there.")], className="mt-3"),
        _render_observations_list(db.fetch_plan_observations(plan_id), readonly=True),
    ])

    detail_children = html.Div([
        html.Pre(summary_text, style={'whiteSpace': 'pre-wrap', 'fontFamily': 'inherit', 'marginBottom': 0}),
        target_table if target_table else None,
        notes_block,
        reason_block,
        context_block,
        observations_block,
    ])

    # ---- Chart: planned entry -> actual entry -> each exit fill (chronological),
    # with initial stop, ratcheted final stop, and each target as reference lines.
    fig = go.Figure()
    exit_fills = sorted(r.get('exit_fill_list') or [], key=lambda f: f['time'])

    x_labels = ['Planned Entry']
    y_vals = [r['entry_price']]
    if r['entry_actual'] is not None:
        x_labels.append('Actual Entry')
        y_vals.append(r['entry_actual'])
    for i, f in enumerate(exit_fills, start=1):
        x_labels.append(f'Exit {i}')
        y_vals.append(f['price'])

    marker_colors = ['gray'] + (['blue'] if r['entry_actual'] is not None else [])
    for f in exit_fills:
        marker_colors.append('green' if sign_favorable(r['direction'], r['entry_actual'] or r['entry_price'], f['price']) else 'red')

    fig.add_trace(go.Scatter(x=x_labels, y=y_vals, mode='lines+markers',
                              marker=dict(size=12, color=marker_colors), line=dict(color='lightgray'),
                              text=[f"{currency} {v:.2f}" for v in y_vals], hoverinfo='text+x'))

    if r['stop_loss'] is not None:
        fig.add_hline(y=r['stop_loss'], line_dash='dot', line_color='red', annotation_text='Initial Stop')
    if r['final_effective_stop'] is not None and r['final_effective_stop'] != r['stop_loss']:
        fig.add_hline(y=r['final_effective_stop'], line_dash='dash', line_color='orange',
                      annotation_text='Stop (final)')
    for i, t in enumerate(target_results, start=1):
        fig.add_hline(y=t['price'], line_dash='dot', line_color='green', opacity=0.5,
                      annotation_text=f'T{i}')

    fig.update_layout(title=f"Price Path – {r['symbol']}", yaxis_title=f"Price ({currency})" if currency else "Price",
                       height=320, margin=dict(l=40, r=20, t=40, b=40))

    return detail_children, fig


def _campaign_detail_table(campaign, currency):
    """The trade detail view's add ladder: each stage's plan beside its fills."""
    head = html.Thead(html.Tr([html.Th(h) for h in
                               ("Stage", "Trigger", "Stop before", "Planned", "Filled", "Avg fill", "When")]))
    rows = []
    for st in campaign.get('stages') or []:
        flag = html.Span(" early", className="badge bg-warning text-dark ms-1") if st.get('early') else None
        rows.append(html.Tr([
            html.Td(st['label']),
            html.Td(f"{currency} {float(st['trigger']):.2f}" if st.get('trigger') not in (None, '') else "-"),
            html.Td(f"{currency} {float(st['stop']):.2f}" if st.get('stop') not in (None, '') else "-"),
            html.Td(f"{st['planned_qty']:.4g}"),
            html.Td(f"{st['filled_qty']:.4g}" if st['filled_qty'] else "-"),
            html.Td([f"{currency} {st['avg_price']:.2f}" if st.get('avg_price') is not None else "-", flag]),
            html.Td(str(st['time'])[:10] if st.get('time') else "-"),
        ]))
    return html.Div([html.H6("Add Ladder (scale-in campaign)", className="mt-3"),
                     dbc.Table([head, html.Tbody(rows)], size='sm', bordered=False, className="mb-0")])


def sign_favorable(direction, reference_price, price):
    """True if `price` is favorable relative to `reference_price` for the given direction."""
    if reference_price is None:
        return True
    return price >= reference_price if direction == 'BUY' else price <= reference_price


@app.callback(
    [Output('cancel-plan-modal', 'is_open'), Output('store-cancel-plan-id', 'data'),
     Output('cancel-plan-title', 'children'), Output('cancel-reason', 'options'),
     Output('cancel-reason', 'value'), Output('cancel-note', 'value'),
     Output('cancel-plan-status', 'children')] + _psych_outputs('cancel'),
    Input({'type': 'btn-cancel-plan', 'index': ALL}, 'n_clicks'),
    State('store-plans', 'data'),
    prevent_initial_call=True,
)
def open_cancel_plan(_clicks, plans):
    # Re-rendered buttons can fire with n_clicks 0/None -- only a real click opens it.
    trig = callback_context.triggered_id
    if not trig or not callback_context.triggered[0].get('value'):
        raise PreventUpdate
    plan = next((p for p in (plans or []) if p['plan_id'] == trig['index']), None)
    if not plan:
        raise PreventUpdate
    reasons = load_cancel_reasons()
    title = f"Cancel plan: {plan['symbol']} {plan['direction']} @ {plan.get('entry_price')}"
    return ([True, plan['plan_id'], title, [{'label': r, 'value': r} for r in reasons],
             reasons[0] if reasons else None, '', ''] + [0] * len(PSYCH_AXES))


@app.callback(
    [Output('store-plans', 'data', allow_duplicate=True),
     Output('cancel-plan-modal', 'is_open', allow_duplicate=True),
     Output('cancel-plan-status', 'children', allow_duplicate=True)],
    Input('btn-cancel-plan-save', 'n_clicks'),
    [State('store-cancel-plan-id', 'data'), State('cancel-reason', 'value'), State('cancel-note', 'value')] +
    _psych_states('cancel') + [State('store-plans', 'data'), State('store-journal', 'data')],
    prevent_initial_call=True,
)
def save_cancel_plan(n_clicks, plan_id, reason, note, *rest):
    if not n_clicks or not plan_id:
        raise PreventUpdate
    psych, (plans, journal) = rest[:len(PSYCH_AXES)], rest[len(PSYCH_AXES):]
    row = next((r for r in (journal or []) if r.get('plan_id') == plan_id), None)
    if not row or row.get('status') != 'Planned (no execution matched yet)':
        return no_update, True, dbc.Alert(
            "Only a committed plan with no fills can be cancelled. If shares were bought, it's a real "
            "trade: exit it instead.", color='danger', className="py-1")
    fields = cancel_fields(reason, note, psych)
    if not db.update_plan(plan_id, fields):
        raise PreventUpdate
    return _plans_with(plans, plan_id, fields), False, ''


def cancel_fields(reason, note, psych_values=None):
    """The plan fields that cancel it. The psych reading is stored only if a
    slider was moved -- an untouched form isn't a genuine neutral reading."""
    fields = {'status': 'cancelled', 'cancelled_at': db.utc_now().isoformat(timespec='seconds'),
              'cancel_reason': (reason or '').strip() or None, 'cancel_note': (note or '').strip() or None,
              'counterfactual': None}
    moved = any(int(v or 0) != 0 for v in (psych_values or []))
    for a, v in zip(PSYCH_AXES, psych_values or [None] * len(PSYCH_AXES)):
        fields[f"cancel_{a['key']}"] = int(v or 0) if moved else None
    return fields


REINSTATE_FIELDS = {'status': 'committed', 'cancelled_at': None, 'cancel_reason': None, 'cancel_note': None,
                    'cancel_psych_risk': None, 'cancel_psych_urgency': None, 'cancel_psych_focus': None,
                    'counterfactual': None}


def _plans_with(plans, plan_id, fields):
    """store-plans with one plan's fields replaced (as the DB now holds them)."""
    out = []
    for p in plans or []:
        if p['plan_id'] == plan_id:
            p = dict(p)
            p.update(fields)
        out.append(p)
    return out


@app.callback(
    Output('cancel-plan-modal', 'is_open', allow_duplicate=True),
    Input('btn-cancel-plan-close', 'n_clicks'),
    prevent_initial_call=True,
)
def close_cancel_plan(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return False


@app.callback(
    Output('store-plans', 'data', allow_duplicate=True),
    Input({'type': 'btn-reinstate-plan', 'index': ALL}, 'n_clicks'),
    State('store-plans', 'data'),
    prevent_initial_call=True,
)
def reinstate_plan_cb(_clicks, plans):
    trig = callback_context.triggered_id
    if not trig or not callback_context.triggered[0].get('value'):
        raise PreventUpdate
    if not db.update_plan(trig['index'], dict(REINSTATE_FIELDS)):
        raise PreventUpdate
    return _plans_with(plans, trig['index'], dict(REINSTATE_FIELDS))


@app.callback(
    [Output('store-plans', 'data', allow_duplicate=True), Output('cancelled-refresh-status', 'children')],
    Input('btn-refresh-cancelled', 'n_clicks'),
    State('store-plans', 'data'),
    prevent_initial_call=True,
)
def refresh_cancelled_cb(n_clicks, plans):
    if not n_clicks:
        raise PreventUpdate
    cancelled = [p for p in (plans or []) if is_cancelled(p)]
    if not cancelled:
        return no_update, html.Span("No cancelled plans yet.", className="text-muted")
    updated, errors = refresh_cancelled_outcomes(cancelled)
    new_plans = plans
    for plan_id, cf in updated.items():
        new_plans = _plans_with(new_plans, plan_id, {'counterfactual': cf})
    done = len(cancelled) - len(updated) - len(errors)
    msg = f"Checked {len(updated)}" + (f", {done} already final" if done else "")
    if errors:
        msg += f" · couldn't fetch: {', '.join(errors)}"
    return (new_plans if updated else no_update), html.Span(msg, className="text-danger" if errors else "text-muted")


@app.callback(
    [Output('cancel-settings-status', 'children'), Output('store-plans', 'data', allow_duplicate=True)],
    Input('btn-save-cancel-settings', 'n_clicks'),
    [State('cancel-reasons-text', 'value'), State('stale-plan-days', 'value'), State('store-plans', 'data')],
    prevent_initial_call=True,
)
def save_cancel_settings(n_clicks, reasons_text, stale_days, plans):
    if not n_clicks:
        raise PreventUpdate
    reasons = parse_cancel_reasons(reasons_text)
    if not reasons:
        return dbc.Alert("Add at least one reason.", color='danger', className="py-1"), no_update
    try:
        days = int(stale_days)
        if days < 1:
            raise ValueError
    except (TypeError, ValueError):
        return dbc.Alert("Stale after must be a whole number of days, 1 or more.", color='danger',
                         className="py-1"), no_update
    db.set_setting('cancel_reasons', reasons)
    db.set_setting('stale_plan_days', days)
    # Re-sending store-plans rebuilds the journal so the stale flags use the new threshold.
    return html.Span(f"Saved {len(reasons)} reasons; stale after {days} days.", className="text-success"), \
        list(plans or [])


@app.callback(
    [Output('gap-settings-status', 'children'), Output('store-plans', 'data', allow_duplicate=True)],
    Input('btn-save-gap-settings', 'n_clicks'),
    [State('stop-gap-atr', 'value'), State('store-plans', 'data')],
    prevent_initial_call=True,
)
def save_gap_settings(n_clicks, value, plans):
    if not n_clicks:
        raise PreventUpdate
    try:
        k = float(value)
        if not 0 <= k <= MAX_STOP_GAP_ATR:
            raise ValueError
    except (TypeError, ValueError):
        return html.Span(f"Enter a number from 0 to {MAX_STOP_GAP_ATR:g}.", className="text-danger"), no_update
    db.set_setting('stop_gap_allowance_atr', k)
    # Re-sending store-plans re-renders the heat banner with the new allowance.
    return html.Span(f"Saved: {k:g} × ATR." if k else "Saved: gap allowance off.", className="text-success"), \
        list(plans or [])


def parse_cancel_reasons(text):
    """One reason per line, trimmed, blanks and duplicates dropped, order kept."""
    out = []
    for line in (text or '').splitlines():
        line = line.strip()
        if line and line not in out:
            out.append(line)
    return out


@app.callback(
    Output('download-journal', 'data'),
    Input('btn-export', 'n_clicks'),
    State('store-journal', 'data'),
    prevent_initial_call=True,
)
def export_journal(n_clicks, data):
    if not n_clicks or not data:
        raise PreventUpdate
    df = pd.DataFrame(data).drop(columns=['exec_notes'], errors='ignore')
    return dcc.send_data_frame(df.to_csv, "trade_journal.csv", index=False)


# ==============================================================
# 6. CRUD: edit / delete plans, truncate tables
# ==============================================================

def campaign_edit_values(plan):
    """The Edit modal's plan type, trail start and add-row values for a plan
    (empty add rows for a scale-out plan)."""
    adds = list(plan.get('adds') or []) if is_campaign(plan) else []
    pad = adds + [{}] * (db.MAX_TARGETS - len(adds))
    return ((plan_type_of(plan), str(int(_scalar(plan.get('trail_start_add')) or 0)))
            + tuple(a.get('price') for a in pad) + tuple(a.get('qty') for a in pad)
            + tuple(a.get('stop_before') for a in pad))


@app.callback(
    [Output('edit-modal', 'is_open', allow_duplicate=True),
     Output('store-editing-plan-id', 'data'),
     Output('edit-symbol', 'value'), Output('edit-direction', 'value'),
     Output('edit-currency', 'value'), Output('edit-account', 'value'),
     Output('edit-date', 'date'), Output('edit-entry', 'value'),
     Output('edit-stop', 'value'), Output('edit-qty', 'value'),
     Output('edit-reason', 'value'), Output('edit-notes', 'value'),
     Output('edit-status', 'children'),
     Output('edit-atr', 'value'), Output('edit-atr-mult', 'value'), Output('edit-atr-timeframe', 'value'),
     Output('edit-target-method', 'value'), Output('edit-atr-status', 'children'),
     Output('edit-stop-method', 'value'), Output('edit-trailing-activate-after', 'value'),
     Output('edit-trailing-atr-mult', 'value'),
     Output('edit-lessons-learned', 'value'), Output('edit-lesson-keep', 'value'),
     Output('edit-screenshot-preview', 'children', allow_duplicate=True),
     Output('store-edit-screenshot-removed', 'data', allow_duplicate=True)] +
    [Output(f'edit-t{i}-price', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [Output(f'edit-t{i}-qty', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [Output(f'edit-t{i}-stop', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [Output(f'edit-t{i}-rmult', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [Output('edit-trade-triggers', 'value')] + _psych_outputs('edit') +
    [Output('edit-plan-type', 'value'), Output('edit-trail-start-add', 'value')] +
    [Output(f'edit-a{i}-{f}', 'value', allow_duplicate=True) for f in ('price', 'qty', 'stop')
     for i in range(1, db.MAX_TARGETS + 1)] +
    [Output('edit-day-catalyst', 'value'), Output('edit-day-setup', 'value'),
     Output('store-edit-loaded', 'data')],
    Input({'type': 'btn-edit', 'index': ALL}, 'n_clicks'),
    State('store-plans', 'data'),
    prevent_initial_call=True,
)
def open_edit_modal(n_clicks_list, plans):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    plan_id = triggered['index']
    plan = next((p for p in (plans or []) if p['plan_id'] == plan_id), None)
    if not plan:
        raise PreventUpdate
    target_row_vals = targets_to_row_values(plan.get('targets'))
    # target_row_vals is [p1,q1,s1,r1, p2,q2,s2,r2, ...]; the Output ordering
    # above is all prices, then all qtys, then all stops, then all rmults, so
    # regroup accordingly.
    prices = target_row_vals[0::4]
    qtys = target_row_vals[1::4]
    stops = target_row_vals[2::4]
    rmults = target_row_vals[3::4]
    if plan.get('screenshot_path'):
        screenshot_preview = html.Img(src=screenshot_url(plan['screenshot_path']) or
                                      app.get_asset_url(plan['screenshot_path']),
                                       style={'maxWidth': '300px', 'maxHeight': '200px', 'display': 'block'})
    else:
        screenshot_preview = html.P("No screenshot attached.", className="text-muted small")
    return (True, plan_id, plan['symbol'], plan['direction'], plan.get('currency') or 'USD',
            plan.get('account_id'), plan.get('planned_date'),
            plan['entry_price'], plan.get('stop_loss'), plan['qty'],
            plan.get('reason', ''), plan.get('notes', ''), '',
            plan.get('atr_at_commit'), plan.get('atr_multiplier') or 1.5, atr_timeframe_of(plan),
            plan.get('target_method') or 'atr',
            html.Small("The ATR saved with this plan. Changing the symbol, currency or timeframe fetches a new ATR "
                       "and, while Method is ATR-derived, recomputes the stop and targets.", className="text-muted"),
            stop_method_of(plan),
            plan.get('trailing_activate_after_target') or 1,
            plan.get('trailing_atr_multiplier') if plan.get('trailing_atr_multiplier') is not None else 2.0,
            plan.get('lessons_learned') or '', plan.get('lesson_keep') or '',
            screenshot_preview, False) + tuple(prices) + tuple(qtys) + tuple(stops) + tuple(rmults) + (
            list(plan.get('trade_triggers') or []),) + tuple(
            _psych_val(plan.get(a['key'])) or 0 for a in PSYCH_AXES) + campaign_edit_values(plan) + (
            _scalar(plan.get('day_catalyst')) or None, _scalar(plan.get('day_setup')) or '',
            edit_form_loaded_values(plan, prices, rmults))


@app.callback(
    Output('edit-modal', 'is_open', allow_duplicate=True),
    Input('btn-edit-cancel', 'n_clicks'),
    prevent_initial_call=True,
)
def cancel_edit(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return False


@app.callback(
    [Output('store-plans', 'data', allow_duplicate=True),
     Output('edit-modal', 'is_open', allow_duplicate=True),
     Output('plan-add-status', 'children', allow_duplicate=True)],
    Input('btn-edit-save', 'n_clicks'),
    [State('store-editing-plan-id', 'data'), State('edit-symbol', 'value'),
     State('edit-direction', 'value'), State('edit-currency', 'value'), State('edit-account', 'value'),
     State('edit-date', 'date'),
     State('edit-entry', 'value'), State('edit-stop', 'value'),
     State('edit-qty', 'value'), State('edit-reason', 'value'), State('edit-notes', 'value'),
     State('edit-atr', 'value'), State('edit-atr-mult', 'value'), State('edit-atr-timeframe', 'value'),
     State('edit-target-method', 'value'),
     State('edit-stop-method', 'value'), State('edit-trailing-activate-after', 'value'),
     State('edit-trailing-atr-mult', 'value'),
     State('edit-trade-triggers', 'value')] + _psych_states('edit') +
    [State('edit-lessons-learned', 'value'), State('edit-lesson-keep', 'value'),
     State('edit-screenshot-upload', 'contents'), State('edit-screenshot-upload', 'filename'),
     State('store-edit-screenshot-removed', 'data'),
     State('store-plans', 'data')] +
    [State(f'edit-t{i}-price', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [State(f'edit-t{i}-qty', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [State(f'edit-t{i}-stop', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [State(f'edit-t{i}-rmult', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [State('edit-plan-type', 'value'), State('edit-trail-start-add', 'value')] +
    [State(f'edit-a{i}-{f}', 'value') for f in ('price', 'qty', 'stop') for i in range(1, db.MAX_TARGETS + 1)] +
    [State('edit-day-catalyst', 'value'), State('edit-day-setup', 'value')],
    prevent_initial_call=True,
)
def save_edit(n_clicks, plan_id, symbol, direction, currency, account_id, planned_date, entry, stop, qty,
              reason, notes, atr_value, atr_mult, atr_timeframe, target_method, stop_method,
              trailing_activate_after, trailing_atr_mult, trade_triggers,
              psych_risk, psych_urgency, psych_focus,
              lessons_learned, lesson_keep, screenshot_contents, screenshot_filename, screenshot_removed,
              plans, *target_vals):
    if not n_clicks or not plan_id:
        raise PreventUpdate
    if not account_id or not symbol or not direction or entry in (None, '') or qty in (None, ''):
        status = dbc.Alert("Account, Symbol, Direction, Entry Price and Quantity are required.", color='danger')
        return no_update, True, status

    # target_vals = the 4 target State groups, then plan type, trail start, and the add rows.
    n4 = 4 * db.MAX_TARGETS
    plan_type, trail_start_add = target_vals[n4], target_vals[n4 + 1]
    adds = adds_from_form(target_vals[n4 + 2:])
    day_catalyst, day_setup = (target_vals[-2], target_vals[-1]) \
        if len(target_vals) >= n4 + 2 + 3 * db.MAX_TARGETS + 2 else (None, None)
    target_vals = target_vals[:n4]
    campaign = plan_type == 'scale_in'
    day = plan_type == 'day_trade'
    prices = target_vals[0:db.MAX_TARGETS]
    qtys = target_vals[db.MAX_TARGETS:2 * db.MAX_TARGETS]
    stops = target_vals[2 * db.MAX_TARGETS:3 * db.MAX_TARGETS]
    rmults = target_vals[3 * db.MAX_TARGETS:4 * db.MAX_TARGETS]
    flat = []
    for p, q, s, r in zip(prices, qtys, stops, rmults):
        flat += [p, q, s, r]
    targets = [] if campaign else build_targets_list(*flat)
    if day:
        targets = day_trade_targets(direction, entry, targets)
    error = (((validate_campaign(direction, entry, stop, adds, stop_method) or validate_swing_timeframe(atr_timeframe))
              if campaign
              else (validate_day_trade(direction, entry, stop, targets, stop_method, atr_timeframe, day_catalyst,
                                       day_setup) if day else validate_swing_timeframe(atr_timeframe))
              or _validate_target_qty_pct(targets))
             or _campaign_conflict(plans, account_id, symbol, currency, campaign or day, _closed_plan_ids(),
                                   exclude_plan_id=plan_id))
    if error:
        status = dbc.Alert(error, color='danger')
        return no_update, True, status

    fields = {
        'plan_type': 'scale_in' if campaign else ('day_trade' if day else 'scale_out'),
        'day_catalyst': ((day_catalyst or '').strip() or None) if day else None,
        'day_setup': (day_setup or None) if day else None,
        'adds': adds if campaign else [],
        'trail_start_add': int(trail_start_add or 0) if campaign else None,
        'symbol': symbol.strip().upper(),
        'direction': direction,
        'currency': currency or 'USD',
        'account_id': account_id,
        'planned_date': planned_date,
        'entry_price': float(entry),
        'stop_loss': float(stop) if stop not in (None, '') else None,
        'targets': targets,
        'qty': float(qty),
        'reason': reason or '',
        'notes': notes or '',
        'target_method': target_method,
        'atr_at_commit': float(atr_value) if atr_value not in (None, '') else None,
        'atr_multiplier': float(atr_mult) if atr_mult not in (None, '') else None,
        'atr_timeframe': atr_timeframe if atr_timeframe in ATR_TIMEFRAME_LABELS else DEFAULT_ATR_TIMEFRAME,
        'stop_method': stop_method if stop_method in dict(STOP_METHODS) else 'ladder',
        'trailing_stop_enabled': 0 if stop_method in (None, 'ladder') else 1,
        'trailing_activate_after_target': int(trailing_activate_after) if trailing_activate_after not in (None, '') else None,
        'trailing_atr_multiplier': float(trailing_atr_mult) if trailing_atr_mult not in (None, '') else None,
        'trade_triggers': list(trade_triggers or []),
        'lessons_learned': lessons_learned or '',
        'lesson_keep': lesson_keep or '',
    }
    # A pre-v2 plan opens with all three sliders at 0 (NULL displays as 0).
    # Saving an unrelated edit must not silently stamp it as "rated calm/
    # patient/neutral" -- only write the v2 axes if this plan was already
    # rated under v2, or the user actually moved a slider this time.
    psych_values = [psych_risk, psych_urgency, psych_focus]
    existing = next((p for p in (plans or []) if p['plan_id'] == plan_id), {})
    if has_psych_v2(existing) or any(psych_values):
        fields.update(_psych_fields(psych_values))
    # Screenshot is the one field NOT always overwritten: a new upload replaces
    # it, an explicit Remove clears it, but leaving both alone (the common
    # case -- editing something unrelated) must not touch the existing file/
    # path, so it's simply left out of `fields` in that case.
    if screenshot_contents:
        fields['screenshot_path'] = _save_plan_screenshot(plan_id, screenshot_filename, screenshot_contents)
    elif screenshot_removed:
        _remove_plan_screenshot(plan_id)
        fields['screenshot_path'] = None
    db.update_plan(plan_id, fields)

    plans = plans or []
    new_plans = []
    for p in plans:
        if p['plan_id'] == plan_id:
            p = dict(p)
            p.update(fields)
            p['adds'] = db._clean_adds(fields.get('adds'))       # the same cleaned form the DB holds
        new_plans.append(p)

    status = dbc.Alert(f"✅ Plan for {fields['symbol']} updated and saved to the database.", color='success')
    return new_plans, False, status


@app.callback(
    [Output('edit-screenshot-preview', 'children', allow_duplicate=True),
     Output('store-edit-screenshot-removed', 'data', allow_duplicate=True)],
    Input('edit-btn-remove-screenshot', 'n_clicks'),
    prevent_initial_call=True,
)
def mark_edit_screenshot_removed(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return html.P("Screenshot will be removed when you save.", className="text-muted small"), True


@app.callback(
    [Output('match-modal', 'is_open'), Output('store-matching-plan-id', 'data'),
     Output('match-rt-select', 'options'), Output('match-rt-select', 'value'),
     Output('match-status', 'children')],
    Input({'type': 'btn-match', 'index': ALL}, 'n_clicks'),
    [State('store-plans', 'data'), State('store-roundtrips', 'data'), State('store-plan-matches', 'data'),
     State('store-openpos', 'data'), State('store-plan-lot-matches', 'data'),
     State('store-unmatched-rts', 'data'), State('store-accounts', 'data')],
    prevent_initial_call=True,
)
def open_match_modal(n_clicks_list, plans, roundtrips, plan_matches, openpos, plan_lot_matches,
                     unmatched, accounts):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    plan_id = triggered['index']
    plan = next((p for p in (plans or []) if p['plan_id'] == plan_id), None)
    if not plan:
        raise PreventUpdate

    archived = {r['rt_id'] for r in (unmatched or []) if is_archived(r, accounts)}
    rt_candidates = [rt for rt in (roundtrips or [])
                      if rt['direction'] == plan['direction']
                      and (rt['rt_id'] not in archived or (plan_matches or {}).get(plan_id) == rt['rt_id'])
                      and plan_fits_position(plan, rt.get('account_id'), rt['symbol'], rt.get('currency'))]
    rt_candidates.sort(key=lambda rt: rt['exit_time'], reverse=True)
    options = [{
        'label': (f"{rt['entry_time'][:10]} → {rt['exit_time'][:10]}  "
                  f"{rt.get('currency') or ''} {rt['entry_price']:.2f}→{rt['exit_price']:.2f}  qty {rt['qty']:.4g}"),
        'value': rt['rt_id'],
    } for rt in rt_candidates]
    # Open lots (still-held pyramided positions) compete for the same
    # override alongside closed round trips -- prefixed 'open:' the same way
    # match_plans_to_roundtrips encodes a lot match internally, so save/clear
    # below can tell the two kinds apart by the value alone.
    lot_candidates = [op for op in (openpos or [])
                       if op.get('direction', 'BUY') == plan['direction']
                       and plan_fits_position(plan, op.get('account_id'), op['symbol'], op.get('currency'))]
    options += [{
        'label': f"OPEN since {op['since'][:10]}  avg {op.get('currency') or ''} {op['avg_price']:.2f}  qty {op['qty_open']:.4g}",
        'value': f"open:{op['lot_id']}",
    } for op in lot_candidates]
    current_value = (plan_matches or {}).get(plan_id)
    if current_value is None and plan_id in (plan_lot_matches or {}):
        current_value = f"open:{plan_lot_matches[plan_id]}"

    note = html.Small(
        f"{len(rt_candidates)} round trip(s), {len(lot_candidates)} open lot(s) found for "
        f"{plan['symbol']} ({plan['direction']}).", className="text-muted")
    return True, plan_id, options, current_value, note


@app.callback(
    Output('match-modal', 'is_open', allow_duplicate=True),
    Input('btn-match-cancel', 'n_clicks'),
    prevent_initial_call=True,
)
def cancel_match(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return False


@app.callback(
    [Output('store-plan-matches', 'data', allow_duplicate=True),
     Output('store-plan-lot-matches', 'data', allow_duplicate=True),
     Output('match-modal', 'is_open', allow_duplicate=True)],
    Input('btn-match-save', 'n_clicks'),
    [State('store-matching-plan-id', 'data'), State('match-rt-select', 'value'),
     State('store-plan-matches', 'data'), State('store-plan-lot-matches', 'data')],
    prevent_initial_call=True,
)
def save_match(n_clicks, plan_id, selected_value, plan_matches, plan_lot_matches):
    if not n_clicks or not plan_id or not selected_value:
        raise PreventUpdate
    plan_matches = dict(plan_matches or {})
    plan_lot_matches = dict(plan_lot_matches or {})
    if isinstance(selected_value, str) and selected_value.startswith('open:'):
        lot_id_selected = selected_value[len('open:'):]
        db.set_plan_lot_match(plan_id, lot_id_selected)
        db.clear_plan_match(plan_id)  # a plan matches at most one of a lot or a round trip
        plan_matches = {pid: rid for pid, rid in plan_matches.items() if pid != plan_id}
        # A lot can only be claimed by one plan -- drop it from whoever had it before.
        plan_lot_matches = {pid: lid for pid, lid in plan_lot_matches.items() if lid != lot_id_selected}
        plan_lot_matches[plan_id] = lot_id_selected
    else:
        db.set_plan_match(plan_id, selected_value)
        db.clear_plan_lot_match(plan_id)
        plan_lot_matches = {pid: lid for pid, lid in plan_lot_matches.items() if pid != plan_id}
        # A round trip can only be claimed by one plan -- drop it from whoever had it before.
        plan_matches = {pid: rid for pid, rid in plan_matches.items() if rid != selected_value}
        plan_matches[plan_id] = selected_value
    return plan_matches, plan_lot_matches, False


@app.callback(
    [Output('store-plan-matches', 'data', allow_duplicate=True),
     Output('store-plan-lot-matches', 'data', allow_duplicate=True),
     Output('match-modal', 'is_open', allow_duplicate=True)],
    Input('btn-match-clear', 'n_clicks'),
    [State('store-matching-plan-id', 'data'), State('store-plan-matches', 'data'),
     State('store-plan-lot-matches', 'data')],
    prevent_initial_call=True,
)
def clear_match(n_clicks, plan_id, plan_matches, plan_lot_matches):
    if not n_clicks or not plan_id:
        raise PreventUpdate
    db.clear_plan_match(plan_id)
    db.clear_plan_lot_match(plan_id)
    db.delete_plan_lock(plan_id)  # a cleared match is re-matched and locked afresh
    plan_matches = dict(plan_matches or {})
    plan_matches.pop(plan_id, None)
    plan_lot_matches = dict(plan_lot_matches or {})
    plan_lot_matches.pop(plan_id, None)
    return plan_matches, plan_lot_matches, False


@app.callback(
    [Output('confirm-delete-plan', 'displayed'), Output('confirm-delete-plan', 'message'),
     Output('store-pending-delete-plan', 'data')],
    Input({'type': 'btn-delete', 'index': ALL}, 'n_clicks'),
    State('store-plans', 'data'),
    prevent_initial_call=True,
)
def ask_delete_plan(n_clicks_list, plans):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    plan_id = triggered['index']
    plan = next((p for p in (plans or []) if p['plan_id'] == plan_id), None)
    if not plan:
        raise PreventUpdate
    msg = f"Delete the trade plan for {plan['symbol']} ({plan['direction']}, entry {plan['entry_price']})? This cannot be undone."
    return True, msg, plan_id


@app.callback(
    [Output('store-plans', 'data', allow_duplicate=True),
     Output('plan-add-status', 'children', allow_duplicate=True),
     Output('store-plan-matches', 'data', allow_duplicate=True)],
    Input('confirm-delete-plan', 'submit_n_clicks'),
    [State('store-pending-delete-plan', 'data'), State('store-plans', 'data'), State('store-plan-matches', 'data')],
    prevent_initial_call=True,
)
def do_delete_plan(submit_n_clicks, plan_id, plans, plan_matches):
    if not submit_n_clicks or not plan_id:
        raise PreventUpdate
    db.delete_plan(plan_id)  # cascades to plan_matches in the DB (ON DELETE CASCADE)
    new_plans = [p for p in (plans or []) if p['plan_id'] != plan_id]
    new_matches = dict(plan_matches or {})
    new_matches.pop(plan_id, None)
    status = dbc.Alert("🗑️ Plan deleted.", color='secondary')
    return new_plans, status, new_matches


@app.callback(
    Output('confirm-clear-plans', 'displayed'),
    Input('btn-clear-plans', 'n_clicks'),
    prevent_initial_call=True,
)
def ask_clear_plans(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return True


@app.callback(
    [Output('store-plans', 'data', allow_duplicate=True), Output('clear-status', 'children', allow_duplicate=True),
     Output('store-plan-matches', 'data', allow_duplicate=True)],
    Input('confirm-clear-plans', 'submit_n_clicks'),
    prevent_initial_call=True,
)
def do_clear_plans(submit_n_clicks):
    if not submit_n_clicks:
        raise PreventUpdate
    n = db.delete_all_plans()  # cascades to plan_matches in the DB (ON DELETE CASCADE)
    return [], dbc.Alert(f"🗑️ Deleted all {n} trade plan(s) from the database.", color='secondary'), {}


@app.callback(
    Output('confirm-clear-executions', 'displayed'),
    Input('btn-clear-executions', 'n_clicks'),
    prevent_initial_call=True,
)
def ask_clear_executions(n_clicks):
    if not n_clicks:
        raise PreventUpdate
    return True


@app.callback(
    [Output('store-executions', 'data', allow_duplicate=True),
     Output('store-roundtrips', 'data', allow_duplicate=True),
     Output('store-openpos', 'data', allow_duplicate=True),
     Output('clear-status', 'children', allow_duplicate=True)],
    Input('confirm-clear-executions', 'submit_n_clicks'),
    prevent_initial_call=True,
)
def do_clear_executions(submit_n_clicks):
    if not submit_n_clicks:
        raise PreventUpdate
    n = db.delete_all_executions()
    return [], [], [], dbc.Alert(f"🗑️ Deleted all {n} imported execution(s) and their derived round-trip trades "
                                  f"from the database. Trade plans were kept.", color='secondary')


@app.callback(
    Output('db-counts', 'children'),
    [Input('store-plans', 'data'), Input('store-executions', 'data')],
)
def show_db_counts(plans, executions):
    c = db.counts()
    return f"Database currently holds {c['plans']} trade plan(s) and {c['executions']} imported execution(s)."


# ==============================================================
# 7. MCP tools
#
# Dash 4's native MCP server (enabled above via enable_mcp=True /
# configure_mcp_server) exposes these plain functions directly as tools over
# the app's own /_mcp endpoint -- no separate server process, no new
# dependency. Each one wraps the same db.py/app.py logic the UI callbacks use,
# with a clean signature instead of a callback's Input/State list, so it's a
# purpose-built tool rather than a mechanical wrapper around a button click.
#
# Important: Dash's own MCP instructions note applies here too -- a tool call
# runs this function and its side effects (DB writes are real and persisted)
# but does NOT push an update to the user's already-open browser tab. If
# they have the app open while you call a write tool, tell them to refresh
# to see it.
# ==============================================================

def _journal_from_db():
    """get_journal's actual computation, reading fresh from the database
    (there's no dcc.Store to read outside of a browser session)."""
    executions = db.fetch_executions()
    plans = db.fetch_plans()
    rt_df, op_df = build_round_trips(executions, plans)
    _reconcile_closed_lot_matches(op_df, rt_df)
    plan_matches = db.fetch_plan_matches()
    plan_lot_matches = db.fetch_plan_lot_matches()
    journal_rows, _ = _build_journal_and_unmatched(plans, rt_df, op_df, plan_matches, plan_lot_matches)
    return journal_rows


def _account_filter(records, account_id):
    return _filter_view(records, account_id) if account_id else list(records or [])


@mcp_enabled(expose_docstring=True)
def list_accounts() -> list:
    """List the Wealthsimple accounts TAPEs knows about: account_id (the
    Wealthsimple account number), account_type (TFSA, RRSP, LIRA, ...),
    nickname and base_currency. Every plan, execution and position belongs to
    exactly one account; pass account_id to the other tools to drill down."""
    return db.fetch_accounts()


@mcp_enabled(expose_docstring=True)
def list_plans(status: str = None, account_id: str = None) -> list:
    """List trade plans from the database. Pass status='draft',
    'committed' or 'cancelled' to filter; omit to get all plans regardless of status.
    account_id (optional) limits the result to one account; omit for all accounts (see list_accounts)."""
    plans = _account_filter(db.fetch_plans(), account_id)
    if status:
        plans = [p for p in plans if p.get('status') == status]
    return plans


@mcp_enabled(expose_docstring=True)
def cancel_plan(plan_id: str, reason: str, note: str = None) -> dict:
    """Cancel a committed plan that has no fills yet: it's kept (status
    'cancelled'), dropped from execution matching and scoring, and can later be
    replayed with refresh_cancelled_outcomes. reason is free text but should
    normally be one of the list returned in `reasons` (editable in the app's
    Data Management). A plan with fills can't be cancelled -- it's a real trade.
    Doesn't update an open browser tab; refresh it to see the change."""
    plan = next((p for p in db.fetch_plans() if p['plan_id'] == plan_id), None)
    if not plan:
        return {'error': f"No plan found with id '{plan_id}'"}
    row = next((r for r in _journal_from_db() if r.get('plan_id') == plan_id), None)
    if plan.get('status') != 'committed' or not row or row.get('status') != 'Planned (no execution matched yet)':
        return {'error': "Only a committed plan with no fills can be cancelled.", 'reasons': load_cancel_reasons()}
    if not (reason or '').strip():
        return {'error': "A reason is required.", 'reasons': load_cancel_reasons()}
    db.update_plan(plan_id, cancel_fields(reason, note))
    return {'cancelled': plan_id, 'reasons': load_cancel_reasons()}


@mcp_enabled(expose_docstring=True)
def reinstate_plan(plan_id: str) -> dict:
    """Undo a cancellation: the plan goes back to committed, and its reason,
    note and "what would have happened" result are cleared."""
    plan = next((p for p in db.fetch_plans() if p['plan_id'] == plan_id), None)
    if not plan or not is_cancelled(plan):
        return {'error': f"No cancelled plan with id '{plan_id}'"}
    db.update_plan(plan_id, dict(REINSTATE_FIELDS))
    return {'reinstated': plan_id}


@mcp_enabled(name='refresh_cancelled_outcomes', expose_docstring=True)
def refresh_cancelled_outcomes_tool() -> dict:
    """Replay every cancelled plan against daily price history since it went
    live (up to 60 sessions) to see what it would have done if kept: whether
    the entry filled, how it ended (stopped_out / targets_done / still_open /
    never_filled) and its R (scale-out: per share vs. planned entry-to-stop;
    campaign: vs. peak risk taken). Results are saved; a result is redone
    only until it's final (exited, or the full window covered). Fetches from
    Yahoo Finance, so it can take a few seconds per plan."""
    plans = db.fetch_plans()
    updated, errors = refresh_cancelled_outcomes(plans)
    out = []
    for p in plans:
        if not is_cancelled(p):
            continue
        cf = updated.get(p['plan_id'], p.get('counterfactual'))
        out.append({'plan_id': p['plan_id'], 'symbol': p['symbol'], 'reason': p.get('cancel_reason'),
                    'if_kept': counterfactual_text(cf, p.get('currency')), 'result': cf})
    return {'plans': out, 'errors': errors}


@mcp_enabled(expose_docstring=True)
def get_plan(plan_id: str) -> dict:
    """Get one trade plan by id, including its pre-trade risk/reward computed
    from the plan alone: max_loss (full position stopped at the initial stop),
    max_reward (the full scale-out ladder -- each target's Qty % exiting at
    that target; reward_basis 'best_target' means no Qty % is set, so it's the
    whole position at the furthest target), risk_reward_ratio, and
    stop_out_scenarios (the result if stopped out after each stage).
    gap_allowance_per_share, max_loss_with_gap and risk_reward_with_gap add
    the stop gap allowance (setting, x the plan's ATR) to the stop distance --
    the figures to size with, since a gap can jump the stop.

    A scale-in campaign (plan_type 'scale_in') has no targets: max_loss is
    its peak-stage risk, max_reward/risk_reward_ratio are None (the exit is
    the trailing stop), and campaign_stages lists the probe and each add
    with the stop in place, shares, average cost and the result if stopped.

    screenshot_file is the absolute path of the chart screenshot saved with
    the plan (None if there isn't one). The image itself isn't in this
    result; open that path with a file-reading tool (e.g. Claude Code's Read,
    which displays images) to see the chart as it was at planning time."""
    plans = [p for p in db.fetch_plans() if p['plan_id'] == plan_id]
    if not plans:
        return {'error': f"No plan found with id '{plan_id}'"}
    plan = plans[0]
    gap = gap_per_share(plan.get('atr_at_commit'), plan.get('entry_price'), plan.get('stop_loss'))
    ladder = compute_plan_ladder(plan['direction'], plan.get('entry_price'), plan.get('stop_loss'),
                                 plan.get('targets') or [], plan.get('qty'), gap) or {}
    plan = dict(plan)
    plan['gap_allowance_per_share'] = _fmt_money(gap) if gap else None
    plan['max_loss_with_gap'] = _fmt_money(ladder.get('max_loss_gap'))
    plan['risk_reward_with_gap'] = round(ladder['rr_gap'], 2) if ladder.get('rr_gap') is not None else None
    plan['max_loss'] = _fmt_money(ladder.get('max_loss'))
    plan['max_reward'] = _fmt_money(ladder.get('reward'))
    plan['reward_basis'] = ladder.get('reward_basis')
    plan['best_target_reward'] = _fmt_money(ladder.get('best_target_reward'))
    plan['risk_reward_ratio'] = round(ladder['rr'], 2) if ladder.get('rr') is not None else None
    plan['screenshot_file'] = screenshot_file(plan.get('screenshot_path'))
    plan['stop_out_scenarios'] = [
        {'scenario': sc['label'], 'stop_then': sc['stop'], 'result': _fmt_money(sc['pnl']),
         'r': round(sc['r'], 2) if sc['r'] is not None else None} for sc in ladder.get('scenarios', [])]
    if is_campaign(plan):
        stages = compute_campaign_stages(plan['direction'], plan.get('entry_price'), plan.get('stop_loss'),
                                         plan.get('qty'), plan.get('adds') or [], gap) or {}
        plan.update({'max_loss': _fmt_money(stages.get('peak_risk')), 'max_reward': None,
                     'max_loss_with_gap': _fmt_money(stages.get('peak_risk_gap')) if gap else None,
                     'risk_reward_with_gap': None,
                     'reward_basis': 'campaign', 'best_target_reward': None, 'risk_reward_ratio': None,
                     'stop_out_scenarios': [],
                     'campaign_stages': [{k: (_fmt_money(v) if isinstance(v, float) else v) for k, v in st.items()}
                                         for st in stages.get('stages', [])]})
    return plan


def _date_or_none(value):
    """'YYYY-MM-DD' (or any date-like text) as a date, None if blank; raises on garbage."""
    if value in (None, ''):
        return None
    return pd.to_datetime(value).date()


@mcp_enabled(expose_docstring=True)
def get_daily_journal(since: str = None, until: str = None, search: str = None, limit: int = 50) -> dict:
    """The Daily Trading Journal, newest first.
    entries: structured daily entries (one per date, since 2026-10-23) with
    the template's fields -- regime (composite, label, layers T/V/C/L/M/S/B,
    flags, weekly), delta5/delta20/flicker and the effective regime TAPEs
    computed (effective_limits, effective_tactics, effective_note),
    fomo_panic, heat_pct/heat_ceiling/positions/beta_pct, drift_actions,
    execution, watchlist [{symbol, tactic, trigger, news}], psych, lesson
    and market.
    legacy_notes: the older free-text notes (obs_id, obs_date, created_at,
    note).
    since / until (optional, 'YYYY-MM-DD', inclusive) limit the date range;
    search (optional) matches note text, fields or the date (case-insensitive);
    limit caps each list (default 50). matching/total count both kinds.
    Notes about a single trade are in get_trade_notes instead."""
    try:
        start, end = _date_or_none(since), _date_or_none(until)
    except (ValueError, TypeError):
        return {'error': "since/until must be dates like 'YYYY-MM-DD'"}
    items = journal_list_items(db.fetch_daily_journal_entries(), db.fetch_daily_observations())
    needle = (search or '').strip().lower()
    keep = []
    for o in items:
        day = _date_or_none(o.get('obs_date'))
        if (start and day and day < start) or (end and day and day > end):
            continue
        if needle and needle not in (o.get('note') or '').lower() and needle not in (o.get('obs_date') or ''):
            continue
        keep.append(o)
    keep.sort(key=lambda o: (o['obs_date'] or '', o.get('created_at') or ''), reverse=True)
    cap = max(1, int(limit or 50))
    entries = [o['entry'] for o in keep if o['kind'] == 'entry'][:cap]
    legacy = [{k: o.get(k) for k in ('obs_id', 'obs_date', 'created_at', 'note')}
              for o in keep if o['kind'] == 'legacy'][:cap]
    return {'entries': entries, 'legacy_notes': legacy, 'matching': len(keep), 'total': len(items)}


def weekly_journal_rows(reviews, daily_entries, checklist, checked_by_week, actions=()):
    """Weekly Review rows for get_weekly_journal, newest first: the balance
    with its basis and change vs. the previous week on the same basis, both
    notes, weekly-checklist progress (with what's still unticked), and the
    lessons from that week's structured daily entries (the 7 days to
    week_ending). Pure: callers pass what they read from the DB."""
    changes = weekly_balance_changes(sorted(reviews, key=lambda r: r['week_ending']))
    items = checklist_items(checklist)
    rows = []
    for r in sorted(reviews, key=lambda r: r['week_ending'], reverse=True):
        end = _as_date(r['week_ending'])
        try:
            detail = json.loads(r.get('balance_detail_json') or 'null')
        except (TypeError, ValueError):
            detail = None
        currency, with_cash = balance_basis(r)
        change = changes.get(r['week_ending'])
        ticked = set(checked_by_week.get(r['week_ending']) or [])
        lessons = [{'obs_date': e['obs_date'], 'lesson': e['lesson'], 'effective_limits': e.get('effective_limits')}
                   for e in sorted(daily_entries or [], key=lambda e: e['obs_date'])
                   if e.get('lesson') and end - dt.timedelta(days=6) <= _as_date(e['obs_date']) <= end]
        rows.append({
            'week_ending': r['week_ending'],
            'balance': round(r['balance'], 2) if r.get('balance') is not None else None,
            'balance_currency': currency or ('mixed' if r.get('balance') is not None else None),
            'balance_includes_cash': with_cash,
            'balance_detail': detail,
            'change': round(change, 2) if isinstance(change, (int, float)) else change,
            'user_note': r.get('user_note'), 'coaching_note': r.get('llm_note'),
            'checklist': {'done': sum(1 for i in items if i['id'] in ticked), 'total': len(items),
                          'unticked': [i['label'] for i in items if i['id'] not in ticked]},
            'daily_lessons': lessons,
            'entry': ({k: v for k, v in db.decode_weekly_entry(r).items() if k in WEEKLY_ENTRY_PROMPT_KEYS}
                      if r.get('entry_saved_at') else None),
            'focus_actions': [dict(id=action_label(a), **{k: a.get(k) for k in ('text', 'rules', 'status',
                                                                                  'status_note', 'history')})
                              for a in actions if a['source_week'] == r['week_ending']],
            'actions_reviewed': [{'id': action_label(a), 'text': a['text'], 'status': h.get('status'),
                                  'note': h.get('note'), 'reviewed_on': h.get('week')}
                                 for a in actions for h in a.get('history') or []
                                 if in_review_window(h.get('week'), r['week_ending'],
                                                     action_window_start(reviews, r['week_ending']))],
            'created_at': r.get('created_at'), 'updated_at': r.get('updated_at'),
        })
    return rows


@mcp_enabled(expose_docstring=True)
def get_weekly_journal(since: str = None, until: str = None, search: str = None, limit: int = 12) -> dict:
    """The Weekly Review journal, newest week first. Each week: week_ending,
    balance (balance_currency 'CAD', or 'mixed' for weeks saved before
    2026-10-08 that summed USD and CAD as-is; balance_includes_cash; the
    per-currency balance_detail), change vs. the previous week on the same
    basis (or 'basis_changed' when the method differs, so no real change can
    be read), user_note, coaching_note (the reply pasted back from the
    coaching chat), checklist {done, total, unticked} for the weekly
    checklist, daily_lessons (lesson/proposal lines from that week's
    structured daily entries), entry (the weekly template, None if not
    filled in: regime, posture next week, book, results in R, lesson
    decisions, graduations, watchlist, strategy changes, market),
    focus_actions (the actions created from that week's coaching note, id
    A<n>, with status and review history) and actions_reviewed (actions
    marked done / not_done / dropped in that week's entry).
    since / until (optional, 'YYYY-MM-DD', inclusive) filter on week_ending;
    search (optional) matches either note, a lesson or the date
    (case-insensitive); limit caps the weeks returned (default 12).
    Returns {weeks, matching (before the limit), total (all weeks)}.
    Day-by-day entries are in get_daily_journal."""
    try:
        start, end = _date_or_none(since), _date_or_none(until)
    except (ValueError, TypeError):
        return {'error': "since/until must be dates like 'YYYY-MM-DD'"}
    reviews = db.fetch_weekly_reviews()
    checked = {r['week_ending']: db.fetch_weekly_checklist_state(r['week_ending']) for r in reviews}
    rows = weekly_journal_rows(reviews, db.fetch_daily_journal_entries(), load_checklist('weekly'), checked,
                               db.fetch_weekly_actions())
    needle = (search or '').strip().lower()
    keep = []
    for w in rows:
        day = _date_or_none(w['week_ending'])
        if (start and day < start) or (end and day > end):
            continue
        text = ' '.join([w['week_ending'], w['user_note'] or '', w['coaching_note'] or '']
                        + [x['lesson'] for x in w['daily_lessons']]
                        + [str((w['entry'] or {}).get(k) or '') for k in ('market', 'strategy_changes', 'graduations')]
                        + [a['text'] for a in w['focus_actions']]).lower()
        if needle and needle not in text:
            continue
        keep.append(w)
    return {'weeks': keep[:max(1, int(limit or 12))], 'matching': len(keep), 'total': len(rows)}


def checklist_view(name, entries, checked_ids):
    """A checklist with its ticks, flattened for MCP: each item with its
    section and sub-heading."""
    checked = set(checked_ids or [])
    items, section, sub = [], None, None
    for e in entries or []:
        if e['kind'] == 'heading':
            if e['level'] == 1:
                section, sub = e['text'], None
            else:
                sub = e['text']
            continue
        items.append({'id': e['id'], 'label': e['label'], 'help': e.get('help') or '', 'section': section,
                      'subheading': sub, 'checked': e['id'] in checked,
                      'ticked_by_stop_review': is_auto_item(e)})
    return {'name': name, 'done': sum(i['checked'] for i in items), 'total': len(items), 'items': items}


@mcp_enabled(expose_docstring=True)
def get_checklist(name: str = 'daily', date: str = None) -> dict:
    """The daily or weekly checklist with what's ticked. name 'daily' or
    'weekly'. date (YYYY-MM-DD): the day for 'daily' (default today), the
    week_ending for 'weekly' (default the latest week with ticks). Returns
    name, date, done, total, items [{id, label, help, section, subheading,
    checked, ticked_by_stop_review}] and dates_with_ticks (the 30 most
    recent, to ask for another day). An item with ticked_by_stop_review can't
    be ticked by hand: it's ticked when that day's stop review (Wealthsimple
    pending orders vs the Drift Report) matched every stop. For 'daily',
    stop_review gives that day's review: checked_at (UTC), all_matched,
    summary and the orders read, or None if none was saved. Read-only; the
    to-do list isn't included."""
    if name not in CHECKLIST_NAMES:
        return {'error': f"name must be one of {sorted(CHECKLIST_NAMES)}"}
    dates = db.fetch_checklist_state_dates(name)
    if not date:
        date = dt.date.today().isoformat() if name == 'daily' else (dates[0] if dates else None)
    if date is None:
        return {'name': name, 'date': None, 'done': 0, 'total': 0, 'items': [], 'dates_with_ticks': []}
    checked = (db.fetch_daily_checklist_state(date) if name == 'daily'
               else db.fetch_weekly_checklist_state(date))
    out = {**checklist_view(name, load_checklist(name), checked), 'date': date, 'dates_with_ticks': dates}
    if name == 'daily':
        out['stop_review'] = db.fetch_stop_review(date)
    return out


@mcp_enabled(expose_docstring=True)
def get_action_register(view: str = 'current') -> dict:
    """The action register: focus actions raised from the weekly coaching
    notes (id A<n>). view 'current' (open, or not done and carried; oldest
    first), 'closed' (done or dropped; most recently closed first) or 'all'.
    Each action: id, raised_week, text, rules, status (open / not_done /
    done / dropped), status_note, times_not_done, closed_week, and history
    [{week, status, note}] of every weekly review. Returns {actions, counts}."""
    if view not in dict(ACTION_REGISTER_VIEWS):
        return {'error': "view must be 'current', 'closed' or 'all'"}
    actions = db.fetch_weekly_actions()
    rows = [{'id': action_label(a), 'raised_week': a['source_week'], 'text': a['text'], 'rules': a.get('rules'),
             'status': a['status'], 'status_note': a.get('status_note'), 'times_not_done': a['times_not_done'],
             'closed_week': a['closed_week'], 'history': a['history']}
            for a in action_register_rows(actions, view)]
    return {'actions': rows, 'counts': action_register_counts(actions)}


@mcp_enabled(expose_docstring=True)
def get_trade_notes(plan_id: str = None, since: str = None) -> dict:
    """The observation log (the "Notes" on a Trade Journal row): dated notes
    about one trade, newest first, each with obs_id, observed_at, event_type
    (e.g. 'entry', 'exit', 'deviation', or none), note, and psych (a readable
    psych reading, or None if not rated). With plan_id: that plan's notes plus
    its symbol, direction, status and account. Without plan_id: every plan's
    notes, grouped by plan (plans with no notes left out); since (optional,
    'YYYY-MM-DD') keeps notes on or after that date. General notes about the
    trading day are in get_daily_journal instead."""
    try:
        start = _date_or_none(since)
    except (ValueError, TypeError):
        return {'error': "since must be a date like 'YYYY-MM-DD'"}
    plans = db.fetch_plans()
    if plan_id:
        plans = [p for p in plans if p['plan_id'] == plan_id]
        if not plans:
            return {'error': f"No plan found with id '{plan_id}'"}
    out = []
    for p in plans:
        notes = []
        for o in db.fetch_plan_observations(p['plan_id']):
            day = _date_or_none(o.get('observed_at'))
            if start and day and day < start:
                continue
            notes.append({'obs_id': o['obs_id'], 'observed_at': o['observed_at'],
                          'event_type': o.get('event_type'), 'note': o['note'],
                          'psych': psych_summary_text(o)})
        if notes or plan_id:
            out.append({'plan_id': p['plan_id'], 'symbol': p['symbol'], 'direction': p['direction'],
                        'status': p.get('status'), 'account_id': p.get('account_id'), 'notes': notes})
    out.sort(key=lambda g: g['notes'][0]['observed_at'] if g['notes'] else '', reverse=True)
    return out[0] if plan_id else {'plans': out}


@mcp_enabled(expose_docstring=True)
def list_plan_screenshots(account_id: str = None) -> list:
    """Every plan that has a chart screenshot saved with it, newest plan
    first: plan_id, symbol, direction, status, planned_date, account_id and
    screenshot_file (absolute path). Open screenshot_file with a
    file-reading tool (e.g. Claude Code's Read, which displays images) to
    see the chart. account_id (optional) limits it to one account."""
    out = []
    for p in _account_filter(db.fetch_plans(), account_id):
        path = screenshot_file(p.get('screenshot_path'))
        if path:
            out.append({'plan_id': p['plan_id'], 'symbol': p['symbol'], 'direction': p['direction'],
                        'status': p.get('status'), 'planned_date': p.get('planned_date'),
                        'account_id': p.get('account_id'), 'screenshot_file': path})
    return sorted(out, key=lambda r: r['planned_date'] or '', reverse=True)


@mcp_enabled(expose_docstring=True)
def get_journal(account_id: str = None) -> list:
    """The full trade journal: every committed plan merged with its matched
    execution or open position (if any) and computed scores -- actual P&L,
    R-multiple, Plan Score, pre-trade risk/reward, target-ladder progress.
    Status is one of 'Closed', 'Open (in progress)', or 'Planned (no
    execution matched yet)'. Reads fresh from the database. account_id (optional) limits the result to one account; omit for all accounts (see list_accounts)."""
    return _account_filter(_journal_from_db(), account_id)


@mcp_enabled(expose_docstring=True)
def get_regime_adherence(since: str = None, until: str = None, account_id: str = None,
                         breaches_only: bool = False) -> dict:
    """Every day a trade was bought (an entry, a campaign's probe or an add),
    checked against the effective regime in force at that day's open: the
    newest daily or weekly entry saved before 09:30 ET that day (usually the
    previous evening's), as it stood then. Each item gives the limits and
    tactics in force, the risk to the stop plus the gap allowance and the
    position as a % of the latest weekly balance (CAD), their caps, and any
    breaches. Sells never break a regime rule, so they aren't listed. since /
    until: ISO dates (inclusive). Returns {days, checked_trades, trades_with_breaches}."""
    rows = [r for r in _account_filter(_journal_from_db(), account_id) if isinstance(r.get('regime_check'), dict)]
    days = []
    for r in rows:
        for d in r['regime_check'].get('days', []):
            if (since and d['date'] < since) or (until and d['date'] > until):
                continue
            if breaches_only and not d['breaches']:
                continue
            gov = d['governing']
            days.append({'date': d['date'], 'plan_id': r['plan_id'], 'symbol': r['symbol'],
                         'account_id': _scalar(r.get('account_id')), 'strategy': plan_type_of(r),
                         'status': r['status'], 'bought': d['stages'], 'shares': d['shares'],
                         'limits_at_open': gov['limits_state'], 'tactics_at_open': gov['tactics_state'],
                         'regime_from': f"{gov['source']} entry {gov['as_of']} (saved {gov['saved_at']} ET)",
                         'risk_pct': d['risk_pct'], 'risk_cap_pct': d['cap_pct'],
                         'position_pct': d['position_pct'], 'max_position_pct': d['max_pos_pct'],
                         'breaches': d['breaches'], 'notes': d['notes']})
    checked = [r for r in rows if r['regime_check'].get('checked')]
    return {'days': sorted(days, key=lambda x: (x['date'], x['symbol'])), 'checked_trades': len(checked),
            'trades_with_breaches': sum(1 for r in checked if r['regime_check']['breaches'])}


@mcp_enabled(expose_docstring=True)
def get_improvement_register(view: str = 'all') -> dict:
    """The Improvement register: every "what to change" lesson from a trade (its
    plan's lessons_learned) or a daily journal entry, with its "what worked
    (keep)" half, its type (practice | strategy | tapes | observation), its
    status (new | adopted | verified | dropped), what it became (became: a
    list of {kind: practice | strategy | action | tapes, ref}, with
    became_text and, for practices/actions/Strategy versions, evidence: the
    practice's results since adoption, the action's status), a note and the
    date adopted. key = 'trade|<plan_id>' or 'day|<date>'. view: a
    status or 'all'. Also returns counts per status and the encouragement
    figures (lesson streak, followed-the-plan trades, changes adopted)."""
    if view not in ('all',) + tuple(v for v, _ in IMPROVEMENT_STATUSES):
        return {'error': "view must be 'all', 'new', 'adopted', 'verified' or 'dropped'"}
    plans, entries = db.fetch_plans(), db.fetch_daily_journal_entries()
    rows = improvement_rows(plans, entries, db.fetch_improvements())
    journal = _journal_from_db()
    summary = encouragement_summary(journal, practice_items(journal, db.fetch_stop_reviews()), entries, rows)
    items = practice_items(journal, db.fetch_stop_reviews())
    actions, versions = db.fetch_weekly_actions(), strategy_versions()
    lessons = [dict(r, became_text=became_text(r['became']),
                    evidence={became_label(b): became_evidence(b, items, actions, versions, r['adopted_on'])
                              for b in r['became'] if b['kind'] != 'tapes'})
               for r in rows if view == 'all' or r['status'] == view]
    return {'lessons': lessons, 'counts': improvement_counts(rows), 'encouragement': summary}


@mcp_enabled(expose_docstring=True)
def set_improvement(key: str, category: str = None, status: str = None, became: list = None,
                    note: str = None) -> dict:
    """Classify or follow through one lesson in the Improvement register. key
    is 'trade|<plan_id>' or 'day|<YYYY-MM-DD>' (from get_improvement_register).
    Omitted fields keep their current value. category: practice | strategy |
    tapes | observation. status: new | adopted | verified | dropped (adopted_on
    is set the first time it's adopted). became: what it turned into, a list of
    'practice:<practice id>', 'strategy:<version>' (from Strategy.md's change
    log), 'action:<n>' or 'A<n>' (a focus action), 'tapes:<feature text>';
    replaces the current list ([] clears it). An adopted or verified lesson
    must have at least one. The user's open browser tab doesn't update; tell
    them to refresh."""
    source, _, ref = (key or '').partition('|')
    rows = {r['key']: r for r in improvement_rows(db.fetch_plans(), db.fetch_daily_journal_entries(),
                                                  db.fetch_improvements())}
    if key not in rows:
        return {'error': f"No lesson with key {key!r}: use get_improvement_register for the keys."}
    if category not in (None, '') and category not in dict(IMPROVEMENT_CATEGORIES):
        return {'error': f"category must be one of {', '.join(dict(IMPROVEMENT_CATEGORIES))}"}
    if status not in (None, '') and status not in dict(IMPROVEMENT_STATUSES):
        return {'error': f"status must be one of {', '.join(dict(IMPROVEMENT_STATUSES))}"}
    cur = rows[key]
    links = cur['became']
    if became is not None:
        links = []
        versions = {v['version'] for v in strategy_versions()}
        action_ids = {str(a['action_id']) for a in db.fetch_weekly_actions()}
        for item in became:
            kind, _, lref = str(item).partition(':')
            if kind not in ('practice', 'strategy', 'action', 'tapes') and str(item)[:1] in ('A', 'a'):
                kind, lref = 'action', str(item)
            lref = lref.strip()
            if kind not in ('practice', 'strategy', 'action', 'tapes') or not lref:
                return {'error': f"Can't read {item!r}: use practice:<id>, strategy:<version>, A<n> or tapes:<text>"}
            if kind == 'practice' and lref not in PRACTICE_BY_ID:
                return {'error': f"Unknown practice {lref!r}; one of {', '.join(PRACTICE_BY_ID)}"}
            if kind == 'strategy':
                lref = lref.lstrip('vV')
                if lref not in versions:
                    return {'error': f"Strategy version {lref!r} isn't in the change log "
                                     f"({', '.join(sorted(versions))})"}
            if kind == 'action':
                lref = _action_ref(lref)
                if lref not in action_ids:
                    return {'error': f"No focus action A{lref} in the action register"}
            links.append({'kind': kind, 'ref': lref})
    new_status = status or cur['status']
    if new_status in ('adopted', 'verified') and not links:
        return {'error': "An adopted or verified lesson needs `became`: what it turned into."}
    return db.save_improvement(source, ref, category=category if category is not None else cur['category'],
                               status=new_status, became=links,
                               note=note if note is not None else cur['note'])


@mcp_enabled(expose_docstring=True)
def get_practice_scorecard(today: str = None, account_id: str = None) -> dict:
    """The Practices scorecard: how the Strategy is executed, measured from the
    Wealthsimple fills. Practices: trade within the regime at the open (per
    buy-day), regime set before the open (per trading day with a buy), plan
    committed before the trade (per trade). Each has done/total this week
    (Monday to `today`), over the last 4 weeks and since its start date, plus
    its exceptions (date, symbols, detail), and practices_vs_results compares
    closed trades that followed every practice with those that broke one.
    today: ISO date (default today)."""
    rows = _account_filter(_journal_from_db(), account_id)
    items = practice_items(rows, db.fetch_stop_reviews())
    card = practice_scorecard(items, today)
    return {'practices': [{k: p[k] for k in ('id', 'name', 'unit', 'start', 'help', 'week', 'recent', 'all',
                                              'exceptions', 'unknown')} for p in card],
            'practices_vs_results': practices_vs_results(rows, items)}


@mcp_enabled(expose_docstring=True)
def get_summary_stats(account_id: str = None, plan_type: str = None) -> dict:
    """Aggregate performance stats across closed trades: win rate, total
    P&L, average/median Plan Score, best/worst-executed symbol, average %
    of planned targets hit. Reads fresh from the database.
    Use pnl_by_currency ({currency: realized P&L}) for P&L: each trade closed
    at its own day's exchange rate, so it's deliberately not converted.
    total_actual_pnl is the old sum across currencies as-is, kept for
    compatibility -- approximate whenever trades span USD and CAD. R-based
    figures (expectancy etc.) need no conversion. account_id (optional) limits
    the result to one account; omit for all accounts (see list_accounts).
    Without account_id, the result also carries by_account: {account_id: the
    same stats for that account alone}, matching the page's All-accounts view.
    plan_type (optional): 'scale_out' or 'scale_in' limits the stats to that
    strategy's trades. A scale-in campaign's R is measured against the peak
    risk it actually took, and it has no Plan Score (no targets)."""
    if plan_type not in (None, '', 'all', 'scale_out', 'scale_in'):
        return {'error': "plan_type must be 'scale_out', 'scale_in' or omitted"}
    journal = _strategy_filter(_journal_from_db(), plan_type)
    if account_id:
        return _compute_summary_stats(_account_filter(journal, account_id))
    stats = _compute_summary_stats(journal)
    stats['by_account'] = summary_stats_by_account(journal, db.fetch_accounts())
    return stats


@mcp_enabled(expose_docstring=True)
def list_open_positions(account_id: str = None) -> list:
    """List currently-open (not yet closed) positions, each tagged with
    plan_status: 'planned' (a draft or committed plan exists for the same
    account/symbol/currency), 'cash_equivalent' (exempted via
    add_cash_equivalent_symbol, e.g. a BIL parking position), or 'no_plan'
    (live risk with no documented plan yet). account_id (optional) limits the result to one account; omit for all accounts (see list_accounts)."""
    executions = db.fetch_executions()
    plans = db.fetch_plans()
    _, op_df = build_round_trips(executions, plans)
    if op_df.empty:
        return []
    buy_plans = [p for p in plans if p.get('direction') == 'BUY']
    cash_equiv = set(db.fetch_cash_equivalent_symbols())
    positions = op_df.copy()
    positions['since'] = positions['since'].astype(str)
    positions['entry_time'] = positions['entry_time'].astype(str)
    records = _account_filter(positions.to_dict('records'), account_id)
    for r in records:
        if r['symbol'] in cash_equiv:
            r['plan_status'] = 'cash_equivalent'
        elif any(plan_fits_position(p, r.get('account_id'), r['symbol'], r.get('currency'))
                 for p in buy_plans):
            r['plan_status'] = 'planned'
        else:
            r['plan_status'] = 'no_plan'
    return records


@mcp_enabled(expose_docstring=True)
def list_unmatched_round_trips(account_id: str = None) -> list:
    """List closed round-trip trades that have no matching plan (excluding
    cash-equivalent symbols, e.g. BIL, which never need one, and trades the
    user has individually dismissed from the list) -- the same set shown in
    the "Executed Trades Without a Plan" card. Trades closed before their
    account's tracking start date (see list_accounts) are archived and left
    out. account_id (optional) limits the result to one account; omit for all accounts (see list_accounts)."""
    executions = db.fetch_executions()
    plans = db.fetch_plans()
    rt_df, op_df = build_round_trips(executions, plans)
    _reconcile_closed_lot_matches(op_df, rt_df)
    plan_matches = db.fetch_plan_matches()
    plan_lot_matches = db.fetch_plan_lot_matches()
    # Reuses the exact same rt_id-based computation the UI card uses (not a
    # symbol/direction re-derivation, which could wrongly hide a genuinely
    # unmatched round trip if a *different* round trip of the same symbol
    # happened to be the one that matched -- see 2026-08-27's IBIT fix).
    _, unmatched = _build_journal_and_unmatched(plans, rt_df, op_df, plan_matches, plan_lot_matches)
    cash_equiv = set(db.fetch_cash_equivalent_symbols())
    dismissed = set(db.fetch_dismissed_round_trips())
    accounts = db.fetch_accounts()
    return [r for r in _account_filter(unmatched, account_id)
            if r['symbol'] not in cash_equiv and r['rt_id'] not in dismissed
            and not is_archived(r, accounts)]


@mcp_enabled(expose_docstring=True)
def list_cash_equivalent_symbols() -> list:
    """List symbols exempted from needing a plan (cash-equivalent parking
    positions like BIL)."""
    return db.fetch_cash_equivalent_symbols()


@mcp_enabled(expose_docstring=True)
def add_cash_equivalent_symbol(symbol: str) -> dict:
    """Mark a symbol as a cash-equivalent parking position (e.g. BIL) that
    never needs a trade plan. Has no effect on scoring either way -- this
    only quiets the "No plan" prompt in Open Positions and removes the
    symbol from "Executed Trades Without a Plan"."""
    db.add_cash_equivalent_symbol(symbol)
    return {'symbols': db.fetch_cash_equivalent_symbols()}


@mcp_enabled(expose_docstring=True)
def remove_cash_equivalent_symbol(symbol: str) -> dict:
    """Un-exempt a symbol previously marked cash-equivalent."""
    removed = db.remove_cash_equivalent_symbol(symbol)
    return {'removed': removed, 'symbols': db.fetch_cash_equivalent_symbols()}


@mcp_enabled(expose_docstring=True)
def create_plan(symbol: str, direction: str, entry_price: float, qty: float,
                 stop_loss: float = None, targets: list = None, reason: str = "",
                 notes: str = "", status: str = "draft", target_method: str = None,
                 atr_at_commit: float = None, atr_multiplier: float = None, atr_timeframe: str = '1D',
                 currency: str = None, planned_date: str = None,
                 psych_risk: int = None, psych_urgency: int = None, psych_focus: int = None,
                 trade_triggers: list = None, stop_method: str = 'ladder',
                 trailing_activate_after_target: int = 1, trailing_atr_multiplier: float = 2.0,
                 account_id: str = None, plan_type: str = 'scale_out', adds: list = None,
                 trail_start_add: int = 0, day_catalyst: str = None, day_setup: str = None) -> dict:
    """Create a new trade plan. direction is 'BUY' or 'SELL'. targets is a list
    of up to 5 {price, qty_pct, stop_after} dicts (qty_pct is % of the
    ORIGINAL quantity, not of whatever remains -- see the Add Plan form's own
    help text). status defaults to 'draft' (excluded from the journal/scoring
    until separately committed via commit_plan) rather than 'committed', so a
    plan created this way gets a review step before it counts toward
    performance stats -- pass status='committed' to skip that. currency is
    'USD' or 'CAD' -- every price on this plan (entry/stop/targets) is
    denominated in it; if omitted, TAPEs guesses from the symbol's existing
    open position or trade history (same heuristic the Add Plan form's
    Currency picker uses), falling back to 'USD' if the symbol has never been
    traded before. Optional entry psych reading, each -2..+2: psych_risk
    (Fearful..Overconfident, 0=Calm ideal), psych_urgency (Complacent..FOMO,
    0=Patient ideal), psych_focus (Fatigued..Sharp, higher is better) -- omit
    all three to leave the plan unrated. trade_triggers is a list of any of
    'planned_setup', 'chased_breakout', 'revenge', 'boredom', 'news_tip'.
    stop_method is 'ladder' (default; each target's stop_after only),
    'atr_trail' (highest daily high - trailing_atr_multiplier x ATR(14)) or
    'ema_trail' (EMA(21) - 0.25 x ATR(14) while EMA(21) is above entry);
    either trail starts once trailing_activate_after_target is hit, with the
    ladder's stop_after values in effect before that. Both trails move the
    stop to at least entry once T1 is hit and never lower it. account_id is
    the account the trade is in (see list_accounts); it may be omitted only
    when TAPEs knows exactly one account. atr_timeframe is the bars
    atr_at_commit was read from: '240' (4H), '1D' (default) or '1W'; an
    atr_trail trails by that timeframe's ATR (the EMA trail is always daily).

    plan_type 'scale_in' makes a scale-in campaign (a reversal strategy): a
    small probe (entry_price/qty/stop_loss) plus `adds`, a list of up to 5
    {price, qty, stop_before} -- each add bought once price reaches `price`,
    after the whole position's stop has been raised to `stop_before`. Rules:
    triggers move beyond the entry and each other (never averaging down);
    each stop_before is beyond the previous stop and short of its trigger;
    stop_method must be 'atr_trail' or 'ema_trail' (trailing exit only); no
    targets. trail_start_add: 0 = the trail runs from the probe, k = after add
    k. Omit `adds` to get the default ladder: 3 equal adds (qty each = probe
    qty) at 1R/2R/3R, each stop at the previous level. One campaign per
    account + symbol at a time.

    plan_type 'day_trade' makes a day trade (Strategy 4.5): a news-catalyst
    trade opened and closed in one session. day_catalyst (what happened, when,
    source) and day_setup ('panic_fade', 'gap_and_go' or 'vwap_pullback') are
    required; atr_timeframe is '5' or '15' (defaults to '5'); stop_method
    'ladder'; targets as for scale-out (T1 1R 50%, T2 2R 50%), and T1's
    stop_after defaults to the entry. Not allowed on a symbol already held in
    that account."""
    if plan_type == 'day_trade' and atr_timeframe == DEFAULT_ATR_TIMEFRAME:
        atr_timeframe = DAY_TRADE_DEFAULT_TIMEFRAME
    if atr_timeframe not in ATR_TIMEFRAME_LABELS:
        return {'error': f"atr_timeframe must be one of {list(ATR_TIMEFRAME_LABELS)}"}
    account_ids = [a['account_id'] for a in db.fetch_accounts()]
    if not account_id:
        if len(account_ids) != 1:
            return {'error': f"account_id is required when there is more than one account: {account_ids}"}
        account_id = account_ids[0]
    elif account_id not in account_ids:
        return {'error': f"unknown account_id '{account_id}'; known: {account_ids}"}
    if direction not in ('BUY', 'SELL'):
        return {'error': "direction must be 'BUY' or 'SELL'"}
    if plan_type not in dict(PLAN_TYPES):
        return {'error': f"plan_type must be one of {list(dict(PLAN_TYPES))}"}
    campaign = plan_type == 'scale_in'
    if campaign:
        if stop_method == 'ladder':
            stop_method = 'atr_trail'
        if adds is None:
            adds = default_campaign_adds(direction, entry_price, stop_loss, qty)
        error = validate_campaign(direction, entry_price, stop_loss, adds, stop_method)
        if error:
            return {'error': error}
        targets = []
    day = plan_type == 'day_trade'
    if day:
        targets = day_trade_targets(direction, entry_price, targets)
        error = (validate_day_trade(direction, entry_price, stop_loss, targets, stop_method, atr_timeframe,
                                    day_catalyst, day_setup) or _validate_target_qty_pct(targets))
        if error:
            return {'error': error}
    elif validate_swing_timeframe(atr_timeframe):
        return {'error': validate_swing_timeframe(atr_timeframe)}
    if stop_method not in dict(STOP_METHODS):
        return {'error': f"stop_method must be one of {list(dict(STOP_METHODS))}"}
    unknown = [t for t in (trade_triggers or []) if t not in TRADE_TRIGGER_LABELS]
    if unknown:
        return {'error': f"unknown trade_triggers {unknown}; valid: {list(TRADE_TRIGGER_LABELS)}"}
    psych_values = [psych_risk, psych_urgency, psych_focus]
    if any(v is not None and not -2 <= int(v) <= 2 for v in psych_values):
        return {'error': "psych_risk/psych_urgency/psych_focus must be between -2 and 2"}
    now = db.utc_now().isoformat()
    if not currency:
        _, op_df = build_round_trips(db.fetch_executions(), db.fetch_plans())
        currency = _guess_symbol_currency(
            symbol, op_df.to_dict('records'), db.fetch_executions().to_dict('records')) or 'USD'
    conflict = (_campaign_conflict(db.fetch_plans(), account_id, symbol, currency, campaign or day,
                                   _closed_plan_ids())
                or (_held_conflict(account_id, symbol, currency) if day else None))
    if conflict:
        return {'error': conflict}
    plan = {
        'plan_type': plan_type,
        'day_catalyst': (day_catalyst or '').strip() or None if day else None,
        'day_setup': day_setup if day else None,
        'adds': adds if campaign else [],
        'trail_start_add': int(trail_start_add or 0) if campaign else None,
        'account_id': account_id,
        'plan_id': str(uuid.uuid4())[:8],
        'symbol': symbol.strip().upper(),
        'direction': direction,
        'planned_date': planned_date or now[:10],
        'created_at': now,
        'entry_price': float(entry_price),
        'stop_loss': float(stop_loss) if stop_loss not in (None, '') else None,
        'targets': targets or [],
        'qty': float(qty),
        'reason': reason or '',
        'notes': notes or '',
        'status': status,
        'committed_at': now if status == 'committed' else None,
        'target_method': target_method,
        'atr_at_commit': float(atr_at_commit) if atr_at_commit not in (None, '') else None,
        'atr_multiplier': float(atr_multiplier) if atr_multiplier not in (None, '') else None,
        'atr_timeframe': atr_timeframe,
        'currency': currency,
        'trade_triggers': list(trade_triggers or []),
        'stop_method': stop_method,
        'trailing_activate_after_target': int(trailing_activate_after_target or 1),
        'trailing_atr_multiplier': float(trailing_atr_multiplier or 2.0),
    }
    if any(v is not None for v in psych_values):
        plan.update(_psych_fields(psych_values))
    db.insert_plan(plan)
    return get_plan(plan['plan_id'])


@mcp_enabled(expose_docstring=True)
def commit_plan(plan_id: str) -> dict:
    """Commit a draft plan, making it eligible for the journal, scoring, and
    execution matching. No-op error if the plan doesn't exist."""
    now = db.utc_now().isoformat()
    if not db.update_plan(plan_id, {'status': 'committed', 'committed_at': now}):
        return {'error': f"No plan found with id '{plan_id}'"}
    return get_plan(plan_id)


@mcp_enabled(expose_docstring=True)
def delete_plan(plan_id: str) -> dict:
    """Permanently delete a trade plan (draft or committed). Cannot be undone."""
    return {'deleted': db.delete_plan(plan_id)}


@mcp_enabled(expose_docstring=True)
def fetch_current_atr(symbol: str, currency: str = None, timeframe: str = '1D') -> dict:
    """Fetch a fresh ATR(14) reading for a symbol: tries live TradingView
    Desktop first (correcting the chart's symbol/timeframe if needed), falls
    back to a Yahoo Finance proxy if TradingView Desktop isn't reachable.
    currency_guess is a best-effort guess (from the symbol's open position or
    trade history, same heuristic the Add Plan form's Currency picker uses) --
    None if the symbol has never been traded before; ATR itself is denominated
    in whatever currency the underlying instrument trades in. Pass currency
    ('USD' or 'CAD') to pick the listing explicitly -- a ticker listed in both
    the US and Canada otherwise resolves to the guess, or the US listing.
    timeframe is the bars to read: '240' (4H), '1D' (default) or '1W'."""
    if timeframe not in ATR_TIMEFRAME_LABELS:
        return {'error': f"timeframe must be one of {list(ATR_TIMEFRAME_LABELS)}"}
    _, op_df = build_round_trips(db.fetch_executions(), db.fetch_plans())
    currency_guess = _guess_symbol_currency(
        symbol, op_df.to_dict('records'), db.fetch_executions().to_dict('records'))
    try:
        atr_value, source_note = fetch_atr(symbol.strip().upper(), currency=currency or currency_guess,
                                           timeframe=timeframe)
    except Exception as e:
        return {'error': str(e)}
    return {'symbol': symbol.strip().upper(), 'atr': round(atr_value, 4), 'timeframe': timeframe,
            'source': source_note,
            'currency_guess': currency_guess}


@mcp_enabled(expose_docstring=True)
def get_beta_weighted_exposure(force_refresh: bool = False, account_id: str = None) -> dict:
    """Beta-weighted net market exposure across open positions: sum of
    (position market value * beta vs. the benchmark), both as a raw dollar
    figure and as a % of the same mark-to-market portfolio value the
    portfolio-heat/risk figures use. A different question from risk-to-stop
    -- this measures whether positions that look diversified by ticker are
    actually one large correlated bet on the same market direction (e.g.
    several leveraged or single-theme index/sector ETFs that move almost
    identically in a risk-off move). Cash-equivalent symbols are excluded.
    Beta itself is computed from realized daily price history (covariance/
    variance vs. SPY over the trailing 60 trading days by default), never a
    data provider's static precomputed beta, and is cached for up to 24h --
    pass force_refresh=True to bypass the cache and recompute. Reads fresh
    from the database, including a fresh price fetch per open position (this
    can take a few seconds for a book with several positions).
    total_exposure_dollars/portfolio_value/per_symbol market_value are all
    summed across symbols as-is (USD/CAD mixed together, not FX-converted) --
    treat them as approximate if you hold positions in more than one currency.
    UPDATE 2026-10-08: when a Bank of Canada rate is available for every
    currency held, total_exposure_dollars, portfolio_value and per_account are
    in CAD (`currency` says which), so the % is right across USD and CAD;
    portfolio_value_by_currency gives the unconverted split.
    per_symbol is keyed 'SYMBOL|CURRENCY'; per_account is
    {account_id: beta-weighted exposure dollars} for that account. account_id
    (optional) computes everything for that one account only."""
    executions = db.fetch_executions()
    _, op_df = build_round_trips(executions, db.fetch_plans())
    open_positions = _account_filter(op_df.to_dict('records') if not op_df.empty else [], account_id)
    total, priced_symbols, price_errors = compute_mark_to_market_balance(open_positions)
    accounts = _account_filter(db.fetch_accounts(), account_id)
    pw = portfolio_with_cash(open_positions, stage_prices(open_positions, total, priced_symbols, price_errors),
                             db.fetch_latest_account_snapshots(),
                             executions.assign(timestamp=executions['timestamp'].astype(str)).to_dict('records')
                             if not executions.empty else [], accounts)
    portfolio_value, fx, by_currency = pw['value'], pw['fx'], pw['positions']
    cash_equiv = db.fetch_cash_equivalent_symbols()
    (total_exposure, total_exposure_pct, per_symbol, beta_errors, betas_as_of,
     per_account) = compute_portfolio_beta_exposure(
        open_positions, priced_symbols, cash_equiv, portfolio_value, force_refresh=force_refresh, fx=fx)
    return {
        'total_exposure_dollars': round(total_exposure, 2),
        'total_exposure_pct_of_portfolio_value': (
            round(total_exposure_pct, 2) if total_exposure_pct is not None else None),
        'portfolio_value': round(portfolio_value, 2),
        'currency': BASE_CURRENCY if fx else 'mixed (not FX-converted)',
        'portfolio_value_by_currency': {c: round(v, 2) for c, v in by_currency.items()},
        'cash_included_by_currency': {c: round(v, 2) for c, v in pw['cash']['by_currency'].items()},
        'fx_rates_used': fmt_fx_note(fx) or None,
        'benchmark': BETA_BENCHMARK_SYMBOL,
        'lookback_days': BETA_LOOKBACK_DAYS,
        'betas_as_of': betas_as_of,
        'per_symbol': {
            sym: {'beta': round(v['beta'], 3), 'market_value': round(v['market_value'], 2)}
            for sym, v in per_symbol.items()
        },
        'per_account': per_account,
        'price_errors': price_errors,
        'beta_errors': beta_errors,
    }


@mcp_enabled(name='get_fx_rates', expose_docstring=True)
def get_fx_rates_tool(force_refresh: bool = False) -> dict:
    """The exchange rates TAPEs uses to combine currencies: CAD per 1 unit of
    each foreign currency (the Bank of Canada daily average rate), with the
    date each was published. Cached for up to 12 hours; force_refresh=True
    asks the Bank again. errors names any currency that couldn't be refreshed
    (an older cached rate is used) or has no rate at all."""
    return get_fx_rates(force_refresh=force_refresh)


@mcp_enabled(expose_docstring=True)
def get_plan_drift_report(account_id: str = None) -> list:
    """For every open lot with a committed, matched plan: compare the plan
    against LIVE PRICE (not just what's been executed so far) to catch the
    case where price has already reached a target -- or even broken the
    stop -- before the corresponding Wealthsimple order has been placed or
    filled (Wealthsimple has no API, so fills only reach TAPEs via manual
    CSV re-import and can lag behind real price action). Meant to be run
    each morning before market open. Cash-equivalent symbols and lots with
    no committed plan matched are skipped (nothing to compare against).
    Reads fresh from the database, including a fresh price fetch per open
    lot (this can take a few seconds for a book with several positions).

    Each report entry: lot_id, plan_id, symbol, currency (denominates every
    price field in this entry), current_price, atr_14d (daily ATR(14) from
    completed bars, whatever timeframe the plan uses; None with atr_14d_error
    if the fetch failed), actual_stop
    (the real, execution-based ratcheted stop), should_be_stop (actual_stop
    further ratcheted through any not-yet-executed target live price has
    already reached), next_target_price/next_target_qty_pct (the next
    not-yet-executed target, or None if every target is done or there are
    none), next_target_reached (live price has already reached it),
    stop_breached_live (live price is now past should_be_stop -- the single
    most actionable flag here), should_be_qty_open/actual_qty_open/qty_drift
    (positive qty_drift means you're holding more than the plan says you
    should be by now). should_be_stop is the stop to set for the next session;
    stop_action is 'raise' when it improves on actual_stop, else 'hold'.
    stop_method is 'ladder', 'atr_trail' or 'ema_trail'; trailing_active is
    True once a trailing plan's activation target is hit or live-reached and a
    trailing level exists (computed from completed daily bars only -- see
    compute_trailing_series). A price-fetch failure for one symbol is reported
    as {lot_id, symbol, error} instead of dropping that lot silently;
    trailing_stop_error means the trailing history couldn't be fetched and the
    stop shown is ladder + entry floor only. Each entry also carries
    account_id and plan_type. A scale-in campaign (plan_type 'scale_in') has
    no targets; instead adds_done/adds_planned and the next add's
    next_add_price/next_add_qty/next_add_stop_before, with next_add_reached
    True when live price has hit its trigger -- then the action is: raise
    the stop to next_add_stop_before (should_be_stop includes it), then buy
    next_add_qty. A negative qty_drift on a campaign is shares still to buy. account_id (optional) limits the result to one account; omit for all accounts (see list_accounts).
    If the user ran today's stop review (a screenshot of their Wealthsimple
    pending orders, checked in the app), each lot also carries placed_stop,
    placed_limit (a stop limit's limit; its stop is None when only the limit
    was shown), placed_qty, placed_stop_ok and placed_stop_issues (e.g. "Raise
    the stop by 1.62 to 43.59.")."""
    executions = db.fetch_executions()
    plans = db.fetch_plans()
    rt_df, op_df = build_round_trips(executions, plans)
    if op_df.empty:
        return []
    _reconcile_closed_lot_matches(op_df, rt_df)
    plan_matches = db.fetch_plan_matches()
    plan_lot_matches = db.fetch_plan_lot_matches()
    journal_rows, _ = _build_journal_and_unmatched(plans, rt_df, op_df, plan_matches, plan_lot_matches)

    op_by_lot = {row['lot_id']: row for row in op_df.to_dict('records')}
    cash_equiv = set(db.fetch_cash_equivalent_symbols())

    report = []
    atr_cache = {}
    for row in journal_rows:
        lot_id = row.get('lot_id')
        if not lot_id or lot_id not in op_by_lot:
            continue  # not matched to a still-open lot -- nothing live to check against
        pos = op_by_lot[lot_id]
        symbol = pos['symbol']
        if symbol in cash_equiv or not _in_view(pos, account_id):
            continue
        try:
            current_price, _ticker = fetch_current_price(symbol, _scalar(pos.get('currency')))
        except Exception as e:
            report.append({'lot_id': lot_id, 'account_id': _scalar(pos.get('account_id')),
                           'symbol': symbol, 'error': str(e)})
            continue
        trailing, trailing_error = _build_trailing_for_row(row, pos)
        drift = _drift_for_row(row, pos, current_price, trailing)
        atr_14d, atr_error = drift_daily_atr(pos, atr_cache)
        next_target = drift['next_target']
        entry = {
            'lot_id': lot_id,
            'plan_id': row['plan_id'],
            'account_id': _scalar(pos.get('account_id')),
            'symbol': symbol,
            'currency': pos.get('currency'),
            'stop_method': stop_method_of(row),
            'atr_timeframe': atr_timeframe_of(row),
            'current_price': round(current_price, 2),
            'atr_14d': round(atr_14d, 2) if atr_14d is not None else None,
            'actual_stop': round(drift['actual_stop'], 2) if drift['actual_stop'] is not None else None,
            'should_be_stop': round(drift['should_be_stop'], 2) if drift['should_be_stop'] is not None else None,
            'stop_action': _stop_action(row['direction'], drift['actual_stop'], drift['should_be_stop']),
            'next_target_price': round(next_target['price'], 2) if next_target else None,
            'next_target_qty_pct': next_target['qty_pct'] if next_target else None,
            'next_target_reached': drift['next_target_reached'],
            'stop_breached_live': drift['stop_breached_live'],
            'should_be_qty_open': round(drift['should_be_qty_open'], 4),
            'actual_qty_open': round(drift['actual_qty_open'], 4),
            'qty_drift': round(drift['qty_drift'], 4),
            'trailing_active': drift['trailing_active'],
            'plan_type': plan_type_of(row),
            'day_trade_overdue': bool(drift.get('day_trade_overdue')),
        }
        if drift.get('plan_type') == 'scale_in':
            nxt = drift['next_add'] or {}
            entry.update({
                'adds_done': drift['adds_done'], 'adds_planned': drift['adds_planned'],
                'next_add_price': nxt.get('price'), 'next_add_qty': nxt.get('qty'),
                'next_add_stop_before': nxt.get('stop_before'), 'next_add_reached': drift['next_add_reached'],
            })
        if trailing_error:
            entry['trailing_stop_error'] = trailing_error
        if atr_error:
            entry['atr_14d_error'] = atr_error
        report.append(entry)
    placed = db.fetch_daily_placed_orders(dt.date.today().isoformat())
    if placed:
        checks = compare_placed_stops(placed, report, op_df.to_dict('records'), cash_equiv, account_id)
        for e in report:
            c = next((c for c in checks if e.get('lot_id') in c['lot_ids']), None)
            if c:
                e['placed_stop'] = None if c['stop_is_bound'] else c['order_stop']
                e['placed_limit'] = c['order_limit']
                e['placed_qty'] = c['order_qty']
                e['placed_stop_ok'] = c['ok']
                e['placed_stop_issues'] = [t for _, t in c['issues']]
    return report


# ==============================================================
# 7b. Weekly Review: mark-to-market balance + user note + copy/paste
# coaching-chat prompt
#
# Deliberately NOT a direct LLM API call -- the user runs one dedicated,
# ongoing chat thread (in claude.ai or wherever) for this, pasting each
# week's generated prompt into it and pasting the response back here. This
# avoids a new API key/cost/dependency entirely, and the persistent thread
# gives the model its own conversational memory of past weeks' discussions
# as a bonus on top of (not instead of) the precomputed trend aggregates
# below, which stay the reliable, non-fuzzy source for pattern-spotting.
# WEEKLY_TREND_WEEKS and WEEKLY_COACHING_SYSTEM_PROMPT live up near the top
# constants (not here) since serve_layout's Container is built -- and its
# system-prompt Textarea/Clipboard read WEEKLY_COACHING_SYSTEM_PROMPT
# directly -- before this section of the file is reached.
# ==============================================================


def _observations_by_plan(observations=None):
    """Group db.fetch_all_observations() by plan_id -- shared by
    _this_week_journal_rows (attaching each trade's own observation log) and
    compute_weekly_trend_context (joining deviation-tagged psych readings to
    trade outcomes), so neither has to query per-plan."""
    observations = observations if observations is not None else db.fetch_all_observations()
    by_plan = {}
    for o in observations:
        by_plan.setdefault(o['plan_id'], []).append(o)
    return by_plan


def _this_week_journal_rows(week_ending_date):
    """Closed journal rows whose exit fell in the 7 days ending on
    week_ending_date (inclusive) -- the "this week" half of the weekly LLM
    prompt (see compute_weekly_trend_context for the "trailing trends" half).

    Each row's `observations` field carries that plan's full dated log --
    including the deviation-tagged entry from the auto-detected "exited
    contrary to plan" prompt, with its free-text note and psych reading.
    Without this, the coach only ever saw the entry-time psych fields and the
    auto-generated exec_notes, never the actual account of WHY a trade
    deviated -- confirmed missing in practice (2026-08-29): an IBIT deviation
    observation's comment and non-zero psych score never appeared in any
    week's prompt because this function never queried plan_observations at all."""
    journal_rows = _journal_from_db()
    df = pd.DataFrame(journal_rows)
    if df.empty or 'status' not in df.columns:
        return []
    closed = df[df['status'] == 'Closed'].copy()
    if closed.empty:
        return []
    closed['exit_time'] = pd.to_datetime(closed['exit_time'], utc=True, errors='coerce')
    closed = closed.dropna(subset=['exit_time'])
    end = pd.Timestamp(week_ending_date, tz='UTC') + pd.Timedelta(days=1)
    start = end - pd.Timedelta(days=7)
    window = closed[(closed['exit_time'] >= start) & (closed['exit_time'] < end)]
    if window.empty:
        return []
    cols = ['plan_id', 'account_id', 'symbol', 'direction', 'currency', 'entry_price', 'entry_actual', 'exit_actual',
            'actual_pnl', 'r_multiple', 'score', 'stop_breached', 'stop_outcome', 'exec_notes']
    rows = window[[c for c in cols if c in window.columns]].to_dict('records')
    source = {r['plan_id']: r for r in window.to_dict('records')}

    by_plan = _observations_by_plan()
    for row in rows:
        plan_row = source[row['plan_id']]
        row['entry_psych'] = psych_summary_text(plan_row)
        row['entry_psych_distortion_index'] = psych_distortion_index(plan_row)
        row['trade_triggers'] = trade_triggers_text(plan_row.get('trade_triggers'))
        row['strategy'] = plan_type_of(plan_row)
        if is_day_trade(plan_row):
            row['day_trade'] = {'catalyst': _scalar(plan_row.get('day_catalyst')),
                                'setup': day_setup_label(plan_row.get('day_setup')),
                                'carried_overnight': _scalar(plan_row.get('carried_overnight'))}
        row['observations'] = [
            {'observed_at': o['observed_at'], 'note': o['note'], 'event_type': o.get('event_type'),
             'psych': psych_summary_text(o)}
            for o in by_plan.get(row['plan_id'], [])
        ]
    return rows


def regime_checks_for_week(journal_rows, week_ending):
    """Every buy-day in the 7 days to week_ending, from open and closed trades,
    with the regime in force at that day's open and any breaches."""
    end = pd.to_datetime(week_ending).date()
    start = (end - dt.timedelta(days=6)).isoformat()
    out = []
    for r in journal_rows or []:
        check = r.get('regime_check')
        if not isinstance(check, dict):
            continue
        for d in check.get('days', []):
            if not (start <= d['date'] <= end.isoformat()):
                continue
            gov = d['governing']
            out.append({'date': d['date'], 'symbol': r['symbol'], 'account': _scalar(r.get('account_id')),
                        'strategy': plan_type_of(r), 'bought': d['stages'],
                        'limits_at_open': gov['limits_state'], 'tactics_at_open': gov['tactics_state'],
                        'regime_from': f"{gov['source']} entry {gov['as_of']}",
                        'risk_pct': round(d['risk_pct'], 2) if d['risk_pct'] is not None else None,
                        'risk_cap_pct': d['cap_pct'],
                        'position_pct': round(d['position_pct'], 1) if d['position_pct'] is not None else None,
                        'max_position_pct': d['max_pos_pct'], 'breaches': d['breaches']})
    return sorted(out, key=lambda x: (x['date'], x['symbol']))


def lessons_for_prompt(journal_rows, week_ending, stop_reviews, entries, plans, improvements, actions=None):
    """The weekly prompt's lessons and improvement: keep/change lessons from the trades closed in
    the 7 days to week_ending, the improvement register's open and recently adopted items, and
    the encouragement figures (streaks, followed-the-plan trades, changes adopted)."""
    end = pd.to_datetime(week_ending).date()
    start = (end - dt.timedelta(days=6)).isoformat()
    trades = [{'symbol': r['symbol'], 'closed': str(r.get('exit_time'))[:10], 'r': _scalar(r.get('r_multiple')),
               'followed_plan': r.get('followed_plan'), 'keep': lesson_text(r.get('lesson_keep')),
               'change': lesson_text(r.get('lessons_learned')), 'no_lesson_yet': trade_needs_lesson(r)}
              for r in journal_rows or [] if r.get('status') == 'Closed'
              and start <= str(r.get('exit_time') or '')[:10] <= end.isoformat()]
    register = improvement_rows(plans, entries, improvements)
    items = practice_items(journal_rows, stop_reviews)
    summary = encouragement_summary(journal_rows, items, entries, register, end)
    versions = strategy_versions()
    return {'trades_closed_this_week': trades,
            'register_open': [{'date': r['date'], 'from': r['label'], 'lesson': r['change'],
                               'type': r['category']} for r in register if r['status'] == 'new'],
            'adopted_this_week': [{'date': r['date'], 'lesson': r['change'], 'became': became_text(r['became'])}
                                  for r in register if r['status'] in ('adopted', 'verified')
                                  and start <= str(r['adopted_on'] or '') <= end.isoformat()],
            'adopted_awaiting_verification': [
                {'date': r['date'], 'lesson': r['change'], 'adopted_on': r['adopted_on'],
                 'became': became_text(r['became']),
                 'evidence': {became_label(b): became_evidence(b, items, actions, versions, r['adopted_on'])
                              for b in r['became'] if b['kind'] != 'tapes'}}
                for r in register if r['status'] == 'adopted'],
            'encouragement': {k: v for k, v in summary.items() if k != 'adopted_list'}}


def _cancelled_this_week(week_ending):
    """Plans cancelled in the 7 days to week_ending: symbol, reason, note and
    what they would have done if kept (cached replay)."""
    end = pd.to_datetime(week_ending).date()
    out = []
    for r in _journal_from_db():
        if r.get('status') != 'Cancelled':
            continue
        when = pd.to_datetime(_scalar(r.get('cancelled_at')), errors='coerce')
        if pd.isna(when) or not (end - dt.timedelta(days=6) <= when.date() <= end):
            continue
        out.append({'symbol': r['symbol'], 'strategy': plan_type_of(r),
                    'account': _scalar(r.get('account_id')), 'cancelled_on': when.date().isoformat(),
                    'reason': _scalar(r.get('cancel_reason')), 'note': _scalar(r.get('cancel_note')),
                    'psych_at_cancel': _cancel_psych_text(r),
                    'if_kept': counterfactual_text(r.get('counterfactual'), r.get('currency'))})
    return out


def _daily_journal_this_week(week_ending):
    """The structured daily entries in the 7 days to week_ending, trimmed to
    what the weekly review uses: regime and effective limits, heat, the
    one-line market and lesson/proposal, and execution notes."""
    end = pd.to_datetime(week_ending).date()
    out = []
    for e in sorted(db.fetch_daily_journal_entries(), key=lambda x: x['obs_date']):
        d = _as_date(e['obs_date'])
        if not (end - dt.timedelta(days=6) <= d <= end):
            continue
        out.append({k: e.get(k) for k in ('obs_date', 'regime', 'composite', 'delta5', 'delta20', 'flags',
                                          'effective_limits', 'effective_tactics', 'heat_pct', 'positions',
                                          'execution', 'lesson_keep', 'lesson', 'market')})
    return out


WEEKLY_ENTRY_PROMPT_KEYS = ('weekly_regime', 'weekly_composite', 'daily_regime', 'daily_composite', 'delta20',
                            'flags', 'posture_limits', 'posture_tactics', 'posture_note', 'heat_pct', 'positions',
                            'beta_pct', 'largest_theme', 'largest_theme_pct', 'results', 'lesson_decisions',
                            'graduations', 'watchlist', 'strategy_changes', 'market')


def weekly_entry_for_prompt(row):
    """The saved weekly template for the prompt, or a note that it wasn't filled in."""
    if not row or not row.get('entry_saved_at'):
        return "not filled in this week"
    e = db.decode_weekly_entry(row)
    return {k: e.get(k) for k in WEEKLY_ENTRY_PROMPT_KEYS}


def _stale_plans_summary():
    return [{'symbol': r['symbol'], 'days_old': r['age_days'], 'plan_id': r['plan_id']}
            for r in _journal_from_db() if r.get('stale')]


def _cancel_psych_text(record):
    vals = {a['key']: _scalar(record.get(f"cancel_{a['key']}")) for a in PSYCH_AXES}
    if all(v is None for v in vals.values()):
        return None
    return psych_summary_text(vals)


def compute_weekly_trend_context(trailing_weeks=WEEKLY_TREND_WEEKS):
    """Precomputed trend aggregates for the weekly LLM coaching note -- NOT
    raw trade history. Feeding an LLM the whole trade history every week gets
    more expensive and less useful as history grows (the same problem as
    naive RAG); everything returned here is a small, fixed-size summary
    regardless of how many trades exist. Covers: win rate / Plan Score trend
    by week, stop-breach ("exited contrary to plan") frequency by week,
    psych-reading trend specifically on deviation-tagged exits, symbols that
    recur in deviations/losses, and the psych/trade-trigger vs. performance
    splits (see compute_psych_performance)."""
    journal_rows = _journal_from_db()
    df = pd.DataFrame(journal_rows)
    result = {'trailing_weeks': trailing_weeks, 'closed_trade_count': 0}
    if df.empty or 'status' not in df.columns:
        return result

    closed = df[df['status'] == 'Closed'].copy()
    if closed.empty:
        return result

    closed['exit_time'] = pd.to_datetime(closed['exit_time'], utc=True, errors='coerce')
    closed = closed.dropna(subset=['exit_time'])
    cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(weeks=trailing_weeks)
    recent = closed[closed['exit_time'] >= cutoff].copy()
    result['closed_trade_count'] = len(recent)
    if recent.empty:
        return result

    recent['week'] = recent['exit_time'].dt.tz_localize(None).dt.to_period('W-SUN').apply(
        lambda p: p.start_time.date().isoformat())
    recent['is_win'] = recent['actual_pnl'] > 0
    # stop_breached is True only for a breach beyond slippage tolerance (see
    # classify_stop_exits); a within-tolerance stop-out is counted separately
    # so it stays visible without inflating the breach rate.
    recent['is_slippage'] = (recent['stop_outcome'] == 'slippage') if 'stop_outcome' in recent else False

    weekly = recent.groupby('week').agg(
        n=('plan_id', 'count'),
        win_rate_pct=('is_win', lambda s: round(s.mean() * 100, 1)),
        avg_plan_score_pct=('score', lambda s: round(s.mean(), 1) if s.notna().any() else None),
        stop_breach_rate_pct=('stop_breached', lambda s: round((s == True).mean() * 100, 1)),
        stop_slippage_rate_pct=('is_slippage', lambda s: round(s.mean() * 100, 1)),
    ).reset_index().sort_values('week')
    result['weekly_series'] = weekly.to_dict('records')

    result['win_rate_pct'] = round(recent['is_win'].mean() * 100, 1)
    scored = recent[recent['score'].notna()]
    result['avg_plan_score_pct'] = round(scored['score'].mean(), 1) if not scored.empty else None
    result['stop_breach_rate_pct'] = round((recent['stop_breached'] == True).mean() * 100, 1)
    result['stop_slippage_count'] = int(recent['is_slippage'].sum())
    result['stop_slippage_rate_pct'] = round(recent['is_slippage'].mean() * 100, 1)

    losses = recent[recent['actual_pnl'] < 0]
    result['recurring_symbols_in_losses'] = (
        losses['symbol'].value_counts().head(5).to_dict() if not losses.empty else {})
    breaches = recent[recent['stop_breached'] == True]
    result['recurring_symbols_in_deviations'] = (
        breaches['symbol'].value_counts().head(5).to_dict() if not breaches.empty else {})

    all_observations = db.fetch_all_observations()
    result['psych'] = compute_psych_performance(recent, all_observations, cutoff)
    return result


def _outcome_stats(sub):
    """n always; averages/rates only once n reaches PSYCH_MIN_SAMPLE, so the
    coach can't over-read two or three trades."""
    n = len(sub)
    out = {'n': n}
    if n < PSYCH_MIN_SAMPLE:
        out['insufficient_sample'] = True
        return out
    scores = sub['score'].dropna() if 'score' in sub else pd.Series(dtype=float)
    rs = sub['r_multiple'].dropna() if 'r_multiple' in sub else pd.Series(dtype=float)
    targets_total = sub.get('targets_total', pd.Series(0, index=sub.index)).fillna(0)
    targets_hit = sub.get('targets_hit', pd.Series(0, index=sub.index)).fillna(0)
    breached = sub.get('stop_breached', pd.Series(False, index=sub.index)) == True
    slipped = sub.get('stop_outcome', pd.Series(None, index=sub.index)) == 'slippage'
    # A stop-out (breach OR within-tolerance slippage) is the stop doing its
    # job or failing -- never a discretionary "early exit".
    early_exit = (targets_total > 0) & (targets_hit == 0) & ~breached & ~slipped
    out.update({
        'win_rate_pct': round((sub['actual_pnl'] > 0).mean() * 100, 1),
        'avg_plan_score_pct': round(scores.mean(), 1) if not scores.empty else None,
        'avg_r_multiple': round(rs.mean(), 2) if not rs.empty else None,
        'stop_breach_rate_pct': round(breached.mean() * 100, 1),
        'stop_slippage_rate_pct': round(slipped.mean() * 100, 1),
        'early_exit_rate_pct': round(early_exit.mean() * 100, 1),
    })
    return out


def compute_psych_performance(closed, observations, cutoff=None):
    """Psych-model-v2 outcome aggregates for the weekly coaching prompt.
    `closed` is a DataFrame of closed journal rows; `observations` is the raw
    observation list (db.fetch_all_observations). Pre-v2 rows (no v2 rating)
    are excluded rather than treated as neutral -- their two-axis scale mixed
    dimensions and can't be mapped onto these axes.

    - by_axis: each axis split low (<= -1) / mid (0) / high (>= 1).
    - distortion: calm (index 0-1) vs elevated (2+) -- see psych_distortion_index.
    - error_patterns: the specific mistakes each distortion should predict
      (fear -> early exits before the first target; overconfidence/FOMO ->
      stop breaches), via early_exit_rate_pct / stop_breach_rate_pct in each
      bucket's stats, compared against the calm baseline.
    - by_trigger: per trade trigger (multi-select, so one trade can count
      under several), plus 'untagged'; 'planned_setup' is the baseline.
    - at_deviation: the same axis split, but using the psych reading logged on
      the deviation-tagged ("exited contrary to plan") observation instead of
      the entry-time reading -- a different question: what state were you in
      when you broke the plan."""
    result = {'model_note': "Psych model v2 (Risk emotion / Urgency / Focus + trade triggers) "
                            "started 2026-09-29; earlier trades used a different two-axis "
                            "scale and are excluded from these stats.",
              'min_sample_for_averages': PSYCH_MIN_SAMPLE}
    closed = closed.copy()
    for a in PSYCH_AXES:
        if a['key'] not in closed.columns:
            closed[a['key']] = None
    rated = closed[closed.apply(lambda r: has_psych_v2(r), axis=1)] if not closed.empty else closed
    result['rated_trade_count'] = len(rated)

    if not rated.empty:
        result['by_axis'] = {}
        for a in PSYCH_AXES:
            col = rated[a['key']].fillna(0)
            result['by_axis'][a['title']] = {
                f"{a['neg']} (<= -1)": _outcome_stats(rated[col <= -1]),
                f"{a['mid']} (0)": _outcome_stats(rated[col == 0]),
                f"{a['pos']} (>= 1)": _outcome_stats(rated[col >= 1]),
            }
        distortion = rated.apply(psych_distortion_index, axis=1)
        result['distortion'] = {
            'calm (index 0-1)': _outcome_stats(rated[distortion <= 1]),
            'elevated (index 2+)': _outcome_stats(rated[distortion >= 2]),
        }

    if 'trade_triggers' in closed.columns:
        triggers = closed['trade_triggers'].apply(lambda t: t if isinstance(t, list) else [])
        tagged = closed[triggers.apply(len) > 0]
        result['tagged_trade_count'] = len(tagged)
        if not tagged.empty:
            by_trigger = {}
            for key, label in TRADE_TRIGGERS:
                sub = closed[triggers.apply(lambda ts: key in ts)]
                if len(sub):
                    by_trigger[label] = _outcome_stats(sub)
            result['by_trigger'] = by_trigger

    deviation_by_plan = {}
    for o in (observations or []):
        if o.get('event_type') == 'deviation' and has_psych_v2(o):
            prev = deviation_by_plan.get(o['plan_id'])
            if prev is None or o['observed_at'] > prev['observed_at']:
                deviation_by_plan[o['plan_id']] = o
    if deviation_by_plan and not closed.empty:
        with_dev = closed[closed['plan_id'].isin(deviation_by_plan)]
        if not with_dev.empty:
            result['at_deviation'] = {'trades_with_deviation_reading': len(with_dev)}
            for a in PSYCH_AXES:
                vals = with_dev['plan_id'].map(lambda pid: _psych_val(deviation_by_plan[pid].get(a['key'])) or 0)
                result['at_deviation'][a['title']] = {
                    f"{a['neg']} (<= -1)": _outcome_stats(with_dev[vals <= -1]),
                    f"{a['pos']} (>= 1)": _outcome_stats(with_dev[vals >= 1]),
                }

    dev_obs = [o for o in (observations or []) if o.get('event_type') == 'deviation' and has_psych_v2(o)]
    if dev_obs:
        dev_df = pd.DataFrame(dev_obs)
        dev_df['observed_at'] = pd.to_datetime(dev_df['observed_at'], utc=True, errors='coerce')
        dev_df = dev_df.dropna(subset=['observed_at'])
        if cutoff is not None:
            dev_df = dev_df[dev_df['observed_at'] >= cutoff]
        if not dev_df.empty:
            dev_df['week'] = dev_df['observed_at'].dt.tz_localize(None).dt.to_period('W-SUN').apply(
                lambda p: p.start_time.date().isoformat())
            agg = {'n': ('obs_id', 'count')}
            agg.update({f"avg_{a['key']}": (a['key'], 'mean') for a in PSYCH_AXES})
            result['deviation_psych_weekly'] = (
                dev_df.groupby('week').agg(**agg).round(2).reset_index().sort_values('week').to_dict('records'))
            result['deviation_count'] = len(dev_df)

    return result


def _build_weekly_review_prompt(week_ending, week_summary, trend_context):
    """The per-week message to paste into the dedicated coaching chat (see
    WEEKLY_COACHING_SYSTEM_PROMPT for the one-time setup message). week_summary
    is this week's specific activity (small, bounded) -- including TAPEs's own
    open-position list (symbol/qty/price only, not the full record), added
    specifically so a Wealthsimple holdings CSV pasted into the same chat has
    something to be reconciled against (see WEEKLY_COACHING_SYSTEM_PROMPT).
    trend_context is the precomputed multi-week aggregates from
    compute_weekly_trend_context (also small and fixed-size). Neither is raw
    historical trade rows -- see that function's docstring for why."""
    return (
        f"Week ending: {week_ending}\n\n"
        f"THIS WEEK:\n{json.dumps(week_summary, indent=2, default=str)}\n\n"
        f"TRAILING-WEEK TRENDS:\n{json.dumps(trend_context, indent=2, default=str)}\n"
    )


# ==============================================================
# 8. Run
# ==============================================================
if __name__ == '__main__':
    app.run(debug=True)
