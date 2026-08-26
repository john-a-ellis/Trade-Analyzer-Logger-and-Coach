# Trade Journal & Execution Scoring

A Plotly Dash app for logging why you took a trade and what your plan was —
entry, an initial protective stop, and up to five price targets with a
stop that ratchets as each target is hit — importing your actual fills from
a Wealthsimple activity export, and seeing how well the plan was executed
and how profitable it actually was. All data is saved to a local SQLite
database, so nothing is lost between sessions.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Then open the URL it prints (usually `http://127.0.0.1:8050`).

## How it works

**1. Add a Trade Plan** — log the reasoning up front. Only Symbol,
Direction, Entry Price and Quantity are required. Everything else is
optional:

- **Initial Protective Stop** — where you'll cut the loss if the trade
  doesn't work.
- **Up to 5 Price Targets**, each with:
  - **Price** — the level you're scaling out at.
  - **Qty %** — how much of the position to exit there.
  - **Move Stop To** — where to ratchet the protective stop once that
    target is hit (e.g. move it to breakeven after target 1, then trail it
    up behind each subsequent target). Leave it blank if you don't want to
    move the stop at that step.
- **Reason / Setup** and **Notes** — your thesis, free-form.

Leave any target row's price blank to skip it — you don't need all 5.

**2. Import Executions (CSV)** — drag in a Wealthsimple activity export (or
any CSV with symbol/date/action/price/qty-like columns — it will try a
best-effort generic parse). Individual buy/sell fills are automatically
grouped into completed **round-trip trades** per symbol using FIFO lot
matching, so partial fills and scaling in/out are handled correctly. Only
long trades (buy-to-open, sell-to-close) are auto-detected, since that's
what a Wealthsimple cash/TFSA/RRSP account produces.

**3. Matching** — plans are automatically matched to the closest-in-time
executed round trip for the same symbol and direction. You don't have to
link them by hand. If you import trades you never logged a plan for, they
show up under **"Executed Trades Without a Plan"** with a one-click **Log
Plan** button that pre-fills the form so you can document them
retroactively.

**4. The Journal** — every plan (matched or not) becomes a row. For matched
rows you get:

- **Actual $ P&L and % return** — always shown, since it doesn't depend on
  having set a stop or any targets.
- **R-multiple** — P&L relative to your planned risk (`entry − initial
  stop`), if you set an initial stop.
- **Plan Score** — `actual P&L ÷ planned reward × 100`, where the planned
  reward is a blend of your active targets weighted by their Qty %. 100%
  means you captured exactly the blended planned reward; less means you
  left money on the table; negative means a loss.
- **Targets column** — e.g. "3/4", how many of the planned targets were
  actually reached by an exit fill.
- **Execution notes** — plain-language flags, e.g. "Hit 3 of 4 planned
  price target(s)," "Final exit at 109.80 breached the stop level in effect
  at that point (110.00)" (the *ratcheted* stop, not just the original
  one), or "Entered X% better/worse than planned."

Click **View** on any row to see the full **Target Ladder** — each target's
planned price/qty%/stop-move alongside whether it was hit and the actual
fill that matched it — plus a price-path chart showing the entry, every
exit fill in order, the initial stop, the final ratcheted stop (if
different), and each target as a reference line.

Use the Symbol and Score filters to slice the journal, and **Download
Journal as CSV** to export everything.

### How target/stop matching works

Since the app only sees your actual fills (not the full intraday price
path), it matches targets to fills using a simple, transparent rule:
targets are checked in the order they'd naturally be reached (nearest the
entry price first), and each is matched to the earliest not-yet-used exit
fill whose price reached that level. Once a target is hit, if it has a
"move stop to" value, the effective stop ratchets there. The *final* exit
fill (chronologically last) is then checked against whatever stop was in
effect at that point — so a loss that happens after you've already moved
your stop to breakeven is correctly flagged as a stop breach, even though
it's above your original stop. This is a reasonable approximation for
journaling, not an exact fill-by-fill reconciliation (one fill is only
ever attributed to one target).

**Editing / deleting a plan** works the same way for the target ladder —
click **Edit** on any journal row to open all 5 target rows pre-filled,
change what you need, and save.

## Data persistence (SQLite)

All trade plans and imported executions are saved to a local SQLite
database (`trade_journal.db`, created automatically next to `app.py` the
first time you run it). Nothing is lost when you close the browser tab or
restart the app — it's reloaded from the database on startup.

- **Executions** — every fill from a CSV import is saved, deduplicated on
  (symbol, timestamp, action, qty, price), so re-importing the same or an
  overlapping export is safe and just skips rows already on file. Round
  trips are recomputed from the full execution history each time, so they
  always reflect everything you've ever imported.
- **Plans** — full CRUD from the UI:
  - **Create**: the "Add Trade Plan" form.
  - **Read**: plans reload automatically into the journal on every app
    start.
  - **Update**: click **Edit** on any journal row to open a modal, change
    any field, and save — updates the database immediately.
  - **Delete**: click **Del** on any journal row; you'll get a confirmation
    prompt before it's permanently removed.
- **Truncate** — the **Data Management** card (below the "Executed Trades
  Without a Plan" section) shows how many plans and executions are
  currently stored, and has two buttons, each behind a confirmation prompt:
  - **Delete All Trade Plans** — clears the `plans` table only.
  - **Delete All Imported Executions & Trades** — clears the `executions`
    table (and therefore the round trips derived from it); your trade plans
    are untouched, so you can re-import a corrected CSV without re-entering
    your reasoning.

If you want to inspect or back up the data directly, `trade_journal.db` is
a standard SQLite file — open it with `sqlite3 trade_journal.db`, DB
Browser for SQLite, or any SQLite client. `db.py` also exposes the
underlying functions (`fetch_plans`, `fetch_executions`, `insert_plan`,
`update_plan`, `delete_plan`, `delete_all_plans`, `delete_all_executions`,
etc.) if you want to script against it directly.

## Notes on the data / methodology

- Commission is included in P&L calculations where present in the CSV (your
  export shows $0 commission on all trades, which Wealthsimple confirms is
  normal for their self-directed accounts).
- Trades are matched and scored in their native currency (CAD or USD) — the
  app doesn't attempt FX conversion, so a mixed-currency portfolio's total
  P&L in the summary panel is a sum across currencies and should be read
  with that in mind.
- Short selling isn't auto-detected from CSV imports (Wealthsimple
  cash/registered accounts don't support it), but you can still log a short
  plan manually — you'll just need to use "Log Plan" or add it directly
  rather than relying on auto-import matching. Target/stop ratcheting works
  the same way for shorts (targets below entry, stop moves down as each is
  hit).
- Target/stop matching only runs against **closed** round trips. A position
  that's still partially open (some targets scaled out, position not fully
  closed) shows up under "Open Positions" without live target tracking —
  the target ladder analysis appears once the trade is fully closed.

## Fixed from the original starter code

The starter parser identified buys/sells using the `direction` column, which
Wealthsimple exports as `"LONG"` for every fill (opening or closing) — so it
never actually distinguished a buy from a sell. This version uses
`activity_sub_type` (`BUY`/`SELL`) instead, which is what correctly resolves
your export into round-trip trades.
