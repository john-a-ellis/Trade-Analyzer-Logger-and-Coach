# TAPEs — Trading Analysis, Performance and Evaluation System

*"Reading the tape."*

This file gives Claude Code standing context on this project. Read this before making
architectural decisions or extending the data model.

**Naming history:** this project was called TALCS (Trading Analysis, Logging and
Coaching System) until 2026-08-29, when it was renamed to TAPEs — "reading the tape"
being genuine, longstanding trader slang for watching price/trade action, which reads
better than a forced acronym. The letters were also reassigned: the "Coaching" (C)
role that TALCS's name gave the AI-review pillar is now folded into "Evaluation" (E),
and "Performance" (P) replaces the earlier brainstormed "Psychology" — a deliberate
choice, not a typo, since psychological state is only one input the app tracks
(alongside the plan/execution/scoring data the P and E letters cover more broadly).
Nothing about the app's actual design changed because of this rename — see the
Weekly Review and psych-scale sections below, which predate it and still apply exactly
as documented, just under the new name.

## What TAPEs Is

A Plotly Dash app for logging discretionary ETF/stock trades: the plan taken *before*
entry, what actually happened, and a post-trade score on both profitability and
plan-adherence. Part of a three-system trading framework — see below.

## The Three-System Architecture

| System | Role | Connected to Claude? |
|---|---|---|
| **Wealthsimple** | Trade execution (TFSA + retirement accounts) | No — no public API. Manual only, by design. Never automate execution here, even if a connector existed. |
| **TradingView** | Opportunity scanning/analysis, home of two custom Pine Script indicators | Yes — via MCP in the claude.ai chat environment, **and** (as of 2026-08-26) via the same MCP server added to Claude Code itself, user-scoped so it's available from any repo, not just this one. |
| **TAPEs** (this repo) | Trade planning, logging, execution reconciliation, coaching/scoring | Yes, via Claude Code working directly in this repo, **and** (as of 2026-08-27) TAPEs itself is an MCP *server* while running — see below. |

**Wealthsimple is the single source of truth** for what actually happened in the
account (fills, timing, quantities). TradingView indicators inform *planning*, not
final record-keeping.

**Important distinction:** Claude Code having the TradingView MCP connection does
*not* mean TAPEs (the Dash app) can call OUT to MCP tools as a *client* — TAPEs runs
as its own standalone Flask process, independent of any Claude session. Where TAPEs
itself needs live TradingView data (the ATR fetch — see below), it talks directly to
TradingView Desktop's Chrome DevTools Protocol (CDP) debug port (`127.0.0.1:9222`),
the same port the tradingview-mcp server connects to. CDP allows multiple independent
debugger connections to one target, so TAPEs's direct read and a Claude Code session's
MCP connection can both be live at once without conflicting.

TAPEs being an MCP *server* is the opposite relationship: Claude Code (or any MCP
client) calls *into* TAPEs, not the other way around — see "TAPEs as an MCP server" below.

### TAPEs as an MCP server (implemented 2026-08-27)

Dash 4's own MCP integration (`dash.mcp`, `enable_mcp=True` on the `Dash(...)`
constructor) exposes plain Python functions as MCP tools directly over the app's own
`/_mcp` HTTP endpoint — no separate server process, no new dependency. TAPEs uses this
instead of raw Dash callbacks as tools (`configure_mcp_server(include_callbacks=False)`
in `app.py`): callbacks are awkward to call this way (most are pure-render needing fake
Store data, or keyed to a button's `n_clicks` alongside a dozen unrelated `State` args),
so purpose-built `@mcp_enabled` functions wrap the same `db.py`/`app.py` logic instead,
in the "MCP tools" section near the bottom of `app.py`.

**Available tools:** `list_plans`, `get_plan`, `get_journal`, `get_summary_stats`,
`list_open_positions`, `list_unmatched_round_trips`, `list_cash_equivalent_symbols`,
`add_cash_equivalent_symbol`, `remove_cash_equivalent_symbol`, `create_plan`,
`commit_plan`, `delete_plan`, `fetch_current_atr` (reuses the same TradingView-Desktop-
then-Yahoo-Finance fetch the Add Plan form uses). Since added: `list_accounts`,
`get_beta_weighted_exposure`, `get_plan_drift_report`, `get_fx_rates`, and
`list_plan_screenshots`. Also added: `cancel_plan`, `reinstate_plan` and
`refresh_cancelled_outcomes` (2026-10-20).

**Journal notes over MCP (2026-10-22):**
- **`get_daily_journal(since, until, search, limit=50)`:** Daily Trading Journal entries, newest
  first, returned as {entries, matching, total}.
- **`get_trade_notes(plan_id=None, since=None)`:** a plan's observation log, each note with its
  `event_type` and a readable `psych` reading. Without `plan_id` it returns every plan that has
  notes, grouped by plan.

Both are read-only, built on `db.fetch_daily_observations`/`fetch_plan_observations`, and tested
in `tests/test_mcp_notes.py`.

**`get_weekly_journal(since, until, search, limit=12)` (2026-10-23):** the Weekly Review rows,
newest week first, as {weeks, matching, total}. Each week carries:
- **Balance:** the balance with its basis (`balance_currency` 'CAD' or 'mixed',
  `balance_includes_cash`, the per-currency `balance_detail`).
- **Change:** the change vs. the previous week on the same basis, or `'basis_changed'`
  (`weekly_balance_changes`).
- **Notes:** `user_note` and `coaching_note` (the `llm_note` column).
- **Weekly checklist:** {done, total, unticked}, measured against the *current* list. Ticks
  made under the pre-2026-10-22 list used other item ids, so older weeks read 0 done.
- **`daily_lessons`:** the lesson/proposal lines from that week's structured daily entries.

The pure part is `weekly_journal_rows`. Search covers both notes, the lessons and the date.
It's read-only and tested in `tests/test_mcp_notes.py` with every DB read stubbed. The MCP
server now lists 25 tools (27 as of 2026-11-05, with `get_action_register` and `get_checklist`; 28 with `get_regime_adherence` and 29 with `get_practice_scorecard`, 2026-11-06; 31 with `get_improvement_register` and `set_improvement`, 2026-11-07).

**Action register (2026-11-01):** every focus action from the coaching notes, current and closed.
- **In the app:** an "Action register" button beside Save Week on the Weekly Review card opens
  `action-register-modal` (xl, read-only).
  - Tabs Current / Closed / All, with counts. The tab choice is remembered via `PERSIST`.
  - Columns: ID, week raised, text with rule badges, a status badge (with "not done n×"), and the
    review history (week, status, note).
  - Statuses are still set in each week's entry.
- **Order:** current is oldest raised first; closed is most recently closed first.
- **Code:** pure `action_register_rows`/`action_register_counts`/`render_action_register`; callbacks
  `open_action_register` and `render_action_register_cb`, which re-renders on `store-weekly-actions`.
- **MCP:** `get_action_register(view='current'|'closed'|'all')` returns {actions, counts}.
- **Tests:** `ActionRegister` in `tests/test_weekly_entry.py`. Checked with a headless-Chrome
  screenshot on real data: 5 actions, 3 current (A1 not done once, A4, A5) and 2 done (A2, A3).

**Plan screenshots (2026-10-10):** `get_plan` returns `screenshot_file`, the absolute
path of the chart saved with the plan, and `list_plan_screenshots` lists every plan that
has one. To see a chart, open that path with Claude Code's Read tool, which displays
images.
- **Why a path, not the image:** Dash serialises an `@mcp_enabled` function's return
  value into one JSON TextContent (`dash/mcp/primitives/tools/tool_decorated_mcp_functions.py`).
  An image in the result would arrive as ~330 KB of base64 text, not something viewable.
- **Image results do exist in Dash,** but only for callback-based tools, which TAPEs
  doesn't use.
- **The alternative** (a dedicated tool returning a real ImageContent by overriding Dash's
  result handling) was offered and not chosen: it patches Dash internals, and the TAPEs
  MCP server only runs locally, where Claude Code can read the files anyway.
- **Enlarge (2026-10-12):**
  - The trade detail view's thumbnail is clickable
    (`{'type': 'btn-enlarge-screenshot', 'index': plan_id}`, zoom-in cursor).
  - A click opens `screenshot-modal` (xl) with the full image and an "Open full size in a
    new tab" link. `enlarge_screenshot` only acts on a real click, since a re-rendered
    thumbnail fires with n_clicks 0.
  - **Cache-busting:** all screenshot images now use `screenshot_url`, which appends
    `?v=<file mtime>`. A replaced screenshot reuses the same file name (the plan id), so a
    browser could otherwise keep showing its cached old copy. That may explain the
    2026-10-10 CHPS mismatch: the user saw the right chart in TAPEs while the file read
    earlier showed the NASDAQ fund.
  - Verified with tests (the URL stamp, click-only opening, the detail view's clickable
    thumbnail) and a headless-Chrome screenshot of the modal on the real VFV chart.
- **Verified:** read the real CHPS screenshot this way. Doing so showed it's of the
  NASDAQ (USD, Xtrackers) CHPS rather than the TSX fund the plan is for; the user was
  told. `create_plan` defaults to
`status='draft'`, not `'committed'` — a plan created this way gets the same review
step before it counts toward scoring, unless `status='committed'` is passed explicitly.

**Two things to know before using it:**
1. **The server only exists while the app is running.** Unlike tradingview-mcp (which
   spawns its own process on demand), this MCP endpoint is embedded in the Dash process
   itself — `python app.py` has to actually be running for the connection to work.
2. **Dash's own MCP instructions say it plainly: calling a tool runs the function (DB
   writes are real and persisted) but does NOT push an update to the user's already-open
   browser tab.** If they have TAPEs open in a browser while a write tool runs, tell
   them to refresh to see the change.

Registered in Claude Code at **project (local) scope**, not user scope like
tradingview-mcp — this server is meaningless outside a running instance of this
specific repo's app, unlike TradingView Desktop control which is useful from anywhere.
Config: `{"type": "http", "url": "http://127.0.0.1:8050/_mcp"}` (Dash's default port
8050 + the default `_mcp` path — both configurable via `mcp_path`/`app.run(port=...)`
if the app is ever run on a different port). Verified end-to-end against an isolated
copy of the real database before wiring this up for real: all 14 tools tested,
including a full create→commit→verify-in-journal→delete round trip and a live
TradingView ATR fetch through `fetch_current_atr`. Not yet confirmed from an actual
Claude Code session after a restart (the config's exact schema — `"type": "http"` — is
my best understanding of Claude Code's MCP config shape for a Streamable HTTP server,
not something I could verify without restarting Claude Code myself).

## The Two Custom TradingView Indicators

### 1. Sniper Entry/Exit with SL&TP by KhanSaab V.06
Swing-trade entry/exit signal generator. Entry triggers on EMA alignment + a
volume-weighted momentum (VWM) score crossing a threshold. On trigger, computes:
- Stop loss: ATR × multiplier (different multiplier for intraday vs. swing/HTF-aligned)
- 5 take-profit levels (T1–T5) spaced across ATR multiples (default 1R → 6R)
- Trailing stop that activates once T1 is hit

**Known limitations (see full review below in this doc / in chat history):**
- Its "position state" (`lastSignalState`, T1–T5 hit flags, etc.) is a self-contained
  simulation — it does *not* know your real Wealthsimple position, fills, or
  discretionary exits. Treat its dashboard as a planning aid, never as "current status."
- T1–T5 are ATR-multiple based, not the fixed-percentage scale-out levels described in
  the trading strategy (3% / 15% / 20%, with SSO at 10%/6% and QLD at 12%/6%).
- Only a single-stage trailing stop; the written strategy has two stages (move to
  breakeven at TP1, trailing only starts at TP2).
- No per-symbol overrides for leveraged ETFs (SSO/QLD use different SL/TP1 % in the
  written strategy; the indicator applies the same ATR multiplier to everything).
- State resets silently on timeframe change.

### 2. Macro Regime Model (Weighted 3-State) - Left Aligned
Risk-on/risk-off environment filter. Combines 6 weighted layers (Trend, Volatility,
Credit, Liquidity, Macro data, Sentiment) into a composite score, classified into 5
regimes: Full Risk-On, Partial Risk-On, Neutral, Partial Risk-Off, Full Risk-Off.

**Purpose:** used to decide whether to be fully invested, partially in cash, or fully
in cash (BIL) — not a stop/target generator. Trend symbol is typically set to SPY, or
SPY+QQQ+DIA when assessing the broader environment.

Buys of a cash-equivalent parking ETF (BIL and similar) are deliberately **not**
planned trades — there's no thesis/stop/target to log, just moving to cash. TAPEs
supports this directly: mark a symbol as cash-equivalent (small input on the Open
Positions card) and it's excluded from the "No plan" prompt there and from "Executed
Trades Without a Plan," with **zero effect on scoring either way** — the journal is
built one row per *plan* (`db.py`'s own docstring: round trips/journal are derived,
not persisted), so a symbol with no plan was already excluded from every performance
metric (win rate, Plan Score, etc.) before this existed. See `db.cash_equivalent_symbols`.

**Known limitations:**
- Credit and Liquidity layers' 20-day lookback (`val[20]`) is computed in the chart's
  own bar sequence, not inside the `request.security()` call — correct on a daily
  chart, silently wrong on intraday timeframes.
- Failed/unresolved data feeds (FRED series, obscure tickers) default to a neutral (0)
  reading with no visible distinction from a genuinely neutral layer.
- Nominal layer weights don't map to actual influence on the composite, since raw
  layer ranges differ (e.g. Macro layer sums 4 sub-signals, Trend sums 2).
- No hysteresis at regime boundaries — composite can flicker between adjacent states.

There is no third indicator. Chris Vermeleun's BAN (Best Asset Now) strategy — which
originally inspired parts of this approach — is referenced conceptually in the trading
strategy doc but his actual system/code is not available; it informs strategy, not
implementation.

## Confirmed Workflow (this is the design TAPEs should implement)

1. Use Sniper + Macro Regime Model in TradingView to identify and assess trade
   opportunities.
2. Execute the trade manually in Wealthsimple.
3. Enter the actual fill price into TAPEs. TAPEs pulls a **fresh ATR reading at commit
   time** (not whatever ATR Sniper computed at signal time — ATR moves) and generates
   ATR-derived TP1–TP5 and stop levels off the real entry price.
   **Implemented (2026-08-26/27).** Fully automatic as of 2026-08-27 — there is no
   "Fetch ATR" button anymore; entering/changing the symbol (Add Plan form or Edit
   modal) triggers the fetch on its own, and the resulting stop/T1–T5 targets
   recompute automatically whenever entry price, ATR, or the ATR multiplier change
   (as long as Method is "ATR-derived" — see step 4). ATR itself tries two sources
   in order:
   1. **Live TradingView Desktop**, read directly over CDP (`app.fetch_atr_from_tradingview`)
      — *actively* corrects the chart to match the plan before reading: sets the
      symbol if it doesn't match (bare-ticker comparison), forces the 1D timeframe
      if it isn't already, then reads OHLC bars and computes Wilder's-smoothing
      ATR(14) (matches Pine's `ta.atr` default). This mutates the user's live
      TradingView Desktop session on purpose — an earlier passive
      read-whatever's-on-screen-and-warn design was tried first and rejected
      after testing showed it just used the wrong instrument's ATR too easily.
      Switching the **saved chart layout** was also tried (to a layout named
      "Multi-Window") but was **removed** after hands-on testing surfaced a
      documented TradingView bug where switching between custom/saved layouts
      via the API doesn't reliably apply — confirmed on this exact chart, so it's
      not a bug in TAPEs. The user manages the layout manually; symbol and
      timeframe correction both tested reliable (verified live via an independent
      TradingView MCP connection, not just TAPEs's own read) and stay automatic.
   2. **Yahoo Finance proxy** (`app.fetch_atr_from_yfinance`), used only when
      TradingView Desktop isn't reachable — always daily bars regardless of the
      chart's actual timeframe, so it's the coarser fallback, not the primary source.
   Either way the fetched value lands in an editable field, per the earlier decision
   that a wrong auto-fetched number must never silently drive a stop-loss.
4. **The initial computed plan is always ATR-based.** A 3% (or symbol-specific,
   e.g. 6% for SSO/QLD) initial-target override is available but is a **manual, deliberate
   edit at plan finalization** — not an automatic per-symbol rule (for now — may become
   automatic later, but don't build it that way by default yet).
   **Implemented.** An explicit ATR-derived/manual-override radio on the plan form sets
   `plans.target_method`; `plans.atr_at_commit` and `plans.atr_multiplier` record the
   provenance of whatever stop/targets were used. This radio is also the override
   mechanism for the automatic recompute in step 3: while it's "ATR-derived", stop/
   targets keep recomputing from entry+ATR; flipping it to "Manual override" freezes
   whatever is currently in those fields, so a hand-typed value survives further
   symbol/entry changes and persists through commit, per the same "deliberate edit"
   principle this step already called for. Every stop/target price field also shows a
   live read-only "% from entry" next to it, informational only, recomputed on every
   edit including hand-typed ones.
5. TAPEs allows full editing of the plan before it's committed.
   **Implemented** as a draft/commit lifecycle: "Save as Draft" vs. "Commit Plan" on the
   Add Plan form, a "Draft Trade Plans" card (Edit/Commit/Discard), and `plans.status` +
   `plans.committed_at` in the schema. Drafts are excluded from the journal, scoring, and
   execution matching until committed.
6. Once committed, TAPEs draws the finalized plan as a **second, separate overlay** on
   the TradingView chart via the MCP drawing tools (lines/boxes) — this is distinct
   from Sniper's own native plotted lines, which stay as-is. Both overlays should be
   independently toggleable:
   - Sniper's native lines: controlled via the indicator's own `showVisuals` input in
     TradingView, as it already exists.
   - The TAPEs-committed plan overlay: needs its own show/hide control in TAPEs's UI
     (it's drawn externally via MCP, not part of the Pine script, so it cannot live in
     TradingView's indicator settings panel).
   - This overlay is **session-live only** — drawn into the current TradingView browser
     session via MCP, not a persistent saved annotation. It needs to be re-applied each
     time you want it visible in a fresh session.
   **Not yet implemented.** Now technically more reachable than before — Claude Code
   itself can drive the TradingView MCP drawing tools (`draw_shape` etc.) directly in a
   chat session — but TAPEs the Dash app still has no code path to trigger that
   automatically on commit. Building this would mean either (a) doing the draw manually
   via a Claude Code/claude.ai request after each commit, or (b) giving TAPEs its own
   CDP-based drawing calls the same way the ATR fetch works. Neither is built yet.
7. Trade executions are re-downloaded from Wealthsimple at the end of any day a trade
   was made (not just at position close), to keep TAPEs current.
8. **Reconciliation approach:** TAPEs attempts to auto-match newly downloaded
   Wealthsimple execution rows to the nearest still-open TAPEs plan by symbol. This is
   necessary because a single planned trade typically corresponds to multiple separate
   sell rows in the Wealthsimple export (50% out at TP1, 50% of remainder at TP2, rest
   at TP3+), potentially spread across several days, and the same symbol may be
   re-entered later as a fresh, unrelated trade. The user can **manually override** the
   match when the auto-match guesses wrong.
   **Implemented.** A "Match" button on each journal row opens a picker of candidate
   round trips (same symbol+direction) and persists the override in a `plan_matches`
   table, keyed on a deterministic round-trip id (`app.round_trip_id`, a hash of the
   composing execution row ids — not a random uuid, so it's stable across recomputes).

   **Bug found and fixed (2026-08-27):** auto-matching only ever considered *closed*
   round trips, never still-open positions, and had no sanity check on fit at all —
   just "nearest available candidate for this symbol+direction," however poor. A plan
   for a brand-new IBIT position (95 shares bought, 47 scaled out, 48 still held) got
   matched to a *completely unrelated* IBIT round trip closed 3+ months earlier, and
   showed as "Closed." Fixed in `app.match_plans_to_roundtrips`: open positions now
   compete in the same candidate pool as round trips (via `build_round_trips`'s
   `entry_price`/`entry_time`/`qty_total_entered`/`exit_fill_list` fields on each open
   position, added for this), and every candidate must pass a fit gate
   (`MATCH_MAX_DATE_GAP_DAYS`=30, `MATCH_MAX_PRICE_DEVIATION`=20%) or the plan is left
   unmatched rather than forced onto a bad fit. The date gate is skipped for open
   positions specifically (there's at most one per symbol, so there's no other episode
   to confuse it with, and the "Log Plan" button on Open Positions deliberately invites
   writing a plan long after the position was actually entered) — but a wide gap still
   gets flagged as an execution note ("verify this is the trade you meant") so a
   genuinely wrong match stays visible rather than silent. `build_journal_row` gained a
   third state alongside "Closed"/"Planned (no execution matched yet)": **"Open (in
   progress)"** — entry deviation and target-ladder progress against whatever partial
   exits have happened, plus P&L realized on shares already sold, but no R-multiple/Plan
   Score (the trade isn't over). Verified against the real DB, not just synthetic cases:
   the IBIT plan now matches its actual position and shows 1/3 targets hit; the old
   closed round trip is confirmed back in "Executed Trades Without a Plan"; a second
   real case (IBB, plan written 64 days after the position was entered) matches
   correctly and correctly shows the wide-gap flag.

   **Known follow-ups from this fix, not yet built:** (a) the manual match-override UI
   only targets round trips, not open positions — if the wide-gap flag above ever turns
   up a genuinely wrong open-position match, there's currently no override, only editing
   the plan or waiting for the position to close; (b) open positions are looked up by
   symbol alone (`build_round_trips` groups executions by symbol only, with no account
   dimension — the raw Wealthsimple export's `account_id`/`account_type` columns are
   parsed but discarded) — the same symbol held in two accounts (e.g. TFSA + the
   retirement account CLAUDE.md's architecture table mentions) would silently collapse
   into one position. Neither is a regression from this fix; both are pre-existing
   limits of the symbol-only data model that this fix's review surfaced.
   **Both since resolved:** (a) by pyramiding support (2026-09-04, the Match modal lists
   open lots), (b) by multi-account support (2026-10-07, positions keyed by account +
   symbol + currency — see that section).

   **A second bug in this same date-gate logic, found and fixed 2026-08-31.** The
   open-position exemption from the date gate (a plan logged well after entry, like
   IBB's above, correctly matches while the position is open) didn't survive the
   position actually closing: once it becomes a round trip, matching re-applies the
   date gate meant to catch stale, unrelated candidates — and a plan created 65 days
   after its round trip's entry_time got rejected outright, falling from "Open (in
   progress)" straight to "Planned (no execution matched yet)" the instant the
   position closed. Confirmed live on the real IBB position (plan written 2026-08-27,
   position finally closed 2026-08-31). Fixed in `match_plans_to_roundtrips`: the date
   gate on a round-trip candidate is now skipped when it's the *sole* round-trip
   candidate for that symbol+direction — the same "nothing else to confuse it with"
   reasoning already used for open positions, just extended to cover the moment one
   becomes the other. The gate still applies whenever there's real ambiguity (more
   than one round-trip candidate), so the original IBIT fix's protection is untouched
   — verified with three isolated scenarios: the original multi-candidate IBIT case
   (old unrelated round trip still correctly rejected), the new IBB case (sole
   candidate matches despite the 65-day gap), and a genuine-ambiguity case (two stale
   round trips, correctly left unmatched).

   **A second, unrelated bug surfaced right after fixing the first one:** once IBB
   correctly showed as Closed, it then failed to appear in the Trade Journal table at
   all. Cause: the "Filter by Score Range" slider had a hard-coded `[-100, 150]`
   min/max — Plan Score (`actual_pnl / planned_reward_total * 100`) is unbounded above
   when the actual gain far exceeds a modest planned target, and IBB's real score came
   out to 391.65%. The slider couldn't select beyond its own fixed max, so this wasn't
   a narrow filter setting silently hiding the row, it was a permanent exclusion with
   no way to reveal it. Fixed via a new `update_score_filter_range` callback: the
   slider's own min/max now expand to cover whatever's actually in the journal data
   (with headroom beyond it), recomputed whenever `store-journal` changes, falling
   back to the familiar -100/150 default when nothing in the data needs more room.
9. On trade close, TAPEs scores the trade on two axes: **profitability** and
   **plan-adherence** (how closely actual execution followed the committed plan —
   entry, stop discipline, scale-out timing/levels).
   **Partially implemented.** Profitability (P&L, %, R-multiple, a blended Plan Score)
   is fully computed. Plan-adherence exists today only as qualitative execution notes
   (targets hit, stop breach/respect, entry deviation %) plus that same blended score —
   there is no separate, formal plan-adherence number yet. See Open items below.

   **Pre-trade risk/reward (implemented 2026-08-27, `app.compute_plan_risk_reward`).**
   Distinct from the Plan Score above: this is computed from the plan alone (entry,
   stop, targets, qty), before or regardless of execution. Max loss = full position
   stopped out at the initial stop; max reward = full position exiting at the single
   most favorable target (not the qty%-weighted blended target the Plan Score uses --
   that one models the realistic scale-out outcome, this one is the upper/lower bound
   on each side). Shown three places: a live-updating summary on the Add Plan form and
   Edit modal (recomputes as you type), a "Plan R:R" column in the journal table, and
   full detail (max loss/reward in dollars) in the trade detail view.
   **Superseded 2026-10-10:** the reward and R:R now follow the scale-out ladder
   (`compute_plan_ladder`). The form also shows a stop-out-at-each-stage table. See
   "Plan reward follows the scale-out ladder" below.

## Data Freshness (implemented 2026-09-03)

Wealthsimple has no API (see the Three-System Architecture above) — executions only
ever enter TAPEs via a manual CSV re-import, so the app can silently drift out of sync
with the real account with no visible signal that it's happened. A page-level banner
(right under the header, `render_data_freshness_banner_cb`) now surfaces the most
recent execution's `imported_at` timestamp and how long ago that was — green under
`DATA_FRESHNESS_CAUTION_HOURS` (24h), caution above it, warning above
`DATA_FRESHNESS_WARNING_HOURS` (72h). Deliberately doesn't account for weekends/
holidays (a Friday import checked Monday reads as "stale" even though nothing was
missed) — an acceptable simplification, since an occasional unnecessary caution is
better than a silent gap. The same freshness note is also appended directly onto the
Open Positions **portfolio risk banner** specifically, not just shown page-level: a
stale "OK" risk reading is worse than no reading at all, since it gives false
confidence exactly when accuracy matters most, so the feature most endangered by
staleness carries its own explicit caveat.

## Open Positions: Live R:R and Portfolio Risk (implemented 2026-09-02/03)

The Open Positions card has a manual **"Refresh Live Prices"** button (Yahoo Finance,
reusing the same fetch as the Weekly Review balance) — deliberately not automatic,
since fetching several symbols on every page load would be slow. Once refreshed, two
things appear:

**Current R:R (per position)** — the standard R-multiple convention (Van Tharp): current
price vs. entry, ALWAYS measured against the ORIGINAL planned risk (entry to *initial*
stop), regardless of whether the position is currently up or down. This was a deliberate
choice after a brainstorm about whether the risk leg should shift to the *current* price
once winning: it shouldn't, for two concrete reasons found while working through it —
(a) if risk were measured as current-price-to-stop, the ratio blows up toward ±infinity
as a losing position approaches its stop (a shrinking denominator), exactly when a
stable reading matters most; (b) if the stop has ratcheted to breakeven (a common state
after a target hit), reward and risk become the same number and the ratio degenerates to
a constant 1.0 no matter how far price has run. Using the fixed original risk avoids
both, and matches how closed-trade `r_multiple` already works elsewhere in this app —
"R" means the same thing whether a trade is open or closed.

**"Risk-Free" badge** — shown once the *current, ratcheted* stop (after any already-hit
target has moved it, replayed via the same `analyze_targets_and_stop` logic used for
closed trades and the Journal's "Open (in progress)" rows — not previously wired into
this card) is at or above breakeven. This is the industry-standard treatment for this
state: once a stop is at breakeven, practitioners generally describe the trade as
"risk-free" qualitatively rather than continuing to compute a formal ratio against it.

**Portfolio risk banner** — total dollar risk across open, non-cash-equivalent positions
(current price to the *current, ratcheted* stop — deliberately the ratcheted one here,
not the initial one used for R:R, since this metric answers "how much could I actually
still lose from here" and should reflect the real current protective level) as % of
total portfolio value (mark-to-market, including cash-equivalents in the denominator
since that's the whole portfolio). **Green** under `PORTFOLIO_RISK_CAUTION_PCT` (5%),
**Caution** above it, **Warning** above `PORTFOLIO_RISK_WARNING_PCT` (6%). A position
with no stop on record has its FULL current value treated as at risk, not excluded —
an unprotected position's downside is real and unbounded in principle, and silently
excluding it would understate the risk figure, defeating the point of a warning
threshold. Cash-equivalent positions are excluded from the risk numerator (no
directional risk) but not the portfolio-value denominator.

## Weekly Review (Coaching, implemented 2026-08-29)

The first piece of TAPEs's "Coaching" pillar: one row per week (`db.weekly_reviews`,
keyed on `week_ending`) holding a mark-to-market balance, a freeform user note, and a
freeform coaching note. Manually triggered — filled in when the user sits down for a
weekly review, not a scheduled/background job, matching how Wealthsimple data itself
only gets re-pulled at the end of a trading day rather than automatically.

**Balance is mark-to-market of open positions, not a true account balance.**
`qty_open × current price`, summed across every open position, using the existing
open-positions data plus a fresh price fetch. This is deliberate, not an oversight:
TAPEs has only ever ingested trade executions, never a full cash ledger (deposits,
withdrawals, dividends aren't tracked), so a genuine total account balance isn't
computable from what's in the database. The price fetch (`app.fetch_current_price`)
deliberately uses **Yahoo Finance only**, not the TradingView-Desktop-first path
`fetch_atr` uses for ATR — that path actively takes over the live TradingView chart
(changing its symbol/timeframe) for one deliberate action tied to committing a single
plan; cycling the user's live chart through every open position back-to-back just to
price a portfolio would be disruptive for no real accuracy gain, since Yahoo's daily
close is precise enough for a week-over-week balance trend. Week-over-week change is
computed on read (diffed against the previous week's row), never stored, the same
"derived, not persisted" principle the module docstring states for round trips/journal.
`fetch_current_price` drops any NaN row before taking the last close (fixed 2026-08-29
after a live NaN silently poisoned the whole portfolio sum via `NaN + x = NaN`);
`compute_mark_to_market_balance` also independently rejects a NaN price/qty per symbol
as a second line of defense, excluding just that symbol rather than corrupting the total.

**Not a direct LLM API call — deliberately.** The first design (2026-08-29, same day)
called the Anthropic API directly from TAPEs. The user redirected this after seeing it
built: they'd rather run one dedicated, ongoing external chat (in claude.ai or
wherever) for this specifically, pasting each week's generated prompt into it and
pasting the response back into TAPEs. This avoids a new API key/cost/dependency
entirely (`anthropic` was added to requirements.txt, then removed again the same day),
and the persistent chat thread gives the model its own conversational memory of past
weeks' discussions as a bonus on top of — not instead of — the precomputed trend
aggregates below, which stay the reliable, non-fuzzy source for pattern-spotting
regardless of how long that thread runs. Two copyable pieces, matching how a real
conversation would start: `WEEKLY_COACHING_SYSTEM_PROMPT` (a fixed constant, copied
once when starting the dedicated thread) and the per-week data prompt (regenerated and
copied fresh each week, via `dcc.Clipboard`).

**The per-week prompt is fed precomputed trend aggregates, not raw trade history.**
`app.compute_weekly_trend_context()` computes a small, fixed-size summary — win rate
and Plan Score by week, stop-breach ("exited contrary to plan") frequency by week,
psych-reading trend specifically on deviation-tagged observations, symbols that recur
in deviations/losses, and the FOMO/impatience-vs-Plan-Score split the psych scales
were built to support — regardless of how many weeks of history exist. This plus that
week's specific closed trades (`app._this_week_journal_rows`) form the entire prompt
(`app._build_weekly_review_prompt`). Dumping the whole raw trade history into the
prompt every week was explicitly rejected: cost and the odds of the LLM (or the user,
skimming) missing a real pattern both get worse forever as history grows, whereas
these aggregates stay the same size no matter how much history accumulates.

**Weekly holdings reconciliation against Wealthsimple (implemented 2026-09-03).** Since
Wealthsimple has no API, TAPEs can silently drift out of sync with the real account with
no visible signal (see also Data Freshness above). The per-week prompt now also includes
TAPEs's own open-position list (symbol/qty/avg price, trimmed the same way as the closed
trades -- not the full record) specifically so the user can paste a genuine Wealthsimple
holdings CSV export into the same coaching-chat message and have the LLM cross-check the
two. `WEEKLY_COACHING_SYSTEM_PROMPT` carries the standing instruction for this (checking
for a discrepancy by symbol, not flagging the untracked cash balance or a holding the
user has said is intentionally out of scope, and only running the check when a holdings
export is actually attached) so it doesn't need to be re-explained every week. First real
run of this (done by hand before the feature existed, validating the idea): comparing a
real `holdings-report` CSV against `list_open_positions` immediately caught one position
(XEI) with zero record in TAPEs at all -- confirmed intentional (not part of the account
XEI is managing in TAPEs, being considered for a transfer) rather than a bug -- and a
~0.27-share BIL rounding difference consistent with an not-yet-imported interest/dividend
event. A CSV export was deliberately chosen over a screenshot: exact figures, no
vision-reading ambiguity.

## Beta-Weighted Net Market Exposure (implemented 2026-09-03)

A second portfolio-level risk metric, alongside (not merged into) portfolio heat --
they answer different questions. Heat is "how much could I lose if every stop got
hit." This one catches positions that look diversified by ticker but are actually one
large correlated bet on the same market direction (several leveraged or single-theme
index/sector ETFs that move almost identically in a risk-off move despite different
tickers and "different sectors").

**Beta per position** (`app.compute_beta`): covariance(position daily returns,
benchmark daily returns) / variance(benchmark daily returns), computed directly from
realized price history (default: 60 trading days vs. SPY, both configurable) --
deliberately NOT yfinance's `info['beta']` (a fixed 5-year monthly regression with
inconsistent coverage, especially for smaller/leveraged ETFs) and deliberately NOT a
nominal-leverage special case for 2x funds (e.g. SSO isn't hardcoded to beta=2.0) --
computing from real price history naturally captures a leveraged fund's actual
trailing relationship, including volatility decay. Fetched via the same
`yf.download(..., period=..., interval='1d', progress=False, auto_adjust=False)` /
candidate-list-with-`.TO`-fallback / MultiIndex-flattening convention already
established by `fetch_atr_from_yfinance` and `fetch_current_price` -- no new
market-data pattern introduced.

**Bug found and fixed during verification, not just transient flakiness as first
assumed:** the first implementation aligned the position's and benchmark's return
series *by position* (last N values of each, independently sliced), on the theory that
two US-listed daily series over the same window would already line up -- explicitly
flagged in that code's own comment as "a reasonable approximation." Testing against
real data (`compute_beta('SSO')` vs SPY) surfaced a wildly wrong result (beta ≈ -0.015
for a 2x S&P 500 fund, which should be strongly positive near 2). The first diagnosis
was wrong: two immediate re-runs in fresh processes both returned the correct value
(≈1.97), which looked like classic transient yfinance flakiness and was reported as
such -- but re-running the exact same script again reproduced the *identical* wrong
value to 15 decimal places, which flakiness cannot explain (a genuine network glitch
would produce a *different* bad number each time, not the same one). The real cause:
SSO's price history was missing a single bar (2026-08-28) that SPY's history had, only
4 rows from the end of the window -- a real Yahoo Finance data gap, not corrupted data.
Positional slicing silently paired every day after that gap against the wrong calendar
date on the other side, corrupting the covariance for the whole tail of the window.
Fixed by aligning on the shared date index (pandas inner join on both series' closes)
*before* computing returns, so a gap on one side merges into a single multi-day return
on both sides instead of shifting the pairing -- still the same calendar period on
both sides, just occasionally a 2-day-equivalent return instead of two 1-day ones.
Verified against real market data post-fix: SSO ≈1.974, QLD ≈3.42, BIL ≈-0.003 (all
plausible for a 2x S&P fund, a 2x Nasdaq fund, and a T-bill ETF respectively).
**Lesson for any future code that compares two yfinance return series:** never assume
row-count parity between two symbols' histories; align by date, not position.

**Error handling:** an unresolvable symbol, insufficient history, or a fetch failure
raises inside `compute_beta`; callers (`get_cached_beta`, `compute_portfolio_beta_exposure`)
catch it and exclude that position from the total (recorded in an `errors` dict), the
same "don't silently treat a failure as zero" principle `compute_mark_to_market_balance`
already uses for price fetches. Cash-equivalent symbols are excluded entirely, before
any beta computation is attempted.

**Caching -- a deliberate deviation from established convention, flagged per the
request that raised this feature:** there is no caching mechanism anywhere else in
TAPEs -- ATR and current-price fetches are always live, with staleness handled purely
by a manual refresh button and no persistence at all. Beta gets a genuine persisted,
timestamped cache instead (`db.beta_cache`, keyed on symbol+benchmark+lookback_days,
via `db.upsert_beta_cache`/`db.fetch_beta_cache`), because it costs meaningfully more
than a single price fetch (a full lookback window of daily history for BOTH the
position and the benchmark, per symbol) and doesn't change intraday, so recomputing on
every click would be a lot of redundant network calls for a number that's still
correct from an hour ago. `app.get_cached_beta` reuses a cache entry younger than
`BETA_CACHE_MAX_AGE_HOURS` (24h) unless `force_refresh=True`; a failed computation is
cached too (`beta=None, error=...`) so a bad symbol isn't retried on every page load.

**Rate-limit mitigation, added after the above investigation:** a burst of several
`compute_beta` calls in quick succession (a full portfolio recompute makes 2
yfinance downloads per symbol) risks a truncated/rate-limited response that still
looks like valid data -- a more dangerous failure mode than a clean exception, since
nothing would otherwise catch a wrong-but-plausible number. `compute_portfolio_beta_exposure`
now pauses `BETA_FETCH_THROTTLE_SECONDS` (0.5s) between symbols, but only when a live
fetch actually happened (never between two cache hits). This is a mitigation, not a
guarantee -- yfinance's data quality is fundamentally best-effort, consistent with this
codebase's existing tolerance for that elsewhere (no retries exist for ATR/price either).

**Aggregation** (`app.compute_portfolio_beta_exposure`): sum of (market value × beta)
across open, non-cash-equivalent, priced positions. Reuses the exact `priced_symbols`
dict and portfolio total `compute_mark_to_market_balance` already produces -- "account
equity" is defined identically to portfolio heat, so the two metrics stay comparable.
Returns both a dollar figure and a % of portfolio value, plus `betas_as_of` (the
OLDEST computed_at among symbols actually used in the total -- the honest "as of" for
the aggregate as a whole, not the freshest contributor).

**No concentration ceiling or threshold is hardcoded**, per the request -- this is a
measurement tool first. The dashboard banner (`beta-exposure-banner`) is neutral
(`color='info'`), unlike the color-coded portfolio-risk banner next to it, since no
target number has been agreed on. A future threshold should be a configurable setting,
not a constant, if one is ever added.

**Surfacing:** a "Compute Market Exposure" button on the Open Positions card (manual
trigger, same as "Refresh Live Prices" -- requires it to have been clicked first, since
beta exposure reuses its staged prices rather than re-fetching them), and a new MCP
tool `get_beta_weighted_exposure(force_refresh: bool = False)` following the same
thin-wrapper/error-dict conventions as `fetch_current_atr`/`get_summary_stats` --
reads fresh from the database, including a fresh price fetch per position, rather than
depending on any browser-side Store.

**Verified end-to-end against the real database** (read-only against existing data;
the only write is to the new, additive `beta_cache` table): `get_beta_weighted_exposure()`
correctly priced and computed beta for all 7 non-cash-equivalent open positions
(CRAK, GLD, IGE, ITA, QLD, SSO, XLK), correctly excluded BIL as cash-equivalent, and a
second call reused the cache (no re-fetch) rather than recomputing.

**Non-goals for this pass, per the request:** beta-weighting the risk-to-stop (heat)
figure itself -- plausible future extension, not done here. Sector/theme tagging --
separate manual effort, not part of this task.

## Daily/Weekly Checklists, Help Pane, and Holdings Reconciliation (implemented 2026-09-03)

Built from a formal daily/weekly workflow doc and a "where each tool fits"/risk-framework
reference doc the user supplied. Three adjustments were proposed and accepted before
building (see that conversation for the full reasoning):

1. Checklist items that map to an existing TAPEs feature reference it by name rather
   than being inert text -- the checklist is meant to walk through the app, not
   duplicate what it already shows.
2. The static Help pane deliberately excludes any point-in-time figure (e.g. "heat is
   currently running X%", "N trades have no plan") -- those belong in memory/CLAUDE.md's
   evolving narrative or the live banners that already compute them fresh, never in a
   reference pane that would otherwise silently go stale the next time it's read.
3. The weekly workflow's "reconcile Wealthsimple against TAPEs" step, previously only a
   paste-into-the-external-coaching-chat cross-check (see Weekly Review above), got a
   real in-app CSV upload + programmatic diff instead, per the user's explicit choice --
   a meaningfully bigger addition than the checklists themselves.

**Daily/weekly checklists** (`app.DAILY_CHECKLIST_SECTIONS` / `WEEKLY_CHECKLIST_ITEMS`,
10 and 8 items respectively, matching the supplied workflow docs almost verbatim). Item
wording lives in these code constants, not the database -- a deliberate choice (over a
DB-editable list) since only *which ids are checked* needs to persist;
`db.daily_checklist_state` / `weekly_checklist_state` each store one
JSON-list-of-checked-ids row per date/week_ending (same "small JSON blob in one row"
convention as `plans.targets_json`, rather than one row per item). The weekly checklist
is embedded directly in the existing Weekly Review card, sharing its
`weekly-week-ending` date picker rather than adding a second one -- both are naturally
keyed the same way. The daily checklist gets its own card and date picker (defaulting
to today) near the top of the page, since it's a different cadence. Items with no TAPEs
feature to check against (regime read, Sniper dashboard, Options Walls sanity-check,
placing the order in Wealthsimple) are plain self-reported checkboxes -- none of those
three systems has a connector into TAPEs (see the Three-System Architecture table), so
these can never auto-check, by design, not an oversight.

**Page-wide Accordion restructuring (2026-09-03, same day, after the section
restructuring above):** the user's original "put the daily checklist's sections in a
dbc.Accordion" suggestion turned out to mean every full-width card on the page, not
just the daily checklist's internal sections. The 8 full-width cards -- Daily
Checklist, Daily Trading Journal, Weekly Reconciliation, Weekly Review, Draft Trade
Plans, Executed Trades Without a Plan, Data Management, and Trade Journal -- are now
each one `dbc.AccordionItem` (its former `CardHeader` becomes the item's `title`,
`CardBody` children become the item's body) inside one of two page-level
`dbc.Accordion` components (`accordion-block-1` covers the first four, in their
original page order; `accordion-block-2` the last four), both `always_open=True` so
several sections can stay open at once, matching the daily checklist's own internal
accordion behavior. Two accordions, not one, because the full-width cards aren't
contiguous on the page -- the "1. Add a Trade Plan" / "2. Import Executions + Open
Positions" paired two-column row sits between Weekly Review and Draft Trade Plans, and
splitting the accordion in two there preserves the existing page order rather than
reshuffling cards to make one continuous block. All items default to `active_item`
(expanded) on load, so the page looks the same as before the change until you actually
collapse something -- no card was judged more or less important than another. The
three side-by-side paired rows (Add Trade Plan/Import+Open Positions, Summary
Stats/Export, Trade Details/Price Path) deliberately stay as plain `dbc.Card`s, not
accordions, per explicit scope decision -- including Open Positions, despite it being
one of the longer cards on the page, since it's paired in a column rather than
spanning the full width. This was a purely structural move: every component id inside
each card's body is unchanged, so no existing callback needed touching.

**Daily checklist revised into three sections (2026-09-03, same day):** restructured
from a flat 8-item list into `DAILY_CHECKLIST_SECTIONS` -- "Pre-Market Open" (regime
check, review/adjust open positions and stops, push any plan changes into TAPEs),
"During Market Hours" (scan watchlist, sanity-check Options Walls) with "When a Trade
Is Found" as a conditional sub-group nested inside it (size/commit/place -- only
applies on days a qualifying setup actually appears), and "Post-Market Close" (import
today's Wealthsimple fills, confirm plans/fills matched) -- 10 items total, up from 8.
`app.DAILY_CHECKLIST_ITEMS` (the flat list used for overall progress counting and the
toggle callback's id lookup) is now *derived* from `DAILY_CHECKLIST_SECTIONS` via
`_section_all_items`, not hand-maintained separately, so the two can't drift out of
sync. Rendered as a `dbc.Accordion` (`_render_daily_checklist_accordion`, `always_open`
so multiple sections can stay open at once) per the user's own suggestion, so a
finished section can be rolled up without losing the others -- each AccordionItem's
title shows that section's own "n/total" progress. Pre-Market Open starts expanded by
default (the natural first thing to open each day); the other two start collapsed.
Checkbox ids are unchanged/reused where the item survived the restructuring (e.g.
`check_regime`, `size_trade`), so no migration was needed for the (essentially
nonexistent, same-day) existing checked-state data.

**Help pane** (`HELP_PANE_MARKDOWN`, a `dbc.Offcanvas` opened via a header "Help"
button): the "where each tool fits" map and the hard risk-framework limits (per-trade
cap, portfolio heat ceiling, correlation awareness, trend filter, progressive
de-risking, no-trade-without-a-plan, Wealthsimple-is-ground-truth), trimmed of the
point-in-time figures per adjustment #2 above. Offcanvas (slide-in side panel) rather
than a Modal, per explicit request, so it stays open alongside the page while working
instead of blocking it -- the first Offcanvas in this app (every prior popup was a
`dbc.Modal`).

**Weekly Wealthsimple Holdings Reconciliation** (new card, above Weekly Review):
`app.parse_wealthsimple_holdings_csv` parses a Wealthsimple *holdings* export (a
current-positions snapshot -- different shape from the *activity/fills* export
`parse_wealthsimple_csv` already handles) into symbol/qty, summed across accounts
(TAPEs's own open-position model has no account dimension either -- see the known
follow-up on this from the 2026-08-27 IBIT matching fix -- so this stays consistent
with what TAPEs can already represent) and with CURRENCY/cash rows dropped, matching
the same "TAPEs doesn't track cash" policy `WEEKLY_COACHING_SYSTEM_PROMPT` already
states for the chat-based version of this check. The real export has a trailing
blank line then an "As of `<timestamp>`" footer with a different column count than the
header, which pandas' C parser errors on -- trimmed off before parsing rather than
caught via exception handling.

`app.compute_holdings_discrepancies` cross-checks the parsed snapshot against
`store-openpos`, flagging three kinds: `untracked_in_tapes` (Wealthsimple has it,
TAPEs doesn't -- e.g. the real, already-known XEI case, held in an account TAPEs
doesn't manage), `untracked_in_wealthsimple` (TAPEs still shows it open, Wealthsimple's
export doesn't -- likely closed and not yet re-imported), and `qty_mismatch` (both
have it, quantities differ by more than `HOLDINGS_QTY_TOLERANCE`, 1.0 share -- loose
enough to swallow the kind of sub-1-share dividend-reinvestment drift already seen on
BIL, tight enough to catch a real mismatch). A per-symbol "Dismiss" button persists an
acknowledged, intentional gap (`db.dismissed_holdings_discrepancies`, same per-item
"display-only, never touches the underlying data" pattern as `dismissed_round_trips`)
so it stops re-flagging on every future upload without changing any of TAPEs's own
records. The diff itself is ephemeral (recomputed fresh on each upload into
`store-holdings-diff`, not persisted) -- consistent with the "derived, not persisted"
principle `db.py`'s own docstring states for round trips/journal.

This is a faster, exact complement to the existing LLM-based chat cross-check
(`WEEKLY_COACHING_SYSTEM_PROMPT`'s reconciliation instruction), not a replacement --
the chat path still adds value for qualitative pattern discussion across weeks; this
path is for a precise, immediate symbol/quantity diff without waiting on an external
conversation.

**Verified end-to-end against the real database and a real holdings export**
(downloaded 2026-09-03): `parse_wealthsimple_holdings_csv` correctly parsed all 9 real
positions (BIL, CRAK, GLD, IGE, ITA, QLD, SSO, XEI, XLK), correctly dropped the USD
cash row, and `compute_holdings_discrepancies` against real `store-openpos` data
correctly surfaced exactly one discrepancy (XEI, `untracked_in_tapes`) -- matching the
same XEI finding already documented above under Weekly Review, and correctly did NOT
flag BIL's known ~0.27-share rounding drift (within `HOLDINGS_QTY_TOLERANCE`). The
checklist DB functions were verified in an isolated scratch-dir test (round-trip
save/fetch, independence across dates, and the dismiss/undismiss round trip) rather
than against the real database, since there's no live external data involved to make
a real-DB test more convincing than an isolated one.

**Not yet click-tested in a real browser session**: the Help Offcanvas, both
checklists' checkbox toggling, and the holdings-CSV upload/dismiss flow. A full
in-process Dash server start (on a throwaway port) confirmed the page and the
`/_dash-dependencies` callback graph both load with no exceptions, which is as far as
this can be verified without a browser.

## Pyramiding Support (implemented 2026-09-04)

Lets a user hold TWO (or more) independently-planned open lots of the same symbol at
once -- e.g. an existing position plus a deliberate pyramid add, each with its own
entry/stop/target plan -- without ever going flat in between. Previously TAPEs assumed
at most one open position per symbol everywhere (`build_round_trips` blended every
still-open BUY fill for a symbol into one row), which surfaced when the user asked how
to book a second entry into an already-planned symbol. A full assessment (inventorying
every place that assumption lived) and a design review (which stress-tested and fixed
a real misattribution bug in the first draft of the splitting algorithm) preceded the
build; the approved plan is the basis for everything below.

**Lot-splitting algorithm** (`app.split_entry_fills_into_lot_buckets`): raw execution
data has no signal distinguishing "part of the original position" from "a deliberate
new pyramid tranche" -- both are just another BUY row. The only such signal is each
plan's own declared qty, so committed BUY plans for a symbol (2+ required; 0-1 plans
means nothing to disambiguate and `build_round_trips` behaves exactly as before) are
sorted by `planned_date`, and fills are greedily assigned to the earliest plan's
bucket until its qty target is reached (within `LOT_SPLIT_QTY_TOLERANCE_PCT`, 2% --
tight on purpose), then the next plan's. **Fails closed rather than guessing** when a
bucket doesn't cleanly close near its target before the next fill arrives (e.g. the
user bought less than a plan declared) -- qty alone can't tell "one lot, bought a bit
under plan" from "two lots, the first under-bought and the second's shares mistaken
for more of the first," and a naive loose-tolerance version of this algorithm was
proven to actually make that exact mistake during design review before any code was
written. On failure, the whole symbol's chain falls back to one blended lot, same as
pre-pyramiding behavior, with a manual override available (see below) if the user
wants to correct it by hand. The final plan in a chain is allowed to fall short of its
declared qty (nothing later to be ambiguous with) -- verified with 6 standalone
scenarios (clean split, undershoot-must-fail, spillover, last-plan-shortfall-ok,
middle-plan-skipped-must-fail, single-plan defensive None) before ever touching
`build_round_trips` itself.

**`build_round_trips(executions, plans=None)`** gained a `plans` parameter (all 6 call
sites updated) and now detects "chains" (maximal fill runs that don't return to flat
until their own end) in a first pass, then replays each chain a second time with
bucket-aware FIFO consumption -- needed because a plan's qty target might only be
satisfied by a fill that arrives after an interleaved partial sell earlier in the same
chain, so the whole chain must be known before any bucket can be assigned. **A single
sell can now span two lots** (e.g. it fully closes the older lot into a round trip
while trimming a newer one) -- handled by consuming lots FIFO exactly as before, but
merging same-bucket consumption within one sell into a single synthetic exit-fill
entry (a first draft that created one entry per LOT instead of per BUCKET was caught
during verification: it corrupted `round_trip_id`'s hash via duplicate execution ids
and didn't match the feature's own worked example). Every open-position row gets a
`lot_id` (sha1 of just its entry fills -- unlike `rt_id`, deliberately excluding exit
fills so it stays stable across partial sells while still open) and a
`deterministic_plan_id` (the plan whose bucket produced it; `None` for a blended/
spillover/fallback row, which still relies on the existing fuzzy matcher).

**Verified zero-regression against the real database**: old-vs-new `build_round_trips`
output is byte-for-byte identical for every symbol except BIL, where a genuine
pre-existing oversell data-quality quirk (a sell exceeding what tracked lots actually
held) was found -- the old code recorded such a sell's FULL nominal qty in its
exit-fill list even though only part of it could be matched against real inventory;
the rewrite only ever records what was actually matched, which is more correct and
affects only BIL (cash-equivalent, excluded from all scoring) in this dataset. The
full worked pyramiding example (two tranches, a sell spanning both, commission
apportioned by qty share) was verified end-to-end through the real function with
exact-value assertions, not just hand-traced.

**`match_plans_to_roundtrips` rekeying** (app.py): `used_open_symbols` -> `used_open_lots`,
candidate/match-id encoding carries `lot_id` not symbol. The open-position date-gate
exemption ("at most one per symbol, nothing to disambiguate") no longer holds, so it's
now identical for round trips and open lots alike -- exempted only when a candidate is
the SOLE one for its symbol+direction, a simplification rather than a new special
case. A lot or round trip carrying a `deterministic_plan_id` is force-assigned to that
plan before the fuzzy matcher runs at all (verified in a second pass after an initial
gap was caught: the first version only force-assigned OPEN lots, so a plan whose lot
had *already closed within the same build_round_trips call* fell through to the fuzzy
matcher instead of being marked deterministic -- fixed by applying the same
force-assignment to round trips' own `deterministic_plan_id`). The two regression
scenarios named in this function's own code comments (the 2026-08-27 IBIT multi-round-
trip disambiguation fix, the 2026-08-31 IBB 64-day-gap open-position exemption) were
re-run against the real database and still resolve correctly.

**Manual override for the residual-ambiguity case**: `db.plan_lot_matches`
(`plan_id` -> `lot_id`, exact mirror of `plan_matches`) plus `set_plan_lot_match`/
`clear_plan_lot_match`/`fetch_plan_lot_matches`. Reuses the EXISTING "Match" button/
modal on the Trade Journal (`match-modal`) rather than building a parallel one --
its dropdown now lists open lots (prefixed `open:`) alongside closed round trips, and
save/clear branch on that prefix to write to the right table. A lot that closes into
a round trip while a manual override still points at it would otherwise silently
orphan (the override table only ever describes the open phase) -- `app.
_reconcile_closed_lot_matches` (called from `_load_initial_state`, `import_csv`, and
the two affected MCP tools) detects this by recomputing a closed round trip's
lot-id-while-open equivalent from its `entry_fill_ids` and migrates the override into
`plan_matches` automatically.

**Three dormant bugs fixed as a prerequisite** (present in the code before this
feature, invisible only because there was never more than one row per symbol to
expose them -- each is the same root shape, a per-symbol dict overwriting instead of
accumulating):
- `compute_portfolio_risk`: read `qty_open` from the shared, symbol-keyed
  `priced_symbols` dict instead of from each position's own record; `plan_stops`/
  `per_symbol_risk` are now keyed by `lot_id` (stops genuinely differ per lot), not symbol.
- `compute_portfolio_beta_exposure`: same qty-source bug; `per_symbol` stays
  symbol-keyed but now ACCUMULATES market_value across lots rather than overwriting
  (beta itself is genuinely symbol-level, computed once per symbol regardless of lot
  count -- a different fix shape than portfolio risk's, not the same one applied twice).
- `compute_holdings_discrepancies`: `tapes_qty` now sums `qty_open` per symbol instead
  of keeping only the last lot's -- Wealthsimple's own export has no lot concept, so
  symbol-level aggregation is correct and sufficient for this comparison only.
- `compute_mark_to_market_balance`: stopped storing `qty_open` in `priced_symbols` at
  all (redundant and unsafe to share across lots); also now skips re-fetching a price
  for a symbol already priced earlier in the same call, avoiding a doubled network
  call once a symbol pyramids.
Each was written as a red test against synthetic two-lot data before the fix, then
confirmed green after, per the approved plan's verification requirement.

**UI**: Open Positions' row action buttons (`btn-open-observations`, `btn-log-open-plan`)
are now keyed by `lot_id`, not symbol -- with two lots sharing a symbol, symbol-keyed
pattern-matching ids would have been a real Dash duplicate-component-id conflict, not
just a UX ambiguity. `render_open_positions` resolves each lot's matched plan via
`store-journal` (already lot-aware) instead of `_plans_by_symbol`'s "most recently
created plan for this symbol" guess, falling back to that same guess only when a lot
has no committed-plan match yet (e.g. still a draft, where there's no better signal).
`open_observations_modal` no longer keeps its own separate, independent "most recent
plan" guess -- it resolves through the same journal lookup first.

**Verified**: `py_compile`, duplicate-callback checker, `serve_layout()` build, and an
in-process server start (hitting `/` and `/_dash-dependencies`) all clean. Not yet
click-tested in an actual browser session -- in particular, the extended Match modal's
open-lot dropdown, and clicking Observations/Log Plan on two lots of a real pyramided
position, have only been verified by calling the underlying functions directly, not
through the UI.

## Cash-Account close-out verified; a third matching bug found and fixed (2026-09-16)

User question: does Import Executions correctly handle a SELL whose proceeds land as
plain cash (not yet redirected into a cash-equivalent parking symbol like BIL)? Short
answer: yes, by design -- TAPEs has never tracked a cash ledger at all (see Weekly
Review above), so there's nothing for a "Cash Account" concept to represent; a SELL
just closes/reduces the position lot, and where the resulting cash goes next is
entirely outside TAPEs's data model, no special-casing needed. Verified by importing a
real two-row activity export (IGE 22 shares, SSO 60 shares, both plain SELLs with no
corresponding cash-equivalent BUY) into an isolated scratch copy of the real database
(never the production DB) -- both correctly closed their respective lots into round
trips with correct P&L, and the underlying open-position rows correctly disappeared.

**A third `deterministic_plan_id` matching bug surfaced during that verification,
same root shape as the two fixes below.** SSO's now-closed round trip stayed
"Planned (no execution matched yet)" instead of "Closed" even though the round trip
itself computed correctly. Cause: SSO was never pyramided, so its
`deterministic_plan_id` is genuinely `None` -- but in a DataFrame column shared with
OTHER rows that DO carry a real plan-id string (from actually-pyramided symbols),
pandas came back with `NaN` for the `None` rows instead of preserving `None`. Python's
`bool(float('nan'))` is `True`, so `match_plans_to_roundtrips`'s force-assignment
check (`if det_plan_id and ...`) treated NaN as a real plan reference, silently
"claiming" that round trip for a bogus plan whose id was literally NaN -- which
blocked SSO's actual plan from ever reaching the fuzzy matcher. Fixed by checking
`pd.notna(det_plan_id)` instead of a bare truthiness test, in both the open-lot and
round-trip force-assignment loops. Re-verified against the same scratch scenario
(SSO now correctly shows "Closed") and re-confirmed zero regression on the two
existing matching scenarios this function's own comments already call out (the IBIT
multi-round-trip case, the IBB 64-day-gap case) against the real production database.

## Two fixes found in first real use of pyramiding support (2026-09-04)

**Crash viewing an unmatched "Planned" trade** (`show_detail`): `build_journal_row`'s
default (no-match) branch never set `exit_fill_list` at all -- when `store-journal`'s
mixed list of matched/unmatched rows goes through `pd.DataFrame(...)`, pandas NaN-fills
any column missing from a given row when other rows in the list have it. The existing
`r.get('exit_fill_list') or []` guard didn't catch this because a NaN float is
truthy in Python, so `NaN or []` returns the NaN, not `[]` -- then `sorted(NaN, ...)`
raised `TypeError: 'float' object is not iterable`. Fixed by always initializing
`exit_fill_list: []` in `build_journal_row`'s default block (same pattern already used
for `lot_id: None`), so every journal row has a consistent type for this column and
pandas never has a gap to NaN-fill. This was a latent, pre-existing bug (not
introduced by pyramiding) that surfaced now because testing pyramiding naturally
produces more still-"Planned" (not yet matched) rows to click into.

**Target Qty % must sum to 100** (`app._validate_target_qty_pct`, called from both
`add_plan` and `save_edit` -- draft saves included, not just commits, since silently
allowing an incomplete scale-out ladder into a draft just defers the same problem):
every target row with a price set now requires a Qty % (share of the *original*
position size to exit there), and across all such targets they must sum to exactly
100 (small floating-point tolerance) before a plan can be saved. `commit_draft_plan`
(promoting an existing draft to committed) needed no separate check -- it never edits
targets, so a draft's ladder was already validated when it was first saved or edited.
**Found during verification: 6 of the real plans already in the database (GDX, GLD,
IBB, IGE, ITA, XAR) have target prices with no Qty % at all** -- pre-existing data,
untouched by this change, that will keep displaying/scoring exactly as before, but
editing and re-saving any of those specific plans (even to change an unrelated field)
will now be blocked until their targets' Qty % values are filled in or those targets'
prices are cleared.

## Daily Plan-vs-Actual Drift Report (implemented 2026-09-05)

Wealthsimple has no API -- fills only reach TAPEs via manual CSV re-import -- so price
can move past a target, or even through the stop, before the corresponding order is
placed or filled and shows up in TAPEs. This report checks each open lot's plan
against LIVE price (not just what's been executed so far) specifically to catch that
drift. Meant to be run each morning before market open, alongside the Daily Checklist's
existing "review open positions" step.

**`app.compute_plan_drift`** reuses `analyze_targets_and_stop`'s own nearest-to-entry-
first `target_results` and execution-based ratcheted stop (`actual_stop`) rather than
re-deriving the same sort/ratchet logic -- it only adds a second, price-only pass over
whichever targets weren't hit by a real fill, checking if live price has reached them
anyway (ratcheting `should_be_stop` and reducing `should_be_qty_open` further where it
has). **This closes a real, confirmed gap in `analyze_targets_and_stop` itself**: its
own stop-breach check only ever evaluates against the *last exit fill's* price, so a
position with zero exits so far -- the exact case this report matters most for --
never got checked against its stop at all before this. Degrades correctly with no
target ladder defined at all (`should_be_qty_open` stays the full original size, but
the stop-breach check against `initial_stop` still runs, which is the single most
valuable check on its own).

**MCP tool `get_plan_drift_report()`**: reads fresh from the database (a fresh
`fetch_current_price` per open lot's symbol, matching `get_beta_weighted_exposure`'s
own "reads fresh, can take a few seconds" precedent), reusing the same journal-based
lot-to-plan resolution `render_open_positions` already relies on -- skips cash-
equivalent symbols and lots with no committed plan matched (nothing to compare
against). A price-fetch failure for one symbol is reported per-lot
(`{lot_id, symbol, error}`) rather than dropping that lot silently, same "don't let
one bad symbol wreck the whole result" principle used everywhere else in this app.

**UI**: a new card directly below Open Positions (`btn-check-plan-drift` /
`store-plan-drift`), mirroring "Compute Market Exposure"'s button/store/guard pattern
exactly -- reuses the already-staged `store-open-positions-prices` rather than
fetching its own (unlike the MCP tool, which has no browser session to read a staged
store from). Rows with an actionable finding (a live stop breach, or price already at
the next target) sort first and are color-coded (danger/warning, with a colored
left-border accent) -- quiet, on-plan rows sort last -- since this is meant to be
scanned quickly each morning, not read top-to-bottom uniformly.

**Results open in a large (`size='xl'`, scrollable) modal (`drift-modal`) rather than
inline in the card** (revised same day, per explicit request) -- the card's own column
width couldn't comfortably show every column across several open lots at once. Each
row also gets a **"Reconciled" checkbox** (`{'type': 'drift-reconciled-check', 'index':
lot_id}`) so a lot can be checked off once its Wealthsimple order has actually been
placed/updated to match the report; a reconciled row dims (`opacity-50`) so what's
left to handle stays visually obvious. Checked state persists to
`db.daily_drift_reconciled` -- one row per DATE holding a JSON list of reconciled
lot ids, the same "small JSON blob in one row" shape as `daily_checklist_state` --
deliberately keyed by day, not just lot_id: the report is re-checked against live
price fresh every morning, so yesterday's checkmark on a still-open lot must not
silently carry over and hide today's re-check of the same position. The modal header
shows a running "`n`/`total` reconciled" count (`_drift_progress_text`), and toggling a
checkbox re-renders the row list immediately (not just the store) so the dimming
applies right away rather than waiting for the next full recompute.

**Verified end-to-end against the real database, both the MCP tool and the UI
callback**: correctly lot-aware (IGE appears as two independent rows, its two real
pyramided lots from the pyramiding-support work, each with its own stop/target/qty),
and **surfaced a real, live finding during verification**: GLD's live price (406.77)
had already dropped below its should-be stop (412.08) with zero drift on the
executed-quantity side -- exactly the scenario this report exists to catch. `qty_drift`
can legitimately go *negative* (IGE, ITA both did) when a position was already scaled
out further than today's lower live price alone would suggest -- expected, not a bug,
since real execution reflects whatever price was in effect at the time, not today's.

**Part 2 (resetting TradingView alerts at the current stop/next-target levels) is
deliberately out of scope for TAPEs's own code.** These can only be TradingView
alerts -- Wealthsimple has no alert API, and this project's whole design keeps
Wealthsimple execution manual and unautomated. More fundamentally: **TAPEs itself (a
standalone Flask process) cannot call TradingView MCP tools** -- only a Claude Code or
claude.ai session can reach MCP; TAPEs's own TradingView access is a separate, direct
CDP connection used only for reading chart data (ATR), never the alert-creation UI.
This is the identical fork already called out for the not-yet-built "draw the
committed plan on the chart" feature (see workflow step 6 below): duplicate
TradingView's alert automation into TAPEs's own CDP client, or have a Claude Code
session read this report and drive the *existing* `alert_create`/`alert_delete`
TradingView MCP tools directly. The latter was chosen -- no new code, just a daily
manual workflow: **run `get_plan_drift_report()` via the TAPEs MCP connection, then
for each lot with `stop_breached_live` or `next_target_reached`, call
`mcp__tradingview__alert_create`/`alert_delete` to reset that symbol's alert to
`should_be_stop` and `next_target_price`.** A local scheduled task (Windows Task
Scheduler triggering this same Claude Code prompt automatically each morning) was
considered and explicitly deferred -- a cloud-scheduled routine can't reach either
localhost service (TAPEs's MCP server or TradingView Desktop's CDP port), so any real
automation here would have to run on the user's own machine, which is new
infrastructure this project doesn't have today.

## Configurable ATR Ladder, Trailing Stop, Journal Status Filter, Pre-Trade Risk Check (implemented 2026-09-22)

Five related requests, resolved via `AskUserQuestion` before building (each answer was
the recommended option): configurable R-multiples for the ATR-derived ladder (not
auto-filling Qty%/stop_after too); a trailing stop that activates only after a chosen
target is hit, trailing by an ATR(14) multiple; a pre-trade risk check that's
informational only, never blocks Save Draft/Commit; plus a straightforward Trade
Journal status filter. Planned via Plan Mode; the user corrected the first trailing-stop
draft mid-review ("This should be an option alternative. The current implementation
should also be available") before approving -- see that item's own note below.

**1. Configurable R-multiples per target.** A new `r_multiple` key alongside
`price`/`qty_pct`/`stop_after` in each target dict -- round-trips through
`targets_json` with no schema migration (just another whitelisted key in
`db._clean_targets`). Each target row on the Add Plan form / Edit modal gets an "R×"
input (`{prefix}-t{i}-rmult`), defaulting to the existing `np.linspace(1, 6,
MAX_TARGETS)` spread so a plan that never touches these fields computes byte-for-byte
identical stop/targets to before. `_auto_apply_stop_targets` (inside
`register_atr_callbacks`) now reads these as both Input (editing R× recomputes that
target) and takes a per-slot fallback to the same default spread when a field is left
blank. Verified directly against the real registered Dash callback (not a reimplementation):
confirmed identical output to the old hardcoded spread when all R× fields are blank,
and confirmed a custom R× on one target only recomputes that target's price.

**2. Trailing stop -- a per-plan, OPT-IN ALTERNATIVE to the ladder, not a replacement.**
`trailing_stop_enabled` defaults to `0`/off; every existing plan and every new plan
that never touches the new checkbox behaves exactly as before, zero change. This was
an explicit user correction mid-planning: the first draft read as a two-stage
replacement of the ladder-only model, and the user clarified both mechanisms must stay
permanently available side by side.

New plan columns (`db._migrate`, same `if 'col' not in cols: ALTER TABLE` idiom as
every other column there): `trailing_stop_enabled`, `trailing_activate_after_target`
(1-based, nearest-to-entry order -- matches `target_results`' own ordering in
`analyze_targets_and_stop`), `trailing_atr_multiplier`. Form UI lives inside
`atr_block(prefix)` (shared by Add Plan and Edit modal): a switch, an "Activate after"
T1-T5 select, and a "Trail ATR ×" input.

`app.compute_trailing_stop_level(symbol, direction, since_date, atr_multiplier)`:
running favorable price extreme (high-water mark for a long, low-water mark for a
short) since `since_date`, pulled back by `atr_multiplier` × a fresh ATR(14). Yahoo
Finance only (`start=since_date` rather than a fixed lookback period) -- same
"many symbols in one pass, don't hijack the live chart" reasoning as
`fetch_current_price`. Verified against real market data (SPY): both directions
produce a level correctly pulled back from the raw running high/low, not equal to it.

`compute_plan_drift` gained optional `trailing_config`/`trailing_level` params (stays
pure -- never fetches network data itself; the caller pre-fetches and passes in).
Internally tracks a `live_reached[i]` flag per target (already-hit OR live-price-
reached, nearest-first order) and, once configured: if the activation target is
actually hit, `actual_stop` is overridden to `trailing_level`; if it's hit OR merely
live-reached, `should_be_stop` is overridden too (both converge once truly active --
they only differ when live price has reached the activation target but no real fill
has happened yet). A new `trailing_active` boolean in the return dict flags this for
callers/UI. Verified with 4 scenarios: no trailing config (baseline unchanged),
trailing configured but activation target not yet reached (ladder value unchanged,
`trailing_active=False`), only live-reached (only `should_be_stop` overridden), and
actually hit via a real exit fill (`actual_stop` also overridden) -- all exact-value
asserted.

`app._fetch_trailing_level_for_row(row, pos)` (row = a journal row, which already
carries every plan-level field via `dict(plan)`; pos = the matched open-position/lot
record) is the shared caller-side helper used by both `get_plan_drift_report` (MCP
tool) and `compute_plan_drift_cb` (UI callback) -- fetches
`trailing_level` unconditionally whenever a matched plan has `trailing_stop_enabled`
(accepted minor inefficiency before the activation target is reached, in exchange for
much simpler caller code). `since_date` = the activation target's own `matched_time` if
actually hit by a real fill, else today's date (an explicit approximation: there's no
real historical anchor for a target that's only should-be-reached via live price, not
executed). A fetch failure is returned as an error string, never raised -- the caller
degrades to ladder-only behavior for that lot rather than crashing or blocking the rest
of the report, same "don't let one bad symbol wreck the whole result" principle used
everywhere else in this app. Verified: real market data confirms the mechanics work
correctly when data is available; a deliberately-forced fetch failure confirms
`compute_plan_drift` falls back cleanly to the pure ladder value with `trailing_active`
staying `False`, never crashing and never silently applying a stale/bad level.

**3. Drift report UI update.** Once trailing is active for a lot, `actual_stop`/
`should_be_stop` already reflect it automatically (no restructuring needed) -- a small
"(trailing)" badge is added next to Should-Be Stop in `_render_drift_rows` (UI) and a
`trailing_active` boolean in `get_plan_drift_report`'s per-lot dict (MCP), purely so the
user knows WHY a given number is what it is. Verified zero-regression against the real
database: `get_plan_drift_report()` runs cleanly on all 4 real open, plan-matched
lots (QLD, XLK, CRAK, ARKG), every one correctly `trailing_active: False` since no real
plan uses trailing yet.

**4. Trade Journal status filter.** A `dbc.Checklist` (`filter-status`, defaulting to
all three statuses selected) next to the existing Symbol/Score filters;
`render_journal` filters `df[df['status'].isin(status_filter)]` alongside the existing
filters. Smallest, independent piece -- built and verified first.

**5. Pre-trade risk check -- informational only, never blocks Save Draft/Commit.**
New constant `PER_TRADE_RISK_CAP_PCT = 2.0` alongside the existing
`PORTFOLIO_RISK_CAUTION_PCT`/`WARNING_PCT`. The existing ratcheted-stop-per-lot
computation, previously inline in `render_open_positions`, was extracted into
`_compute_ratcheted_stops_by_lot(data, plans, journal)` -- reused by BOTH
`render_open_positions` (its own risk banner, unchanged) and the new check below, so
"existing portfolio risk" means the same number in both places rather than two
independent computations of it. Verified byte-for-byte identical against the real
database (5 real open positions, 15 real journal rows): both `ratcheted_stops` and
`risk_free_lots` matched exactly between the old inline algorithm and the new extracted
helper.

`compute_pretrade_risk_check(direction, entry, stop, qty, portfolio_value,
existing_total_risk_dollars)`: this trade's own risk (reuses
`compute_plan_risk_reward`'s `max_loss`, doesn't recompute it) as a % of portfolio
value, flagged against the per-trade cap; and the portfolio's projected total risk (%)
if this trade is added, flagged against the existing caution/warning thresholds.
New `html.Div(id='plan-pretrade-risk')` on the Add Plan form (right after Quantity, a
separate callback from `_update_risk_reward` since that one is prefix-generic/shared
with the Edit modal, and "portfolio impact of a NEW trade" doesn't apply to editing an
already-open position) -- same "Click Refresh Live Prices first" guard as every other
banner that depends on `store-open-positions-prices`. Verified with synthetic
scenarios: a trade within both caps (no warning), a trade whose own risk alone exceeds
the per-trade cap, and a trade that's fine alone but pushes projected portfolio heat
over the warning threshold -- confirmed correctly flagged in each case, and confirmed
this is purely informational (the function only returns data; nothing in the UI wiring
disables either button under any of these conditions).

**Full-suite verification throughout**: `py_compile` after every change,
`serve_layout()` build checks, and in-process server starts (hitting `/` and
`/_dash-dependencies`) at 4 checkpoints across the build. A scratch-DB migration test
confirmed both a fresh `init_db()` and an old-shaped DB via `_migrate` pick up the 3 new
trailing-stop columns. The live dev server running on port 8050 at the time of this
work (PID captured 2026-09-18) predates all of it -- **needs a restart** to pick up any
of these five items.

## Callback-registration bug fixed: portfolio-risk banner silently broken (2026-09-22)

Found immediately after the previous feature was reported complete: the user's next
message was a live callback error, `_compute_ratcheted_stops_by_lot() takes 3
positional arguments but 6 were given`. Cause: extracting that helper out of
`render_open_positions` had inserted its `def` line directly between the
`@app.callback(...)` decorator and `render_open_positions`'s own `def` -- Dash bound
the decorator (6 Inputs) to the WRONG function (a plain 3-arg helper), so the Open
Positions card's entire render callback silently threw on every trigger. This is
exactly the kind of ordering mistake `py_compile` and `serve_layout()` can't catch
(the module imports fine; only actually building the Dash callback graph and firing
a request would surface it) -- caught here only because the user reported the live
error, not by this session's own verification steps. Fixed by moving the decorator
back to sit directly above `render_open_positions`; confirmed via
`app.app.callback_map` that `open-positions.children` is now bound to
`render_open_positions` and `_compute_ratcheted_stops_by_lot` is a plain, callable,
undecorated helper. **Lesson for future refactors that extract a helper from a
decorated callback function: always verify the decorator ends up directly above the
ORIGINAL function's `def` line, not the new helper's** -- an Edit tool insertion
between a decorator and its function is easy to make and easy to miss by inspection
alone; checking `app.callback_map` (or an in-process server hit on the actual page,
not just `/` and `/_dash-dependencies`) is the only way to catch it directly.

## Currency labels on all price/extended-price displays (implemented 2026-09-23)

Request: "anywhere a price or extended price is reflected it is displayed in a
currency" (explicitly NOT position-sizing/qty/R-multiples/percentages/ratios). A full
inventory (via a research subagent) found ~20 display sites across the app, roughly
half already carrying a `currency` value in scope but not showing it, the other half
having no currency source at all because **plans had no `currency` column** --
currency was previously only ever known via a matched execution (open position/round
trip), so a brand-new, uncommitted plan was fundamentally currency-less. Resolved two
design forks with the user before building: pre-trade prices get a real, explicit,
user-set **Currency field on the plan itself** (not just a best-effort display-only
guess), and portfolio-level dollar aggregates that sum across possibly-different-
currency positions (mark-to-market balance, portfolio risk banner, beta exposure) get
a **"(mixed currencies, not FX-converted)" caveat** rather than a misleading single
currency label -- no FX conversion was built, this is a labeling/honesty fix only.

**Schema**: `plans.currency` (`db._migrate`, same idiom as every other column there).
Existing plans backfilled best-effort from the most recent execution for that same
symbol (`SELECT currency FROM executions WHERE symbol = ? ORDER BY timestamp DESC
LIMIT 1`); a plan whose symbol has never been executed stays NULL rather than
guessing. Added to `PLAN_FIELDS`/`insert_plan`'s INSERT (defaults to `'USD'` if
omitted entirely). Verified in an isolated scratch DB: fresh `init_db()` has the
column; an old-shaped DB with a real XEI execution correctly backfills that plan to
CAD while a plan on a never-traded symbol stays NULL. **Verified against the real
production database**: all 16 real plans correctly backfilled to USD (matches
CLAUDE.md's own note that XEI -- the one CAD symbol in this portfolio -- has zero
plan record in TAPEs at all, so nothing here should have picked up CAD).

**Form UI**: a `dbc.Select` "Currency" (USD/CAD) added next to Symbol/Direction on
both the Add Plan form and Edit modal. `app._guess_symbol_currency(symbol, openpos,
executions)` pre-fills it on the Add Plan form only (checks the symbol's current open
position first, then its most recent past execution, else leaves the field at its
plain UI default rather than asserting a guess as fact) -- same "auto-fetch but always
editable, never silently trusted" precedent as the ATR auto-fetch. The Edit modal
does NOT re-guess; an existing plan's currency populates from the plan itself, like
every other field. `prefill_plan_from_roundtrip`/`prefill_plan_from_open_position`
("Log Plan" from Executed-Without-a-Plan / Open Positions) prefill the Currency field
directly from the matched round trip's/open position's own real `currency` --
more authoritative than the guess heuristic, since these flows already know it.

**Bug found and fixed as a byproduct of wiring this through `add_plan`**: while adding
the currency parameter, found that `add_plan`'s own `plan` dict construction was
**missing `trailing_stop_enabled`/`trailing_activate_after_target`/
`trailing_atr_multiplier` entirely** -- the callback correctly READ these three
values as inputs but never wrote them into the dict passed to `db.insert_plan`, so a
brand-new plan created with trailing stop enabled would silently save as
`trailing_stop_enabled=0` regardless of what the user set on the form. This was a
latent bug from the trailing-stop feature's own build (2026-09-22, same session,
before compaction) -- `save_edit` had these fields correctly, `add_plan` did not.
Fixed alongside this feature since both touch the same dict construction.

**`build_journal_row`'s default `'currency': None`** (for a plan with no execution
match yet, status "Planned") **changed to fall back to `plan.get('currency')`** --
previously an unmatched plan showed no currency ANYWHERE, even though it's now always
set (or at least defaults to 'USD'). Matched plans still get the more-authoritative
execution-confirmed currency from the round trip/open position, overwriting this
default, exactly as before.

**Display sites fixed** (currency now shown, sourced from `row['currency']`/
`rt['currency']`/`op['currency']`/`plan.get('currency')`/`pos.get('currency')` --
already-available data that simply wasn't being surfaced): the Trade Journal table's
entry/exit price columns (P&L already had it); the full trade detail view
(`show_detail`) -- planned/actual entry, initial/final stop, max loss/reward, P&L,
the target-ladder table, and the price-path chart's hover text and y-axis title; the
Daily Plan-vs-Actual Drift Report, both the UI table and the `get_plan_drift_report`
MCP tool (current price, actual/should-be stop, next target -- the single biggest
win, since `pos['currency']` was already sitting in scope at both call sites and
simply never added to the result dict); the Match modal's round-trip/open-lot
dropdown option labels; the Draft Trade Plans table's entry price; and the Add Plan
form's own Max Loss/Max Reward summary (`_update_risk_reward`, shared with the Edit
modal) and pre-trade risk banner (`_update_pretrade_risk`'s "this trade's risk"
dollar figure) -- both newly possible now that a real plan-level currency field
exists. ATR's own display (`_auto_fetch_atr`'s status note) also labeled, since ATR is
itself a price-unit (dollars/share) value; `fetch_current_atr`'s MCP return gained a
`currency_guess` field using the same heuristic. `analyze_targets_and_stop`'s
low-level `stop_note` text (no currency concept in that function itself, by design --
it has no currency param and many callers) gets its currency prepended at the ONE call
site that surfaces it to the user (`build_journal_row`'s closed-trade branch, where
`rt['currency']` is already in scope) rather than threading a new param through a
foundational function used by 6+ call sites.

**Mixed-currency aggregates** (per explicit user decision, not a labeling oversight):
the Open Positions portfolio-risk banner, the Weekly Review balance/change column, and
the beta-exposure banner all sum potentially-different-currency positions with no FX
conversion (this portfolio genuinely holds XEI, a CAD-listed ETF, alongside mostly-USD
positions) -- each gained an explicit "(mixed currencies, not FX-converted)"-style
caveat rather than a misleading single currency prefix; the hardcoded `$` sign on the
weekly balance and beta-exposure banners was removed for the same reason. This matches
the pre-existing "(mixed currencies)" convention `_compute_summary_stats` already used
for `total_actual_pnl` -- extended to the newer aggregates rather than introduced fresh.
`get_summary_stats`'s and `get_beta_weighted_exposure`'s MCP docstrings gained the same
caveat in words, matching the UI.

**`create_plan` MCP tool** gained an optional `currency: str = None` param -- when
omitted, guesses the same way the form does (open position, then trade history, then
`'USD'`). Verified in an isolated scratch DB (never the production database) that both
an explicit currency and the omitted/guessed path insert correctly.

**A real mistake made and caught during this session's own verification**: an early
test called `app.create_plan(...)` twice after reassigning `db.DB_PATH` to a scratch
path, on the assumption that would redirect the write -- exactly the gotcha this
project's own persisted memory already warns about (`db.insert_plan`'s `db_path=
DB_PATH` default is bound at function-definition time, so reassigning the module
attribute afterward does NOT redirect an already-defined function's default
argument). Both test plans landed in the REAL production database. Caught immediately
by re-checking `db.fetch_plans()` against the real DB, and both were deleted via
`db.delete_plan` before doing anything else; verified the real DB was restored to
its original 16 plans, all with their correct backfilled currencies, before
continuing. All further `create_plan`/`insert_plan` verification for this feature was
redone with `db_path` passed explicitly to every call, touching only the scratch file.

**Deliberately left unchanged**: the "Executed Trades Without a Plan" DataTable
already has a separate `Currency` column alongside its Entry/Exit/P&L numeric columns
-- satisfies the same goal without needing per-cell string concatenation in a
DataTable (harder to do cleanly than in a `dbc.Row`/`html.Span` layout). No FX
conversion was built anywhere, per the user's own scoping of this request to display
only.

**Full-suite verification**: `py_compile`, `serve_layout()`, and an in-process server
start all clean. Verified against the REAL database (read-only except for the
create_plan mistake above, which was corrected): all 16 real plans backfilled to USD;
all 16 real journal rows carry a non-null currency; `get_plan_drift_report()`'s 4 real
entries and `_render_drift_rows`' rendering of them both correctly show `USD`
throughout. `_guess_symbol_currency`, `_update_risk_reward`, and `_update_pretrade_risk`
were each called directly through their actual registered Dash callback functions
(via `app.callback_map`, not reimplementations) with both synthetic and real-shaped
inputs.

## Drift report price-formatting bug fixed (2026-09-24)

Reported right after the currency-labeling work above: the Drift Report showed prices
"in the incorrect format." Cause: `_render_drift_rows` (UI) and `get_plan_drift_report`
(MCP tool) formatted/rounded current_price, actual_stop, should_be_stop, and
next_target_price with `.4g` (4 significant figures) rather than the `.2f`/`round(x,
2)` convention used for money everywhere else in this app (Open Positions, the Trade
Journal, `show_detail`, max loss/reward, etc.) -- `.4g` gives an inconsistent number of
decimal places depending on the price's magnitude, and drops the decimal point
entirely or switches to scientific notation once the integer part reaches 4+ digits
(confirmed: `194.85` -> `"194.8"`, `1042.335` -> `"1042"`, `12345.6` ->
`"1.235e+04"`). This was a **pre-existing bug from the original Drift Report feature**
(2026-09-05), not something introduced by the currency-labeling work -- it just became
more visible once currency prefixes made the price column get a second look. Fixed
both call sites to use `.2f` (UI) / `round(x, 2)` (MCP), leaving the QUANTITY fields
(`should_be_qty_open`, `actual_qty_open`, `qty_drift`) on their existing `.4g`/4-decimal
convention, matching how quantities are formatted elsewhere (they're share counts, not
prices). Verified against the real database: all 4 real drift-report entries (QLD,
XLK, CRAK, ARKG) now render with consistent 2-decimal prices (e.g. "USD 92.30", not
"USD 92.3" or "USD 92"); `py_compile`, `serve_layout()`, and an in-process server start
all clean.

## Drift report shows the 14-day ATR (2026-11-02)

An "ATR(14)" column sits after Price (Qty Drift went from 2 grid columns to 1), and
`get_plan_drift_report` adds `atr_14d`, plus `atr_14d_error` if the fetch fails.
- **Always daily,** whatever timeframe the plan uses, from completed bars only
  (`drift_daily_atr`, via `fetch_daily_history`), so it can differ from `atr_at_commit`.
- **Fetched once per listing per report,** so pyramided lots don't double up.
- **A failed fetch** shows "—" and a warning; it never blocks the row.

Tests: `DriftReportAtr` in `tests/test_trailing_stops.py`. Checked with a headless-Chrome
screenshot and the real report (8 lots, USD and CAD).

## Stop review: Wealthsimple pending orders vs the drift report (implemented 2026-11-05)

**Why:** TAPEs couldn't see the stop actually placed in Wealthsimple (no API). The user now pastes a
screenshot of Wealthsimple's *Pending orders* list into the Drift Report modal each day.

**Reading the image:** Windows' built-in OCR (`tools/winocr.ps1`, run through Windows PowerShell 5.1;
PowerShell 7 can't load WinRT). It's local, with no new dependency, service or cost.
- **Pipeline:** `ocr_image_lines` → `parse_pending_orders` → `orders_from_screenshot`.
  - `parse_pending_orders` groups lines into rows by the symbol lines ("SYM $price (chg%)"). Each row
    takes the account label under the symbol, "Q x $P CUR" and the order type.
  - `resolve_order_account` matches nickname/type, and tells two LIRAs apart by which one holds the symbol.
- **Real screenshots:** all 5 orders read exactly on both.
  - **The second one (2026-11-06) needed a parser fix.** Windows joined each order and its type into one
    line ("44 X $43.59 USD Stop limit sell"), used a capital X, and split a currency ("U SD").
  - **The fix:** `_ORDER_LINE_RE` is no longer anchored at the end, and the type is read from the rest
    of the line.
- **Paste:** `assets/stop_review_paste.js` catches Ctrl+V while the modal is open and hands the image to
  `store-stop-review-paste` (`dash_clientside.set_props`). `dcc.Upload` takes drops and file picks.
- **Table:** orders land in an editable table (`stop-review-table`), merged without repeats
  (`merge_placed_orders`), to correct before checking.

**Rules agreed with the user:**
- **Comparison level:** against the report's *Set Stop To* (`should_be_stop`: the trail, ladder and entry
  floor), never the plan's initial stop. The tolerance is half a cent.
- **Stop limit orders (2026-11-06):** the user confirmed the list shows a stop limit's *limit*, not its
  stop. So the parser puts it in `limit` and leaves `stop` empty, and the table has a Limit column.
  - **Judged by the limit:** a sell's stop is at or above its limit. So a limit at or above *Set Stop
    To* matches (`stop_is_bound`), while one below it asks for the stop, typed from the order's details.
  - **A typed stop** is checked exactly.
  - **Notes:** every stop limit gets a note that it won't sell on a gap below the limit, which the gap
    allowance assumes it does. Price below the limit gets a "won't fill" warning.
- **Tighter is fine:** a stop above *Set Stop To* is a note and still counts as matched.
- **Stop sells only:** other order types are listed as "not checked".
- **Saved per day:** `db.daily_placed_orders` holds (obs_date, orders_json, all_matched, summary).

**Check** (`compare_placed_stops`, per account + symbol + currency):
- **Issues:**
  - a stop below *Set Stop To* ("raise by");
  - a position with no stop order;
  - a stop order with no position;
  - shares different from the shares held;
  - an unrecognised account;
  - a plan-matched lot missing from the report.
- **Notes:**
  - a stop at or above the shown price ("likely sells at the open");
  - a pyramided position, checked against its highest lot stop;
  - no plan to check against.
- **On Check stops** (`check_stops_cb`):
  - saves the orders;
  - ticks *Reconciled* for matching lots (never unticks a hand tick);
  - shows "placed X" under each drift row's *Set Stop To*;
  - adds a summary line to the daily journal's drift pre-fill (`placed_stop_summary`).

**Self-ticking checklist item:** a daily checklist item with `'auto': 'stop_review'` can't be ticked by
hand (disabled, with a "ticked by the stop review" badge).
- **Ticking:** Check stops ticks it when every stop matches (`stop_review_all_matched`) in the All
  accounts view, and unticks it otherwise.
- **Hand ticks:** `apply_auto_ticks` keeps its saved state against them.
- **The user's item:** "Run the Drift Report and update Wealthsimple" (`item_28bfefe8`) was flagged in the
  saved definition. The editor keeps the key, but "Reset to defaults" drops it.
- **Stale-tab fix (2026-11-09):** Check stops always ticks *today's* checklist, but only redraws the page's
  checklist when its date picker shows today. A tab opened on Saturday still showed 2026-11-07, so the tick
  looked missing (it was saved for 11-09), and the user's own ticks that morning went to 11-07.
  - **Rollover:** `checklist-day-tick` (a 60s `dcc.Interval`) plus `store-checklist-day` (the page's load
    day). `roll_daily_checklist_date` moves the picker to today when it's still on the load day
    (`rolled_checklist_date`); a date picked on purpose is left alone.
  - **Note:** Check stops' checklist note names today's date when the page shows another day.
  - Tests: `test_checklist_date_rolls_over_to_a_new_day` and the other-date note, in `tests/test_stop_review.py`.

**MCP:**
- **`get_checklist(name='daily'|'weekly', date=None)`:** the list with each item's section, sub-heading,
  `checked` and `ticked_by_stop_review`, plus `dates_with_ticks`. For daily it adds that day's
  `stop_review` (`db.fetch_stop_review`).
- **`get_plan_drift_report`:** adds `placed_stop`/`placed_qty`/`placed_stop_ok`/`placed_stop_issues` from
  today's saved review.

**First real result (2026-11-05):**
- IBB matched.
- Four stops were below *Set Stop To*: TAN 41.97 vs 43.59 (still the plan's initial stop), ZXLV 36.44
  vs 36.74, XLU 42.32 vs 42.46, TLT 80.22 vs 80.26.

**Verified:**
- `tests/test_stop_review.py` (31 tests): parsing the real screenshot's OCR lines, accounts, every issue,
  storage, the callbacks with OCR and writes stubbed, the self-ticking item and `get_checklist`.
- The full suite passes (378); 128 callbacks, 0 arity mismatches.
- **Clicked in a real page** (headless Chrome on 9333, the app on 8061, writes stubbed): Ctrl+V of the real
  screenshot, Check stops, the drift rows' placed lines and the disabled, unticked item.
- `get_checklist` was called over the real `/_mcp` endpoint.

## Regime adherence: each buy vs the regime at that day's open (implemented 2026-11-06)

**Why:** the user's main concern is keeping to the regime set before the session. Intraday
regime changes are treated as noise (they swing trade), so only the regime in force at the
open counts.

**Which regime governs a fill** (`regime_at_open`): the newest daily or weekly entry already
**saved** before 09:30 ET on the fill's day, as it stood then (a weekly entry wins a tie on
date).
- **Why not the entry dated that day:** every daily entry so far was first saved at
  4–7 pm ET, after the close. That makes the entry dated D the regime for D+1's open.
  Checking a fill against its own day's entry would use a regime written after the trade.
  Example: the XLB/ZXLV adds on 11-05 are governed by the 11-04 evening entry (Partial
  Risk-Off limits, Neutral tactics), not by 11-05's Full Risk-Off saved that evening.
- **Fill timestamps are Eastern wall clock** labelled `+00:00` (IBB's stop sell at the open
  reads `09:30:01+00:00`), so the date is taken as written and never converted.
- **Edit history (user's choice):** `db.regime_versions` records each save that changes an
  entry's effective limits or tactics, from the daily journal and the weekly entry. An edit
  saved after an open never changes that day's check.
  - Entries saved before this existed were seeded with one version each at their first save.
    `edited_at` is set when they were edited later. A fill whose open falls between those two
    times is noted "edited after this open" (only 10-29's entry, edited 11-01).
  - Deleting a daily entry deletes its versions.

**Checks per buy-day** (`regime_adherence`; sells never break a regime rule and aren't
checked):
- **Tactics** (`regime_buy_tactic_issue`): Full Risk-Off allows no buys; a scale-out entry
  needs Sniper; a day trade needs Day; a campaign add needs Pyramid (probe-only tactics flag
  it). The probe is fine wherever probes or Pyramid are allowed.
- **Per-trade risk:** the position's risk after that day's buys, to the initial stop
  (campaigns: the scheduled stop for the stage reached), plus the gap allowance. It's
  measured against the limits state's cap (half for a day trade).
- **Max position:** cost of the shares held.
- **Base:** both are a % of the **latest saved weekly balance in CAD** on or before that day
  (user's choice), with USD converted at the newest `fx_rates` rate on or before it.
  0.005 points of rounding room.
- **Notes:** the regime was 2+ sessions old; edited after the open; no weekly balance (size
  not checked).
- **Unchecked:** buy-days with no version saved before their open (before the journal began on
  2026-10-23) aren't checked. A trade with only such days has no badge.

**Where:**
- `add_regime_checks` runs in `_build_journal_and_unmatched` and sets `row['regime_check']` and
  `row['regime_breaches']`. Journal rows now carry `entry_fill_list`, and `analyze_campaign`
  returns `buys` (with stages, trims netted) and `exits`.
- **Trade Journal:** "✓ Regime" / "⚠ Regime n" under the status.
- **Detail view:** a "Regime at the open" section, one line per buy-day.
- **Summary Statistics:** a "Regime at the open" row.
- **Weekly prompt:** `regime_checks_this_week`, explained in the coaching system prompt. The
  external chat's copy of that prompt needs re-pasting.
- **MCP:** `get_regime_adherence(since, until, account_id, breaches_only)`; `get_journal` rows
  carry `regime_check`.
- **Help pane:** the Trade Journal line.

**Real data (2026-11-06):** 11 trades checked across 14 buy-days, with 0 breaches. Every probe
and add was small (risk ≤ 0.08%, position ≤ 2.5%), and every one was a campaign or day trade
under Neutral tactics.

**Verified:** `tests/test_regime_adherence.py` (21 tests: the governing-entry rules incl. evening vs
pre-market saves, edits after the open, weekly ties, staleness and seeded edits; tactics, risk,
position, gap allowance + USD conversion, the day-trade half cap, campaign adds; unchecked days;
storage, seeding and weekly versions on scratch DBs; summary, weekly list, MCP tool, detail
section). Full suite 403; 128 callbacks, 0 mismatches. All 36 real detail views render; a
headless-Chrome screenshot of the journal and the XLB detail. The additive migration has
seeded the real DB's `regime_versions` (11 daily + 2 weekly). **Needs a restart.**

## Practices scorecard (implemented 2026-11-06)

**Why:** the user separates the *Strategy* (what's allowed, `claude-project/Strategy.md`) from
*practices* (how it's executed), and wants reporting that reinforces the practices. They're
measured from the Wealthsimple fills, reported and never enforced. They're deliberately not
checked on the Add Plan form, since a plan is written in advance and isn't an execution.

**Fill times:** the export's `effective_date` + `effective_time` are the actual fill times,
confirmed by the user. `parse_wealthsimple_csv` already reads them. They're Eastern wall clock,
labelled `+00:00` once parsed. Buys at 06:20–08:57 ET are real pre-market fills (QLD, GLD, USO,
ARKW, MCD, EWZ, CRAK, SSO), which matters when an entry-window practice is defined.

**Practices** (`PRACTICES`; each counts from its `start`, so a trade is only scored against
practices in force when it was made; all start 2026-10-23, the journal start, per the user):
- **Trade within the regime at the open** (per buy-day): from `regime_check` (see Regime
  adherence below).
- **Regime set before the open** (per trading day with a buy, deduplicated across trades): the
  regime in force at the open exists and is at most 1 session old (the previous evening's or
  that morning's entry). `regime_adherence` now also returns `unchecked_dates`, so a buy-day with
  no regime counts as an exception here.
- **Plan committed before the trade** (per trade): `committed_at` (UTC; else `created_at`) is
  before the first fill (a campaign's first buy, trims netted). A draft created earlier doesn't
  count. MCD (committed 09:36, filled 07:00 on Oct 21) is the only real case, and it's before the
  start.

**Added 2026-11-07** (results are True/False, or None = unknown, which is listed but never
counted):
- **No buys before 10:00 ET, 9:45 for a day trade** (`entry_window_items`, per buy-day;
  `ENTRY_WINDOW_START`, `DAY_TRADE_ENTRY_START`): the user's FOMO practice. Checks every buy:
  entries, probes and adds. Starts 2026-10-23.
- **Day trades closed by 4:00 pm ET** (`day_trade_flat_item`, `DAY_TRADE_FLAT_BY`):
  - Every exit is on the entry day before 16:00.
  - A day trade still open on its own day reads unknown; on a later day it's an exception.
  - The user set 16:00. Strategy 4.5's time stop is 15:45, and its 14:30 entry cut-off isn't
    checked.
- **Add protected at its stop** (`add_protection_items`, per add day, starts 2026-11-04 because
  stop reviews began 2026-11-05):
  - Uses the first stop review saved after the add's last fill (`db.fetch_stop_reviews`,
    `checked_at` UTC → ET), only if it's from the add's day or the next session. Otherwise the
    add is unknown.
  - **Passes when:** stop sells for that account + symbol + currency, at or above the stop
    scheduled for the stage reached, cover every share held at review time. An order's level is
    its stop, else its limit (a stop sits at or above its limit).
  - **Not listed:** a position sold before the review. The real XLB and ZXLV Nov 5 adds are both
    this case.
  - An add's result is `reported_on` the review's date.
- **Daily-journal pre-fill** (`practice_prefill`, in `load_daily_journal` for today's new entry):
  - EXECUTION gets that day's exceptions by `reported_on`, or "Practices: all followed (n
    checks)".
  - With nothing on record, it says "No buys on record for <day> (last import: …). If you traded,
    import the activity export first", since TAPEs can't tell a quiet day from one not yet
    imported.
  - It needs that day's activity imported before the entry is written. The Drift Report isn't
    involved.
- `practice_scorecard` now returns `unknown`. `practices_vs_results` and the counts treat only
  `ok is False` as a break.

**Real data with these (2026-11-07):**
- **Entry window:** 3 of 14 buy-days pass, with 11 exceptions. Nearly every probe and add came at
  09:17–09:54, and EWZ's day trade at 08:57.
- **Day trades flat:** 1 of 1.
- **Practices vs results:** the 5 closed trades that broke a practice averaged +2.00R with 20%
  wins (EWZ's +13.2R dominates); 3 followed every practice (n/a).

**Where:**
- **Practices card:** an accordion item under Summary Statistics (`practices-accordion`, open by
  default, remembered), rendered by `render_practices_cb` for the Account view.
  - Each practice shows this week (Monday to today), the last 4 weeks and since start, with its
    exceptions in an `html.Details`.
  - **Practices vs results:** closed trades that followed every practice vs those that broke one,
    gated at `SUMMARY_MIN_SAMPLE`.
- **Journal:** "⚠ Plan after fill" badge.
- **Detail view:** a "Plan before the trade" line (`render_plan_first`).
- **Weekly prompt:** `practices` (`practices_for_prompt`), explained in the coaching system prompt.
- **MCP:** `get_practice_scorecard(today, account_id)`.
- **Help pane:** a line describing the card.

**Real data:** 14 of 14 buy-days, 5 of 5 trading days and 9 of 9 trades, with 0 exceptions. The
8 closed trades since the start average +1.48R, with 38% wins.

**Verified:** `tests/test_practices.py` (17 tests as of 2026-11-07, incl. the entry window, day-trade flat, add protection and pre-fill; first 6: EDT/EST commit-vs-fill, the created_at and
campaign-buy fallbacks, per-practice items incl. day dedup and the start date, scorecard windows,
the results gate, the callback, prompt, MCP and detail line). Full suite 409; 129 callbacks, 0
mismatches. Headless-Chrome screenshot of the card on real data.

**Test-harness lesson:** `.venv\Scripts\python.exe` is a launcher that starts a child Python, so
killing the port's listener left stale test servers serving old code on 8061. Stop test servers by
command line (`Win32_Process` where CommandLine like `*scratchpad*stop_server.py*`), and never touch
the user's own `app.py` processes.

## Lessons, the Improvement register and encouragement (implemented 2026-11-07)

**Why:** the user wants to encourage finding lessons (from each trade and each day) and
continuous improvement. A survey of the data found:
- Every daily entry had a lesson line, but only 7 of 36 trades had one.
- The lessons mixed trading lessons, Strategy questions and TAPEs feature ideas.
- Two lessons had already become real changes with nothing showing it: Oct 23 became the gap
  allowance, and Nov 5 became the stop review.

Agreed with the user:
- **Keep/change format:** each lesson is written as two halves, so "what went right" is
  captured too and "nothing to change" is a fine answer.
- **Streaks:** the user finds them motivating.

**Keep / change lessons:**
- **Trades:** new `plans.lesson_keep` ("What worked (keep)"). The existing `lessons_learned` is now
  "What to change"; the user's existing lessons were almost all changes, so nothing was moved.
  - Edit modal: two side-by-side boxes (`edit-lesson-keep`, `edit-lessons-learned`).
  - Detail view: `render_trade_lessons`.
- **Daily journal:** new `daily_journal_entries.lesson_keep` (`dj-lesson-keep`). `lesson` is now
  "What to change / proposal". It still feeds the weekly entry's lesson decisions unchanged.
- **Placeholders:** `lesson_text` treats "None", "n/a", "-", "nothing to change" etc. as no lesson.
- **Prompts:**
  - `trade_needs_lesson`: a "Lesson?" badge in the journal on trades closed from
    `LESSON_PROMPT_START` (2026-10-23) with neither half written. The real ones: KRE, XLB, IBB,
    ZXLV, ITA and IWM.
  - `closed_without_lesson_text`: a reminder added to today's EXECUTION pre-fill.

**Improvement register:** the "Improvement register" button on the Practices card opens
`improvement-modal`.
- **What's listed:** every "what to change" lesson from a trade or a daily entry, newest first,
  with its keep half (`improvement_rows`).
- **Storage:** `db.improvements`, keyed (source 'trade'|'day', source_ref plan_id|obs_date). It
  holds only category, status, link ("Became…"), note and adopted_on; the text stays in the plan
  or the entry. A row exists once classified.
  - Category: practice / strategy / tapes / observation.
  - Status: new → adopted → verified, or dropped. `save_improvement` sets `adopted_on` the first
    time a lesson is adopted or verified.
- **UI:** tabs New / Adopted / Verified / Dropped / All with counts (remembered). Per row: a type
  select (sentinel 'unset', since `dbc.Select` shows '' as blank), a status select, Became and a
  note.
  - **Save changes** writes only rows that differ from what's stored (`save_improvements_cb`).
  - Each card has a coloured left edge by status.
- **Seeded on the real DB:** day 2026-10-23 → adopted 2026-10-24, "Strategy v1.1: stop gap
  allowance"; day 2026-11-05 → adopted 2026-11-05, "TAPEs: daily stop review". The other 14 are
  new.
- **MCP:** `get_improvement_register(view)` (with counts and the encouragement figures) and
  `set_improvement(key, category, status, link, note)`.

**Encouragement:**
- **The strip** (`encouragement_summary` → `render_encouragement`) is a green line on top of the
  Practices card with:
  - the daily-lesson streak, current and best (`lesson_streak`);
  - closed trades that followed the plan (`followed_plan_flags`), including how many were losses
    ("good trades anyway");
  - changes made from your own lessons (adopted + verified, and this month's).
- **Lesson streak:** consecutive NYSE trading days (`NYSE_HOLIDAYS`, 2026–27) whose entry has
  either half. It stays alive until a trading day is missed, so a weekend or an unwritten today
  doesn't break it.
- **Streak column** on the Practices card: each practice's current and best run
  (`practice_streaks`; unknowns are skipped).
- **"✓ Followed the plan"** on closed journal rows: every buy scored by the practices, no
  exception, stop not breached. Set as `row['followed_plan']` in `_build_journal_and_unmatched`.
- **Weekly prompt:** `lessons_and_improvement` (`lessons_for_prompt`) carries this week's closed
  trades with keep/change and followed_plan, the open register items, this week's adoptions and
  the encouragement figures. `_daily_journal_this_week` adds `lesson_keep`.
  - The coaching system prompt now says encouragement comes first: open with what went well
    (specific, not generic), call a loss that followed the plan a good trade, ask for missing
    trade lessons, and suggest what open register items should become or be dropped. The
    external chat's copy needs re-pasting.

**Real data (2026-11-07):**
- Lesson streak 5 (best 5); 3 of 8 closed trades followed the plan (1 a loss); 2 changes adopted.
- Practice streaks: regime at the open 14, regime set 5, plan first 9, entry window 0 (best 1),
  day trades flat 1.

**"Became" links (2026-11-07, replacing the free-text link):** what an adopted lesson turned into
is a list in `improvements.became_json`, several per lesson, alongside Type (kept, per the user).
- **Kinds:** a practice (`PRACTICES` id), a Strategy version (parsed from Strategy.md's change log by
  `strategy_versions`), a focus action (`A<n>`), or a TAPEs feature (typed; ';' separates several).
  In the modal: one multi-select (`imp-became`, `became_options`) plus a "TAPEs" box (`imp-tapes`).
- **Required:** Save refuses (nothing saved) while an adopted or verified lesson has no link;
  `set_improvement(became=[...])` does the same and validates each reference.
- **Evidence** (`became_evidence`): a practice's results since `adopted_on` (n of N, exceptions,
  current run), an action's status and misses, a version's date. Shown under each row, in
  `get_improvement_register` and in the weekly prompt's new `adopted_awaiting_verification`; the
  coaching prompt asks the coach which look ready for Verified (re-paste the setup message).
- **Migration:** an old `link` moved into the note ("Became: …"). The real DB's two adopted lessons
  were then set by hand: Oct 23 → Strategy v1.1 + TAPEs gap allowance; Nov 5 → TAPEs daily stop
  review (backup `trade_journal.backup-pre-became-20261107.db`, taken after the reloader had
  already run the migration, so it has the moved notes).
- **Tests:** `Became` in `tests/test_lessons.py`; checked with a headless-Chrome screenshot.

**Action register is editable (2026-11-07), as well as the weekly entry.** Each row has a status
select and a reason box; "Save changes" (`save_action_register_cb`, pure `action_register_changes`)
records each changed action via `db.review_weekly_action` dated **today**, and refuses (nothing saved)
a not done / dropped without a reason.
- **Review window** (`action_window_start`, `in_review_window`, `reviewed_in_window`): a review counts
  toward week W's entry when it's after the previous weekly review's week_ending and on or before W.
  This replaced "reviewed_week == W" in `actions_for_week`, `render_action_rows` (shows "reviewed <date>
  in the action register"), `weekly_prompt_blocker`, `actions_for_prompt` and `get_weekly_journal`'s
  `actions_reviewed` (now from the history, with `reviewed_on`).
- **No double entries:** saving the weekly entry skips an action already reviewed in the window with the
  same status and note.
- **Edge:** a register change made on the same date as a saved weekly entry counts toward that entry.
- **Tests:** `RegisterUpdates` in `tests/test_weekly_entry.py`; click-tested on 8061 (writes stubbed).
- The daily checklist (TAPEs and `claude-project/Daily and Weekly Checklists.md` v1.1) was aligned with
  the Practices the same day and gained "Review the weekly action register's open items".

**Generate Prompt needs the weekly entry (2026-11-07):** `weekly_prompt_blocker` refuses until the
week's entry is saved and every carried action (`actions_for_week`) has a status this week.
Tests: `PromptNeedsTheEntry` in `tests/test_weekly_entry.py`.

**Not built yet (agreed order, step 4):** before/after measurement for adopted lessons, once they
have a few weeks of data.

**Verified:**
- `tests/test_lessons.py` (15 tests): placeholders; lesson prompts and the pre-fill reminder;
  register rows, counts, storage incl. `adopted_on`, keep-column migrations and the save/render
  callbacks; run, practice and lesson streaks incl. weekends, the Thanksgiving holiday and the
  grace day; followed-the-plan flags; the encouragement summary and strip; both MCP tools; the
  daily form round trip.
- Full suite 435; 132 callbacks, 0 mismatches.
- Headless-Chrome screenshots of the card and the register, which caught and fixed the blank
  type select and an all-sides border.
- **Incident:** the user's running TAPEs (debug mode, auto-reload) exited while these edits were
  in progress, because a reload picked up a half-written file. It needs a manual restart.

## Psych model v2: three single-dimension axes + trade triggers (implemented 2026-09-29)

**Why:** the user found the v1 sliders insufficient, and the root flaw was structural:
a bipolar slider only works if both ends are the same dimension. v1's Fear↔FOMO
paired two unrelated emotions, and Complacent↔Impatient overlapped heavily with FOMO.
Designed with the user in a brainstorm before building.

**Axes** (`app.PSYCH_AXES`, each -2..+2):
- **Risk emotion**: Fearful ↔ Overconfident, 0 = Calm is ideal. The positive end is
  deliberately *over*confidence -- confidence itself is healthy, not a failure mode.
- **Urgency**: Complacent ↔ FOMO, 0 = Patient is ideal (absorbs v1's "Impatient").
- **Focus**: Fatigued/distracted ↔ Sharp, 0 = neutral, **higher is better** (the user's
  explicit choice over a "Wired/overstimulated" opposite end). Being Sharp never counts
  against you.
- **Distortion index** (`psych_distortion_index`, 0-6): `|risk| + |urgency| +
  max(0, -focus)`. This is the single number correlated against outcomes.
- "Discipline" was considered as an axis and deliberately **not** added: it isn't a state
  felt at entry, and TAPEs already measures it objectively (stop breaches, targets hit,
  entry deviation), so it's the *outcome* the states are scored against.

**Trade triggers** (`app.TRADE_TRIGGERS`, multi-select per the user's choice, entry-only):
Planned setup (the baseline), Chased a breakout / late entry, Revenge / make-back after
a loss, Boredom / wanting action, News / tip / social-driven. These are motives, not
feelings, so they're categories rather than sliders. On the Add Plan form, a
**revenge-trade hint** (`compute_revenge_hint`) warns if the most recent closed,
non-cash-equivalent round trip was a loss within `REVENGE_HINT_LOOKBACK_DAYS` (3),
including the current loss streak. It's informational only and never ticks the box.

**Where:** the same three sliders appear on the Add Plan form, the Edit modal, and
mid-trade/exit observations (per the user); triggers are on the Add Plan form and Edit
modal only. The `create_plan` MCP tool accepts optional `psych_risk`/`psych_urgency`/
`psych_focus`/`trade_triggers`, validated.

**Schema:** `plans.psych_risk/psych_urgency/psych_focus` (INTEGER, **no default**),
`plans.trade_triggers_json` (JSON list, exposed as `plan['trade_triggers']`, same shape
as `targets_json`), and the three psych columns on `plan_observations`. NULL means "not
rated", so pre-v2 rows are never mistaken for a genuine neutral reading. **Legacy v1 data
is kept, not converted:** `psych_impatience`/`psych_fomo` stay as-is, since a v1 reading
can't be mapped reliably (a -1 on Fear↔FOMO could belong on either new axis). Only 6
plans + 6 observations had non-neutral v1 readings when this shipped. Detail views show
them labeled "Legacy reading" (`psych_summary_text` falls back automatically).
`save_edit` only writes the v2 axes if the plan was already v2-rated or a slider was
actually moved, so editing an unrelated field on an old plan doesn't silently stamp it
"rated neutral".

**Scoring** (`compute_psych_performance`, feeding `compute_weekly_trend_context['psych']`):
- Per-axis low/mid/high splits.
- Distortion calm (0-1) vs. elevated (2+).
- Per-trigger stats (multi-select, so one trade counts under each of its triggers).
- The same axis split using the deviation-tagged observation's reading.
- Weekly deviation psych averages.
- Each bucket reports win rate, avg Plan Score, avg R, **stop-breach rate, and
  early-exit rate** (exited before the first target without a stop breach). This tests
  the directional hypothesis that fear predicts early exits and overconfidence/FOMO
  predict stop breaches.
- Averages are withheld (`insufficient_sample: True`) until a bucket reaches
  `PSYCH_MIN_SAMPLE` (5) trades.
- v1-rated trades are excluded from all of these stats, and a `model_note` in the output
  says so.
- `WEEKLY_COACHING_SYSTEM_PROMPT` explains the axes, index, triggers, the directional
  hypothesis, and to say so rather than over-read an `insufficient_sample` bucket. The
  per-week rows carry a readable `entry_psych` string, the distortion index, and triggers.

**Verified:**
- Scratch DBs, with `db_path` explicit on every call:
  - An old-shaped DB migrates with its legacy values untouched and v2 columns NULL.
  - Plan v2 fields and triggers round-trip through insert, fetch, and `update_plan`.
  - Observation v2 fields round-trip.
- Unit-tested:
  - The distortion index: Sharp isn't penalized, and legacy/NaN rows read as unrated.
  - `compute_psych_performance` on a synthetic 13-trade set, with exact-value
    assertions: legacy exclusion, multi-trigger counting, the small-sample gate, and the
    breach/early-exit rates.
  - `compute_revenge_hint`: cash-equivalents are ignored, loss streaks counted, and the
    lookback window respected.
- The real registered callbacks (`add_plan`, `open_edit_modal`, `save_edit`,
  `add_observation`) were called via `app.callback_map` with DB writes **stubbed**, so
  none touched the real DB. Return tuples match declared outputs (37/46/3/7), and the
  legacy-plan no-silent-stamp rule holds.
- Read-only against the real DB:
  - The trend context reports 0 rated trades, which is correct since nothing is
    v2-rated yet.
  - `show_detail` renders all 16 real journal rows.
- Checks in order: `py_compile`, then `serve_layout()`, then an in-process server with
  84 callbacks. The server's `/_dash-layout` contains every new component id and none of
  the old ones.
- The additive migration already ran against the real DB (importing `app` calls
  `init_db()`). Confirmed read-only afterward: all 16 plans, 12 observations, and 6+6
  legacy readings intact, new columns NULL.

**Not yet click-tested in a real browser.** Needs a restart of the running app.

**Slider layout fix (2026-10-07).** The axis words used to be slider marks. A mark is
centred on its tick, so the end words overran the Notes modal: "Fearful" and "Fatigued /
distracted" spilled past the left edge, and "Overconfident" was clipped.
`psych_sliders_block` now does three things:
- It puts the words in a three-column row above each slider.
- It uses -2..+2 numbers as the marks.
- It sets `allow_direct_input=False`, hiding Dash 4's number box, which narrowed the
  track so the words didn't line up.

The Notes modal (`observations-modal`) is now `size='lg'`. The fix was verified with
before/after headless-Chrome screenshots of the modal. That's the first real visual
check in this project: `chrome.exe --headless=new --screenshot` works on this machine,
against a throwaway preview app on another port.

## Stop-exit slippage tolerance: respected / slippage / breached (implemented 2026-10-03)

**Problem:** `stop_breached` was true whenever the final exit filled even a fraction of a
cent past the final ratcheted stop. A normal stop-out that filled slightly through its
level was counted the same as holding well past the stop. That inflated the weekly
`stop_breach_rate_pct` and `recurring_symbols_in_deviations`, and put false "⚠ Off-Plan
Exit" prompts in the journal.

**Rule** (`classify_stop_exits` + `analyze_targets_and_stop`, closed trades only):
- Every exit fill not taken at a target is checked against the **stop in effect at that
  fill's time**, and the worst one decides the outcome. Previously only the final fill
  was checked, against the final stop.
- The stop in effect is the initial stop, raised by each target's `stop_after` once that
  target's fill has happened, and replaced by a trailing level once its activation
  target is hit (`trailing_config`/`trailing_level` params).
- **The journal doesn't pass a trailing level today.** Closed trades have no historical
  trailing level to replay, so the journal is ladder-only, same as before. No real plan
  uses trailing yet.
- Tolerance is `STOP_SLIPPAGE_ATR_MULT` (0.1) × `atr_at_commit`. When there's no ATR
  (None/NaN/0), it falls back to `STOP_SLIPPAGE_PCT_FALLBACK` (0.1)% of the stop price.
- The check is direction-aware: below the stop is the bad side for BUY, above for SELL.
- `stop_outcome` is `respected | slippage | breached` (None when there's no stop or no
  exits).
- `stop_breached` is kept for backward compatibility and is True only for `breached`.
- `analyze_targets_and_stop` now returns a 7-tuple; all 7 callers were updated.
- exec_notes wording for the "respected" case is unchanged. Slippage reads "Final exit at
  51.58 hit the stop (51.59) with 0.005 ATR slippage, within tolerance (0.10 ATR)".
  Breach reads "... breached ... by 0.779 ATR, beyond the 0.10 ATR slippage tolerance".
  The note starts with "Exit at" rather than "Final exit at" when the worst fill isn't
  the last one.

**Downstream:**
- `stop_breach_rate_pct` (overall and per week) and `recurring_symbols_in_deviations`
  count only breaches.
- New `stop_slippage_count`/`stop_slippage_rate_pct` (overall), plus
  `stop_slippage_rate_pct` per week and per psych bucket.
- `_outcome_stats`' `early_exit_rate_pct` now excludes slippage, so a stopped-out trade
  is never counted as a discretionary early exit.
- The journal shows "Stopped out (slippage)" instead of the Off-Plan Exit prompt.
- This week's coaching rows carry `stop_outcome`, and `WEEKLY_COACHING_SYSTEM_PROMPT`
  tells the coach slippage isn't a discipline problem.

**Unchanged by design:**
- Plan Score and R-multiple never depended on the breach flag.
- `get_plan_drift_report`'s `stop_breached_live` stays strict with no tolerance, since
  it's a real-time alert. A test pins this.

**Real-data results** (closed trades, read-only):
- ARKG f820e7ac (0.005 ATR) and ITA 6ffb33be (worst fill 0.067 ATR) went from breached
  to slippage.
- IBB, IGE ee56b2df and USO remain breached.
- Every respected trade (incl. DBA 56764413) is unchanged, wording included.
- 8-week `stop_breach_rate_pct`: **41.7% (5/12) → 25.0% (3/12)**; slippage 2/12 (16.7%).
- Two quirks:
  - IBB's worst fill is a 2026-06-25 sell at 183.75, 2.26 ATR below the initial stop.
    It predates the retroactively written plan, and was breached either way.
  - ITA's previously logged "deviation" observation stays in its observation log, but
    its journal badge now reads slippage.

**Tests:** `tests/test_stop_slippage.py` is the repo's first test suite. It uses stdlib
`unittest` (pytest isn't installed) and runs with `.venv/Scripts/python -m unittest
discover -s tests -v`. It has 14 tests:
- all 6 real-journal fixtures, using real fills
- short side, the null-ATR fallback (None/NaN/0/''), and configurability
- a trailing stop, including a pre-activation fill checked against the ladder stop
- all-target exits, and no stop
- the early-exit/slippage stat split, and the strict live alert

It only calls pure functions; importing `app` runs the idempotent `init_db()` as usual.

## Three stop methods: Ladder / ATR(14) trail / EMA(21) trail (implemented 2026-10-05)

Wealthsimple has no trailing-stop orders, so a "trailing" stop is a level TAPEs
recomputes **after each session's close**. The user sets it by hand before the next
session, and the Drift Report's "Set Stop To" column tells them where.
`plans.stop_method` is `ladder | atr_trail | ema_trail`. It supersedes the old
`trailing_stop_enabled` switch, which was backfilled to `atr_trail` and is still
written in sync. On the form, a "Stop method" radio replaces the old switch.

**Rules agreed with the user:**
- **Ladder:** each target's `stop_after` only, unchanged.
- **ATR trail:** activates after a chosen target. Stop = highest daily high of
  completed bars since activation − k × ATR(14).
- **EMA trail:** activates after a chosen target, like the ATR trail. It was hard-wired
  to T1 at first; the user asked for it to be configurable the same day. The default
  is still T1. Stop = EMA(21) − `EMA_TRAIL_BUFFER_ATR` (0.25) × ATR(14), counted **only
  on days EMA(21) is above the actual entry**. Before activation the ladder's
  `stop_after` values apply, plus the entry floor from T1.
- **Entry floor:** both trails move the stop to at least the lot's **actual** entry once
  T1 is hit.
- **Never lower:** both trails ratchet only (running max for longs, min for shorts), and
  the trail is combined with the ladder stop via max/min.
- **End of day only:** levels use completed daily bars only (`_completed_daily_bars`:
  today's bar counts only after 16:00 New York). A level set at day D's close applies
  from the next session (`trail_level_before`).

**Functions:**
- `compute_trailing_series`: pure, from bars to a daily level series.
- `fetch_daily_history`: Yahoo-only, with a 150-day warm-up so the EMA/ATR settle.
- `_build_trailing_for_row`: per open lot, takes an injectable fetcher. A failed fetch
  returns an error and degrades to ladder + entry floor.
- `analyze_targets_and_stop(..., trailing=)`: per-fill stop in effect, with the trail
  as of the prior close.
- `compute_plan_drift(..., trailing=)`, which returns `trail_level`.
- `_stop_action`: raise/hold.
- The drift report shows a "↑ raise" badge, plus an "ATR trail"/"EMA(21) trail" badge
  only when the trail is the binding level. The MCP report adds `stop_method` and
  `stop_action`, and `create_plan` takes the method params.

**The journal path is not network-free for trails.** Closed and "Open (in progress)"
journal rows get the entry floor but not the trail series, because building the series
needs a price fetch, which journal builds avoid.

**Two pre-existing bugs fixed:**
- The old trail **replaced** the ladder stop, so it could lower it.
- The live-target loop in `compute_plan_drift` overwrote `should_be_stop` with
  `stop_after`. Both now ratchet.

**The old code also read today's in-progress bar.**

**Real data (read-only):**
- The user had already enabled the ATR trail on 4 open positions (QLD, XLK, CRAK, ARKG,
  set 9/23–10/05).
- A first pass anchored the ATR trail on closes, applying "intraday fluctuation
  shouldn't impact the stop" to both trails. It was **reverted to highs** after
  comparing old vs. new on these positions: closes lowered three live stops the user may
  already have placed.
- Before the close, the new stops can still sit below what the old code showed. ARKG,
  for example, read 51.69 vs. the old 53.10. That's because the old code used today's
  in-progress high (56.96); after the close it converges.
- TAPEs can't see the stop actually placed in Wealthsimple, so a placed stop should
  never be lowered to match a report.
- ARKG's EMA(21) (51.00) sits below its entry (51.59), so an EMA trail there would
  produce no level yet, which is correct under the gate.
- Journal `stop_breached` values and the 25.0% 8-week breach rate are unchanged.

**Tests:** `tests/test_trailing_stops.py`, 17 tests on synthetic bars, no network:
- the EMA formula, activation, entry gate and ratchet
- the ATR highest-high formula and ratchet
- the short-side mirror and the 16:00 completed-bar cutoff
- next-session application of a level
- the entry floor, no-lowering of the ladder, live-reached-only, and raise/hold
- the build path with an injected fetcher and the fetch-failure fallback
- the legacy-switch fallback

The slippage suite's trailing test was updated to the new interface. All 31 tests pass.
The callbacks were exercised with DB writes stubbed, and the server starts cleanly.

## Remembered view state across sessions (implemented 2026-10-06)

**Approach:** uses Dash's built-in component persistence, not a custom `dcc.Store` --
per the "prefer framework-native" principle in memory. Each component gets `**PERSIST`
= `persistence=VIEW_PERSISTENCE_KEY, persistence_type='local'`, so the browser's
localStorage remembers the user's last choice per component id, across tabs and
restarts. Bumping `VIEW_PERSISTENCE_KEY` ('v1') resets everyone's saved view.

**Persisted (12 components):**
- **Accordions:** both page accordions (`accordion-block-1`/`-2`) and the daily
  checklist's section accordion (which is open/collapsed).
- **Trade Journal filters:** symbol dropdown, score range slider, status checklist.
- **"Executed Trades Without a Plan" table:** sort and column filters only
  (`persisted_props=['sort_by','filter_query']`).
- **Add Plan form method choices:** Stop ATR ×, ATR-derived/Manual, Stop method, trail
  activates-after, Trail ATR ×. These act as remembered defaults.

**Deliberately NOT persisted:**
- **Per-trade data entry:** symbol, prices, qty, targets, psych, triggers, currency,
  notes. A stale half-filled plan could get committed by accident.
- **Date pickers:** they default to today on purpose. A remembered date would open the
  daily checklist on a stale day.
- **The Edit modal's copies** of the shared controls (`atr_block` passes PERSIST only
  for prefix `'plan'`): they're filled from the plan being edited.
- **The drift-reconciled and checklist checkboxes:** already saved in the DB per day.

**Dash gotcha this ran into:** a remembered value is dropped whenever a callback writes
the same prop. `update_score_filter_range` used to write the score slider's `value` on
every journal load, which would have wiped the saved filter on every page load. It now
rewrites `value` only when the slider currently spans its whole range, to expand it to
the new full range (keeping the 2026-08-31 IBB 391%-score fix). A narrowed selection
gets `no_update`. Tested directly: the full range expands to [-100, 401]; a narrowed
range is left alone.

**Verified:** the served `/_dash-layout` carries persistence on exactly these 12 ids
and on no `edit-*` or per-trade fields; the server starts with 84 callbacks. **Not
browser-tested** (no browser automation installed): the actual save/restore happens
in the browser.

## Multi-account support, step 1: accounts + currency-aware instruments (implemented 2026-10-07)

**Why:** the user also trades an RRSP and two LIRAs (TFSA `H550363K3CAD` was the only
account tracked), and wants CAD-listed symbols. Everything was keyed by bare symbol,
which caused two problems:
- The same ticker held in two accounts would merge into one position.
- A ticker listed both in the US (USD) and on the TSX (CAD) got priced in USD, because
  Yahoo lookups tried the bare ticker first.

This resolves the 2026-08-27 follow-up (b) ("open positions are looked up by symbol
alone") and the holdings parser's old "summed across accounts" simplification.

**Decisions made with the user:**
- Accounts can hold USD cash; today only the TFSA does.
- **Risk caps are portfolio-wide only** (corrected twice during planning).
  `PER_TRADE_RISK_CAP_PCT` and the heat caution/warning thresholds measure against
  the combined portfolio. Per-account heat is shown for information, never colour-coded
  or warned on.
- The default view is all accounts combined, with drill-down to one account.
- One strategy applies to every account for now.

**Identity:**
- Account = Wealthsimple `account_id`.
- Instrument = `(symbol, currency)`.
- Position = `(account_id, symbol, currency)`. `build_round_trips` groups by this key.
  `match_plans_to_roundtrips` and the manual Match picker only pair a plan with
  positions that pass `plan_fits_position`: the account must match exactly, and a NULL
  plan currency matches either currency.
- Store dict keys for prices are `price_key` = `"SYM|CUR"` strings (Dash stores are JSON).

**Schema (`db._migrate_accounts`):**
- One-time, after an automatic online backup (`<db>.backup-pre-accounts-<stamp>.db`).
- New `accounts` table: id, type, nickname, base_currency.
- `executions` is rebuilt with `account_id NOT NULL` and
  `UNIQUE(account_id, symbol, currency, timestamp, action, qty, price)`.
  **Every existing `id` and the autoincrement sequence are preserved**, because
  `round_trip_id`/`lot_id` hash execution ids and `plan_matches`/`plan_lot_matches`
  depend on them.
- `plans.account_id` is backfilled to the TFSA.
- `dismissed_holdings_discrepancies` is re-keyed `(account_id, symbol)`; the store and
  pattern ids use `'ACCT|SYM'`.
- `beta_cache` gains `currency` in its key. It's only a cache, so it was dropped and
  recreated.
- `cash_equivalent_symbols` and `weekly_reviews` stay global.

**Import:**
- Wealthsimple activity rows carry `account_id`/`account_type`. Unseen accounts are
  registered via `db.upsert_account`; a second account of the same type gets the
  nickname "TYPE …last4".
- A generic CSV has no account column, so it uses the "Account for generic CSVs" picker
  beside the upload. The import refuses if no account is chosen.

**Market data:**
- `yahoo_candidates(symbol, currency)`: USD → `[SYM]`; CAD → `[SYM.TO, SYM.NE, SYM.V]`;
  unknown → the old `[SYM, SYM.TO]` order.
- Currency is threaded through every fetcher:
  - ATR, current price, daily history and beta. Beta's benchmark stays SPY in USD.
  - The TradingView ATR read uses `TSX:SYM` for CAD and checks the exchange as well as
    the ticker. If it can't get the right listing, it raises and the Yahoo fallback runs.
  - `_auto_fetch_atr` also re-fires when the plan's Currency changes.

**UI:**
- **Header "Account view" dropdown:** All accounts plus each account, remembered via
  `PERSIST`. Stores stay complete; renders filter with `_in_view`/`_filter_view`.
  Filtered: Open Positions, the Drift Report, Trade Journal, Executed Without a Plan,
  Draft Plans, Summary Stats, the symbol filter and the holdings diff.
  An account label appears in the All view once there's more than one account.
- **Account select (required)** on the Add Plan form and Edit modal. It defaults via
  `_default_account`: the drilled-down account, else a still-valid current choice, else
  the most recent plan's account, else the first account. "Log Plan" prefills the
  account from the trade.
- **Data Management:** accounts list with editable nicknames.
- **Risk banner:** portfolio heat (colour-coded, all accounts) plus one plain line per
  account. `account_values`/`risk_by_account` back this. Account value is mark-to-market
  of positions only (no cash until step 2), so an account's % is overstated, and the
  banner says so.
- **Pre-trade check:** unchanged logic, plus one informational "% of its account" line.
- **Beta exposure:** gains a per-account breakdown. `compute_portfolio_beta_exposure`
  now returns 6 values.
- **Weekly prompt:** carries `account` on positions and this week's trades, plus
  `balance_by_account`.
- **Summary Statistics** (added the same day, at the user's request): in the All view
  it's a table with one column per account plus a combined "All accounts" column
  (`summary_stats_by_account`, `_render_summary_breakout`). A single account keeps the
  original list. The card header names the current view. The `get_summary_stats` MCP
  tool adds `by_account` when called without `account_id`.
- **Summary Statistics, more metrics** (2026-10-07, same day). The card moved to the
  top of the page, full width, right under the data-freshness banner, and gained six
  metrics (all in `_compute_summary_stats`, so the table, the list and the MCP tool share
  them):
  - **In R, not dollars:** expectancy, avg win/avg loss, profit factor, and max/current
    drawdown (`_r_stats`). P&L is mixed-currency and there's no cash ledger, so R is the
    only honest cross-account measure for now.
    - Drawdown is measured on cumulative R in exit order, starting from 0.
    - Profit factor is None when there are no losses.
    - All of these are withheld (`r_insufficient_sample`) below `SUMMARY_MIN_SAMPLE` (5)
      trades with an R.
  - Losing streaks, longest and current (`_streak_stats`, exit order, win = P&L > 0).
  - Stop-breach and slippage rates and counts.
  - Real data at the time: expectancy −0.07R, profit factor 0.82, max drawdown 4.32R
    (3.30R below the peak now).
  - Tests: `SummaryStats` in `tests/test_accounts.py`, exact-value.
- **Per-account tracking start date** (2026-10-07, same day). `accounts.tracking_start_date`
  is set via a date picker in Data Management's accounts list.
  - **Hard delete was considered and rejected**, with the user's agreement, for four reasons:
    - Re-importing an export would bring the rows back un-dismissed.
    - Step 2's account cash/FX history needs every real fill.
    - Old QLD/DBA/IBIT trips share symbols with live trades.
    - Wealthsimple stays the source of truth.
  - **Archived** (`is_archived`): an *unplanned* round trip that closed before the
    account's start date. It's hidden from:
    - the Executed-Without-a-Plan table;
    - the Dismissed buttons (replaced by an "N archived" note);
    - the Match picker, unless it's that plan's current manual match;
    - `list_unmatched_round_trips`.
  - **Never archived:** planned trades. Auto-matching, positions and every stat are
    untouched, since the journal is per plan and these trades never counted anyway.
  - The rule is date-based, so a re-import stays archived. Clearing the date restores
    everything.
  - At build time, every unplanned trade was dismissed, and a 2026-08-27 TFSA start date
    archives all 30 (21 regular, 9 BIL). The date was left for the user to set.
- **Account balance on the Add Plan form** (2026-10-08, a sizing reference). The user
  chose **both** sources.
  - **Holdings export:** each upload in Weekly Reconciliation saves every account's
    positions' market value and cash, per currency (no FX), with the export's own
    "As of" time. These go in `db.account_snapshots`, keyed (account_id, as_of);
    re-uploading the same export replaces it. Built by `parse_holdings_snapshot`, which
    shares `_read_holdings_csv` with the discrepancy parser. An account first seen in
    an export is registered.
  - **Live value:** a positions-only value from the staged Refresh Live Prices, plus the
    change since the export.
  - **Display:** `render_account_balance` → `plan-account-balance`. It flags the
    export "stale" after `ACCOUNT_SNAPSHOT_STALE_DAYS` (7).
  - **Until step 2's cash ledger, the export is the only source of an account's cash.**
  - **Verified:** against the real 2026-10-03 export and live prices (TFSA USD 79,895.48
    incl. USD 1.98 cash), plus a headless-Chrome screenshot of the block. No snapshot was
    saved during testing; the first one lands when the user next uploads an export.
- **TradingView delayed-feed fix** (2026-10-08). Found planning CHPS, which is both a TSX
  ETF and an unrelated NASDAQ ETF.
  - **Cause:** without a real-time subscription the chart reports `TSX_DLY:CHPS`.
    `_tv_symbol_matches` compared the raw prefix, so the correct TSX chart failed the CAD
    check (an ~8s timeout, then the Yahoo fallback with a misleading "unavailable" note).
    It also *passed* the USD check, which would silently read the Canadian fund's ATR for
    a USD plan.
  - **Fix:** `_tv_exchange` strips the feed suffix (`TSX_DLY` → `TSX`).
  - **Verified live:** the TradingView ATR (1.85) agrees with Yahoo's `CHPS.TO` (1.83).
  - **Still manual:** a never-traded symbol's Currency defaults to USD, so the user has to
    pick CAD.
  - **Not supported yet:** `CHPS.U` (a USD-priced class on the TSX) would be looked up as a
    US listing.
- **Data Management moved into a modal** (2026-10-07, same day, to cut page clutter).
  - It's opened by the header's "Data" button, next to Help (`data-management-modal`,
    `open_data_management`); it closes with its own ×/backdrop.
  - It's no longer an item in `accordion-block-2`. That accordion now holds 3 items, which
    supersedes the 8-item list in the 2026-09-03 accordion note.
  - Every component id inside is unchanged, so no other callback changed.

**Holdings reconciliation is per account.** The holdings export's `Account Number`
column is the same id as the activity export's `account_id` (confirmed on a real
2026-10-03 export), so the two LIRAs are distinct without any mapping. The diff compares
`(account, symbol, currency)`. TAPEs positions are only checked for accounts that
appear in the export, so exporting one account doesn't flag the others.

**MCP:**
- New `list_accounts`.
- Optional `account_id` filter on `list_plans`, `get_journal`, `get_summary_stats`,
  `list_open_positions`, `list_unmatched_round_trips`, `get_beta_weighted_exposure`
  and `get_plan_drift_report`. Drift entries carry `account_id` and price with the
  lot's currency.
- `create_plan` takes `account_id`, which is required once more than one account exists.
- `fetch_current_atr` takes `currency`.

**Verified:**
- `tests/test_accounts.py`: 21 tests, scratch DBs with `db_path` explicit throughout.
  - The old-shaped DB migration keeps ids, leaves a backup and is idempotent.
  - Per-account dedup, plus the account parameter acting only as a fallback.
  - Both parsers keep the account.
  - Separation by account and by currency; pyramid splitting stays within its account.
  - Matching never crosses accounts.
  - `yahoo_candidates`.
  - Portfolio-wide caps with per-account info figures.
  - `_in_view`, `_default_account` and the per-account holdings diff.
- The full suite passes: 53 tests.
- **Zero regression on the real DB:**
  - A snapshot was taken before migrating and compared after the migration ran on app
    import.
  - Identical: execution count and max id, all 17 journal rows (status, lot, stop
    outcome, score, R, P&L), all 43 round trips, all 5 open lots, the 8-week breach rate
    and the match tables.
  - All 240 executions and every plan landed on the TFSA.
- The server serves `/`, `/_dash-layout` (with the new ids) and `/_dash-dependencies`,
  with 88 callbacks. `callback_map` binds each changed callback to its intended function.
- The real callbacks and MCP tools ran against the real DB and live prices, including a
  CAD ATR read through TradingView (`TSX:XEI`).
- **Not browser-tested.** The running app needs a restart.

**Real holdings check (2026-10-03 export vs. the DB as of the 2026-10-06 import):**
- Flags ARKG (in Wealthsimple, not in TAPEs), ARKW (the reverse) and BIL (off by about
  4 shares).
- The activity import is newer than that holdings export, so these look like trades
  between the two dates, not bugs. Re-check with a fresh holdings export.
- The XEI dismissal mentioned under Weekly Review was not in the DB at migration time,
  so there was nothing to re-key.

**Out of scope (later steps):**
- **Step 2:**
  - A cash ledger from the activity export (deposits, conversions, dividends, interest).
  - USD/CAD cash per account.
  - Bank of Canada FX rates and CAD-base valuation.
  - Returns with and without FX.
  - FX baselines come from each conversion event, not from per-trade "FX Rate" text.
    The user is a Wealthsimple Premium client with no per-trade FX.
  - Until then, cross-currency totals keep the "(mixed currencies, not FX-converted)"
    caveat.
- **Step 3:** per-account or per-currency strategy profiles.

## Per-plan ATR timeframe: 4H / 1D / 1W (implemented 2026-10-08)

**Why:** the ATR fetch was hard-wired to daily bars. TradingView was forced to 1D and
Yahoo was daily-only. The user wanted to plan on 4-hour or weekly bars. This supersedes
"forces the 1D timeframe" and "always daily bars" in workflow step 3.

**Rules agreed with the user:**
- **Selector:** a "Timeframe" selector (`{prefix}-atr-timeframe`; `ATR_TIMEFRAMES` =
  `'240'`/`'1D'`/`'1W'`, which are TradingView resolution strings) sits beside the ATR
  field on Add Plan and Edit. It's a remembered default on Add Plan.
- **Stored on the plan:** `plans.atr_timeframe`. NULL means daily, so there's no backfill;
  read it with `atr_timeframe_of`.
- **Auto-fetch:** changing the timeframe re-fetches the ATR.
- **ATR(14) trail follows the plan's timeframe**, with one refinement the user accepted:
  - The anchor is always the **highest DAILY high** since activation. A weekly bar can
    straddle the activation day and would include pre-activation highs; a 4H high is
    no different from the daily high over the same span.
  - Only the distance changes: `trailing_atr_multiplier` × the timeframe's ATR, via
    `timeframe_atr_by_day` → `compute_trailing_series(trail_atr=...)`.
- **EMA(21) trail is always fully daily:** its EMA and its 0.25 × ATR buffer.
- **Once a day:** every trail is still recomputed once a day from completed bars and
  applied next session.
  - A week counts only once it's finished (its last bar is a Friday, or a later day
    exists after a holiday Friday).
  - Each day takes the ATR at its last completed 4H bar.

**Data:**
- **TradingView:** `fetch_atr_from_tradingview(..., timeframe)` switches the chart to
  that resolution (`_same_resolution` treats `D`/`1D` and `W`/`1W` as equal). If the
  chart won't switch, it raises, and the Yahoo fallback runs.
- **Yahoo:** weekly bars are built from daily (`resample_to_weekly`, indexed by the week's
  last trading day). 4H bars are built from 60-minute bars (`resample_to_four_hour`,
  session-aligned 09:30–13:30 / 13:30–16:00 New York, matching TradingView's 4H bars for
  US/TSX listings).
- **Trail fetch:** `fetch_four_hour_history` pulls the trail's 4H data (Yahoo keeps
  ~730 days of 60m). `_build_trailing_for_row` takes an injectable `fetch_four_hour`.

**Verified against TradingView before relying on Yahoo's 4H:**
- **TSX:CHPS:** all 119 of TradingView's 4H bars had a Yahoo bar with the same start
  time, highs within a cent. ATR(14): TradingView 1.0026 vs Yahoo 0.9986.
- **QLD:** the 12 bars read also matched.
- **Live TAPEs reads** (CHPS CAD): 1W ATR 6.00 vs Yahoo 6.03, and 4H 1.17 vs 1.19 (the
  in-progress bar differs).
- **A slip during that check:** an MCP `chart_set_symbol` to QLD reverted to CHPS
  mid-check, so a 119-bar read was CHPS, not QLD. It was caught because the prices
  didn't fit QLD, and the read was used as the TSX test instead.
- **Restore:** the user's chart was restored to TSX CHPS at 15 minutes.

**Displays:**
- `trail_label`, e.g. "ATR trail (1W)". The EMA trail never shows a timeframe.
- The drift report's binding-trail badge.
- A new detail-view line, "ATR(14, 1W) at commit … | Stop method …" (it didn't show the
  ATR or stop method before).
- `get_plan_drift_report` and `create_plan` (`atr_timeframe`), plus
  `fetch_current_atr(timeframe=)`.

**Slip caught during the build:** the detail-line helper was first inserted between
`@app.callback` and `def show_detail`, which is the same mistake as the 2026-09-22
banner bug. It was caught by inspection and moved. A new check confirms every callback's
arity matches its inputs+states (91 callbacks, 0 mismatches). It's worth re-running after
any helper extraction.

**Tests:** `tests/test_timeframes.py`, 18 tests:
- Resampling: the 4H session split, pre-market dropped, weekly holiday-Friday indexing.
- ATR by day: the partial week is never used; each day takes its last 4H bar.
- Trails: daily-high anchor with the timeframe distance; weekly and 4H trails through
  `_build_trailing_for_row`; the EMA trail stays daily and never fetches 4H; a 4H fetch
  failure degrades to the entry floor.
- Labels and fallbacks.
- Schema round trip and migration.

The full suite passes: 81 tests.

**Real data (read-only):** all 21 committed plans read as 1D. The drift report for QLD,
XLK, CRAK and ARKW is unchanged in kind (all `atr_trail`, 1D, no errors), and all 21
detail views render.

## Totals per currency and in CAD (implemented 2026-10-08)

**Why:** every portfolio total summed USD and CAD as-is under a "(mixed currencies, not
FX-converted)" caveat. That was harmless while almost everything was USD, but by
2026-10-08 the RRSP and both LIRAs were imported and held about CAD 249,000 next to about
USD 80,000. The user asked for both per-currency totals and a combined figure.

**Design:**
- **Base currency is CAD** (`BASE_CURRENCY`, matching `accounts.base_currency`).
- **Rates:** the Bank of Canada Valet API (`FXUSDCAD`, the daily average rate, no key
  needed), fetched by `get_fx_rates`.
  - Cached in `db.fx_rates` (pair, date); re-fetched after `FX_CACHE_MAX_AGE_HOURS`
    (12), so the Bank is called at most a couple of times a day. The table also builds
    a rate history for step 2.
  - A failed fetch falls back to the newest cached rate, named in `errors`.
  - A currency with no rate at all is left out, and `usable_fx` then returns None.
    Callers fall back to per-currency figures plus the old mixed sum with a caveat,
    **never a partly-converted total**.
- **Rates travel with the prices:** `stage_prices` puts `fx` and `by_currency` into the
  staged-prices store, and `staged_portfolio_value` returns (CAD value, fx used,
  by_currency). Every consumer of live prices therefore uses the same rate.
- **`fx=None` parameters:** added to `compute_portfolio_risk`,
  `compute_portfolio_beta_exposure`, `account_values` and `risk_by_account`. Without
  fx they behave exactly as before. Per-lot risk and per-symbol market value stay in
  their own currency.

**Converted to CAD** (heat and the caps now measure one currency):
- **Portfolio risk banner:** heat % and the per-account lines, plus the per-currency
  split and the rate used.
- **Pre-trade check:** the trade's risk is converted at its plan currency's rate
  (`compute_pretrade_risk_check(trade_fx_rate=)`). The trade's dollar figure still
  shows in its own currency.
- **Beta exposure:** the banner and `get_beta_weighted_exposure`, which gains
  `currency`, `portfolio_value_by_currency` and `fx_rates_used`.
- **Weekly Review balance:**
  - New rows store `balance_currency='CAD'` plus `balance_detail_json` per currency.
  - Rows from before 2026-10-08 stay NULL, shown as "(mixed)".
  - `weekly_balance_changes` only diffs rows on the same basis; the first CAD week shows
    "n/a" rather than a fake jump.
  - The coaching prompt carries the currency, the per-currency split and the rate.
- **Add Plan balance block:** "= CAD …" beside both the export total and the live value.

**Per currency only, never converted:** realized P&L. `pnl_by_currency` drives Summary
Statistics' "Total actual P&L". Each trade closed at its own day's rate, so converting
at today's would misstate it; historical conversion is step-2 work.
`total_actual_pnl` (mixed) is kept for MCP compatibility. R-based stats need no
conversion.

**New MCP tool:** `get_fx_rates` (function `get_fx_rates_tool`, named via
`mcp_enabled(name=...)`).

**Real data (2026-10-08, read-only apart from seeding the rate cache):**
- USD/CAD 1.4240.
- Portfolio CAD 363,286.08, i.e. CAD 248,937.70 + USD 80,300.83. The old mixed sum was
  329,238.53.
- **Heat was understated:** 0.465% old vs **0.572%** converted. The at-risk money is
  mostly USD, which the mixed sum valued at par against much larger CAD holdings.
- Summary P&L: CAD −787.91 · USD −546.58.
- Every display renders, with 91 callbacks and 0 arity mismatches.

**Tests:** `tests/test_fx.py`, 12 tests:
- The rate cache, refresh and fallbacks (with an injected fetch).
- `to_base` refusing partial totals; staged values with and without a rate.
- Risk, account and beta conversion, with exact values.
- The pre-trade conversion and weekly basis changes.
- Per-currency P&L and the weekly schema migration.

The full suite passes: 93 tests.

**Still out of scope:** live cash per currency (only the holdings export has cash), and
converting historical P&L/returns at each trade's own date (step 2).

**Weekly balance includes cash (2026-10-17).** "Compute Balance" used to be positions
only, so the user's cash (CAD ~9,600 at the time, mostly in the RRSP) was missing.
- **What it adds:** each account's cash from its latest holdings export
  (`cash_from_snapshots` over `db.account_snapshots`), converted to CAD.
- **Display:** positions and cash per currency, the export date, and per-account totals.
- **Warnings:** an account with no export (its cash isn't counted); fills after an
  account's export (its cash may be spent or missing proceeds); an export over 7 days old.
- **Saved rows:** they store `balance_detail_json` = {positions, cash, cash_as_of}.
  `balance_basis(row)` = (currency, includes_cash), and `weekly_balance_changes` plus the
  coaching prompt only diff rows on the same basis. The first with-cash week shows "n/a"
  against the Oct 9 positions-only week, which the table marks "(no cash)".
- **Real data:** CAD 361,802.06 = positions CAD 239,421.12 + USD 79,661.71, plus cash
  CAD 9,624.99 + USD 1.96. RRSP CAD 45,936.99 matches its export (positions 36,707.07 +
  cash 9,229.92).
- **Tests:** 3 new in `tests/test_fx.py`; the full suite passes (139 tests).

**Oct 9 weekly row backfilled with cash (2026-10-17, with the user's OK).** The Oct 17
row (with cash) showed "n/a" against Oct 9 (positions only), which was the intended
basis-change guard.
- **The fix:** the Oct 9 row got the cash from the 2026-10-10 07:54 holdings export. It
  was pre-market, so it reflects the Oct 9 close: CAD 359.64 (RRSP 14.64, each LIRA
  172.50; all CAD, so no rate involved).
- **Result:** balance 363,972.95 → 364,332.59, now `balance_basis` ('CAD', True). The Oct
  17 change reads **−2,530.53**.
- **Recorded:** the original value and the source are in that row's
  `balance_detail_json['note']`. The notes were untouched.
- **Context:** the RRSP's ~CAD 9,215 jump in cash that week was a sale into cash, which is
  why a positions-only comparison would also have misled.

**Heat, pre-trade check and beta also include cash (2026-10-17, same day, at the user's
request).** `portfolio_with_cash(openpos, staged, snapshots, executions, accounts)` is now
the one definition of "portfolio value": positions at staged prices plus each account's
export cash, in CAD.
- **Used by:** the weekly balance, the risk banner (heat % and per-account "% of its
  value", now incl. that account's cash), `_update_pretrade_risk` (the per-trade cap, heat
  if added, % of account), the beta-exposure banner, and `get_beta_weighted_exposure`
  (adds `cash_included_by_currency`).
- **Warnings:** `cash_warnings`/`cash_dates_text` show the export date and the
  missing/traded-since/stale warnings everywhere cash is used.
- **Real data:** portfolio CAD 352,174.30 → 361,802.06 with cash; heat 0.058% → 0.057%.
  Only CAD 204.89 is at risk, because nearly everything was parked in cash-equivalents
  (BIL, CSAV in all 4 accounts) plus 47 CRAK.
- **Beta exposure −0.15% is correct, not a bug:** the cash-equivalents are excluded, and
  CRAK's trailing beta vs SPY is −0.12. This was checked before reporting.
- **Tests:** +1 (`test_heat_measures_against_positions_plus_cash`); the full suite passes
  (140 tests), 96 callbacks, 0 arity mismatches.

## Editable daily/weekly checklists (implemented 2026-10-09)

This supersedes the 2026-09-03 note that "item wording lives in code constants, not
the database". The user can now edit both lists in the app: add or remove items, edit
captions and help text, and add, rename, remove or move headings.

**Model:** each list is an ordered list of entries:
`{'kind': 'heading', 'id', 'text', 'level': 1 | 2}` (1 = section, 2 = sub-heading) or
`{'kind': 'item', 'id', 'label', 'help'}`.
- **Storage:** saved in `db.checklist_definitions` (name `daily`/`weekly` →
  entries_json). No row means the built-in default.
- **Defaults:** `DEFAULT_CHECKLISTS` is built from the old `DAILY_CHECKLIST_SECTIONS`/
  `WEEKLY_CHECKLIST_ITEMS`, which are kept only as defaults.
- **Default heading ids** are the old section titles, so the daily accordion's
  remembered open/closed state carries over.
- **Read with:** `load_checklist(name)`.
- **Saving the exact default** deletes the row, so the list follows the built-in default
  again.

**Item ids are stable:** renaming or moving keeps the id, and new entries get
`item_<hex>`/`heading_<hex>`. Ticked state in `daily/weekly_checklist_state` is
untouched by edits. Progress counts only the current items, so a deleted item's old
ticks just stop counting.

**Rendering:**
- **Daily:** an accordion per section (`_daily_sections`; items before the first section
  go in an untitled "Checklist" section), with sub-headings as small headings inside it.
- **Weekly:** a flat list (`_render_checklist_entries`) with any headings inline.
- **Data flow:** both read `store-checklists`. The render and toggle callbacks take it as
  Input/State, so a save re-renders immediately.

**Editor:** `checklist-editor-modal`, opened by an "Edit list" button on each list
(`{'type': 'btn-edit-checklist', 'index': name}`).
- **Rows:** a Section/Sub-heading picker for headings; caption + help fields for items;
  ↑/↓/× per row; buttons for + Item / + Section / + Sub-heading / Reset to defaults.
- **Draft:** unsaved edits live in `store-checklist-draft`.
- **No lost typing:** every structural action first merges what's typed
  (`_merge_editor_values`) so it isn't lost.
- **Phantom clicks:** re-rendered buttons can fire the callback with n_clicks None, so
  only a truthy trigger value acts.
- **Save** trims whitespace and refuses empty text or duplicate ids (`validate_checklist`).

**Move rule** (`move_checklist_entry`, pure):
- An item moves one row, so it can cross into the neighbouring sub-heading or section.
- A heading moves with its block: everything up to the next heading of the same or
  higher level, so a section carries its sub-headings.
- The block swaps with the previous/next sibling block, or with a single row (a loose
  item, or the parent heading, which moves a sub-heading into the previous/next section).
- Edges are no-ops.

**Verified:**
- `tests/test_checklists.py`, 17 tests:
  - the defaults match the old structure and ids;
  - every move rule, including crossing sections and edges;
  - grouping, validation and typing preserved across actions;
  - per-section counts;
  - storage round trip, reset, and that the loaded default is a copy;
  - the real registered editor callbacks (open/add/move/delete, ignored phantom clicks,
    reset + save-as-default, empty-text refusal) with DB writes stubbed.
- The full suite passes: 110 tests. 94 callbacks, 0 arity mismatches.
- Headless-Chrome screenshots of the editor (daily defaults) and of the accordion with an
  added section.
- No definitions were saved to the real DB.

## Daily to-do list (implemented 2026-11-02)

One-off items for the day (research, admin, "build a plan for X"), which the user fills in during
the pre-market checklist. It's an always-visible block at the top of the Daily Checklist card, above
the accordion, and is deliberately separate from the fixed checklist.

**Rules agreed with the user:**
- **Adding:** type a line and press Enter or Add. Only the text and dates are stored; no categories,
  priorities or due dates.
- **Carry-forward:** an unfinished item stays on the list and shows its age ("from 3 days ago").
- **Closing:** tick it (done) or × (dropped). Either stays visible for that day, so a mistaken click
  can be undone (untick or Undo), then disappears.
- **Separate from progress:** to-dos never count toward the checklist's n/total.
- **Not sent anywhere:** they stay out of the coaching prompt, the journal and MCP. Adding them to the
  prompt later is easy if a pattern of slipping tasks shows up.
- **Trade decisions don't belong here:** entries go in plans and stop changes in the drift report. A
  "build a plan for X" item is fine, because the plan's own checks still apply.
- **The pre-market checklist item** ("Review or add today's to-dos") is the user's to add with the
  list editor; the code doesn't add it.

**Storage:** `db.todos`, one row per item (text, added_date, status `open | done | dropped`,
closed_date), via `fetch_todos`/`add_todo`/`set_todo_status`.

**Date picker:** the list follows `daily-checklist-date`. `todos_for_date` shows it as it stood that
day (`todo_state_on`: closed later means still open then), and adding or closing uses the picked date.

**Callback:** `update_todos_cb` re-renders its own checkboxes and buttons, which can fire it again.
A tick only counts if it differs from what's saved, a drop needs a real click, and a date change
applies no ticks.

**Verified:**
- `TodoList` in `tests/test_checklists.py`, 9 tests. The full suite passes: 306 tests. 125 callbacks,
  0 arity mismatches. The real DB's table is created and empty.
- **Clicked in a real page**, a first for this project: headless Chrome driven over its debug port
  (9333), against the real app on port 8061 with to-dos held in memory. Enter to add, the box
  clearing, tick, ×, Undo and reload all worked, and the update-call count stayed flat after each
  (no re-render loop). Unticking is covered by the unit tests only.

## Performance Over Time (implemented 2026-10-09)

A collapsible section right under Summary Statistics.
- **Collapsed on load:** `start_collapsed=True`, deliberately **not** `PERSIST`, so it's
  collapsed on every page load.
- **Lazy build:** charts are built only while it's open; `update_performance` raises
  PreventUpdate when `active_item` is empty.
- **Scope:** it follows the Account view.
- **R, not dollars:** everything is in R, which puts USD and CAD on one scale. P&L appears
  only per currency, in the monthly table.

**Contents** (`performance_frame` → closed planned trades in exit order):
1. **Cumulative R with drawdown from its running peak** (starting at 0) as a second panel
   sharing the trade axis. Never a second y-scale.
2. **Rolling expectancy and win rate** over the last `PERF_ROLLING_WINDOW` (10) trades, in
   two panels, shown only once 10 trades exist.
3. **R distribution.** At first this was 0.5R histogram bins. Since 2026-10-13, at the
   user's request, it's a horizontal violin (`spanmode='hard'`, so the outline never
   extends past the real best and worst trades) with a median/quartile box.
   - Every trade is drawn as its own dot (gains blue, losses red, legend top right) at a
     deterministic offset, so the chart is identical on every render.
   - With few trades the smoothed outline is partly guesswork; the dots carry the real
     data.
   - The average win and average loss are marked.
4. **Process by month:** average Plan Score (bars by sign), plus targets hit / stop
   breached / stop slippage % as grouped bars.
5. **Monthly table** (`monthly_performance`): trades, win rate, expectancy, avg Plan Score,
   targets hit, breach/slippage, realized P&L per currency.

**Notes:**
- A small-sample note is shown under `PERF_SMALL_SAMPLE` (30) trades.
- A trade without an R counts as 0R in the curves and is noted.

**Look:** built with the dataviz skill.
- **Palette:** the validated light palette in `PERF_COLORS`, with the validator run: blue
  `#2a78d6` and red `#e34948` for gain/loss, orange and aqua as slots 2–3.
- **Contrast:** aqua is under 3:1 on the surface, and the monthly table is its required
  table view.
- **Marks:** 2px lines, 8px markers with surface rings, 4px rounded bar ends, hairline
  grid, hover text on every mark, left-aligned titles (`_left_panel_titles`).
- **Checked visually:** headless-Chrome screenshots of real data; a title/tick collision
  was found and fixed that way.

**Real data at build time:**
- 15 closed planned trades (2026-08-28 → 10-07); cumulative −1.64R; max drawdown 4.32R.
- Rolling expectancy −0.24R.
- By month: Aug +0.78R (3 trades) / Sep −0.46R (9) / Oct +0.05R (3).
- These match Summary Statistics (expectancy −0.11R, drawdown 4.32R).

**Tests:** `tests/test_performance.py`, 10 tests (frame order and cumulative/drawdown
values, the rolling window, no-R trades, the monthly rollup, account filtering, edge-case
figures, exact distribution bins, collapsed = no build). The full suite passes: 120 tests,
95 callbacks.

**Deferred:**
- Account value over time: only 6 weekly balances, split between mixed and CAD.
- True % returns: needs the cash ledger (step 2).
- Performance by psych state: no v2-rated trades yet.
- The archived pre-plan trades: left out, per the user.

## Plan reward follows the scale-out ladder (implemented 2026-10-10)

**Why:** the user asked whether max risk/reward accounts for scaling out. It didn't. Max
reward assumed the whole position exits at the furthest target, so the plan R:R was
overstated. In the worked example (50/25/12.5/6.25/6.25% at 105–125, stop 95) it read
5 : 1 against the ladder's real 1.94 : 1. It also disagreed with the post-trade Plan
Score, which already weights by Qty %.

**`compute_plan_ladder(direction, entry, stop, targets, qty)`** (targets =
{price, qty_pct, stop_after}, taken nearest-to-entry first) returns:
- **`max_loss`:** unchanged; the full position stopped at the initial stop. It's still
  the true worst case and what the risk caps use. `compute_plan_risk_reward` remains
  only for the pre-trade check's max loss.
- **`ladder_reward`:** each target's Qty % sold at that target. This is the `reward`, and
  `rr` = reward / max_loss.
- **`best_target_reward`:** the old figure, kept only as the fallback when no target has
  a Qty % (`reward_basis='best_target'`) and as a comparison line.
- **`scenarios`:** the result if stopped out before T1, after T1, after T1+T2, …
  - Slices already sold are banked at their targets, and the rest is stopped at the stop
    then in effect.
  - Each hit target's `stop_after` replaces the stop, the same rule as
    `analyze_targets_and_stop`'s replay. For a trailing plan it's the floor.
  - The list stops once the position is fully sold.
- **`ladder_complete`:** False when Qty % doesn't add up to 100 (the form says so).

**Where it shows:**
- **Add Plan / Edit summary** (`render_plan_ladder`; `_update_risk_reward` now also reads
  each target's Qty % and Move Stop To): max loss, planned reward, R:R, the scenario
  table, and the furthest-target comparison.
- **Journal "Plan R:R" column:** `build_journal_row` adds `reward_basis`.
- **Detail view wording.**
- **`get_plan` (MCP):** adds `reward_basis`, `best_target_reward` and
  `stop_out_scenarios`.

These are display only: scores, stats and risk caps are unchanged.

**Real plans (read-only):** all 21 committed plans have a Qty % ladder (GDX and IBB add up
to 87.5%). The planned R:R roughly halved:
- CRAK 5.74 → 2.3, VFV 12.0 → 5.0, IBB 4.0 → 1.0.
- Five plans are now below 1 : 1: GDX 0.33, IBIT 0.68, IGE 65958984 0.62, ITA 0.79, XAR 0.68.

**Verified:**
- `tests/test_plan_ladder.py`, 10 tests: the worked example (exact R per scenario),
  target order, the short side, the no-Qty% fallback, an incomplete ladder, a missing
  Move Stop To, fully sold early, no stop/entry, the real registered form callback, and
  the journal row.
- The full suite passes: 130 tests.
- A headless-Chrome screenshot of the form block. That caught a white table background
  in the coloured box and a wrapping "1.94 : 1"; both were fixed.

## Scale-in campaigns: a second strategy (implemented 2026-10-19)

**Why:** a reversal strategy. Open a small **probe** as a down-trend (or, for a short, an
up-trend) starts to reverse, **add** as the reversal confirms, and hold through the new
trend on a trailing stop. This is the opposite shape to a scale-out plan, which has one
entry and sells in stages.

**Rules agreed with the user:**
- **Exit:** trailing stop only, with no targets. The form switches a ladder stop method
  to the ATR trail.
- **Add sizing:** equal adds for now. "Scaled" is in the menu but disabled, so it can be
  added later.
- **Stop before every add:** before each add, the stop on the whole position must already
  be raised to that add's `stop_before`.
- **Never averaging down:** each trigger is beyond the entry and the previous trigger.
- **Risk cap:** the 2% per-trade cap applies to the campaign's **peak-risk stage**, not
  just the probe.
- **Defaults:** 3 adds (`DEFAULT_CAMPAIGN_ADDS`). "Fill defaults" lays them out at
  1R/2R/3R beyond the entry, each add the probe's size, each stop at the previous level.
  With that ladder, no stage risks more than the probe.
- **Trail start:** configurable. `trail_start_add` 0 means from the probe (the default);
  k means after add k.
  - The ATR trail can run from the start, because early on it sits below the scheduled
    stop and can only tighten it.
  - The EMA(21) trail waits for a **close above EMA(21)** (`ema_gate='close'`). The
    scale-out rule (EMA above entry) doesn't fit a campaign, which starts below a
    falling EMA.
  - There is no entry floor; the add ladder's stop schedule is the floor.
- **One campaign per account + symbol, and nothing else open beside it**
  (`_campaign_conflict`, checked in both directions). The campaign then owns every fill
  for that position.

**Schema:** three new `plans` columns:
- `plan_type`: `scale_out` or `scale_in`. NULL reads as `scale_out`, so no backfill is
  needed.
- `adds_json`: up to 5 adds of {price, qty, stop_before}, exposed as `plan['adds']` via
  `db._clean_adds`.
- `trail_start_add`.

**Pure functions:**
- `default_campaign_adds`.
- `compute_campaign_stages`: per-stage shares, average cost, stop, P&L if stopped, plus
  `peak_risk`/`probe_risk`.
- `validate_campaign`.
- `analyze_campaign`: replays the real fills.
- `compute_campaign_drift`.

**Form** (Add Plan and Edit):
- A "Plan type" radio swaps the target ladder for the add ladder (`campaign_block`): the
  number of adds, size, per-add shares, trail start, "Fill defaults", and rows of
  trigger / shares / stop-before, each with a % from entry.
- The summary box shows the stage table with peak risk.
- The pre-trade check measures the campaign at its peak (`max_loss_override`).
- `create_plan` (MCP) takes `plan_type`/`adds`/`trail_start_add`; with `adds` omitted it
  uses the default ladder.
- **Slip caught by screenshot:** `dbc.Select` shows a numeric value 0 as blank, so the
  trail-start options are strings.

**Fills and matching:**
- Campaign plans never take part in pyramid lot splitting. Round trips and open lots now
  carry `entry_fill_list` and `first_entry_price`.
- The matcher compares a campaign's planned entry with the **first** buy (the probe), not
  the average cost, which climbs with every add.

**Journal** (`_campaign_journal_row`):
- **Stages:** buys are assigned to the probe and then each add, by quantity, within the
  2% lot tolerance. Anything beyond the last add is "extra".
- **Peak risk taken** is the most the position stood to lose at the stop then scheduled,
  measured after every buy. It's the campaign's 1R: **R = P&L / peak risk taken**, so
  campaigns sit on the same R scale as scale-out trades.
- **No Plan Score:** a campaign has no targets to score against.
- **Stop outcome:** checked against the scheduled stop in effect at each exit.
- **Notes:** adds made vs planned; an add bought before its trigger (flagged "early" when
  it's past the stop-slippage tolerance); partial adds; extra shares; peak risk taken vs
  planned.
- **Open campaigns** also say "Next: raise the stop to X, then buy Q at P".
- **Detail view:** an "Add Ladder" table (plan beside fills), with campaign wording
  ("open-ended" reward, R vs peak risk).
- **Journal table:** "Adds n/m" in the targets column.
- **Heat:** uses the scheduled stop.
- **`get_plan`** returns `campaign_stages`.
- **Pre-existing bug fixed:** the detail view's max reward / R:R guards let NaN through
  ("USD nan") once rows of different shapes shared a DataFrame. They now use `_scalar`.

**Drift report:** `_drift_for_row` dispatches between the scale-out and campaign paths in
both the UI and MCP.
- Price reaching an add's trigger means **"raise the stop to stop_before, then buy"**.
  `should_be_stop` includes that raise.
- `should_be_qty_open` follows the add ladder, so a negative drift reads "buy N".
- The trail applies once its start is in (for the actual stop) or live-reached (for the
  should-be stop), and never lowers the schedule.
- MCP entries add `plan_type`, `adds_done`/`adds_planned` and the next add's
  price/qty/stop_before/reached.

**Strategy filter:** a picker in the Summary Statistics header (`strategy-view`: All /
Scale-out / Scale-in, remembered) drives both Summary Statistics and Performance Over
Time. `get_summary_stats(plan_type=...)` does the same over MCP. The weekly coaching
rows carry `strategy`, and `WEEKLY_COACHING_SYSTEM_PROMPT` explains campaigns. That
prompt is pasted once into the external coaching chat, so the copy there needs updating
by hand.

**Verified:**
- `tests/test_campaigns.py`, 33 tests:
  - the ladder and stage table, and the validation rules;
  - the conflict rule and the storage round trip;
  - the real form, pre-trade and default-ladder callbacks, and `create_plan` (writes
    stubbed);
  - `analyze_campaign` (peak risk, early add, breach, partial and extra fills);
  - closed, open and unmatched journal rows;
  - matching by the probe, and no pyramid splitting;
  - drift (between adds, add reached, gap through two adds, breach, trail start, never
    lowering);
  - the EMA close gate and the campaign trailing anchor;
  - the heat stop, and the strategy filter (callback and MCP).
- The full suite passes: 170 tests. 102 callbacks, 0 arity mismatches; the server
  serves `/`, `/_dash-layout` and `/_dash-dependencies`.
- **Zero regression on the real DB** (read-only, old vs new code in separate
  processes):
  - all 22 journal rows, 50 round trips and 6 open lots are identical apart from the new
    fields;
  - the drift report is identical with prices stubbed. That covers only CRAK, the one
    plan-matched non-cash lot at the time.
- Headless-Chrome screenshots of the form section, the detail view and the drift rows.
- No campaign exists in the real DB yet, so nothing campaign-specific has run on real
  fills. **Not click-tested in a browser;** the running app needs a restart.

## Cancelled plans, stale flag, "what would have happened" (implemented 2026-10-20)

**Why:** the user often abandons a committed plan before executing it (conditions
change, or they decide against it) and wanted to keep those plans rather than delete
them. Kept, they show which trades were turned down and whether that was the right call.
It also fixes two quiet problems with abandoned "Planned" plans:
- one could be auto-matched to a later, unrelated trade (within 30 days / 20%);
- one blocked a new scale-in campaign on that symbol.

**Status:** `plans.status = 'cancelled'`, set only for a committed plan whose journal
row is "Planned (no execution matched yet)". A plan with fills is a real trade and must
be exited instead.
- **New columns:** `cancelled_at`, `cancel_reason`, `cancel_note`,
  `cancel_psych_risk/urgency/focus`, and `counterfactual_json` (exposed as
  `plan['counterfactual']`).
- **Psych at cancel:** stored only if a slider was moved, the same rule as `save_edit`.
- **`plan_fits_position` returns False for a cancelled plan.** That one rule keeps it
  out of:
  - matching;
  - the Match picker;
  - `_fallback_plan_for` (heat, Open Positions' Plan? badge);
  - `list_open_positions`' plan_status;
  - `_campaign_conflict`.
- **Journal:** `_build_journal_and_unmatched` appends `cancelled_journal_row`s with
  status "Cancelled". The row keeps the unmatched shape (planned ladder, max loss, R:R)
  but has no P&L or R, and is never scored.
- **Summary:** `_compute_summary_stats` excludes cancelled rows from `plan_count`. It
  adds `cancelled_count` plus `_cancel_stats`: reasons, filled / would have won / avg R
  / never filled / still running. These show as a "Cancelled plans" summary row.

**Editable reasons and the stale threshold:** a new `app_settings` table holds them
(`db.get_setting`/`set_setting`). Defaults are `DEFAULT_CANCEL_REASONS` (5) and
`DEFAULT_STALE_PLAN_DAYS` (10).
- **Where to edit:** Data Management → "Cancelled plans", with reasons one per line.
  Saving re-sends store-plans so the stale flags rebuild.
- **Renaming:** a cancelled plan keeps the reason text it was given, even if that
  reason is later renamed or removed.

**Stale flag:** a Planned row whose `plan_age_days` (since `committed_at`, else
planned_date/created_at) is at or above the threshold gets `stale`/`age_days`. The
journal shows a "Stale Nd" badge and a warning line above the table. The weekly prompt
carries `stale_planned_plans`.

**UI:**
- **Cancel:** a "Cancel" button on Planned rows opens `cancel-plan-modal`, with a reason
  select fed by the editable list, a note and psych sliders.
- **Reinstate:** replaces "Match" on Cancelled rows. It sets the status back to
  committed and clears the reason, note and replay (`REINSTATE_FIELDS`).
- **Status filter:** gained "Cancelled". It now has its own persistence key
  (`v1-status2`), because a filter saved before would otherwise hide cancelled rows.
- **Detail view:** says "cancelled before any fill" and shows the psych reading at
  cancel.
- **Slip caught by screenshot:** cancelled rows showed "Cancelled *". The missing
  `manual_match` became NaN in the DataFrame, and NaN is truthy. Fixed both ways
  (`== True` check, explicit False).

**"What would have happened"** (`replay_cancelled_plan`, pure; `refresh_cancelled_outcomes`
fetches and saves):
- **Window:** daily bars from the session after the plan went live, up to
  `CANCEL_REPLAY_BARS` (60).
- **Entry:** a stop-type entry when the entry was beyond the prior close (breakout),
  filled once price trades through it, at the open if it gapped past. Otherwise a
  limit-type entry, filled once price comes back to it.
- **Scale-out:**
  - Each bar checks the stop first (worst case), then targets nearest-first.
  - A hit target sells its Qty % and moves the stop to its Move Stop To.
  - With no Qty % at all, everything sells at T1.
  - Trailing plans add the entry floor at T1 and the trail from the activation target,
    on daily bars (a 4H/1W plan's ATR trail uses daily ATR and says so).
  - R = P&L per share / planned entry-to-stop.
- **Campaigns:**
  - Each bar checks the stop first, then each add at its trigger, with the stop raised
    to its stop_before.
  - The trail runs from the probe or add k, with the EMA trail on the close gate.
  - R = P&L / peak risk taken (`analyze_campaign`).
- **Outcomes:** `never_filled | stopped_out | targets_done | still_open`. Anything
  still open is marked at the last close.
- **Final:** a result is final once it exits or covers the full window; until then it's
  redone on each refresh and shown "so far".
- **Where it runs:** the "Check what would have happened" button by the journal filters,
  or the MCP tool `refresh_cancelled_outcomes`. It fetches Yahoo daily bars; one
  symbol's failure is reported and the rest still run.

**Weekly coaching:** `week_summary` gains:
- `cancelled_plans_this_week` (reason, note, psych, an "if kept" line);
- `stale_planned_plans`;
- `cancellation_record`.

`WEEKLY_COACHING_SYSTEM_PROMPT` explains them: cancels that would have won, especially
with a Fearful reading or "Price ran away", are a pattern to name. The external chat's
copy of that prompt needs re-pasting.

**MCP:** new `cancel_plan(plan_id, reason, note)` (returns the reason list),
`reinstate_plan`, `refresh_cancelled_outcomes`; `list_plans(status='cancelled')`.

**Verified:**
- `tests/test_cancelled.py`, 25 tests:
  - the replay with exact values: breakout fill, target then moved stop, gap through
    the stop, limit entry, stop before target on the same bar, never-filled finality,
    still open marked to close, no Qty %, the short side, a campaign add then stop
    (R −1.0 vs peak risk), trailing plans, the text;
  - status rules: never fits a position, the campaign rule, no fill matching;
  - stale flags and summary counts;
  - storage round trip including reinstate, and settings;
  - the real callbacks (open with the editable list and phantom clicks, save refusing a
    plan with fills, reinstate, settings save and validation);
  - refresh with an injected fetch (only unfinished results; a failed fetch reported);
  - the MCP tools.
- The full suite passes: 195 tests. 108 callbacks, 0 arity mismatches.
- **Real DB:** the journal is identical to the pre-change code apart from the new
  `age_days`/`stale` fields. No plan is stale or cancelled today. Read-only apart from
  the additive migration.
- Headless-Chrome screenshot of the journal with cancelled/stale rows and the cancel
  form. **Not click-tested in a browser;** the running app needs a restart.

## Long lists: recent first, paged, scrolling in place (implemented 2026-10-20)

**Why:** the Daily Trading Journal (36 entries) and the Trade Journal (22 plans) each
rendered every entry, so the page kept getting longer and harder to navigate. Defaults
agreed with the user:
- Daily: last 30 days, 10 per page.
- Trade Journal: the Active tab, 15 per page, closed/cancelled trades from the last 90
  days.

**Display only:** Summary Statistics, Performance, the weekly prompt and MCP still use
everything.

**Both lists:**
- Each sits in a scrolling panel (`SCROLL_PANEL`, max 640px) with a pinned header row
  (`STICKY_HEADER`), so the page around it no longer grows.
- A `dbc.Pagination` below the list is hidden when there's only one page.
- A "Showing a–b of n" line says what's filtered out.
- A new filter or tab resets to page 1. The render callback lists the pager's
  `active_page` as both Input and Output (a same-callback cycle, which Dash allows) and
  keeps the page only when the pager itself triggered.
- Pure helpers: `paginate` (clamps the page), `daily_obs_view`, `journal_page_view`,
  `journal_activity_date`.

**Daily Trading Journal:** a period select (`daily-obs-period`: 7 / 30 / 90 days / all,
remembered) and a search box (`daily-obs-search`). The search matches the note or the
date, and is deliberately not remembered so a forgotten search can't hide entries.

**Trade Journal:**
- **Tabs:** `filter-status` is now a button-style RadioItems tab group: Active (Open +
  Planned), Closed, Cancelled, All, with counts in the labels.
  - It has its own persistence key (`v1-status3`), because the old checklist saved a
    list of statuses.
  - The 2026-10-20 cancelled-plans note's `v1-status2` is superseded.
- **Period:** `journal-period` (30 / 90 days / this year / all) limits only finished
  rows (Closed by exit date, Cancelled by cancel date); live rows always show.
- **Sort:** `journal-sort` defaults to most recent activity (exit, cancel, entry, then
  plan date). The old fixed Plan Score order is still an option.
- **Stale warning:** shows only on the Active and All tabs.
- **Unchanged:** the symbol and score filters still apply first.
- **Remembered:** the period and sort are remembered via `PERSIST`; the page number
  isn't.

**Slip caught by the first test run:** the new constants were first placed after
`serve_layout`. The layout is built when the app is imported, so it raised a NameError.
They now sit next to `PERSIST`.

**Verified:**
- `tests/test_list_paging.py`, 6 tests: paging clamps, daily period/search/date-search,
  journal tabs/counts/period/both sorts/pages, and both real callbacks (page reset on a
  new filter, counts in the tab labels).
- The full suite passes: 201 tests. 108 callbacks, 0 arity mismatches.
- Real data through the real callbacks: Daily "Showing 1–10 of 21 entries (36 in
  all)", 3 pages; Journal Closed tab "1–15 of 20", most recent first. Checked with a
  headless-Chrome screenshot.
- **Not click-tested in a browser;** the running app needs a restart.

## Strategy v1.0, the Claude Project, and the Help pane (2026-10-22)

**Source of truth:** the strategy now lives in `claude-project/Strategy.md`. It supersedes every
older strategy description, including:
- `Current Trading Strategy 2026-08-26.txt`;
- the percentage ladder, "trend filter" and per-trade notes elsewhere in this file;
- `prompts/chart-analysis-system-prompt.md`.

Those stay as history only. The user's instruction is to rely on `Strategy.md` for strategy and
treat tool behaviour (including TAPEs) as fact.

**Files in `claude-project/`** (for a Claude Project):
- `Project Instructions.md`;
- `Strategy.md`;
- `Daily and Weekly Checklists.md`;
- `Tool Reference.md`.

Current sources of the six tools are copied into `indicators/`. The Pine scripts were read from
TradingView without saving anything, and `OptionsLevelsforTradingview.py` came from the sibling
repo.

**Key rules, summarised** (see `Strategy.md` for the rest):
- The Macro Regime v7.5 comes first. The *effective* regime adjusts for direction (Δ5/Δ20,
  flicker, flags).
- Limits by effective regime:

  | Effective regime | Per-trade risk | Heat | Max position | Tactics |
  |---|---|---|---|---|
  | Full Risk-On | 2.0% | 6% | 10% | Sniper + Pyramid |
  | Partial Risk-On | 1.5% | 5% | 10% | Sniper + Pyramid |
  | Neutral | 1.0% | 4% | 7.5% | Pyramid only |
  | Partial Risk-Off | 0.5% | 2% | 5% | Pyramid probes only |
  | Full Risk-Off | — | — | — | No new trades |

- At most 10 positions; long only; inverse ETFs a separate, off-by-default case.
- Trades in the RRSP/LIRA can graduate to investments.

**Checklists:** the new daily (21 items: pre-market / during market with a "When a trade
qualifies" sub-heading / after the close) and weekly (14 items) lists were loaded into TAPEs'
saved checklist definitions. They were parsed from the checklists file and passed
`validate_checklist`. Both lists had been on the built-in defaults, so nothing custom was
replaced.

**Help pane:** `HELP_PANE_MARKDOWN` was rewritten to summarise Strategy v1.0:
- where each tool fits;
- the regime → limits table and downgrade actions;
- the hard limits;
- the two tactics and trade → investment;
- a TAPEs day-to-day reference.

It still carries no live figures. The offcanvas is now titled "Help — Strategy, Tools & Risk" and
is 600px wide, so the table fits. Checked with a headless-Chrome screenshot; 201 tests pass, with
108 callbacks and 0 arity mismatches. **Keep the Help pane in step with `Strategy.md`** whenever
the strategy changes.

**Help pane reviewed (2026-11-01).**
- **Strategy (v1.2) gaps filled:**
  - Day trade in the regime table's tactics.
  - "Into Neutral" and "heat above the new ceiling" downgrade actions, and the deteriorating and
    improving definitions.
  - The portfolio-value definition, the reward check (Sniper R:R ≥ 1.5), and naming the listing.
  - Sniper's 4H/1W ladders and EMA50 exit; the campaign add condition, no adds in Partial
    Risk-Off, and one campaign per account + symbol.
  - Day trades: one open at a time, and the 20-trade review.
  - Graduation: 3 × weekly ATR and the 40-week average.
  - The Step 7 discipline check.
- **New "TAPEs screen by screen" section:** every card and the header (Account view, Data),
  replacing the shorter "Using TAPEs day to day". It includes the action register and plain-line
  FOCUS items.
- **Rule reference 4.5 fixed in two places:** `RULE_REF` only matched 4.1–4.4, so a `[4.5]`
  citation on a focus item was dropped; and the coaching prompt said "4.1-4.4 = Part 4 sections".
  Test: `test_day_trade_section_is_a_rule_reference`.
- **Checked:** headless-Chrome screenshots of all three parts at 600px. Use
  `--virtual-time-budget` or the shot is taken before Dash renders.

## Structured Daily Trading Journal (implemented 2026-10-23)

**Why:** free-text daily notes couldn't be checked or aggregated. The user supplied a fixed
template whose fields follow the Strategy's steps (`claude-project/Strategy.md`): REGIME
(composite, label, layers T/V/C/L/M/S/B, flags STRESS/CREDIT/BREADTH, weekly, Δ5, Δ20,
flicker → EFFECTIVE), FOMO/PANIC, PORTFOLIO, DRIFT REPORT ACTIONS, EXECUTION, WATCHLIST
(EXCH:TICKER, tactic, trigger, news-sourced), PSYCH, LESSON/PROPOSAL and MARKET.

**Choices made with the user:** Δ5/Δ20 come from logged entries; portfolio and drift are
pre-filled but editable; the 39 old free-text notes stay as legacy notes (not converted).

**Storage:** `db.daily_journal_entries`, one row per `obs_date` (PK), each field a column;
flags and watchlist as JSON (`flags`/`watchlist` on read). `save_daily_journal_entry`
keeps `created_at` from the first save. Legacy notes stay in `daily_observations`.

**Computation (`compute_effective_regime`, pure):**
- **Δ5/Δ20:** today's composite minus the logged entry 5/20 sessions back (business days);
  a neighbour within ±1 session is used when that day wasn't logged, exact match first, then
  the earlier side. A typed value overrides (stored with `delta5_manual`).
- **Flicker:** 2+ label changes in the last 10 sessions, needing 5+ logged labels; else
  "unknown" unless typed.
- **Deteriorating:** Δ5 ≤ −0.95, a better label in the prior 5 sessions, or any flag → the
  next-worse state's **limits**. **Improving:** Δ5 ≥ +0.95 and Δ20 ≥ 0, no flags → the
  next-better state's **tactics**. Flicker → the worst label in the window. Weekly 2+ states
  from daily → its limits if more cautious. Full Risk-Off (or 0% limits) → no new trades.
- `REGIME_LIMITS` mirrors the Strategy table; `regime_from_composite` flags a label that
  differs from the composite's band (hysteresis/override).
- The computed deltas, flicker, effective states, note and heat ceiling are **stored** with
  the entry, so a later rule change doesn't silently rewrite history.

**Checks:** `check_watchlist` (exchange prefix, held/where, held without a plan, plan status,
listing currency vs plan currency, tactic allowed by the effective tactics, news-sourced);
heat over the ceiling and positions at `MAX_OPEN_POSITIONS` (10); `journal_date_gap` marks an
entry first written more than a day from its date ("written Nd later" badge, warning on save).

**Pre-fill (today only):** `journal_portfolio_prefill` (heat via the same
`portfolio_with_cash`/`compute_portfolio_risk` path as the banner -- needs Refresh Live Prices;
positions = distinct non-cash account+symbol+currency; beta from Compute Market Exposure) and
`journal_drift_prefill` (from today's Drift Report; ✓ = reconciled).

**UI:** "Today's entry" opens `dj-modal`; each structured row has "Open" (edit by date) and
shows chips (label, composite, Δ5, EFF, flags, heat/ceiling, positions, date gap) plus
market/lesson/execution/watch lines. Legacy notes keep Edit/× with a "legacy" badge. Paging,
period and search cover both kinds (`journal_list_items`). The old free-text add box is gone.

**Downstream:** the weekly prompt adds `daily_journal_this_week` and the coaching system prompt
asks for a review of each lesson/proposal (re-paste it into the external chat). MCP
`get_daily_journal` now returns `entries` (structured) and `legacy_notes`.

**Verified:** `tests/test_daily_journal.py` (22 tests: deltas incl. neighbour/manual,
deteriorating/improving/flicker/weekly/Full Risk-Off, watchlist, date gap, pre-fills, form round
trip, scratch-DB storage, the real callbacks with writes stubbed); `test_mcp_notes.py` updated
(and now stubs the structured fetch). Full suite 229 tests; 113 callbacks, 0 arity mismatches.
Real DB: table created, 0 structured / 39 legacy, read-only otherwise. Headless-Chrome
screenshots of the form and list. **Not click-tested in a browser;** the app needs a restart.

## Weekly template, coaching actions, and rules in the coaching prompt (implemented 2026-10-23)

Three requests from the user, built together.

**1. Weekly template** (`weekly_entry_modal`, "Weekly entry" on the Weekly Review card and an
"Entry" button on each week in the list). It follows the weekly checklist:
- **Header:** week ending, balance and Δ in CAD and % (`weekly_balance_delta`, same-basis only).
- **Regime (weekly close):** weekly and daily label/composite, Δ20 (auto from the daily journal,
  typed only if that day wasn't logged), flags.
- **Posture next week:** `compute_effective_regime` on those fields, shown live and stored
  (`posture_limits`/`posture_tactics`/`posture_note`).
- **Book:** heat, positions, beta (pre-filled for the current week via
  `journal_portfolio_prefill`), largest theme and its %, which are typed because TAPEs has no
  sector tags. Beta above the S6.6 soft ceiling and a theme above 25% (S6.5) are flagged.
- **Results (R):** `weekly_results`, from trades closed in the 7 days to week_ending: trades,
  win %, average win and loss, expectancy, Sniper (scale-out) vs Pyramid (scale-in), and gap losses
  beyond −1R. Stored as a snapshot.
- **Last week's actions** (see 2) and **daily lessons**: each lesson from the week's daily entries
  gets Strategy change / Dropped plus a note.
- **Graduations/trims, watchlist** (checked with `check_watchlist` against the posture's tactics),
  **strategy changes, market.**

Storage: new columns on `weekly_reviews` (`db.WEEKLY_ENTRY_COLUMNS`, lists as JSON,
`db.weekly_entry_fields`/`decode_weekly_entry`). `entry_saved_at` marks a saved entry; until then
the form pre-fills from the week's last daily entry. Saving the entry also saves a staged balance.

**2. Coaching focus items become tracked actions.**
- `WEEKLY_COACHING_SYSTEM_PROMPT` tells the coach to end every reply with a `FOCUS NEXT WEEK` block:
  at most 3 one-line items, a carried action starting with its id (`A12`), each ending with rule
  numbers in brackets.
- Saving the coaching note (Save Week or the edit modal) runs `parse_focus_items` (the last such
  block; numbered or bulleted items; rule refs from brackets; `ref` for a carried id) and
  `db.replace_focus_actions` for that week. Carried items aren't re-created. Re-saving keeps ids
  for unchanged text. No block → a warning and no actions.
- `db.weekly_actions`: id (shown as `A<id>`), source_week, text, rules, status
  `open | done | not_done | dropped`, history of reviews per week. `not_done` keeps an action
  active; done/dropped close it. Not done / dropped need a reason.
- The next weekly entry lists the active actions (`actions_for_week`), so "last week's actions"
  is pre-filled. The weekly prompt carries `focus_actions` (open ones with history and
  `times_not_done`, and those reviewed this week); the coach is told never to restate an open
  action as new and to deal with ones stalled 2+ weeks.
- The coaching notes saved before this have no FOCUS block, so tracking starts with the next reply.
- **Plain-line items (2026-11-01):** copying a reply out of the claude.ai chat drops the list
  markers, so the 2026-10-31 reply's items were plain lines and none were found. With no numbered
  or bulleted items under the heading, the parser now takes the lines ending in a `[rule]`
  citation, or else the first three lines. A heading with no items gets its own warning.

**3. Strategy.md rules in the coaching prompt.** `coaching_system_prompt()` = the instructions +
Strategy.md Parts 2–5 and the change log (`strategy_rules_text`, read from the file each session;
Parts 1 and 6 are about evaluating a symbol). Citation scheme: F1–F10 Foundations, S1–S7 steps
(S6.3 = Step 6 check 3), 4.1–4.4, P5. The weekly prompt carries `strategy_version`; the coach asks
for a re-paste if it differs. The setup text in the card names the version it includes. The
instructions were also brought up to date (cash is now counted; reconciliation is per account).
**The external coaching chat needs the new setup message pasted once.**

**Elsewhere:** the weekly prompt adds `weekly_entry` (`weekly_entry_for_prompt`);
`get_weekly_journal` adds `entry`, `focus_actions` and `actions_reviewed` per week, and searches
them; the Help pane's weekly line describes the flow.

**Verified:** `tests/test_weekly_entry.py` (20 tests: parsing incl. the prompt's own example,
the prompt's parts and fallback, action storage and history on scratch DBs, the old-table
migration, action views, results and balance Δ with exact values, and the real callbacks with DB
reads and writes stubbed); `test_mcp_notes.py` updated. Full suite 252 tests; 118 callbacks,
0 arity mismatches. Real DB (read-only apart from the additive migration): 8 weeks, 0 actions;
the week to 2026-10-17 reads 5 trades, 40% wins, expectancy −0.53R, 3 gap losses. Headless-Chrome
screenshot of the form on real data. **Not click-tested in a browser;** the app needs a restart.

## Stop gap allowance in planned risk (implemented 2026-10-24)

**Why:** stops get jumped. Of 21 closed trades, the 6 that went to their stop lost 1.24R on
average, not 1R: USO, ARKW and CHPS gapped 0.78, 0.89 and 1.51 ATR through their stops. So the
real risk taken was about 25% above the per-trade limit, and planned R:R was overstated. The
coach's 2026-10-17 note suggested the same thing.

**Rule:** risk counted for planning = stop distance + `k` × ATR per share. `k` is the setting
`stop_gap_allowance_atr` (default `DEFAULT_STOP_GAP_ATR` 0.5, 0–3, edited in Data Management →
"Stop gap allowance"). 0.5 ATR on the standard 2.5 × ATR daily stop is 0.2R, close to the 0.24R
observed. ATR is the plan's `atr_at_commit` (the form's ATR field while planning). With no ATR, the
allowance is `k / STANDARD_STOP_ATR` (2.5) of the stop distance. `gap_per_share`,
`load_gap_allowance_atr`.

**Where it applies:**
- **Plan form:** `compute_plan_ladder(..., gap)` adds `max_loss_gap`/`rr_gap`; the summary shows
  both ("1.94 : 1 (1.61 : 1 with gap allowance)") and the allowance per share. The colour still
  follows the plain R:R.
- **Campaigns:** `compute_campaign_stages(..., gap)` adds `peak_risk_gap`, the allowance on every
  share held at each stage. With the standard ladder, peak risk equals the probe's, but the gap
  part grows with each add: real open campaigns read about 40% higher (PPLN 59 → 83, MCD
  131 → 184, KRE 111 → 156).
- **Pre-trade check:** the trade's risk includes the allowance (`gap_per_share` param; a campaign
  passes `peak_risk_gap`), and a new line gives the most shares within the 2% cap at stop distance
  + allowance.
- **Heat:** `compute_portfolio_risk(..., gap_by_lot=)`, from `gap_allowance_by_lot` (the lot's
  plan ATR, same lot → plan resolution as the ratcheted stops), in the banner, the pre-trade check
  and the daily journal's heat pre-fill. Unprotected lots stay at full value with no add.
- **MCP `get_plan`:** `gap_allowance_per_share`, `max_loss_with_gap`, `risk_reward_with_gap`.

**Unchanged by design:** closed-trade R and every stat built on it (R stays measured against the
planned stop, so gap losses remain visible beyond −1R); stop placement; the slippage/breach
classification.

**Adopted as Strategy.md v1.1 (2026-10-24), at the user's request:**
- S6.1 sizes on stop distance + 0.5 × ATR; a campaign's probe is sized on its peak risk with the
  allowance.
- S6.3 heat includes the allowance.
- 4.2's "peak risk never exceeds the probe's" now reads "the risk to the stop never exceeds the
  probe's".
- Part 6's plan format shows risk with the allowance.
- The change log has the 1.1 line, and the Help pane (now "v1.1") matches.

The coaching setup message and the Claude Project's copy of Strategy.md need re-pasting and
re-uploading by the user.

**Verified:** `tests/test_gap_allowance.py` (11 tests: per-share with and without ATR, the setting
and clamping, ladder, campaign and pre-trade figures, heat incl. an unprotected lot, the lot
resolution, both form callbacks, the setting callback, and `get_plan`); the 3 positional callback
tests updated for the new ATR argument. Full suite 263 tests; 119 callbacks, 0 arity mismatches.
**Not click-tested in a browser;** the app needs a restart.

## Matching fix: trades from before tracking start are never auto-matched (2026-10-26)

**Found on real data:** the user had two committed EWZ plans for one position:
- **873f4001,** planned 10-24 at 31.91;
- **19e80fc8,** planned 10-26 at 30.79, the actual fill.

What happened:
- Pyramid splitting gave the 124-share lot to the earlier plan.
- The later plan then fell through to the fuzzy matcher. Through the sole-candidate date
  exemption (the 2026-08-31 IBB rule), it matched an EWZ round trip that closed on 2026-08-07,
  before the TFSA's tracking start (2026-08-26). It showed as "Closed".

**Fix:** `match_plans_to_roundtrips(..., accounts=)` skips round trips that `is_archived`
(closed before the account's tracking start) as fuzzy candidates. Manual matches and
pyramid-split claims are unaffected. `_build_journal_and_unmatched` passes `accounts`, reading
them from the DB when not given.
- **Real DB:** of 28 plans, only 19e80fc8 changed.
- **Test:** `test_trade_closed_before_tracking_start_is_never_auto_matched` in
  `tests/test_accounts.py`.

**Data correction (at the user's request):** today's EWZ buy (124) and sell (62) belong to
19e80fc8. Set via `db.set_plan_lot_match(19e80fc8…, lot eced322bcfeb)`; a manual lot match
outranks the split's claim.
- 873f4001 now reads "Planned" with no fills. The user was advised to Cancel it.
- The lot id is unchanged if 873f4001 is cancelled (checked), so the manual match holds.

**Underlying cause, not changed:** two committed plans for one position read as a pyramid. A
superseded pre-plan should be edited, or cancelled before entry, rather than left beside a new
one.

## Campaigns: a same-day over-buy that's sold back is netted out (2026-10-29)

**Found on real data (MCD, campaign 8389d6d7):** Add 1 was planned at 12 shares. The user bought 24
by mistake and sold 12 back about 24 minutes later at the same price (257.39 → 257.40). TAPEs read
that as:
- Add 1 = 24 shares;
- the sell as a stop-checked exit ("the trail did its job");
- peak risk taken USD 273.58 instead of about 136, which would roughly halve the campaign's R at
  close.

**Rule (`find_overbuy_trims`, applied first in `analyze_campaign`):** a sell is a *trim* when, at that
moment, all of these hold:
- more shares are held than the plan's cumulative quantity for the stage reached;
- the sell is no larger than that excess;
- it's on the same day as the buy that created the excess;
- it's above the scheduled stop by more than the slippage tolerance.

The trimmed shares come off that buy. Then:
- the stage counts only its planned shares;
- the trim isn't an exit for the stop check;
- peak risk is measured without the excess;
- a note records it (quantity, buy and sell price, P&L).

The trim's P&L is real and stays in the trade's P&L. Open rows show the net shares, the net
average cost and P&L = trims + real exits against that cost. Closed rows keep the round trip's
total P&L. A sell on a later day, one larger than the excess, or one at the stop is never a trim.

**The drift report** (`compute_campaign_drift`) leaves trims out of "shares sold", so
`should_be_qty_open` stays at the planned ladder. The first version of this fix missed that, and MCD
showed "should be 12" (24 planned − 12 sold) instead of 24. Fixed and tested
(`test_drift_doesnt_count_the_trim_as_sold`).

**Real DB:** across 30 plans only MCD changed: Add 1 = 12 at 257.39, peak risk 136.01 (planned
131.24), net 24 shares at 251.60, trim +0.07. **Tests:** `OverbuyTrim` in `tests/test_campaigns.py`
(4 tests).

## Margin account added; weekly balances backfilled (2026-10-31)

**How it arrived:** a holdings upload on 2026-10-30 (evening) unintentionally included a margin
account, `HQBSHCY01CAD`, and TAPEs registered it with no type. Its snapshot (as of 2026-10-30 19:18)
has no positions and cash CAD −5,000.00 (money owed). The user then decided to include it from when
the LIRAs were added. They confirmed the balance was −5,000 for all of Oct 9 – Oct 30, with no
activity.

**Done (DB backed up first: `trade_journal.backup-pre-margin-20261031-121457.db`):**
- The account is now type Margin, nickname "Margin", `tracking_start_date` 2026-10-08.
- Weekly rows Oct 9, 17 and 23 each lost CAD 5,000:
  - Oct 9: 364,332.59 → 359,332.59;
  - Oct 17: 361,802.06 → 356,802.06;
  - Oct 23: 360,667.79 → 355,667.79.

  Each row's `balance_detail_json` cash CAD includes the −5,000, `cash_as_of` names the account,
  and its `note` records the source and the prior balance. Week-over-week changes are unchanged
  (−2,530.53, −1,134.27). Rows up to Oct 3 (TFSA only) were not touched.

**Live effects:** `portfolio_with_cash` already counts the −5,000 through the latest snapshot. That
covers heat %, the pre-trade check and the 2% cap, and Compute Balance. No activity export has
included this account, so it has no trades. To keep it current, it must be selected in future
holdings exports. Otherwise its 2026-10-30 snapshot goes "stale" after 7 days. The account isn't
registered (taxable; can borrow and short). Strategy.md's Foundation 7 doesn't cover it yet.

## Market exposure banner shows the percentages (2026-10-31)

The user saw "no %'s" after Compute Market Exposure. The calculation worked (real data: CAD 5,147,
1.44% of portfolio), but the banner only said it mid-sentence: account lines were dollars only and
positions weren't shown. `render_beta_exposure` now leads with "Beta exposure: X% of portfolio".
- Each account's line gives its beta-weighted CAD and its % of the portfolio.
- A table lists every position: beta, market value, beta-weighted CAD and % of the portfolio,
  largest first.
- A footnote explains the method.

The stored `store-beta-exposure` data (used by the journal pre-fills) is unchanged. Test:
`BetaBanner` in `tests/test_fx.py`. Checked with a headless-Chrome screenshot on real data.

## Day trades: a third plan type (implemented 2026-11-01, Strategy.md v1.2)

**Strategy:** the user pasted the day-trade draft into Strategy.md themselves (v1.2, dated
2026-10-31). The leftovers were tidied:
- the version line;
- a duplicate Step 7 news bullet;
- the Part 6 day-trade block, which sat outside the code block;
- the 1.2 change-log indentation.

The interim "until TAPEs has a Day trade plan type" paragraph in 4.5 was replaced with how TAPEs
now handles one. The Help pane reads v1.2 and has a Day trade block.

**Design:** a day trade is a scale-out plan underneath: entry, intraday stop, T1/T2, Ladder stop.
Scoring, R, heat and the gap allowance (0.5 × the 5m ATR) need no changes. On top:
- **Plan type `day_trade`** (`PLAN_TYPES`, `plan_type_of`, `is_day_trade`). New plan columns
  `day_catalyst` and `day_setup` (`DAY_SETUPS`: panic fade / gap-and-go / VWAP pullback).
- **Timeframes:** `INTRADAY_TIMEFRAMES` 5m/15m. They're in `ATR_TIMEFRAME_LABELS` and the selector,
  and Yahoo fetches 5d of 5m/15m bars. `validate_swing_timeframe` keeps them off every other plan
  type, because the trail code only knows 4H/1D/1W.
- **Rules (`validate_day_trade`):** catalyst and setup required, timeframe 5m/15m, Ladder only, a
  stop on the losing side, at least one target. `day_trade_targets`: T1's Move Stop To defaults to
  the entry.
- **It owns its symbol:** `_campaign_conflict` treats a day trade like a campaign. A new day trade
  is refused on a symbol already held in that account (`_held_conflict`, which reads the DB's open
  positions).
- **Same-day only:** the fuzzy matcher only pairs a day trade with a position opened on its planned
  date. Day trades never take part in pyramid splitting.
- **Carried overnight:** `build_journal_row` now wraps `_journal_row` and adds
  `_add_day_trade_notes`: "flat the same day", "open, flat by 15:45 ET", or "⚠ carried overnight",
  with `row['carried_overnight']`. The drift report (`_drift_for_row`) sets `day_trade_overdue`,
  should-be 0 shares and "⚠ Day trade carried overnight: sell it", sorted first. MCP drift adds
  `day_trade_overdue`.
- **Pre-trade check:** the cap is half the per-trade cap (`DAY_TRADE_RISK_FRACTION`; `cap_pct`
  param), and so is the most-shares line. It also shows "Day trades today: n of 2" and "Day-trade
  losses today vs 1% of the portfolio", turning red at either limit. Informational only, like every
  cap.
- **Form:** a Day trade section (`daytrade_block`: catalyst, setup, a rules summary). On Add Plan,
  switching to Day trade fills 5m, Stop ATR × 1.0, Ladder, T1 1R / T2 2R at 50/50; switching away
  puts the timeframe back to 1D (`register_day_trade_callbacks`). The Edit modal loads and saves
  the two fields.
- **Reporting:**
  - The strategy filter gains "Day trades". `weekly_results` gains a Day bucket and
    `day_trades_carried_overnight`.
  - The weekly rows carry `day_trade` {catalyst, setup, carried_overnight}, and the coaching prompt
    explains day trades.
  - The detail view shows the setup, catalyst and time stop.
  - Cancelled day trades aren't replayed (that would need intraday bars).
  - MCP `create_plan` takes `plan_type='day_trade'`, `day_catalyst` and `day_setup`; the timeframe
    defaults to 5m.

**Runtime error found by the user right after the build:** "Output plan-stop-method.value@… is already
in use". The day-trade defaults callback and the campaign toggle both fired on `plan-plan-type`, and
both wrote `plan-stop-method` with `allow_duplicate=True`. Dash suffixes a duplicate output with a
hash of the callback's *inputs*, so the two collided. Fixed by moving the day trade's stop method
into the campaign toggle (`_toggle_plan_type`).

Neither the arity check nor the in-process server start catches this, since Dash rejects it in the
browser. `CallbackGraph.test_no_output_is_written_by_two_callbacks` (in `tests/test_day_trades.py`)
now fails on any output id + suffix registered twice; a throwaway two-callback app confirmed it
detects the case. **Lesson:** when two callbacks share an Input, never let both write the same output.

**Not checked by TAPEs:** the 9:45–14:30 ET entry window and the 15:45 time stop. Wealthsimple
export times can't be trusted to the minute, so only the date is checked. **Superseded
2026-11-07:** the export's effective_time is the actual fill time, confirmed by the user. The
Practices scorecard now checks a day trade's entry at 9:45 or later and that it's flat by 16:00
(the user's 4 pm rule). The 14:30 entry cut-off still isn't checked.

**Verified:** `tests/test_day_trades.py` (16 tests) plus the updated weekly-results test. The full
suite passes (288 tests); 122 callbacks, 0 arity mismatches. The real DB journal is identical for
all 33 plans (old vs new code). Headless-Chrome screenshot of the form section. **Not click-tested
in a browser;** the app needs a restart. The coaching setup message changed (prompt text + v1.2
rules), so the external chat and the Claude Project's Strategy.md need updating by the user.

## Limits and tactics banner; risk checks follow the regime (implemented 2026-11-02)

**Why:** the user wanted the allowed tactics and limits on screen. The pre-trade check and heat
banner used fixed numbers (2% per trade; heat caution 5%, warning 6%), so on the 2026-11-02 entry
(Partial Risk-Off limits: 0.5%, 2% heat) a 1.5% trade still read as fine. Agreed with the user: both
checks now follow the limits in force. They still only warn and never block a save.

**`current_posture(daily_entries, weekly_rows, today)`:**
- The newest daily or saved weekly entry with an effective regime, not dated after today. A weekly
  entry wins a tie on date, since it's written after Friday's daily entry.
- `stale` = 2+ weekdays since its date, so a Friday entry is current on Monday. Holidays aren't
  known, so it can warn early, the same trade-off as the data-freshness banner.
- A stale entry still drives the checks; the banner turns amber and says how old it is.

**`posture_caps`:** per-trade cap and heat thresholds.
- Warning is the ceiling; caution keeps its share of it (5/6).
- Full Risk-Off has no ceiling, so any open risk warns.
- No entry at all falls back to the fixed maximums, and the banner and form say so.
- Day trades stay at half the per-trade cap.

**Where:**
- `posture-banner`, under the data-freshness banner (`render_posture_banner`). Limits and tactics
  are separate lines, since a deteriorating regime gives them different states.
- `_update_pretrade_risk`, `compute_pretrade_risk_check` (`caution_pct`/`warning_pct`) and
  `_render_portfolio_risk_banner`.

**Also:** `REGIME_LIMITS` gained day trades in Full/Partial Risk-On and Neutral, to match Strategy
v1.2's table.

**Tactics and max position in the pre-trade check (same day, at the user's request):**
- **`tactic_warning`:** a scale-out plan is Sniper, a scale-in plan is Pyramid. It flags a plan type
  the tactics in force don't allow, a campaign with adds when only a probe is allowed, and any new
  trade under Full Risk-Off. There's no warning without a usable entry.
- **`pretrade_position_check` (Strategy 6.2):** shares x entry as a % of the portfolio, against the
  max position (10% with no entry; skipped under Full Risk-Off, which is flagged as no new trades).
  - A campaign is measured at each stage's trigger, since an add can breach the cap the probe fits.
  - Shares already held in the same account + symbol + currency count too (a pyramid lot), at their
    staged price. A position across accounts isn't combined.
  - The "most shares" line gives the shares the position cap allows too, since 6.2 takes the smaller.
- Both turn the check red, and neither blocks a save. The Edit modal has no pre-trade check, so
  neither runs there.

**Verified:** `tests/test_posture.py` (including the real form callback and the heat banner); the
full suite and the callback check pass. Headless-Chrome screenshot of the real page. Real data:
daily entry for 2026-11-02, limits Partial Risk-Off, tactics Neutral.

## Edit modal no longer rewrites a plan on opening (fixed 2026-11-03)

**Found:** the user couldn't save EWZ 19e80fc8 with one target. Reproduced by clicking through a real
page with saves and ATR fetches stubbed. Filling the modal writes the symbol, entry, ATR and R×
fields, and those are the auto-fetch and recompute callbacks' Inputs. So while Method was ATR-derived:
- opening a plan fetched a **new ATR** (TradingView first, so it could also move the live chart);
- that recomputed the **stop and every target**, and Save then stored them;
- a row with a blank R× got the default 1R-6R spread, so an emptied target came back with no Qty %,
  and Save refused it ("Every target with a price needs a Qty %"). The user's workaround was 0% rows.

This was the open question listed below about re-opening ATR-derived plans, and it did real damage.

**Fix:**
- `store-edit-loaded` holds what the modal was filled with. `_auto_fetch_atr` and
  `_auto_apply_stop_targets` ignore a write that matches it (`edit_form_unchanged`).
- `auto_target_prices`: a blank R× is no target, and clearing an R× removes it. On the Edit modal an
  emptied price also stays empty. One R× change moves only that target, not the stop or other rows.
- The same blank-R× rule fixes the Add Plan form's day-trade defaults, which left T3-T5 priced with
  no Qty %. Switching away from Day trade restores the default R× spread.
- **Still by design:** changing symbol, currency or timeframe on Edit fetches a new ATR and recomputes
  while Method is ATR-derived. For a past trade, set Manual override first.

**Saved plans:** seven differed from the 2026-10-31 backup. The user confirmed six were deliberate
(CHPS a0b4bcfe, MCD 8389d6d7, TLT a8c54f4e, IWM 04d55493, WSP b54224a9, XLV ecd14f29). The 17 plans in
the 2026-10-07 backup are unchanged. A plan created after that and edited before 2026-10-31 can't be
checked, because there's no backup of it from before the edit.

**EWZ 19e80fc8 repaired, at the user's request** (backup `trade_journal.backup-pre-ewz-fix-20261103-194623.db`):
- **Stop 30.562 → 28.30**, which is 2.5 × the daily ATR at the Oct 23 close (0.9961, Yahoo). The saved
  0.0912 was a 15m ATR fetched on Nov 3, and the backup's 1.6302 wasn't the Oct 26 figure either.
- **ATR** 0.9961, 1D, Manual override, so nothing recalculates. Later Edit saves will ask for 5m/15m,
  because `validate_day_trade` requires it; with Manual override, changing that leaves the stop alone.
- **One target:** the user's 32.614 at 100%, moving the stop to the entry. The 0% rows are gone.
- **Results:** R 13.16 → 1.20; expectancy 0.53R → 0.01R; profit factor 2.32 → 1.03. No other plan changed.

**Tests:** `EditFormKeepsThePlan` in `tests/test_plan_ladder.py`; re-clicked in a real page.

## Closed trades are locked (implemented 2026-11-09)

**Why:** positions, round trips and plan matches are rebuilt from the fills on every load, so a
finished trade's match, and with it R, Plan Score and the stats, could change later (a new plan, an
import, a tracking-start date, a rule change). EWZ (2026-10-26) and IBB (2026-08-31) both did. A plain
"closed" flag was rejected: it would only repeat what the fills say, and could go stale.

**`db.plan_locks`:** plan_id (cascade on delete), rt_id, closed_at, locked_at, `snapshot_json` (R,
P&L, Plan Score, stop outcome, currency at the moment of locking). `save_plan_lock` / `delete_plan_lock`
/ `fetch_plan_locks`.

**Matching:** `match_plans_to_roundtrips(..., locks=)` applies locks right after manual matches, before
pyramid claims and the fuzzy matcher. Each row gets `rt_id` and `lock_state`:
- `locked` (incl. a manual match on the locked trade itself), with `lock_changes` = [(label, was, now)]
  where the result differs from the snapshot (`lock_changes`, 0.005 tolerance);
- `broken`: the locked rt_id no longer exists (a corrected or late fill, or the trade reopened). The
  plan is matched afresh and shown as the fills read now;
- `superseded`: a manual match moved it.

**Writing:** `compute_journal` reads the locks and calls `sync_plan_locks` (pure decision:
`plan_lock_updates`): a newly closed trade, or one a manual match moved, is locked; a superseded lock on
a trade that isn't closed is deleted; a broken lock is left for the user. `_build_journal_and_unmatched`
reads locks from the DB when not given (MCP paths), but only `compute_journal` writes.

**UI:** "Closed 🔒" (tooltip with the lock date). "⚠ Fills changed since it closed" or "⚠ Result
recalculated" with an **Accept** button (`accept_lock_cb`): it deletes the lock and re-sends
store-plan-matches, so the journal rebuilds and locks the trade as it reads now. Match → Clear also
deletes the lock.

**Real data:** DB backed up first (`trade_journal.backup-pre-locks-20261109.db`). All 30 closed matches
were reviewed beforehand (each plan against its own trade; the wide gaps are the known after-entry
plans) and locked on the user's next page load; 0 changes, journal statuses unchanged.
- **EWZ 19e80fc8 is locked at R 13.16:** the 2026-11-03 repair was undone by a plan save at
  2026-11-04 00:55 UTC (stop back to 30.562, ATR 0.0912 15m). If it's repaired again, it shows as
  "Result recalculated" until accepted.

**Tests:** `tests/test_plan_locks.py` (10: a later plan can't take a locked trade, broken and
recalculated locks, manual matches, the update decisions and tolerance, storage + cascade on a scratch
DB, and the compute/accept/clear callbacks with writes stubbed). Full suite 459.

## Open / Not-Yet-Decided

- **The "Coaching" piece of TAPEs has started, as of 2026-08-29 — see the new
  "Weekly Review" section below.** Everything before that date had gone into
  Analysis and Logging only (plan schema, ATR-derived stops/targets,
  execution reconciliation, scoring, psych sliders, screenshot attachment,
  observation log). A full interactive coaching chat (ask any question,
  get an answer grounded in your data) is still **not** built and was
  deliberately deferred in favor of this narrower, lower-risk first step —
  see that discussion's conclusion: for precise structured questions
  ("success rate when FOMO is -2"), tool-calling/exact queries beat
  classic embedding-based RAG, and an in-app interactive chat is a much
  bigger lift than it looks (new LLM API dependency + cost per query, an
  agentic tool-calling loop to build from scratch, a real chat UI with
  streaming/conversation-state concerns, and careful scoping of which tools
  a casual chat box should be allowed to call versus the MCP server's full
  read/write set). Revisit that fuller vision once the weekly note proves
  useful and there's more history for it to work with.
- **Open positions need a plan before closed-history gaps do** — resolved as of
  2026-08-27. The Open Positions card cross-references each held symbol against
  `store-plans` (any status — a draft in progress still counts as "being planned
  for") and shows a Plan?/No plan badge + "Log Plan" button, prefilling symbol/BUY/
  avg price/qty. This sits above "Executed Trades Without a Plan" in the page —
  deliberately: an unplanned open position is live risk, an unplanned *closed*
  trade is just a journaling gap.
- **`app.layout` was a snapshot frozen at process-start, not per-session** —
  resolved as of 2026-08-27. It's now `app.layout = serve_layout`, a callable Dash
  re-invokes for every new browser session, so each one re-reads the DB fresh. The
  bug this fixed: a long-running server process (started once, left running) would
  keep serving whatever `store-executions`/`store-plans`/etc. looked like at
  startup to any *new* browser session, discarding anything imported or changed
  since — reported as "Open Positions doesn't persist across sessions." Confirmed
  fixed via an isolated test harness (own `db.py` + scratch `trade_journal.db` in
  one directory, no `db.DB_PATH` reassignment): inserted a row into the DB while
  the server kept running, re-fetched `/_dash-layout` from that same process with
  no restart, and the new row appeared.
- **Full trade-plan schema** — resolved as of 2026-08-26. `plans` now has: the original
  symbol/direction/dates/entry/stop/`targets_json`/qty/reason/notes, plus `status` +
  `committed_at` (draft/commit lifecycle), `target_method`/`atr_at_commit`/
  `atr_multiplier` (ATR provenance). A separate `plan_matches` table holds manual
  plan↔round-trip overrides, keyed on a deterministic round-trip id rather than the
  execution/plan rows themselves, so it survives recomputation.
- Whether the 3%-style override should eventually become automatic per-symbol
  (currently: manual only, by design, until proven out).
- Exact mechanics of the plan-adherence scoring algorithm — still just qualitative
  notes + the profitability-based blended score; no separate formal adherence metric.
- The TAPEs-committed plan overlay on the TradingView chart (workflow step 6) — not
  built. Newly *feasible* now that TradingView MCP is reachable from Claude Code, but
  nobody has wired either a manual "ask Claude to draw it" step or a CDP-based
  auto-draw into TAPEs itself.
- TradingView symbol/timeframe correction (now automatic, triggered by editing the
  symbol field rather than a button) has been confirmed by the user in the actual
  running app (2026-08-27), on top of earlier direct-function-call and live-CDP
  testing. Still not click-tested end to end in a real browser: the automatic
  stop/target recompute, the "% from entry" readouts, Save Draft → Commit, and the
  Match modal.
- Opening the Edit modal on an existing plan whose `target_method` is "ATR-derived"
  re-populates entry/ATR, which re-triggers the automatic recompute (by design —
  see step 4) — for a plan whose stored stop/targets already came from that same
  ATR+multiplier formula this reproduces the same numbers, but it hasn't been tested
  against a plan where they don't (e.g. an ATR-derived plan hand-tweaked without ever
  flipping the radio to "Manual override"). If that turns out to matter in practice,
  the fix is either warning on the mismatch or making Method sticky at the field level.
- "Executed Trades Without a Plan" is now a sortable/filterable `dash_table.DataTable`
  with a single "Log Plan for Selected Trade" button (row-select, then click) instead
  of a per-row button — not yet click-tested in a real browser either.
- Beta-weighted net market exposure (see its own section above) is verified end-to-end
  via direct function calls against the real database, and the MCP tool has been
  called directly too, but the new "Compute Market Exposure" button/banner on the Open
  Positions card has not yet been click-tested in a real browser session.

## Environment Notes

- claude.ai chat (with TradingView MCP connected) is used for market analysis, Pine
  Script discussion, and architecture/strategy conversations.
- Claude Code (VS Code, this repo) is used for all actual TAPEs implementation — data
  model, Dash app, reconciliation logic, scoring. As of 2026-08-26 it *also* has its own
  TradingView MCP connection (user-scoped, so it follows into any repo) — useful for
  reading the live chart directly from a Claude Code session, but this is a separate
  connection from TAPEs's own CDP client (see the Three-System Architecture note above)
  and from claude.ai chat's MCP connection; none of the three share state with each other.
- These two environments (claude.ai chat and Claude Code) do not share conversation
  context automatically, even though both can now reach TradingView. This file is the
  handoff mechanism — keep it updated as decisions are made or changed, in either
  environment.