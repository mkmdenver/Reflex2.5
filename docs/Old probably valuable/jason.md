# Model Manifest — `momentum_filter_v2`

This manifest maps snapshot fields to their sources and the decision stages.

## Filters
| Metric      | Source                                   | Purpose                                      |
|-------------|-------------------------------------------|----------------------------------------------|
| volatility  | `snapshot_metrics.compute_volatility()`   | Ensure market activity before considering trades |
| volume      | `snapshot_builder` (recent tick sizes sum)| Confirm liquidity before entry               |

## Entry
| Param         | Source / Meaning                         | Purpose                         |
|---------------|------------------------------------------|---------------------------------|
| momentum      | `snapshot_builder.compute_momentum()`    | Detect breakout strength        |
| volatility(min)| same as filter metric                   | Double‑check at entry           |
| throttle      | model param (cockpit‑tunable)            | Control position size           |
| torque        | model param (cockpit‑tunable)            | Control stop/exit aggressiveness|
| lookback      | model param                               | Momentum window                 |
| threshold     | model param                               | Minimum momentum for entry      |
| cooldown_sec  | model param                               | Prevent instant re‑entries      |

## Exit
| Trigger           | Source / Field                   | Purpose                  |
|-------------------|----------------------------------|--------------------------|
| gain_points       | `last_price − entry_price`       | Fixed gain take‑profit   |
| drawdown_points   | `entry_price − last_price`       | Fixed stop               |
| volume_near_bid   | `bid_size > threshold`           | Order‑flow based exit    |
| tape_pressure     | `snapshot_builder.compute_tape_pressure()` | Order‑flow exit |
| max_hold_seconds  | model param                      | Time‑based forced exit   |

## Add Model
| Trigger              | Source / Field                 | Purpose                           |
|----------------------|--------------------------------|-----------------------------------|
| ask_volume_absorbed  | `ask_size < threshold`         | Liquidity absorption add          |
| spread_narrowing     | `spread < narrow_threshold`     | Confirm tightening market         |
| min_momentum         | model param                     | Avoid scale‑in if momentum fades  |
| add_count / max_adds | evaluator flags + model param   | Limit number of adds              |

## Pipeline Summary
1. **Snapshot Loop** populates fields (e.g., 0.5s cadence).
2. **Filter Stage** checks `volatility` and `volume`.
3. **Entry Logic** runs `momentum_breakout` if filters pass.
4. **Exit Logic** watches gain/drawdown and order‑flow metrics.
5. **Add Logic** evaluates absorption, spread, and momentum.
6. **Diagnostics** logs decisions to CSV for replay and Cockpit.

---
*Fixed formatting and tables from [json.md](https://rockymountaintechnet-my.sharepoint.com/personal/mike_malone_rockymountaintech_net/Documents/Forms/DispForm.aspx?ID=2294&web=1&EntityRepresentationId=af8fc958-d041-42da-8de0-81ce67af774a).* [19](https://rockymountaintechnet-my.sharepoint.com/personal/mike_malone_rockymountaintech_net/Documents/Reflex/models/json.md)
