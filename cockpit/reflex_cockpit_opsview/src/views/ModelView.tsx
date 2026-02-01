import React, { useMemo, useState } from 'react';
import { Card, Chip } from '../components/Card';
import { getBars1m, getIntents, getTraderHealth, getTrades, type IntentDoc, type TradeDoc } from '../lib/api';
import { usePoll } from '../lib/usePoll';

function fmtTs(ts: any): string {
  const n = typeof ts === 'number' ? ts : Number(ts);
  if (!Number.isFinite(n) || n <= 0) return '';
  return new Date(n * 1000).toLocaleTimeString();
}

function toneFromHealth(ok: boolean, err?: string | null): 'ok' | 'warn' | 'bad' {
  if (!ok) return 'bad';
  if (err) return 'warn';
  return 'ok';
}

type ModelStub = {
  model_id: string;
  universe_id: string;
  pti_id: string;
  state: string;
  waiting_on: string;
  confidence: number;
};

export default function ModelView() {
  const [symbol, setSymbol] = useState('SPY');

  const health = usePoll(getTraderHealth, 2000, []);
  const intents = usePoll(() => getIntents(50), 2500, []);
  const activeTrades = usePoll(() => getTrades('active'), 2500, []);

  const models: ModelStub[] = useMemo(
    () => [
      {
        model_id: 'rbf_scalp_v1',
        universe_id: 'us_smallcap_momo',
        pti_id: 'rbf_pti',
        state: 'SEARCH',
        waiting_on: 'flag + volume confirmation',
        confidence: 0.41,
      },
      {
        model_id: 'swing_breakout_v0',
        universe_id: 'us_trend_daily',
        pti_id: 'breakout_pti',
        state: 'COOLDOWN',
        waiting_on: 'daily close confirmation',
        confidence: 0.22,
      },
      {
        model_id: 'fx_revert_m5_stub',
        universe_id: 'fx_majors',
        pti_id: 'revert_pti',
        state: 'DISABLED',
        waiting_on: 'feed not wired',
        confidence: 0.0,
      },
    ],
    [],
  );

  const [barsStatus, setBarsStatus] = useState<string>('idle');
  const [barsCount, setBarsCount] = useState<number>(0);

  async function testBars() {
    setBarsStatus('loading');
    try {
      const res = await getBars1m(symbol, 180);
      setBarsCount(res.count || 0);
      setBarsStatus(res.ok ? 'ok' : 'error');
    } catch (e: any) {
      setBarsStatus('error');
      setBarsCount(0);
    }
  }

  const healthTone = toneFromHealth(!!health.data?.ok, health.data?.last_poll_error);

  return (
    <div className="page">
      <div className="grid">
        <div className="span-8">
          <Card
            title="Models (stub runtime)"
            right={<Chip text={health.data?.ok ? 'TRADER OK' : 'TRADER DOWN'} tone={healthTone} />}
          >
            <div className="hint">
              This is a fresh ModelView scaffold. Wire model state docs later; for now it displays placeholder models and known Trader hooks.
            </div>
            <div className="tableWrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Universe</th>
                    <th>PTI</th>
                    <th>State</th>
                    <th>Waiting on</th>
                    <th>Conf</th>
                  </tr>
                </thead>
                <tbody>
                  {models.map((m) => (
                    <tr key={m.model_id}>
                      <td>{m.model_id}</td>
                      <td>{m.universe_id}</td>
                      <td>{m.pti_id}</td>
                      <td><Chip text={m.state} tone={m.state === 'DISABLED' ? 'warn' : 'info'} /></td>
                      <td className="muted">{m.waiting_on}</td>
                      <td>{Math.round(m.confidence * 100)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <div className="spacer" />

          <Card title="Live stream: recent intents (Trader /v1/intents)">
            <div className="hint">
              Hook is live in trader/app.py as a cache-backed list. This panel is for discussion now; later we’ll show per-model state transitions.
            </div>
            <div className="tableWrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Symbol</th>
                    <th>Side</th>
                    <th>Source</th>
                    <th>Trade</th>
                  </tr>
                </thead>
                <tbody>
                  {(intents.data?.intents || []).slice(0, 15).map((it: IntentDoc) => (
                    <tr key={it.intent_id}>
                      <td className="muted">{fmtTs(it.created_ts)}</td>
                      <td>{it.symbol}</td>
                      <td>{it.side}</td>
                      <td>{it.source}</td>
                      <td className="mono">{String(it.trade_id || '').slice(0, 8)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </div>

        <div className="span-4">
          <Card title="Trader API health">
            <div className="kvs">
              <div className="kv"><div className="k">ok</div><div className="v">{String(health.data?.ok ?? false)}</div></div>
              <div className="kv"><div className="k">adapters</div><div className="v">{(health.data?.adapters || []).join(', ') || '—'}</div></div>
              <div className="kv"><div className="k">last poll</div><div className="v">{health.data?.last_poll_at || '—'}</div></div>
              <div className="kv"><div className="k">poll err</div><div className="v">{health.data?.last_poll_error || '—'}</div></div>
              <div className="kv"><div className="k">orders sync</div><div className="v">{health.data?.orders_last_sync_at || '—'}</div></div>
              <div className="kv"><div className="k">orders err</div><div className="v">{health.data?.orders_last_sync_error || '—'}</div></div>
            </div>
          </Card>

          <div className="spacer" />

          <Card title="Active trades (Trader /v1/trades?status=active)">
            <div className="tableWrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>State</th>
                    <th>Qty</th>
                  </tr>
                </thead>
                <tbody>
                  {(activeTrades.data?.trades || []).slice(0, 10).map((t: TradeDoc) => (
                    <tr key={t.trade_id}>
                      <td>{t.symbol}</td>
                      <td className="muted">{t.state}</td>
                      <td>{t.qty ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>

          <div className="spacer" />

          <Card
            title="DataHub bars hook (stub)"
            right={<Chip text={barsStatus.toUpperCase()} tone={barsStatus === 'ok' ? 'ok' : barsStatus === 'error' ? 'bad' : 'info'} />}
          >
            <div className="hint">
              Uses datahub /v1/history/bars1m (from uploaded api.py). This is a connectivity probe only.
            </div>
            <div className="row">
              <input className="input" value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} />
              <button className="btn" onClick={testBars}>Fetch 180 bars</button>
            </div>
            <div className="muted">Returned bars: {barsCount}</div>
          </Card>
        </div>
      </div>
    </div>
  );
}
