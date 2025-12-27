Perfect — here’s the **model manifest** for your new `momentum_filter_v2` JSON.  
Think of this as the “wiring diagram” for the model: every snapshot field it touches, where that field comes from in the Reflexion pipeline, and how it’s used in the decision chain.  

---

## 📜 **Model Manifest — `momentum_filter_v2`**

| **Stage**       | **Field**               | **Source in Reflexion** | **Purpose in Model** |
|-----------------|-------------------------|-------------------------|----------------------|
| **Filters**     | `volatility`            | `snapshot_metrics.compute_volatility()` — std dev of returns over last N ticks | Ensures market is active enough before considering trades |
|                 | `volume`                | `snapshot_builder` — sum of sizes in recent ticks | Confirms sufficient liquidity before entry |
| **Entry Model** | `momentum`               | `snapshot_builder.compute_momentum()` — price change over lookback | Detects breakout strength |
|                 | `volatility` (min)      | Same as filter metric | Double‑checks volatility at entry time |
|                 | `throttle` / `torque`   | Model params (cockpit‑tunable) | Adjusts aggressiveness of entry logic |
|                 | `lookback`              | Model param | Defines momentum calculation window |
|                 | `threshold`             | Model param | Minimum momentum required to trigger entry |
|                 | `cooldown_seconds`      | Model param | Prevents immediate re‑entry after exit |
| **Exit Models** | `gain_points`           | `snapshot_fields` — last_price − entry_price | Fixed gain exit trigger |
|                 | `drawdown_points`       | `snapshot_fields` — entry_price − last_price | Fixed stop exit trigger |
|                 | `volume_near_bid`       | `snapshot_fields` — bid_size > threshold | Orderflow exit condition |
|                 | `tape_pressure`         | `snapshot_builder.compute_tape_pressure()` | Orderflow exit condition |
|                 | `max_hold_seconds`      | Model param | Time‑based forced exit |
| **Add Model**   | `ask_volume_absorbed`   | `snapshot_fields` — ask_size < threshold | Liquidity absorption add trigger |
|                 | `spread_narrowing`      | `snapshot_fields` — spread < narrow threshold | Confirms tightening market before adding |
|                 | `min_momentum`          | Model param | Avoids scaling in if momentum has faded |
|                 | `add_count` / `max_adds`| Evaluator flags + model param | Limits number of adds per trade |

---

### 🔹 How This Plays Out in the Pipeline
1. **Snapshot Loop** populates all the above fields every 0.5s from the double‑ring buffers.
2. **Evaluator Filter Stage** checks `volatility` and `volume` against your filter rules.
3. **Entry Logic** runs `momentum_breakout` if filters pass, using `lookback`, `threshold`, `throttle`, `torque`, and `min_volatility`.
4. **Exit Logic** monitors `gain_points`, `drawdown_points`, `volume_near_bid`, `tape_pressure`, and `max_hold_seconds`.
5. **Add Logic** watches `ask_volume_absorbed`, `spread_narrowing`, and `min_momentum` until `max_adds` is reached.
6. **Diagnostics Layer** logs every decision with these fields into CSV for replay analysis and cockpit display.

---

