import React, { useMemo, useState } from 'react';
import { Card, Chip } from '../components/Card';
import { api, type PortfolioOverview } from '../lib/api';
import { usePoll } from '../lib/usePoll';

type ModelAlloc = {
  model_id: string;
  enabled: boolean;
  account_id: string;
  max_gross_usd: number;
  max_risk_usd: number;
  max_positions: number;
  priority: number;
};

const DEFAULT_ALLOCS: ModelAlloc[] = [
  { model_id: 'rbf_scalp_v1', enabled: true, account_id: 'alpaca:paper', max_gross_usd: 15000, max_risk_usd: 75, max_positions: 3, priority: 10 },
  { model_id: 'swing_breakout_v0', enabled: false, account_id: 'alpaca:paper', max_gross_usd: 15000, max_risk_usd: 120, max_positions: 2, priority: 5 },
  { model_id: 'fx_revert_m5_v0', enabled: false, account_id: 'fx:sim', max_gross_usd: 5000, max_risk_usd: 40, max_positions: 3, priority: 3 },
];

function money(x: any): string {
  const n = Number(x);
  if (!Number.isFinite(n)) return '$0';
  return n.toLocaleString(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
}

export default function RiskView() {
  const { data, error, loading } = usePoll(() => api.trader.overview(), 2000, []);
  const overview = (data as PortfolioOverview | null) ?? null;

  const accounts = useMemo(() => Object.keys(overview?.overview || {}), [overview]);

  const [allocs, setAllocs] = useState<ModelAlloc[]>(DEFAULT_ALLOCS);

  const totals = useMemo(() => {
    let cash = 0;
    let equity = 0;
    let bp = 0;
    for (const aid of accounts) {
      const b = overview?.overview?.[aid]?.balances;
      if (!b) continue;
      cash += Number(b.cash || 0);
      equity += Number(b.equity || 0);
      bp += Number(b.buying_power || 0);
    }
    return { cash, equity, bp };
  }, [accounts, overview]);

  return (
    <div>
      <div className="pageHeader">
        <div>
          <div className="pageTitle">Portfolio / RiskView</div>
          <div className="pageSub">Stub UI. Reads cached portfolio data from Trader. Allocation table is local-only for now.</div>
        </div>
        <div className="row">
          {loading ? <Chip text="loading" tone="info" /> : <Chip text="live" tone={error ? 'warn' : 'ok'} />}
          {error ? <Chip text="stale" tone="warn" /> : null}
        </div>
      </div>

      <div className="grid">
        <div className="span-5">
          <Card title="Accounts & Balances" right={<span className="muted">/v1/portfolio/overview</span>}>
            <div className="kpiRow">
              <div className="kpi"><div className="kpiLabel">Total Cash</div><div className="kpiValue">{money(totals.cash)}</div></div>
              <div className="kpi"><div className="kpiLabel">Total Equity</div><div className="kpiValue">{money(totals.equity)}</div></div>
              <div className="kpi"><div className="kpiLabel">Total Buying Power</div><div className="kpiValue">{money(totals.bp)}</div></div>
            </div>
            <div style={{ height: 12 }} />
            <div className="table">
              <div className="tHead">
                <div>Account</div>
                <div>Broker</div>
                <div>Cash</div>
                <div>Equity</div>
                <div>BP</div>
                <div>Positions</div>
              </div>
              {(accounts.length ? accounts : ['(no accounts)']).map((aid) => {
                const row = overview?.overview?.[aid];
                const b = row?.balances;
                const pos = row?.positions || [];
                return (
                  <div className="tRow" key={aid}>
                    <div className="mono">{aid}</div>
                    <div>{row?.broker_id || row?.kind || '-'}</div>
                    <div>{money(b?.cash)}</div>
                    <div>{money(b?.equity)}</div>
                    <div>{money(b?.buying_power)}</div>
                    <div>{pos.length}</div>
                  </div>
                );
              })}
            </div>
          </Card>

          <div style={{ height: 12 }} />

          <Card title="Open Positions (all accounts)" right={<span className="muted">cache-only</span>}>
            <div className="table">
              <div className="tHead">
                <div>Account</div>
                <div>Symbol</div>
                <div>Qty</div>
                <div>Avg</div>
                <div>Mkt</div>
                <div>uP/L</div>
                <div>Stop</div>
                <div>Target</div>
              </div>
              {accounts.flatMap((aid) => (overview?.overview?.[aid]?.positions || []).map((p: any) => ({ aid, p }))).slice(0, 200).map(({ aid, p }: any, idx: number) => (
                <div className="tRow" key={`${aid}:${p.symbol}:${idx}`}> 
                  <div className="mono">{aid}</div>
                  <div className="mono">{p.symbol}</div>
                  <div className="mono">{Number(p.qty || 0).toFixed(0)}</div>
                  <div className="mono">{Number(p.avg_price || 0).toFixed(4)}</div>
                  <div className="mono">{Number(p.market_price || 0).toFixed(4)}</div>
                  <div className={Number(p.unrealized_pl || 0) >= 0 ? 'mono good' : 'mono bad'}>{Number(p.unrealized_pl || 0).toFixed(2)}</div>
                  <div className="mono">{p.stop_price ? Number(p.stop_price).toFixed(4) : '-'}</div>
                  <div className="mono">{p.target_price ? Number(p.target_price).toFixed(4) : '-'}</div>
                </div>
              ))}
            </div>
          </Card>
        </div>

        <div className="span-7">
          <Card title="Model Allocations (stub)" right={<span className="muted">local-only</span>}>
            <div className="muted" style={{ marginBottom: 10 }}>
              This table is a placeholder for the real Orchestrator / RiskGovernor. For now it’s editable UI state only.
            </div>

            <div className="table">
              <div className="tHead">
                <div>Enabled</div>
                <div>Model</div>
                <div>Account</div>
                <div>Max Gross</div>
                <div>Max Risk</div>
                <div>Max Pos</div>
                <div>Priority</div>
              </div>
              {allocs.map((a, i) => (
                <div className="tRow" key={a.model_id}>
                  <div>
                    <input
                      type="checkbox"
                      checked={a.enabled}
                      onChange={(e) => {
                        const v = e.target.checked;
                        setAllocs((prev) => prev.map((x, ix) => (ix === i ? { ...x, enabled: v } : x)));
                      }}
                    />
                  </div>
                  <div className="mono">{a.model_id}</div>
                  <div>
                    <select
                      className="select"
                      value={a.account_id}
                      onChange={(e) => {
                        const v = e.target.value;
                        setAllocs((prev) => prev.map((x, ix) => (ix === i ? { ...x, account_id: v } : x)));
                      }}
                    >
                      {[...accounts, 'alpaca:paper', 'alpaca:live', 'sim:cash', 'sim:margin', 'fx:sim']
                        .filter((v, ix, arr) => arr.indexOf(v) === ix)
                        .map((aid) => (
                          <option key={aid} value={aid}>
                            {aid}
                          </option>
                        ))}
                    </select>
                  </div>
                  <div>
                    <input
                      className="input"
                      value={a.max_gross_usd}
                      onChange={(e) => {
                        const v = Number(e.target.value || 0);
                        setAllocs((prev) => prev.map((x, ix) => (ix === i ? { ...x, max_gross_usd: v } : x)));
                      }}
                    />
                  </div>
                  <div>
                    <input
                      className="input"
                      value={a.max_risk_usd}
                      onChange={(e) => {
                        const v = Number(e.target.value || 0);
                        setAllocs((prev) => prev.map((x, ix) => (ix === i ? { ...x, max_risk_usd: v } : x)));
                      }}
                    />
                  </div>
                  <div>
                    <input
                      className="input"
                      value={a.max_positions}
                      onChange={(e) => {
                        const v = Number(e.target.value || 0);
                        setAllocs((prev) => prev.map((x, ix) => (ix === i ? { ...x, max_positions: v } : x)));
                      }}
                    />
                  </div>
                  <div>
                    <input
                      className="input"
                      value={a.priority}
                      onChange={(e) => {
                        const v = Number(e.target.value || 0);
                        setAllocs((prev) => prev.map((x, ix) => (ix === i ? { ...x, priority: v } : x)));
                      }}
                    />
                  </div>
                </div>
              ))}
            </div>

            <div style={{ height: 12 }} />
            <div className="row">
              <button className="btn" onClick={() => setAllocs(DEFAULT_ALLOCS)}>Reset</button>
              <button
                className="btn primary"
                onClick={() => alert('Stub only: next step is /v1/risk/allocations (new endpoint) backed by Orchestrator state.')}
              >
                Save (stub)
              </button>
            </div>
          </Card>

          <div style={{ height: 12 }} />

          <Card title="Roadmap hooks" right={<Chip text="discussion" tone="info" />}>
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              <li><span className="mono">/v1/risk/allocations</span> (GET/PUT): Orchestrator truth for this table</li>
              <li><span className="mono">/v1/risk/denials</span>: last N denied intents with reasons</li>
              <li><span className="mono">/v1/risk/exposure</span>: per-model open risk (worst-case-to-stop)</li>
              <li><span className="mono">/v1/risk/ownership</span>: which model owns which symbol (conflict control)</li>
            </ul>
          </Card>
        </div>
      </div>
    </div>
  );
}
