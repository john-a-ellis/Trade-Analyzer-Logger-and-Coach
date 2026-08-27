# TALCS — Trade Analysis, Log and Coaching System

This file gives Claude Code standing context on this project. Read this before making
architectural decisions or extending the data model.

## What TALCS Is

A Plotly Dash app for logging discretionary ETF/stock trades: the plan taken *before*
entry, what actually happened, and a post-trade score on both profitability and
plan-adherence. Part of a three-system trading framework — see below.

## The Three-System Architecture

| System | Role | Connected to Claude? |
|---|---|---|
| **Wealthsimple** | Trade execution (TFSA + retirement accounts) | No — no public API. Manual only, by design. Never automate execution here, even if a connector existed. |
| **TradingView** | Opportunity scanning/analysis, home of two custom Pine Script indicators | Yes — via MCP in the claude.ai chat environment, **and** (as of 2026-08-26) via the same MCP server added to Claude Code itself, user-scoped so it's available from any repo, not just this one. |
| **TALCS** (this repo) | Trade planning, logging, execution reconciliation, coaching/scoring | Yes, via Claude Code, working directly in this repo |

**Wealthsimple is the single source of truth** for what actually happened in the
account (fills, timing, quantities). TradingView indicators inform *planning*, not
final record-keeping.

**Important distinction:** Claude Code having the TradingView MCP connection does
*not* mean TALCS (the Dash app) can call MCP tools — TALCS runs as its own standalone
Flask process, independent of any Claude session, so it has no path to invoke MCP.
Where TALCS itself needs live TradingView data (the ATR fetch — see below), it talks
directly to TradingView Desktop's Chrome DevTools Protocol (CDP) debug port
(`127.0.0.1:9222`), the same port the tradingview-mcp server connects to. CDP allows
multiple independent debugger connections to one target, so TALCS's direct read and a
Claude Code session's MCP connection can both be live at once without conflicting.

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

## Confirmed Workflow (this is the design TALCS should implement)

1. Use Sniper + Macro Regime Model in TradingView to identify and assess trade
   opportunities.
2. Execute the trade manually in Wealthsimple.
3. Enter the actual fill price into TALCS. TALCS pulls a **fresh ATR reading at commit
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
      not a bug in TALCS. The user manages the layout manually; symbol and
      timeframe correction both tested reliable (verified live via an independent
      TradingView MCP connection, not just TALCS's own read) and stay automatic.
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
5. TALCS allows full editing of the plan before it's committed.
   **Implemented** as a draft/commit lifecycle: "Save as Draft" vs. "Commit Plan" on the
   Add Plan form, a "Draft Trade Plans" card (Edit/Commit/Discard), and `plans.status` +
   `plans.committed_at` in the schema. Drafts are excluded from the journal, scoring, and
   execution matching until committed.
6. Once committed, TALCS draws the finalized plan as a **second, separate overlay** on
   the TradingView chart via the MCP drawing tools (lines/boxes) — this is distinct
   from Sniper's own native plotted lines, which stay as-is. Both overlays should be
   independently toggleable:
   - Sniper's native lines: controlled via the indicator's own `showVisuals` input in
     TradingView, as it already exists.
   - The TALCS-committed plan overlay: needs its own show/hide control in TALCS's UI
     (it's drawn externally via MCP, not part of the Pine script, so it cannot live in
     TradingView's indicator settings panel).
   - This overlay is **session-live only** — drawn into the current TradingView browser
     session via MCP, not a persistent saved annotation. It needs to be re-applied each
     time you want it visible in a fresh session.
   **Not yet implemented.** Now technically more reachable than before — Claude Code
   itself can drive the TradingView MCP drawing tools (`draw_shape` etc.) directly in a
   chat session — but TALCS the Dash app still has no code path to trigger that
   automatically on commit. Building this would mean either (a) doing the draw manually
   via a Claude Code/claude.ai request after each commit, or (b) giving TALCS its own
   CDP-based drawing calls the same way the ATR fetch works. Neither is built yet.
7. Trade executions are re-downloaded from Wealthsimple at the end of any day a trade
   was made (not just at position close), to keep TALCS current.
8. **Reconciliation approach:** TALCS attempts to auto-match newly downloaded
   Wealthsimple execution rows to the nearest still-open TALCS plan by symbol. This is
   necessary because a single planned trade typically corresponds to multiple separate
   sell rows in the Wealthsimple export (50% out at TP1, 50% of remainder at TP2, rest
   at TP3+), potentially spread across several days, and the same symbol may be
   re-entered later as a fresh, unrelated trade. The user can **manually override** the
   match when the auto-match guesses wrong.
   **Implemented.** A "Match" button on each journal row opens a picker of candidate
   round trips (same symbol+direction) and persists the override in a `plan_matches`
   table, keyed on a deterministic round-trip id (`app.round_trip_id`, a hash of the
   composing execution row ids — not a random uuid, so it's stable across recomputes).
9. On trade close, TALCS scores the trade on two axes: **profitability** and
   **plan-adherence** (how closely actual execution followed the committed plan —
   entry, stop discipline, scale-out timing/levels).
   **Partially implemented.** Profitability (P&L, %, R-multiple, a blended Plan Score)
   is fully computed. Plan-adherence exists today only as qualitative execution notes
   (targets hit, stop breach/respect, entry deviation %) plus that same blended score —
   there is no separate, formal plan-adherence number yet. See Open items below.

## Open / Not-Yet-Decided

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
- The TALCS-committed plan overlay on the TradingView chart (workflow step 6) — not
  built. Newly *feasible* now that TradingView MCP is reachable from Claude Code, but
  nobody has wired either a manual "ask Claude to draw it" step or a CDP-based
  auto-draw into TALCS itself.
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

## Environment Notes

- claude.ai chat (with TradingView MCP connected) is used for market analysis, Pine
  Script discussion, and architecture/strategy conversations.
- Claude Code (VS Code, this repo) is used for all actual TALCS implementation — data
  model, Dash app, reconciliation logic, scoring. As of 2026-08-26 it *also* has its own
  TradingView MCP connection (user-scoped, so it follows into any repo) — useful for
  reading the live chart directly from a Claude Code session, but this is a separate
  connection from TALCS's own CDP client (see the Three-System Architecture note above)
  and from claude.ai chat's MCP connection; none of the three share state with each other.
- These two environments (claude.ai chat and Claude Code) do not share conversation
  context automatically, even though both can now reach TradingView. This file is the
  handoff mechanism — keep it updated as decisions are made or changed, in either
  environment.