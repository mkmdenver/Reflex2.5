import React, { useEffect, useMemo, useState } from 'react';
import { Card, Chip } from '../components/Card';
import { api, type TraderHealth } from '../lib/api';
import { usePoll } from '../lib/usePoll';

function pct(n: number, d: number): number {
  if (d <= 0) return 0;
  return Math.max(0, Math.min(100, (n / d) * 100));
}

function Gauge(props: { label: string; value: number; max: number; unit?: string }) {
  const percent = pct(props.value, props.max);
  return (
    <div className="gauge">
      <div className="gaugeTop">
        <div className="gaugeLabel">{props.label}</div>
        <div className="gaugeVal">{props.value.toFixed(0)}{props.unit || ''}</div>
      </div>
      <div className="bar"><div className="barFill" style={{ width: `${percent}%` }} /></div>
      <div className="muted">0 → {props.max}{props.unit || ''}</div>
    </div>
  );
}

export default function EngineerView() {
  const h = usePoll(() => api.trader.health(), 2000, []);
  const [events, setEvents] = useState<any[]>([]);
  const [sseStatus, setSseStatus] = useState<'DISCONNECTED' | 'CONNECTING' | 'LIVE' | 'ERROR'>('DISCONNECTED');

  useEffect(() => {
    setSseStatus('CONNECTING');
    const url = api.trader.eventsUrl();
    const es = new EventSource(url);
    es.onopen = () => setSseStatus('LIVE');
    es.onerror = () => setSseStatus('ERROR');
    es.onmessage = (msg) => {
      try {
        const obj = JSON.parse(msg.data);
        setEvents((prev) => [obj, ...prev].slice(0, 80));
      } catch {
        // ignore
      }
    };
    return () => {
      setSseStatus('DISCONNECTED');
      es.close();
    };
  }, []);

  const health: TraderHealth | null = h.data;
  const tone: 'ok' | 'warn' | 'bad' = !health?.ok ? 'bad' : (health?.last_poll_error || health?.orders_last_sync_error) ? 'warn' : 'ok';

  const adapters = (health?.adapters || []).length;
  const errCount = Number(Boolean(health?.last_poll_error)) + Number(Boolean(health?.orders_last_sync_error));

  const sseChip = useMemo(() => {
    if (sseStatus === 'LIVE') return <Chip text="SSE LIVE" tone="ok" />;
    if (sseStatus === 'CONNECTING') return <Chip text="SSE CONNECT" tone="info" />;
    if (sseStatus === 'ERROR') return <Chip text="SSE ERROR" tone="warn" />;
    return <Chip text="SSE OFF" tone="bad" />;
  }, [sseStatus]);

  return (
    <div>
      <div className="pageTitle">EngineerView</div>
      <div className="pageSub">Steam-punk gauges for heartbeats, cache staleness, and event firehose. Placeholder now; wires are real.</div>

      <div className="grid">
        <div className="span-5">
          <Card title="Trader health" right={<Chip text={tone === 'ok' ? 'OK' : tone === 'warn' ? 'DEGRADED' : 'DOWN'} tone={tone} />}>
            <div className="kv">
              <div className="k">Adapters</div>
              <div className="v">{adapters}</div>
              <div className="k">Last reconcile</div>
              <div className="v">{health?.last_poll_at || '—'}</div>
              <div className="k">Last reconcile error</div>
              <div className="v mono">{health?.last_poll_error || '—'}</div>
              <div className="k">Orders sync</div>
              <div className="v">{health?.orders_last_sync_at || '—'}</div>
              <div className="k">Orders sync error</div>
              <div className="v mono">{health?.orders_last_sync_error || '—'}</div>
            </div>
            {h.error ? <div className="error">{h.error}</div> : null}
          </Card>
        </div>

        <div className="span-7">
          <Card title="Gauges" right={sseChip}>
            <div className="grid" style={{ gridTemplateColumns: 'repeat(3, 1fr)' }}>
              <Gauge label="Adapters" value={adapters} max={8} />
              <Gauge label="Active errors" value={errCount} max={5} />
              <Gauge label="SSE events buffered" value={events.length} max={80} />
            </div>
            <div className="muted" style={{ marginTop: 8 }}>
              These are placeholders until we wire true latency metrics (submit→ack, ack→fill, data-in→state-out).
            </div>
          </Card>
        </div>

        <div className="span-12">
          <Card title="Event firehose (SSE)" right={<div className="muted">/trader/v1/events</div>}>
            <div className="tableWrap">
              <table className="table">
                <thead>
                  <tr>
                    <th style={{ width: 140 }}>Type</th>
                    <th style={{ width: 160 }}>Time</th>
                    <th style={{ width: 160 }}>Account</th>
                    <th>Payload</th>
                  </tr>
                </thead>
                <tbody>
                  {events.map((e, idx) => (
                    <tr key={idx}>
                      <td className="mono">{String(e?.type || '')}</td>
                      <td className="mono">{e?.ts ? new Date(e.ts * 1000).toLocaleTimeString() : ''}</td>
                      <td className="mono">{String(e?.account_id || '')}</td>
                      <td className="mono" style={{ whiteSpace: 'pre-wrap' }}>{JSON.stringify(e)}</td>
                    </tr>
                  ))}
                  {!events.length ? (
                    <tr>
                      <td colSpan={4} className="muted">No events yet. (If Trader isn't running, this will stay empty.)</td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
