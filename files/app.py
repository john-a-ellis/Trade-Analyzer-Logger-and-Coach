# app.py
"""
Trade Journal & Execution Scoring
----------------------------------
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
import uuid
import db
import datetime as dt

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Dash, dcc, html, Input, Output, State, ALL, callback_context, no_update
import dash_bootstrap_components as dbc
from dash.exceptions import PreventUpdate

# ==============================================================
# 1. CSV parsing
# ==============================================================

REQUIRED_WS_COLS = {
    'effective_at', 'settlement_date', 'activity_type', 'activity_sub_type',
    'direction', 'symbol', 'name', 'currency', 'quantity', 'unit_price', 'commission'
}


def decode_upload(contents: str) -> str:
    """Strip the dcc.Upload data-URL header and return raw text."""
    import base64
    _, content_string = contents.split(',', 1)
    decoded = base64.b64decode(content_string)
    return decoded.decode('utf-8', errors='replace')


def parse_wealthsimple_csv(contents: str) -> pd.DataFrame:
    """
    Parse a Wealthsimple activity export and return one row per executed fill.

    Returns columns: symbol, name, timestamp, action (BUY/SELL), qty, price,
    currency, commission, description.

    Note: uses `activity_sub_type` (BUY/SELL) rather than `direction`, since
    Wealthsimple exports `direction` as "LONG" for both opening and closing
    fills -- it does not indicate buy vs. sell.
    """
    text = decode_upload(contents)
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip().lower() for c in df.columns]

    missing = REQUIRED_WS_COLS - set(df.columns)
    if missing:
        raise ValueError(f"CSV is missing expected Wealthsimple columns: {sorted(missing)}")

    trades = df[df['activity_type'] == 'Trade'].copy()
    if trades.empty:
        return pd.DataFrame(columns=['symbol', 'name', 'timestamp', 'action', 'qty',
                                      'price', 'currency', 'commission', 'description'])

    trades['timestamp'] = pd.to_datetime(trades['effective_at'], errors='coerce', utc=True)
    fallback = pd.to_datetime(trades['settlement_date'], errors='coerce', utc=True)
    trades['timestamp'] = trades['timestamp'].fillna(fallback)

    trades['action'] = trades['activity_sub_type'].astype(str).str.upper().str.strip()
    trades = trades[trades['action'].isin(['BUY', 'SELL'])].copy()

    trades['qty'] = pd.to_numeric(trades['quantity'], errors='coerce').abs()
    trades['price'] = pd.to_numeric(trades['unit_price'], errors='coerce')
    trades['commission'] = pd.to_numeric(trades['commission'], errors='coerce').fillna(0)
    trades['symbol'] = trades['symbol'].astype(str).str.strip()
    trades['name'] = trades.get('name', trades['symbol']).fillna(trades['symbol'])
    trades['currency'] = trades.get('currency', 'CAD').fillna('CAD')
    trades['description'] = trades.get('description', '')

    trades = trades.dropna(subset=['timestamp', 'price', 'qty', 'symbol'])
    trades = trades[trades['qty'] > 0].sort_values('timestamp')

    return trades[['symbol', 'name', 'timestamp', 'action', 'qty', 'price',
                    'currency', 'commission', 'description']].reset_index(drop=True)


def parse_generic_csv(contents: str) -> pd.DataFrame:
    """Best-effort fallback parser for non-Wealthsimple CSVs."""
    text = decode_upload(contents)
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip().lower() for c in df.columns]

    mapping = {
        'symbol': ['symbol', 'ticker', 'description'],
        'timestamp': ['time', 'date', 'effective', 'settlement'],
        'action': ['action', 'direction', 'side', 'activity_sub_type'],
        'price': ['price', 'unit'],
        'qty': ['qty', 'quantity', 'shares'],
    }
    rename = {}
    for req, candidates in mapping.items():
        for col in df.columns:
            if col in rename.values():
                continue
            if any(c in col for c in candidates):
                rename[col] = req
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
    df['commission'] = pd.to_numeric(df.get('commission', 0), errors='coerce').fillna(0)
    df['name'] = df.get('name', df['symbol'])
    df['description'] = df.get('description', '')

    df = df.dropna(subset=['timestamp', 'price', 'qty', 'symbol'])
    df = df[df['action'].isin(['BUY', 'SELL']) & (df['qty'] > 0)].sort_values('timestamp')
    return df[['symbol', 'name', 'timestamp', 'action', 'qty', 'price',
               'currency', 'commission', 'description']].reset_index(drop=True)


def detect_csv_format(contents: str) -> str:
    try:
        text = decode_upload(contents)
    except Exception:
        return 'generic'
    lines = [l for l in text.split('\n') if l.strip()]
    if not lines:
        return 'generic'
    header = lines[0].lower()
    ws_keywords = ['effective_at', 'settlement_date', 'activity_type', 'activity_sub_type']
    return 'wealthsimple' if all(k in header for k in ws_keywords) else 'generic'


# ==============================================================
# 2. Round-trip construction (FIFO matching of BUY/SELL fills)
# ==============================================================

def build_round_trips(executions: pd.DataFrame):
    """
    Group individual BUY/SELL fills into completed round-trip (long) trades
    using FIFO lot matching per symbol. Also returns any still-open positions.

    Only long trades (BUY to open, SELL to close) are auto-detected. Short
    trades are not present in Wealthsimple cash-account exports and are not
    matched here; if you plan a short manually, log its execution yourself.
    """
    round_trips = []
    open_positions = []
    if executions is None or executions.empty:
        return pd.DataFrame(), pd.DataFrame()

    for symbol, g in executions.sort_values('timestamp').groupby('symbol'):
        lots = []          # open lots: [qty_remaining, price, timestamp, commission]
        entry_fills = []
        exit_fills = []

        for _, row in g.iterrows():
            if row['action'] == 'BUY':
                if not lots:
                    entry_fills = []
                    exit_fills = []
                lots.append([row['qty'], row['price'], row['timestamp'], row['commission']])
                entry_fills.append(row)

            elif row['action'] == 'SELL':
                if not lots:
                    # Sell with no open long lots -- can't attribute, skip (would be a short).
                    continue
                qty_to_sell = row['qty']
                exit_fills.append(row)
                while qty_to_sell > 1e-9 and lots:
                    lot = lots[0]
                    matched_qty = min(lot[0], qty_to_sell)
                    lot[0] -= matched_qty
                    qty_to_sell -= matched_qty
                    if lot[0] <= 1e-9:
                        lots.pop(0)

                if not lots and entry_fills:
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

                    round_trips.append({
                        'rt_id': str(uuid.uuid4())[:8],
                        'symbol': symbol,
                        'direction': 'BUY',
                        'entry_time': min(f['timestamp'] for f in entry_fills),
                        'exit_time': max(f['timestamp'] for f in exit_fills),
                        'qty': total_qty,
                        'entry_price': entry_price,
                        'exit_price': exit_price,
                        'commission': commission,
                        'realized_pnl': exit_rev - entry_cost - commission,
                        'currency': row['currency'],
                        'n_entry_fills': len(entry_fills),
                        'n_exit_fills': len(exit_fills),
                        'exit_fill_list': exit_fill_list,
                    })
                    entry_fills, exit_fills = [], []

        open_qty = sum(l[0] for l in lots)
        if open_qty > 1e-6:
            open_cost = sum(l[0] * l[1] for l in lots)
            open_positions.append({
                'symbol': symbol,
                'qty_open': open_qty,
                'avg_price': open_cost / open_qty if open_qty else np.nan,
                'currency': g['currency'].iloc[-1],
                'since': min(l[2] for l in lots),
            })

    rt_df = pd.DataFrame(round_trips)
    if not rt_df.empty:
        rt_df = rt_df.sort_values('exit_time', ascending=False).reset_index(drop=True)
    op_df = pd.DataFrame(open_positions)
    return rt_df, op_df


# ==============================================================
# 3. Plan <-> round-trip matching and scoring
# ==============================================================

def match_plans_to_roundtrips(plans, round_trips_df):
    """
    Greedy nearest-date matching of plans to round trips, one-to-one, per
    symbol+direction. Returns (journal_rows, unmatched_plan_ids, unmatched_rt_ids).
    """
    if round_trips_df is None or round_trips_df.empty:
        rt_by_key = {}
    else:
        rt_by_key = {k: v for k, v in round_trips_df.groupby(['symbol', 'direction'])}

    used_rt_ids = set()
    matches = {}  # plan_id -> rt_id

    for (symbol, direction), rts in rt_by_key.items():
        candidate_plans = [p for p in plans if p['symbol'] == symbol and p['direction'] == direction]
        if not candidate_plans:
            continue
        pairs = []
        for p in candidate_plans:
            p_date = pd.to_datetime(p.get('planned_date') or p.get('created_at'), utc=True, errors='coerce')
            for _, rt in rts.iterrows():
                if rt['rt_id'] in used_rt_ids:
                    continue
                if pd.isna(p_date) or pd.isna(rt['entry_time']):
                    diff = pd.Timedelta(days=9999)
                else:
                    diff = abs(rt['entry_time'] - p_date)
                pairs.append((diff, p['plan_id'], rt['rt_id']))
        pairs.sort(key=lambda x: x[0])
        claimed_plans = set()
        for diff, plan_id, rt_id in pairs:
            if plan_id in claimed_plans or rt_id in used_rt_ids:
                continue
            matches[plan_id] = rt_id
            claimed_plans.add(plan_id)
            used_rt_ids.add(rt_id)

    rt_lookup = {}
    if round_trips_df is not None and not round_trips_df.empty:
        rt_lookup = {r['rt_id']: r for r in round_trips_df.to_dict('records')}

    journal_rows = []
    for p in plans:
        rt = rt_lookup.get(matches.get(p['plan_id']))
        journal_rows.append(build_journal_row(p, rt))

    unmatched_rt_ids = []
    if round_trips_df is not None and not round_trips_df.empty:
        unmatched_rt_ids = [rid for rid in round_trips_df['rt_id'] if rid not in used_rt_ids]

    return journal_rows, unmatched_rt_ids


def _fmt_money(v):
    return None if v is None or pd.isna(v) else round(float(v), 2)


def analyze_targets_and_stop(direction, entry_planned, initial_stop, targets, exit_fills):
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

    Returns (target_results, targets_hit, targets_total, final_effective_stop, stop_note).
    """
    sign = 1 if direction == 'BUY' else -1
    clean_targets = [t for t in (targets or []) if t.get('price') not in (None, '')]
    # Nearest-to-entry first: ascending price for longs, descending for shorts.
    sorted_targets = sorted(clean_targets, key=lambda t: t['price'], reverse=(sign == -1))

    fills = sorted(exit_fills or [], key=lambda f: f['time'])
    fill_remaining = [f['qty'] for f in fills]

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

    stop_note = None
    if fills:
        final_fill = fills[-1]
        if current_stop not in (None, ''):
            breached = (final_fill['price'] < current_stop) if sign == 1 else (final_fill['price'] > current_stop)
            if breached:
                stop_note = f"Final exit at {final_fill['price']:.2f} breached the stop level in effect at that point ({current_stop:.2f})"
            else:
                stop_note = f"Final exit at {final_fill['price']:.2f} respected the stop level in effect at that point ({current_stop:.2f})"

    return target_results, targets_hit, targets_total, current_stop, stop_note


def build_journal_row(plan, rt):
    """Merge a plan with its matched round trip (if any) and compute scores."""
    row = dict(plan)
    row['matched'] = rt is not None
    sign = 1 if plan['direction'] == 'BUY' else -1

    entry_planned = plan.get('entry_price')
    initial_stop = plan.get('stop_loss')
    targets = plan.get('targets') or []
    qty_planned = plan.get('qty')

    row.update({
        'entry_actual': None, 'exit_actual': None, 'qty_actual': None,
        'entry_time': None, 'exit_time': None, 'actual_pnl': None,
        'actual_return_pct': None, 'r_multiple': None, 'score': None,
        'color': 'secondary', 'exec_notes': [], 'currency': None,
        'target_results': [], 'targets_hit': 0, 'targets_total': len(
            [t for t in targets if t.get('price') not in (None, '')]),
        'final_effective_stop': initial_stop,
    })

    if rt is None:
        # No execution yet -- still show the planned target ladder, all unhit.
        target_results, _, targets_total, _, _ = analyze_targets_and_stop(
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

    target_results, targets_hit, targets_total, final_stop, stop_note = analyze_targets_and_stop(
        plan['direction'], entry_planned, initial_stop, targets, exit_fills)

    # Blended planned exit price across active targets (weighted by qty_pct),
    # used to generalize the old single-take-profit "Plan Score" to multiple targets.
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
        notes.append(stop_note)
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
    })
    return row


# ==============================================================
# 4. App layout
# ==============================================================

app = Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP])
server = app.server
app.title = "Trade Journal & Execution Scoring"


# ------------------------------------------------------------
# Load persisted data from SQLite at startup
# ------------------------------------------------------------
db.init_db()


def _load_initial_state():
    executions_df = db.fetch_executions()
    round_trips_df, open_pos_df = build_round_trips(executions_df)
    plans = db.fetch_plans()

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

    return (executions_out.to_dict('records'), rt_out.to_dict('records'),
            op_out.to_dict('records'), plans)


INITIAL_EXECUTIONS, INITIAL_ROUNDTRIPS, INITIAL_OPENPOS, INITIAL_PLANS = _load_initial_state()


def labeled(label, component, width=4):
    return dbc.Col([dbc.Label(label), component], width=width)


def target_rows_form(prefix):
    """Build the 5 optional price-target rows (Price / Qty % / Move Stop To)
    used in both the Add Plan form and the Edit modal. `prefix` is 'plan'
    or 'edit' so the generated component ids don't collide."""
    rows = [
        html.P("Price Targets (up to 5, optional) — as each is hit, note where you'll move the "
               "protective stop.", className="text-muted small mb-1"),
        dbc.Row([
            dbc.Col(html.Small("Target"), width=1), dbc.Col(html.Small("Price"), width=3),
            dbc.Col(html.Small("Qty %"), width=3), dbc.Col(html.Small("Move Stop To"), width=4),
        ], className="mb-1"),
    ]
    for i in range(1, db.MAX_TARGETS + 1):
        rows.append(dbc.Row([
            dbc.Col(html.Div(f"T{i}", className="pt-2"), width=1),
            dbc.Col(dbc.Input(id=f'{prefix}-t{i}-price', type='number', size='sm'), width=3),
            dbc.Col(dbc.Input(id=f'{prefix}-t{i}-qty', type='number', size='sm', min=0, max=100), width=3),
            dbc.Col(dbc.Input(id=f'{prefix}-t{i}-stop', type='number', size='sm'), width=4),
        ], className="mb-1"))
    return html.Div(rows)


plan_form = dbc.Form([
    dbc.Row([
        labeled("Symbol *", dbc.Input(id='plan-symbol', placeholder="e.g. ARKK"), 4),
        labeled("Direction *", dbc.Select(
            id='plan-direction',
            options=[{'label': 'Long (BUY)', 'value': 'BUY'}, {'label': 'Short (SELL)', 'value': 'SELL'}],
            value='BUY'
        ), 4),
        labeled("Planned Date", dcc.DatePickerSingle(
            id='plan-date', date=dt.date.today(), display_format='YYYY-MM-DD'
        ), 4),
    ], className="mb-2"),
    dbc.Row([
        labeled("Entry Price *", dbc.Input(id='plan-entry', type='number'), 6),
        labeled("Initial Protective Stop (optional)", dbc.Input(id='plan-stop', type='number'), 6),
    ], className="mb-2"),
    dbc.Row([
        labeled("Quantity *", dbc.Input(id='plan-qty', type='number'), 6),
    ], className="mb-2"),
    dbc.Row([dbc.Col([target_rows_form('plan')], width=12)], className="mb-2"),
    dbc.Row([
        labeled("Reason / Setup (optional)", dbc.Textarea(id='plan-reason', rows=3,
                 placeholder="Why this trade? What's the thesis / setup?"), 12),
    ], className="mb-2"),
    dbc.Row([
        labeled("Notes (optional)", dbc.Textarea(id='plan-notes', rows=2), 12),
    ], className="mb-2"),
    dbc.Button("Add Trade Plan", id='btn-add-plan', color='primary'),
    html.Div(id='plan-add-status', className="mt-2"),
])

app.layout = dbc.Container([
    dcc.Store(id='store-executions', storage_type='memory', data=INITIAL_EXECUTIONS),
    dcc.Store(id='store-roundtrips', storage_type='memory', data=INITIAL_ROUNDTRIPS),
    dcc.Store(id='store-openpos', storage_type='memory', data=INITIAL_OPENPOS),
    dcc.Store(id='store-plans', storage_type='memory', data=INITIAL_PLANS),
    dcc.Store(id='store-journal', storage_type='memory', data=[]),
    dcc.Store(id='store-unmatched-rts', storage_type='memory', data=[]),
    dcc.Store(id='store-editing-plan-id', storage_type='memory'),
    dcc.Store(id='store-pending-delete-plan', storage_type='memory'),
    dcc.Download(id='download-journal'),

    # ---- Edit-plan modal ----
    dbc.Modal([
        dbc.ModalHeader(dbc.ModalTitle("Edit Trade Plan")),
        dbc.ModalBody([
            dbc.Row([
                labeled("Symbol *", dbc.Input(id='edit-symbol'), 4),
                labeled("Direction *", dbc.Select(
                    id='edit-direction',
                    options=[{'label': 'Long (BUY)', 'value': 'BUY'}, {'label': 'Short (SELL)', 'value': 'SELL'}],
                ), 4),
                labeled("Planned Date", dcc.DatePickerSingle(id='edit-date', display_format='YYYY-MM-DD'), 4),
            ], className="mb-2"),
            dbc.Row([
                labeled("Entry Price *", dbc.Input(id='edit-entry', type='number'), 6),
                labeled("Initial Protective Stop", dbc.Input(id='edit-stop', type='number'), 6),
            ], className="mb-2"),
            dbc.Row([labeled("Quantity *", dbc.Input(id='edit-qty', type='number'), 6)], className="mb-2"),
            dbc.Row([dbc.Col([target_rows_form('edit')], width=12)], className="mb-2"),
            dbc.Row([labeled("Reason / Setup", dbc.Textarea(id='edit-reason', rows=3), 12)], className="mb-2"),
            dbc.Row([labeled("Notes", dbc.Textarea(id='edit-notes', rows=2), 12)], className="mb-2"),
            html.Div(id='edit-status'),
        ]),
        dbc.ModalFooter([
            dbc.Button("Cancel", id='btn-edit-cancel', color='secondary'),
            dbc.Button("Save Changes", id='btn-edit-save', color='primary'),
        ]),
    ], id='edit-modal', is_open=False),

    dcc.ConfirmDialog(id='confirm-delete-plan'),
    dcc.ConfirmDialog(id='confirm-clear-plans',
                       message="This will permanently delete ALL trade plans from the database. This cannot be undone. Continue?"),
    dcc.ConfirmDialog(id='confirm-clear-executions',
                       message="This will permanently delete ALL imported executions (fills) and the round-trip "
                               "trades derived from them. Trade plans are kept. This cannot be undone. Continue?"),

    dbc.Row([dbc.Col([
        html.H1("Trade Journal & Execution Scoring", className="mt-3"),
        html.P("Log why you took a trade and what your plan was, import your actual fills, "
               "and see how well the plan was executed.", className="text-muted"),
    ])]),

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
                    html.Div(id='csv-upload-status', className="mt-2"),
                ])
            ]),
            dbc.Card([
                dbc.CardHeader("Open Positions (not yet closed)"),
                dbc.CardBody([html.Div(id='open-positions')])
            ], className="mt-3"),
        ], width=6),
    ], className="mt-3"),

    dbc.Row([dbc.Col([
        dbc.Card([
            dbc.CardHeader("Executed Trades Without a Plan"),
            dbc.CardBody([
                html.P("These round-trip trades were found in your import but don't have a matching plan yet. "
                       "Click \"Log Plan\" to pre-fill the form above.", className="text-muted small"),
                html.Div(id='unmatched-roundtrips')
            ])
        ])
    ])], className="mt-3"),

    dbc.Row([dbc.Col([
        dbc.Card([
            dbc.CardHeader("Data Management"),
            dbc.CardBody([
                html.Div(id='db-counts', className="text-muted small mb-2"),
                html.P(f"Data is stored in {db.DB_PATH}", className="text-muted small"),
                dbc.Button("Delete All Trade Plans", id='btn-clear-plans', color='outline-danger',
                           size='sm', className="me-2"),
                dbc.Button("Delete All Imported Executions & Trades", id='btn-clear-executions',
                           color='outline-danger', size='sm'),
                html.Div(id='clear-status', className="mt-2"),
            ])
        ])
    ])], className="mt-3"),

    dbc.Row([
        dbc.Col(dbc.Card([
            dbc.CardHeader("Trade Journal (Plan + Execution)"),
            dbc.CardBody([
                dbc.Row([
                    dbc.Col([html.P("Filter by Symbol:"),
                             dcc.Dropdown(id='filter-symbol', options=[], value=None, placeholder="All symbols")],
                            width=4),
                    dbc.Col([html.P("Filter by Score Range:"),
                             dcc.RangeSlider(id='filter-score', min=-100, max=150, step=10,
                                              marks={i: str(i) for i in range(-100, 151, 50)},
                                              value=[-100, 150])],
                            width=8),
                ], className="mb-2"),
                html.Div(id='journal-table'),
            ])
        ]), width=8),
        dbc.Col([
            dbc.Card([dbc.CardHeader("Summary Statistics"),
                      dbc.CardBody([html.Ul(id='summary-stats', className="list-unstyled")])]),
            dbc.Card([dbc.CardHeader("Export"),
                      dbc.CardBody([dbc.Button("Download Journal as CSV", id='btn-export', color='secondary')])],
                     className="mt-3"),
        ], width=4),
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


# ==============================================================
# 5. Callbacks
# ==============================================================

@app.callback(
    [Output('store-executions', 'data'),
     Output('store-roundtrips', 'data'),
     Output('store-openpos', 'data'),
     Output('csv-upload-status', 'children')],
    Input('upload-csv', 'contents'),
)
def import_csv(contents):
    """Parse the uploaded CSV, persist new fills to SQLite (deduplicated),
    then reload the full execution history from the DB (the source of truth)
    and recompute round trips from that."""
    if not contents:
        raise PreventUpdate

    fmt = detect_csv_format(contents)
    try:
        new_exec = parse_wealthsimple_csv(contents) if fmt == 'wealthsimple' else parse_generic_csv(contents)
    except ValueError as ve:
        return no_update, no_update, no_update, dbc.Alert(f"Error processing CSV: {ve}", color='danger')
    except Exception as e:
        return no_update, no_update, no_update, dbc.Alert(f"Unexpected error: {e}", color='danger')

    if new_exec.empty:
        return no_update, no_update, no_update, dbc.Alert("No trade rows found in that CSV.", color='warning')

    n_new = db.insert_executions(new_exec)
    combined = db.fetch_executions()
    round_trips, open_pos = build_round_trips(combined)

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

    n_rt = len(round_trips)
    n_dupe = len(new_exec) - n_new
    dupe_note = f" ({n_dupe} rows already in the database were skipped.)" if n_dupe else ""
    msg = dbc.Alert(
        f"✅ Parsed {len(new_exec)} fills ({fmt} format), {n_new} new saved to the database.{dupe_note} "
        f"Database now has {len(combined)} fills → {n_rt} completed round-trip trades "
        f"({len(open_pos)} positions still open).",
        color='success'
    )
    return combined_out.to_dict('records'), rt_out.to_dict('records'), op_out.to_dict('records'), msg


@app.callback(
    Output('open-positions', 'children'),
    Input('store-openpos', 'data'),
)
def render_open_positions(data):
    if not data:
        return html.P("None.", className="text-muted")
    df = pd.DataFrame(data)
    rows = [dbc.Row([
        dbc.Col(html.B("Symbol"), width=3), dbc.Col(html.B("Qty Open"), width=3),
        dbc.Col(html.B("Avg Price"), width=3), dbc.Col(html.B("Since"), width=3),
    ])]
    for _, r in df.iterrows():
        since = pd.to_datetime(r['since']).strftime('%Y-%m-%d') if pd.notna(r['since']) else '-'
        rows.append(dbc.Row([
            dbc.Col(r['symbol'], width=3), dbc.Col(f"{r['qty_open']:.4g}", width=3),
            dbc.Col(f"{r['currency']} {r['avg_price']:.2f}", width=3), dbc.Col(since, width=3),
        ]))
    return rows


def build_targets_list(*flat_vals):
    """flat_vals is (price1, qty1, stop1, price2, qty2, stop2, ...) for up to
    MAX_TARGETS targets. Rows with no price are skipped."""
    targets = []
    for i in range(0, len(flat_vals), 3):
        price, qty_pct, stop_after = flat_vals[i], flat_vals[i + 1], flat_vals[i + 2]
        if price in (None, ''):
            continue
        targets.append({
            'price': float(price),
            'qty_pct': float(qty_pct) if qty_pct not in (None, '') else None,
            'stop_after': float(stop_after) if stop_after not in (None, '') else None,
        })
    return targets


def targets_to_row_values(targets):
    """Inverse of build_targets_list: pad/truncate a plan's targets list to
    MAX_TARGETS rows of (price, qty_pct, stop_after) for populating the edit form."""
    targets = (targets or [])[:db.MAX_TARGETS]
    out = []
    for i in range(db.MAX_TARGETS):
        if i < len(targets):
            t = targets[i]
            out += [t.get('price'), t.get('qty_pct'), t.get('stop_after')]
        else:
            out += [None, None, None]
    return out


@app.callback(
    [Output('store-plans', 'data'), Output('plan-add-status', 'children'),
     Output('plan-symbol', 'value'), Output('plan-entry', 'value'),
     Output('plan-stop', 'value'), Output('plan-qty', 'value'),
     Output('plan-reason', 'value'), Output('plan-notes', 'value')] +
    [Output(f'plan-t{i}-price', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [Output(f'plan-t{i}-qty', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [Output(f'plan-t{i}-stop', 'value') for i in range(1, db.MAX_TARGETS + 1)],
    Input('btn-add-plan', 'n_clicks'),
    [State('plan-symbol', 'value'), State('plan-direction', 'value'),
     State('plan-date', 'date'), State('plan-entry', 'value'),
     State('plan-stop', 'value'), State('plan-qty', 'value'),
     State('plan-reason', 'value'), State('plan-notes', 'value'), State('store-plans', 'data')] +
    [State(f'plan-t{i}-price', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [State(f'plan-t{i}-qty', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [State(f'plan-t{i}-stop', 'value') for i in range(1, db.MAX_TARGETS + 1)],
    prevent_initial_call=True,
)
def add_plan(n_clicks, symbol, direction, planned_date, entry, stop, qty, reason, notes, plans, *target_vals):
    if not n_clicks:
        raise PreventUpdate
    plans = plans or []
    n_blank_outputs = 8 + 3 * db.MAX_TARGETS

    if not symbol or not direction or entry in (None, '') or qty in (None, ''):
        status = dbc.Alert("Symbol, Direction, Entry Price and Quantity are required.", color='danger')
        return (no_update, status) + (no_update,) * (n_blank_outputs - 2)

    # target_vals arrives as (price1..price5, qty1..qty5, stop1..stop5) since that's
    # the State ordering above; rezip into (price_i, qty_i, stop_i) triples.
    prices = target_vals[0:db.MAX_TARGETS]
    qtys = target_vals[db.MAX_TARGETS:2 * db.MAX_TARGETS]
    stops = target_vals[2 * db.MAX_TARGETS:3 * db.MAX_TARGETS]
    flat = []
    for p, q, s in zip(prices, qtys, stops):
        flat += [p, q, s]
    targets = build_targets_list(*flat)

    symbol = symbol.strip().upper()
    plan = {
        'plan_id': str(uuid.uuid4())[:8],
        'symbol': symbol,
        'direction': direction,
        'planned_date': planned_date,
        'created_at': dt.datetime.utcnow().isoformat(),
        'entry_price': float(entry),
        'stop_loss': float(stop) if stop not in (None, '') else None,
        'targets': targets,
        'qty': float(qty),
        'reason': reason or '',
        'notes': notes or '',
    }
    db.insert_plan(plan)
    plans = plans + [plan]
    status = dbc.Alert(f"✅ Plan added for {symbol} ({direction}) and saved to the database.", color='success')
    return (plans, status, '', None, None, None, '', '') + (None,) * (3 * db.MAX_TARGETS)


@app.callback(
    [Output('plan-symbol', 'value', allow_duplicate=True),
     Output('plan-direction', 'value', allow_duplicate=True),
     Output('plan-entry', 'value', allow_duplicate=True),
     Output('plan-qty', 'value', allow_duplicate=True),
     Output('plan-notes', 'value', allow_duplicate=True)],
    Input({'type': 'btn-log-plan', 'index': ALL}, 'n_clicks'),
    State('store-roundtrips', 'data'),
    prevent_initial_call=True,
)
def prefill_plan_from_roundtrip(n_clicks_list, roundtrips):
    if not any(n_clicks_list or []):
        raise PreventUpdate
    triggered = callback_context.triggered_id
    if not triggered:
        raise PreventUpdate
    rt_id = triggered['index']
    rt_lookup = {r['rt_id']: r for r in (roundtrips or [])}
    rt = rt_lookup.get(rt_id)
    if not rt:
        raise PreventUpdate
    note = (f"Logged retroactively from import. Actual entry {rt['entry_price']:.2f}, "
            f"exit {rt['exit_price']:.2f}, qty {rt['qty']:.4g}. Set your intended stop/target below.")
    return rt['symbol'], rt['direction'], round(rt['entry_price'], 4), round(rt['qty'], 4), note


@app.callback(
    [Output('store-journal', 'data'), Output('store-unmatched-rts', 'data')],
    [Input('store-plans', 'data'), Input('store-roundtrips', 'data')],
)
def compute_journal(plans, roundtrips):
    plans = plans or []
    rt_df = pd.DataFrame(roundtrips) if roundtrips else pd.DataFrame()
    if not rt_df.empty:
        rt_df['entry_time'] = pd.to_datetime(rt_df['entry_time'], utc=True)
        rt_df['exit_time'] = pd.to_datetime(rt_df['exit_time'], utc=True)

    journal_rows, unmatched_rt_ids = match_plans_to_roundtrips(plans, rt_df)

    for row in journal_rows:
        if row.get('entry_time') is not None:
            row['entry_time'] = str(row['entry_time'])
        if row.get('exit_time') is not None:
            row['exit_time'] = str(row['exit_time'])

    unmatched = rt_df[rt_df['rt_id'].isin(unmatched_rt_ids)].copy() if not rt_df.empty else pd.DataFrame()
    if not unmatched.empty:
        unmatched['entry_time'] = unmatched['entry_time'].astype(str)
        unmatched['exit_time'] = unmatched['exit_time'].astype(str)

    return journal_rows, unmatched.to_dict('records')


@app.callback(
    Output('unmatched-roundtrips', 'children'),
    Input('store-unmatched-rts', 'data'),
)
def render_unmatched_roundtrips(data):
    if not data:
        return html.P("None — every executed round-trip trade has a matching plan.", className="text-muted")
    df = pd.DataFrame(data).sort_values('exit_time', ascending=False)
    header = dbc.Row([
        dbc.Col(html.B("Symbol"), width=2), dbc.Col(html.B("Entry"), width=2),
        dbc.Col(html.B("Exit"), width=2), dbc.Col(html.B("Qty"), width=2),
        dbc.Col(html.B("P&L"), width=2), dbc.Col(html.B(""), width=2),
    ], className="mb-1")
    rows = [header]
    for _, r in df.iterrows():
        pnl = r['realized_pnl']
        pnl_color = 'text-success' if pnl >= 0 else 'text-danger'
        rows.append(dbc.Row([
            dbc.Col(r['symbol'], width=2),
            dbc.Col(f"{r['currency']} {r['entry_price']:.2f}", width=2),
            dbc.Col(f"{r['currency']} {r['exit_price']:.2f}", width=2),
            dbc.Col(f"{r['qty']:.4g}", width=2),
            dbc.Col(f"{r['currency']} {pnl:.2f}", width=2, className=pnl_color),
            dbc.Col(dbc.Button("Log Plan", size='sm', color='outline-primary',
                                id={'type': 'btn-log-plan', 'index': r['rt_id']}), width=2),
        ], className="mb-1 align-items-center"))
    return rows


@app.callback(
    Output('filter-symbol', 'options'),
    Input('store-journal', 'data'),
)
def populate_symbol_filter(data):
    if not data:
        return []
    symbols = sorted({row['symbol'] for row in data if row.get('symbol')})
    return [{'label': s, 'value': s} for s in symbols]


@app.callback(
    Output('journal-table', 'children'),
    [Input('store-journal', 'data'), Input('filter-symbol', 'value'), Input('filter-score', 'value')],
)
def render_journal(data, symbol_filter, score_range):
    if not data:
        return html.Div("No trade plans yet — add one above, or import a CSV and log a plan for an "
                         "existing trade.", className="text-muted")
    df = pd.DataFrame(data)
    if symbol_filter:
        df = df[df['symbol'] == symbol_filter]
    if score_range:
        lo, hi = score_range
        has_score = df['score'].notna()
        df = df[~has_score | (df['score'].between(lo, hi))]
    if df.empty:
        return html.Div("No trades match the current filters.", className="text-muted")

    df['_sort'] = df['score'].fillna(-9999)
    df = df.sort_values('_sort', ascending=False)

    header = dbc.Row([
        dbc.Col(html.B("Symbol"), width=1), dbc.Col(html.B("Dir"), width=1),
        dbc.Col(html.B("Entry (plan→actual)"), width=2), dbc.Col(html.B("Exit"), width=1),
        dbc.Col(html.B("Actual P&L"), width=1), dbc.Col(html.B("R-mult"), width=1),
        dbc.Col(html.B("Score"), width=1), dbc.Col(html.B("Targets"), width=1),
        dbc.Col(html.B("Status"), width=1), dbc.Col(html.B(""), width=2),
    ], className="mb-1")

    rows = [header]
    for _, r in df.iterrows():
        entry_disp = f"{r['entry_price']:.2f} → {r['entry_actual']:.2f}" if r['entry_actual'] is not None else f"{r['entry_price']:.2f} → (open)"
        exit_disp = f"{r['exit_actual']:.2f}" if r['exit_actual'] is not None else "-"
        pnl_disp = f"{r['currency'] or ''} {r['actual_pnl']:.2f}" if r['actual_pnl'] is not None else "-"
        r_disp = f"{r['r_multiple']:.2f}R" if r['r_multiple'] is not None else "-"
        score_disp = html.Span(f"{r['score']:.0f}%", className=f"badge bg-{r['color']}") if r['score'] is not None \
            else html.Span("N/A", className="badge bg-secondary")
        targets_total = r.get('targets_total') or 0
        if targets_total:
            targets_hit = r.get('targets_hit') or 0
            t_color = 'success' if targets_hit == targets_total else ('warning' if targets_hit else 'secondary')
            targets_disp = html.Span(f"{targets_hit}/{targets_total}", className=f"badge bg-{t_color}")
        else:
            targets_disp = html.Span("—", className="text-muted")
        actions = dbc.ButtonGroup([
            dbc.Button("View", size='sm', color='secondary', outline=True,
                       id={'type': 'btn-view', 'index': r['plan_id']}),
            dbc.Button("Edit", size='sm', color='primary', outline=True,
                       id={'type': 'btn-edit', 'index': r['plan_id']}),
            dbc.Button("Del", size='sm', color='danger', outline=True,
                       id={'type': 'btn-delete', 'index': r['plan_id']}),
        ])
        rows.append(dbc.Row([
            dbc.Col(r['symbol'], width=1),
            dbc.Col(r['direction'], width=1),
            dbc.Col(entry_disp, width=2),
            dbc.Col(exit_disp, width=1),
            dbc.Col(pnl_disp, width=1),
            dbc.Col(r_disp, width=1),
            dbc.Col(score_disp, width=1),
            dbc.Col(targets_disp, width=1),
            dbc.Col(r['status'], width=1),
            dbc.Col(actions, width=2),
        ], className="mb-1 align-items-center"))
    return rows


@app.callback(
    Output('summary-stats', 'children'),
    Input('store-journal', 'data'),
)
def update_summary(data):
    if not data:
        return [html.Li("No trades yet.")]
    df = pd.DataFrame(data)
    closed = df[df['status'] == 'Closed']
    if closed.empty:
        return [html.Li(f"{len(df)} plan(s) logged, none matched to a closed trade yet.")]

    scored = closed[closed['score'].notna()]
    win_rate = (closed['actual_pnl'] > 0).mean() * 100
    items = [
        html.Li(f"Closed trades: {len(closed)} (of {len(df)} plans)"),
        html.Li(f"Win rate: {win_rate:.1f}%"),
        html.Li(f"Total actual P&L: {closed['actual_pnl'].sum():.2f} (mixed currencies)"),
    ]
    if not scored.empty:
        items.append(html.Li(f"Average Plan Score: {scored['score'].mean():.1f}%"))
        items.append(html.Li(f"Median Plan Score: {scored['score'].median():.1f}%"))
        best = scored.loc[scored['score'].idxmax()]
        worst = scored.loc[scored['score'].idxmin()]
        items.append(html.Li(f"Best-executed: {best['symbol']} ({best['score']:.0f}%)"))
        items.append(html.Li(f"Worst-executed: {worst['symbol']} ({worst['score']:.0f}%)"))
    with_targets = closed[closed['targets_total'] > 0]
    if not with_targets.empty:
        avg_hit_rate = (with_targets['targets_hit'] / with_targets['targets_total']).mean() * 100
        items.append(html.Li(f"Average % of planned targets hit: {avg_hit_rate:.0f}%"))
    return items


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

    summary_lines = [
        f"Symbol: {r['symbol']}  |  Direction: {r['direction']}  |  Status: {r['status']}",
        "",
        f"Planned entry: {r['entry_price']}",
        f"Initial protective stop: {r['stop_loss'] if r['stop_loss'] is not None else 'not set'}",
        f"Planned qty: {r['qty']}",
        "",
        f"Actual entry: {r['entry_actual']}",
        f"Actual (weighted avg) exit: {r['exit_actual']}",
        f"Actual qty: {r['qty_actual']}",
        f"Actual P&L: {r['actual_pnl']} {r['currency'] or ''}",
        f"Actual return: {r['actual_return_pct']}%" if r['actual_return_pct'] is not None else "Actual return: n/a",
        f"R-multiple (vs. initial stop): {r['r_multiple']}" if r['r_multiple'] is not None else "R-multiple: n/a (no initial stop set)",
        f"Plan score (vs. blended targets): {r['score']}%" if r['score'] is not None else "Plan score: n/a (no targets set)",
        f"Stop level in effect at final exit: {r['final_effective_stop']}" if r['final_effective_stop'] is not None else "",
    ]
    summary_text = "\n".join(l for l in summary_lines if l is not None)

    target_results = r.get('target_results') or []
    target_table = None
    if target_results:
        header = dbc.Row([
            dbc.Col(html.B("#"), width=1), dbc.Col(html.B("Planned Price"), width=2),
            dbc.Col(html.B("Qty %"), width=1), dbc.Col(html.B("Move Stop To"), width=2),
            dbc.Col(html.B("Hit?"), width=1), dbc.Col(html.B("Actual Fill"), width=3),
            dbc.Col(html.B("Stop After"), width=2),
        ], className="mb-1")
        trows = [header]
        for i, t in enumerate(target_results, start=1):
            hit_badge = html.Span("Hit", className="badge bg-success") if t['hit'] \
                else html.Span("Not reached", className="badge bg-secondary")
            fill_disp = (f"{t['matched_price']:.2f} × {t['matched_qty']:.4g} @ {str(t['matched_time'])[:19]}"
                         if t['hit'] else "-")
            trows.append(dbc.Row([
                dbc.Col(f"T{i}", width=1),
                dbc.Col(f"{t['price']:.2f}", width=2),
                dbc.Col(f"{t['qty_pct']:.0f}%" if t['qty_pct'] is not None else "-", width=1),
                dbc.Col(f"{t['stop_after']:.2f}" if t['stop_after'] is not None else "-", width=2),
                dbc.Col(hit_badge, width=1),
                dbc.Col(fill_disp, width=3),
                dbc.Col(f"{t['stop_after_hit']:.2f}" if t['stop_after_hit'] is not None else "-", width=2),
            ], className="mb-1 align-items-center"))
        target_table = html.Div([html.H6("Target Ladder", className="mt-3"), html.Div(trows)])

    notes_block = html.Div([
        html.H6("Execution Notes", className="mt-3"),
        html.Ul([html.Li(n) for n in (r['exec_notes'] or [])]) if r['exec_notes'] else html.P("—", className="text-muted"),
    ])
    reason_block = html.Div([
        html.H6("Reason", className="mt-3"), html.P(r['reason'] or '(none given)'),
        html.H6("Notes"), html.P(r['notes'] or '(none given)'),
    ])

    detail_children = html.Div([
        html.Pre(summary_text, style={'whiteSpace': 'pre-wrap', 'fontFamily': 'inherit', 'marginBottom': 0}),
        target_table if target_table else None,
        notes_block,
        reason_block,
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
                              text=[f"{v:.2f}" for v in y_vals], hoverinfo='text+x'))

    if r['stop_loss'] is not None:
        fig.add_hline(y=r['stop_loss'], line_dash='dot', line_color='red', annotation_text='Initial Stop')
    if r['final_effective_stop'] is not None and r['final_effective_stop'] != r['stop_loss']:
        fig.add_hline(y=r['final_effective_stop'], line_dash='dash', line_color='orange',
                      annotation_text='Stop (final)')
    for i, t in enumerate(target_results, start=1):
        fig.add_hline(y=t['price'], line_dash='dot', line_color='green', opacity=0.5,
                      annotation_text=f'T{i}')

    fig.update_layout(title=f"Price Path – {r['symbol']}", yaxis_title="Price", height=320,
                       margin=dict(l=40, r=20, t=40, b=40))

    return detail_children, fig


def sign_favorable(direction, reference_price, price):
    """True if `price` is favorable relative to `reference_price` for the given direction."""
    if reference_price is None:
        return True
    return price >= reference_price if direction == 'BUY' else price <= reference_price


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

@app.callback(
    [Output('edit-modal', 'is_open', allow_duplicate=True),
     Output('store-editing-plan-id', 'data'),
     Output('edit-symbol', 'value'), Output('edit-direction', 'value'),
     Output('edit-date', 'date'), Output('edit-entry', 'value'),
     Output('edit-stop', 'value'), Output('edit-qty', 'value'),
     Output('edit-reason', 'value'), Output('edit-notes', 'value'),
     Output('edit-status', 'children')] +
    [Output(f'edit-t{i}-price', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [Output(f'edit-t{i}-qty', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [Output(f'edit-t{i}-stop', 'value') for i in range(1, db.MAX_TARGETS + 1)],
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
    # target_row_vals is [p1,q1,s1, p2,q2,s2, ...]; the Output ordering above is
    # all prices, then all qtys, then all stops, so regroup accordingly.
    prices = target_row_vals[0::3]
    qtys = target_row_vals[1::3]
    stops = target_row_vals[2::3]
    return (True, plan_id, plan['symbol'], plan['direction'], plan.get('planned_date'),
            plan['entry_price'], plan.get('stop_loss'), plan['qty'],
            plan.get('reason', ''), plan.get('notes', ''), '') + tuple(prices) + tuple(qtys) + tuple(stops)


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
     State('edit-direction', 'value'), State('edit-date', 'date'),
     State('edit-entry', 'value'), State('edit-stop', 'value'),
     State('edit-qty', 'value'), State('edit-reason', 'value'), State('edit-notes', 'value'),
     State('store-plans', 'data')] +
    [State(f'edit-t{i}-price', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [State(f'edit-t{i}-qty', 'value') for i in range(1, db.MAX_TARGETS + 1)] +
    [State(f'edit-t{i}-stop', 'value') for i in range(1, db.MAX_TARGETS + 1)],
    prevent_initial_call=True,
)
def save_edit(n_clicks, plan_id, symbol, direction, planned_date, entry, stop, qty,
              reason, notes, plans, *target_vals):
    if not n_clicks or not plan_id:
        raise PreventUpdate
    if not symbol or not direction or entry in (None, '') or qty in (None, ''):
        status = dbc.Alert("Symbol, Direction, Entry Price and Quantity are required.", color='danger')
        return no_update, True, status

    prices = target_vals[0:db.MAX_TARGETS]
    qtys = target_vals[db.MAX_TARGETS:2 * db.MAX_TARGETS]
    stops = target_vals[2 * db.MAX_TARGETS:3 * db.MAX_TARGETS]
    flat = []
    for p, q, s in zip(prices, qtys, stops):
        flat += [p, q, s]
    targets = build_targets_list(*flat)

    fields = {
        'symbol': symbol.strip().upper(),
        'direction': direction,
        'planned_date': planned_date,
        'entry_price': float(entry),
        'stop_loss': float(stop) if stop not in (None, '') else None,
        'targets': targets,
        'qty': float(qty),
        'reason': reason or '',
        'notes': notes or '',
    }
    db.update_plan(plan_id, fields)

    plans = plans or []
    new_plans = []
    for p in plans:
        if p['plan_id'] == plan_id:
            p = dict(p)
            p.update(fields)
        new_plans.append(p)

    status = dbc.Alert(f"✅ Plan for {fields['symbol']} updated and saved to the database.", color='success')
    return new_plans, False, status


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
     Output('plan-add-status', 'children', allow_duplicate=True)],
    Input('confirm-delete-plan', 'submit_n_clicks'),
    [State('store-pending-delete-plan', 'data'), State('store-plans', 'data')],
    prevent_initial_call=True,
)
def do_delete_plan(submit_n_clicks, plan_id, plans):
    if not submit_n_clicks or not plan_id:
        raise PreventUpdate
    db.delete_plan(plan_id)
    new_plans = [p for p in (plans or []) if p['plan_id'] != plan_id]
    status = dbc.Alert("🗑️ Plan deleted.", color='secondary')
    return new_plans, status


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
    [Output('store-plans', 'data', allow_duplicate=True), Output('clear-status', 'children', allow_duplicate=True)],
    Input('confirm-clear-plans', 'submit_n_clicks'),
    prevent_initial_call=True,
)
def do_clear_plans(submit_n_clicks):
    if not submit_n_clicks:
        raise PreventUpdate
    n = db.delete_all_plans()
    return [], dbc.Alert(f"🗑️ Deleted all {n} trade plan(s) from the database.", color='secondary')


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
# 7. Run
# ==============================================================
if __name__ == '__main__':
    app.run(debug=True)
