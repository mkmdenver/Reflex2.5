# Reflex Core — System Architecture & Components

**Version:** Reflex 2.4  
**Document Type:** Core System Design Reference  
**Audience:** Developers, Bot Creators, Analysts, Platform Maintainers

---

## 🧭 Overview

**Reflex** is a modular, locally-hosted trading platform designed for:

- High-speed **day trading and backtesting**
- Multi-broker connectivity (Alpaca, SIM, others)
- Real-time and historical **pattern detection**, **intent streaming**, and **capital-aware execution**

Built for robustness, Reflex separates concerns cleanly between ingestion, evaluation, order execution, and GUI. Version 2.4 adds execution timing, replay mode, and the foundation for Smart Trader logic.

---

## 🧩 Core Components

### 🔹 DataHub
> The central pub/sub engine for price ingestion and bot symbol tiering

- Ingests live Polygon data or local Parquet files (Replay mode)
- Emits bars and ticks over Redis channels
- Maintains per-symbol stream tier (COLD → WATCH → WARM → HOT)
- Supports high-speed snapshot + stream for bots

### 🔹 Evaluator
> Strategy engine: filters → patterns → trade intents

- **Filter-to-Stream (FTS)** bots promote symbols to tiers
- **Pattern-to-Intent (PTI)** bots detect intraday patterns (e.g., Ross Bull Flag)
- Emits `TradeIntent` JSONs over Redis
- Can run historical (`eval_shape_*`) or real-time

### 🔹 Trader
> Order execution + broker reconciliation

- Accepts intents via REST or Redis queue
- Routes to broker adapter (Alpaca, SIM)
- Enforces caps, cooldowns, inflight limits
- Reconciles positions via broker APIs
- Emits events: `ORDER_REQUEST`, `ORDER_PLACED`, `BROKER_UPDATE`, `ORDER_METRICS`
- Logs JSONL trace in `logs/trader/YYYY-MM-DD.jsonl`

### 🔹 BrokerView (Cockpit)
> Human cockpit: manual trading + dashboards

- Built in React + Vite
- Subscribes to Trader SSE stream
- Shows Orders, Positions, Accounts
- Manual order panel (market, limit, smart stop)
- Rebuilds from `src/` via `npm run dev`

---

## 🔄 Data Flow

```
Polygon → DataHub → Eval Bots → Trade Intents → Trader → Broker
                                      ↘ SSE ↘ Redis ↘ BrokerView
```

- Market data flows into DataHub → emits to bots
- Bots emit intents → Trader filters + dispatches
- Trader emits structured events → UI live updates
- PnL and price (coming soon) will stream from DataHub to UI

---

## 🧪 Modes: Live vs Replay

Reflex 2.4 supports two run modes:

### ✅ Live Mode
- `REFLEX_MODE=LIVE`
- Market data from Polygon WebSocket
- Brokers: real accounts (Alpaca, etc)

### 🔁 Replay Mode
- `REFLEX_MODE=REPLAY`
- Loads tick/quote data from local Parquet lakes
- Supports "day replay" (e.g. simulate 2023-08-21 open→close)
- Trader can operate normally or in shadow mode (no broker write)

---

## ⚙️ Environment Control

### Key `.env` Variables
- `REFLEX_MODE`: LIVE or REPLAY
- `REFLEX_INSTANCE_ID`: e.g. `liveA`, `replayB`
- `REDIS_URL`: always use local Redis or Garnet
- `REFLEX__PG_DSN`: TimescaleDB tick/fundamental store
- `REFLEX_BROKER_DSN`: Broker account + position DB
- `WATCHLIST`: CSV of always-enabled symbols (e.g. `SPY,AAPL,TSLA`)

---

## 🧱 Design Principles

- **Truth lives at the broker** — Trader reconciles via broker API
- **Bots don’t know money** — Trader enforces all caps and inflight limits
- **UI listens, doesn’t ask** — BrokerView is SSE-first (push, not pull)
- **Pattern ≠ Market** — Bots assess setups, but Atmosphere assesses *conditions*
- **Logs are facts** — All decisions logged to JSONL (not just displayed)

---

## 📁 Key Paths

| Path | Purpose |
|------|---------|
| `.env` | Core runtime config |
| `logs/trader/YYYY-MM-DD.jsonl` | Order-level flight recorder |
| `reflex_parquet/instances/` | Replay data lake (Hive-style layout) |
| `tools/` | CLI utils: backfill, symbol add, export to Parquet |
| `evaluator/bots/` | Strategy modules (FTS/PTI) |
| `brokerview/src/` | UI components + state management |

---

## 📌 Next Docs (Related RAG Modules)

- `trader_protocol.md` → order lifecycle, SSE events, Smart Stop logic
- `smart_trader.md` → pattern-aware exits, stop emulation, risk tiers
- `atmosphere_almanac.md` → shape scans, contextual filters, weather states

---

*This document represents a stable, shareable system truth for Reflex 2.4.*
Update it when launching new modules or shifting architectural direction.

