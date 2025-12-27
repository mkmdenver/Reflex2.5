# Reflex RBF Pipeline – DataHub ↔ Evaluator Anchor

This doc is the "don’t drift" anchor for the Ross-style bull-flag path.

It covers:

- Roles of **DataHub**, **RBF Filter**, and **RBF Pattern**.
- What data flows where.
- What *never* happens (no demotions based on missing data).
- Where to hang future metrics for the engineer panel.

---

## 1. Roles

### DataHub

DataHub is the **measurement engine**:

- Ingests live or replayed market data (ticks, quotes, 1m bars).
- Writes long-term history to Timescale.
- Publishes **1m bars** into Redis on channel `hub.bars1m`.
- Manages per-symbol **tiers** in memory – `COLD`, `WATCH`, `WARM`, `HOT`.
- Runs backfill when a symbol is raised to `WATCH` or `WARM`.

Key invariants:

- DataHub **does not** decide strategies or risk.
- Symbol tiers are **in-memory runtime state**, not a DB schema.
- Raising tiers triggers backfill and subscriptions; demotions are rare and controlled.

### RBF Filter (this module: `FTS_rbf.py`)

This is **Filter 2** for the Ross-style bull-flag model.

It:

1. Uses **fundamentals once at startup** to build a *fixed* candidate universe (float, exchange, etc.).
2. Raises those symbols to `WATCH` tier in DataHub, so DataHub will keep them warm.
3. Listens to `hub.bars1m` and maintains a **per-symbol in-memory state**:
   - `day_vol`
   - `last_price`
   - `first_bar_price`
   - optional `prev_close` (future expansion).
4. On each bar, applies simple Ross-ish criteria:
   - Price band in [ROSS_MIN_PRICE, ROSS_MAX_PRICE]
   - Day volume above `ROSS_MIN_DAY_VOL`
   - Current bar volume above `ROSS_MIN_BAR_VOL`
   - If available, gap% between `prev_close` and `first_bar_price` inside [ROSS_MIN_GAP_PCT, ROSS_MAX_GAP_PCT].
5. Emits **add/remove** events on `ROSS_FILTER_STREAM_CHANNEL` (default: `eval.ross_filter_stream`).

The **add events** form the "WARM stream" for the RBF pattern matcher.

### RBF Pattern (separate module)

The pattern matcher:

- Subscribes to `eval.ross_filter_stream` for active symbols.
- Watches fine-grained tick or bar data for actual bull-flag patterns.
- Emits **order intents** on `eval.order_intent` with:
  - `intent` (symbol, side, qty, type, account_id, etc.)
  - `meta` (model name, strength, risk, diagnostics).

Trader consumes those intents and decides how much to allocate, which broker/account to use, etc.

---

## 2. Data flow summary

End-to-end flow:

1. **Startup**
   - RBF Filter connects to Postgres using `PG_DSN`.
   - Runs a query on `symbol_profile_view` to pick candidates by float and exchange.
   - Builds a fixed `Set[str]` of candidate symbols.
   - Publishes raise-tier events for each symbol → DataHub raises them to `WATCH`.

2. **Runtime – measurement**
   - DataHub publishes 1m bars to Redis channel `hub.bars1m`.
   - RBF Filter subscribes to `hub.bars1m`.
   - For each bar whose `sym` is in the candidate set:
     - Update the symbol’s `RossFilterState`:
       - `day_vol += bar.volume`
       - `last_price = bar.close`
       - On first bar, set `first_bar_price`.

3. **Runtime – filtering**
   - On every bar for a candidate:
     - Compute `in_price_band`, `vol_ok`, and `gap_ok`.
     - If all pass:
       - Mark state `active = True`.
       - If this is a new activation:
         - Optionally raise tier to `WARM`.
         - Emit `"kind": "add"` event on `eval.ross_filter_stream`.
     - If they fail after previously passing:
       - Mark `active = False`.
       - Emit `"kind": "remove"` event.
   - No artificial time-grid loop is required; the 1m bars themselves are the grid.

4. **Runtime – intent**
   - RBF Pattern listens to add/remove stream and bars/ticks.
   - When it sees an actual bull-flag setup on an active symbol, it emits an **order intent**.
   - Trader consumes intents and places orders.

---

## 3. Missing data & races

We specifically avoid brittle behaviour here.

**Rule:**  
> "No data" **never** demotes a symbol in the eval list.

Concretely:

- If a bar for a symbol is malformed or missing `close`, the filter simply **skips** that bar.
- If a symbol has not yet seen any bars, it stays `inactive` and never emits.
- Only actual failing of the price/volume/gap conditions flips `active → False` and emits a remove event.

There is no concept of "not ready" inside the RBF filter; that will be handled by:

- DataHub’s own backfill logic when a symbol is raised to `WATCH`.
- The simple fact that no bars = no decisions.

If backfill is slow or something is broken, the worst case is:

- A symbol that *should* be active simply never meets the conditions.
- There is always another bar (or another day); nothing is permanently corrupted.

---

## 4. Tier behaviour

The RBF Filter only sends **upward** tier changes:

- At startup: all candidates raised to `WATCH`.
- On first transition to `active = True`: symbol raised to `WARM`.

Design guarantees:

- It never sends a demotion.
- It never touches `HOT` tiers directly.
- If logic changes in the future, demotions should be introduced carefully and likely as a separate "janitor" process.

This keeps tier management **monotonic** from the perspective of this filter and simplifies mental models:

- Filter elevates; something else cleans up later.

---

## 5. Metrics & engineer panel hooks

The RBF Filter already emits telemetry suitable for an engineer cockpit:

- Published to Redis `EVAL_STATE_CHANNEL` (default `eval.state`).
- Payload shape:

```json
{
  "eval_id": "FTS_RBF",
  "modules": ["filter_to_stream"],
  "rows": 1,
  "last_update": 1764886387.27,
  "stats": {
    "symbols_tracked": 612,
    "active_symbols": 11,
    "bars_seen": 845,
    "adds_emitted": 11,
    "removes_emitted": 3
  }
}



