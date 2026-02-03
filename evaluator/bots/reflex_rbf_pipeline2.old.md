You’re not wrong to pull the ripcord. What you’re describing is exactly the failure mode: *a system with “import-time wiring” + repeated rewrites = groundhog-day regressions*. The only sane move is to freeze a known-good code snapshot and write down the **protocol** so the code can be judged against it.

Below is a clean doc-style summary of everything I know about the Reflex2.3 **communication / control-plane** between Evaluator bots and DataHub (plus the “gotchas” that keep biting). No new code. No experiments. Just the spec as it exists from your logs and files.

---

# Reflex2.3 Communication Map (Evaluator ⇄ DataHub ⇄ Redis)

## 1) The system has two planes

There are two different “kinds of communication” happening at once:

**A) Control plane (commands & membership)**

* Evaluator asks DataHub to change a symbol’s tier (COLD/WATCH/WARM/HOT).
* DataHub reacts by subscribing/unsubscribing and backfilling history as needed.

**B) Data plane (streams of market events)**

* DataHub publishes live streams (minute bars, ticks, etc.) onto Redis channels.
* Evaluator bots subscribe and consume.

The confusion starts when these planes get mixed or miswired.

---

## 2) The canonical tier-command channel

From your working runs (Simple2), the tier command channel is:

**`reflex:{REFLEX_INSTANCE_ID}:cmd.tiers`**

Example from your logs:

* `reflex:live:cmd.tiers`

When this channel is correct, DataHub prints:

* `tier_requests.listen.start channel=reflex:live:cmd.tiers ...`
* `tier_request.applied symbol=SPY tier=WATCH old_tier=COLD source=...`
* `backfill.requested symbol=SPY kinds=['minute'] scope=today requested_by=...`
* later: `tier_request.applied ... tier=WARM ...`
* later: `backfill.requested ... kinds=['minute','tick'] ...`

If Evaluator publishes to the wrong channel, DataHub stays at:

* `refresh.subscriptions trades=0 ... watch=0 warm=0 hot=0`

…and nothing “moves” anywhere.

---

## 3) The bar stream channel

The DataHub minute bar stream is:

**`hub.bars1m`**

Your `bars_tap.py` proves that this channel can be alive even when your filter bot isn’t wired right.

Important nuance: **DataHub only emits bars/ticks for symbols it has subscribed to**, which is tier-driven.
So “bars_tap sees messages” does *not* mean “my universe symbols are subscribed.” It only proves “some symbols are subscribed” (or system is emitting something).

---

## 4) What “tiers” mean in practice (observed behavior)

From your DataHub logs:

* **WATCH**:

  * DataHub appears to backfill **minute** data (today scope) and may not subscribe to ticks/trades.
  * You often see: `backfill.requested ... kinds=['minute'] scope=today`

* **WARM**:

  * DataHub subscribes to **trades/ticks** (and minute bars) for realtime updates.
  * You see: `refresh.subscriptions trades=1 warm=1 ...`
  * And: `backfill.requested ... kinds=['minute','tick'] scope=today`

So, if your FTS design relies on **seeing real-time per-symbol updates**, you typically need symbols in **WARM** (or HOT) to get tick/trade firehose.

---

## 5) How Evaluator bots should behave

### 5.1 FTS role (filter-to-stream)

FTS is not “pattern detection.” It’s a **membership curator**.

Recommended responsibilities:

* Build a *candidate universe* (e.g., float/price table query).
* Request DataHub tiering for that universe so data flows.
* Maintain an **Active Set** (Redis set) and/or **Active Stream** (channel) that downstream PTI can use.

### 5.2 PTI role (pattern-to-intent)

PTI should:

* Consume only the curated, smaller “active” list.
* Subscribe to the needed data streams for those symbols (bars, ticks) and do pattern logic.

---

## 6) The “import-time wiring” landmine (the root cause of recurring regressions)

This is the repeating trap you hit:

* Some module (commonly `common.bus`) computes channel names at **import time**.
* Those channel names depend on environment variables like `REFLEX_INSTANCE_ID`.
* If `.env` is loaded *after* those imports, the channels are wrong for the entire process lifetime.

Symptoms match your logs exactly:

* instance becomes `"default"`
* raise channel becomes `"eval.raisetier"` (fallback)
* `pg_dsn_set=false` if DSN is also env-bound and not loaded yet
* universe becomes empty
* DataHub sees nothing

This is not a “Python problem.” It’s a **design constraint**:

> any environment-dependent configuration must be loaded *before* any module that reads it at import time is imported.

If you keep rewriting bots without enforcing that rule, the regression keeps coming back, forever.

---

## 7) Evidence from your logs: “Simple2 works”

Your Simple2 bot demonstrates the intended protocol:

Evaluator side:

* `stage2.raise_tier.requested ... channel: reflex:live:cmd.tiers`
* Active set key example: `reflex:live:LIVE:run0:eval:fts_simple2:active`

DataHub side:

* `tier_request.applied symbol=SPY tier=WATCH ... source=eval.simple2_filter_to_stream`
* then `tier_request.applied symbol=SPY tier=WARM ...`
* then trades subscription becomes `trades=1 warm=1`

That is the reference behavior.

---

## 8) What “worked but dropped through” means

When you saw:

* FTS bootstrapped the universe and then the program exited

That’s not a market-data issue — that’s simply “no long-running loop” or “no subscription loop.”

When you saw:

* FTS stayed alive but `active=0`, `bars_seen=0`, `msgs_seen=0`

That’s a *data plane* gating issue:

* Either symbols aren’t being tiered to a level that produces the stream you’re listening to,
* or you’re listening to the wrong channel,
* or DataHub isn’t actually subscribed to those symbols.

---

## 9) The architecture decision you were circling

You correctly identified the real scaling tension:

* If you want to update filter state “on every tick/bar” for *hundreds* of symbols, you must:

  * either keep many symbols in WARM/HOT (expensive), or
  * accept a snapshot/polling loop, or
  * do a hybrid “scout pool” approach (warm top N, rotate scouts).

That’s not a bug; it’s a resource strategy choice.

Your “prime the pump / scouts” idea is a reasonable **stopgap**, but it trades off missing “dead then alive” names unless your scout rotation is frequent and smart.

---

## 10) Minimal “protocol contract” (the thing to enforce)

If you take this elsewhere, here’s the short contract to enforce in code review:

1. **Env must be loaded before importing channel config**

   * `REFLEX_INSTANCE_ID`, `REFLEX_MODE`, DSN, Redis URL must exist *before* `common.bus` (or equivalent) loads.

2. **Tier commands must go to** `reflex:{instance}:cmd.tiers`

   * Anything else is a black hole.

3. **DataHub emits data only for subscribed tiers**

   * If you want ticks/trades: tier must be WARM/HOT.
   * WATCH usually isn’t enough for tick-driven evaluation.

4. **FTS curates membership; PTI detects patterns**

   * Don’t conflate.

5. **Redis keys should be run-scoped**

   * You already do this in Simple2 (`...:LIVE:run0:...`) which avoids cross-run contamination.

---

## 11) Practical debugging checklist (no code, just truth)

When “nothing is being raised”:

* In Evaluator logs:

  * Is instance `live` or `default`?
  * Is raise channel `reflex:live:cmd.tiers` or `eval.raisetier`?
  * Is `pg_dsn_set=true`?
  * Is universe non-empty?

* In DataHub logs:

  * Do you see `tier_requests.listen.start channel=reflex:live:cmd.tiers`?
  * Do you see `tier_request.applied ... source=<your bot>`?
  * Does `refresh.subscriptions ... watch/warm` increase?

If any of those are “no,” stop immediately: it’s wiring/config, not trading logic.

---

That’s the spec as it exists in your working Simple2 behavior and DataHub logs. No fluff.

If you hand this to another model or another engineer, the key phrase to tattoo on the wall is:

**“Environment-dependent channels must never be computed at import time, unless env is guaranteed loaded first.”**

That’s the regression generator you’ve been battling.
