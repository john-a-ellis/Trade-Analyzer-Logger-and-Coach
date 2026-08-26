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
| **TradingView** | Opportunity scanning/analysis, home of two custom Pine Script indicators | Yes, via MCP, in the claude.ai chat environment |
| **TALCS** (this repo) | Trade planning, logging, execution reconciliation, coaching/scoring | Yes, via Claude Code, working directly in this repo |

**Wealthsimple is the single source of truth** for what actually happened in the
account (fills, timing, quantities). TradingView indicators inform *planning*, not
final record-keeping.

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
4. **The initial computed plan is always ATR-based.** A 3% (or symbol-specific,
   e.g. 6% for SSO/QLD) initial-target override is available but is a **manual, deliberate
   edit at plan finalization** — not an automatic per-symbol rule (for now — may become
   automatic later, but don't build it that way by default yet).
5. TALCS allows full editing of the plan before it's committed.
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
7. Trade executions are re-downloaded from Wealthsimple at the end of any day a trade
   was made (not just at position close), to keep TALCS current.
8. **Reconciliation approach:** TALCS attempts to auto-match newly downloaded
   Wealthsimple execution rows to the nearest still-open TALCS plan by symbol. This is
   necessary because a single planned trade typically corresponds to multiple separate
   sell rows in the Wealthsimple export (50% out at TP1, 50% of remainder at TP2, rest
   at TP3+), potentially spread across several days, and the same symbol may be
   re-entered later as a fresh, unrelated trade. The user can **manually override** the
   match when the auto-match guesses wrong.
9. On trade close, TALCS scores the trade on two axes: **profitability** and
   **plan-adherence** (how closely actual execution followed the committed plan —
   entry, stop discipline, scale-out timing/levels).

## Open / Not-Yet-Decided

- Full trade-plan schema (fields for plan vs. execution vs. scoring) — not yet
  finalized as of this writing; next step in the design conversation.
- Whether the 3%-style override should eventually become automatic per-symbol
  (currently: manual only, by design, until proven out).
- Exact mechanics of the plan-adherence scoring algorithm.

## Environment Notes

- claude.ai chat (with TradingView MCP connected) is used for market analysis, Pine
  Script discussion, and architecture/strategy conversations.
- Claude Code (VS Code, this repo) is used for all actual TALCS implementation — data
  model, Dash app, reconciliation logic, scoring.
- These two environments do not share context automatically. This file is the handoff
  mechanism — keep it updated as decisions are made or changed, in either environment.