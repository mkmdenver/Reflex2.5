
import React, { useEffect, useMemo, useState } from "react";

type Json = any;

function fmtAge(ms: number): string {
  if (!isFinite(ms) || ms < 0) return "—";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h`;
}

function Badge({ text }: { text: string }) {
  return (
    <span style={{
      display: "inline-block",
      padding: "2px 8px",
      borderRadius: 999,
      background: "rgba(255,255,255,0.08)",
      border: "1px solid rgba(255,255,255,0.12)",
      fontSize: 12
    }}>
      {text}
    </span>
  );
}

function Card({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div style={{
      background: "rgba(20, 20, 24, 0.92)",
      border: "1px solid rgba(255,255,255,0.10)",
      borderRadius: 14,
      padding: 14,
      boxShadow: "0 12px 40px rgba(0,0,0,0.25)"
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <div style={{ fontSize: 14, fontWeight: 700, letterSpacing: 0.2 }}>{title}</div>
        <div>{right}</div>
      </div>
      {children}
    </div>
  );
}

function Table({ cols, rows }: { cols: string[]; rows: React.ReactNode[][] }) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c} style={{ textAlign: "left", padding: "8px 8px", color: "rgba(255,255,255,0.75)", borderBottom: "1px solid rgba(255,255,255,0.10)" }}>
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((cell, j) => (
                <td key={j} style={{ padding: "8px 8px", borderBottom: "1px solid rgba(255,255,255,0.06)", verticalAlign: "top" }}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function useJson(url: string, pollMs: number) {
  const [data, setData] = useState<Json>(null);
  const [err, setErr] = useState<string | null>(null);
  const [lastOk, setLastOk] = useState<Json>(null);

  useEffect(() => {
    let alive = true;
    let t: any = null;

    const run = async () => {
      try {
        const r = await fetch(url, { cache: "no-store" });
        const text = await r.text();
        let j: any = null;
        try { j = JSON.parse(text); } catch { j = { ok: false, error: "non_json", raw: text.slice(0, 200) }; }

        if (!alive) return;

        if (r.ok && j && j.ok !== false) {
          setData(j);
          setLastOk(j);
          setErr(null);
        } else {
          setData(lastOk ?? j);
          setErr(j?.error ?? `http_${r.status}`);
        }
      } catch (e: any) {
        if (!alive) return;
        setData(lastOk);
        setErr(e?.message ?? "fetch_failed");
      }
    };

    run();
    t = setInterval(run, pollMs);
    return () => { alive = false; if (t) clearInterval(t); };
  }, [url, pollMs]);

  return { data, err };
}

function Gauge({ label, value, unit, ok }: { label: string; value: number | null; unit: string; ok: boolean }) {
  const pct = value == null ? 0 : Math.max(0, Math.min(100, value));
  const deg = (pct / 100) * 270;
  const bg = ok ? "rgba(255,255,255,0.08)" : "rgba(255,80,80,0.12)";
  return (
    <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
      <div style={{
        width: 78, height: 78, borderRadius: "50%",
        background: `conic-gradient(rgba(255,255,255,0.85) ${deg}deg, ${bg} 0deg)`,
        border: "1px solid rgba(255,255,255,0.12)",
        display: "grid", placeItems: "center"
      }}>
        <div style={{ width: 62, height: 62, borderRadius: "50%", background: "rgba(10,10,12,0.92)", border: "1px solid rgba(255,255,255,0.10)", display:"grid", placeItems:"center" }}>
          <div style={{ textAlign: "center" }}>
            <div style={{ fontSize: 14, fontWeight: 800 }}>{value == null ? "—" : Math.round(value).toString()}</div>
            <div style={{ fontSize: 10, color: "rgba(255,255,255,0.65)" }}>{unit}</div>
          </div>
        </div>
      </div>
      <div>
        <div style={{ fontSize: 13, fontWeight: 700 }}>{label}</div>
        <div style={{ fontSize: 11, color: ok ? "rgba(255,255,255,0.70)" : "rgba(255,120,120,0.9)" }}>
          {ok ? "ok" : "degraded"}
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const { data, err } = useJson("/v1/summary", 1000);
  const now = Date.now();

  const headerRight = (
    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
      {err ? <Badge text={`retrying (${err})`} /> : <Badge text="live" />}
      <Badge text="TradeView" />
    </div>
  );

  const body = useMemo(() => {
    if (!data) return <div style={{ color: "rgba(255,255,255,0.65)" }}>loading…</div>;

    

    

    // engineer
    const s = (data.status ?? data) as any;
    return (
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 14 }}>
        <Card title="Pipes">
          <div style={{ display: "grid", gap: 14 }}>
            <Gauge label="Trader API" value={s.trader?.latency_ms ?? null} unit="ms" ok={!!s.trader?.ok} />
            <Gauge label="DataHub API" value={s.datahub?.latency_ms ?? null} unit="ms" ok={!!s.datahub?.ok} />
            <Gauge label="Log freshness" value={s.logs?.freshness_score ?? null} unit="%" ok={!!s.logs?.ok} />
          </div>
        </Card>
        <Card title="Signals" right={<Badge text={err ? "noisy" : "clean"} />}>
          <div style={{ fontSize: 12, color: "rgba(255,255,255,0.75)", lineHeight: 1.6 }}>
            <div><span style={{ color:"rgba(255,255,255,0.55)" }}>FTS:</span> {s.logs?.fts_last ?? "—"}</div>
            <div><span style={{ color:"rgba(255,255,255,0.55)" }}>PTI:</span> {s.logs?.pti_last ?? "—"}</div>
            <div><span style={{ color:"rgba(255,255,255,0.55)" }}>Trader:</span> {s.logs?.trader_last ?? "—"}</div>
          </div>
        </Card>
      </div>
    );
  }, [data, err]);

  return (
    <div style={{
      minHeight: "100vh",
      background: "radial-gradient(1200px 800px at 20% 10%, rgba(80,60,30,0.25), transparent 60%), radial-gradient(900px 700px at 90% 40%, rgba(40,80,90,0.20), transparent 55%), #0b0b0e",
      color: "white",
      padding: 16
    }}>
      <div style={{ maxWidth: 1400, margin: "0 auto", display: "grid", gap: 14 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <div style={{ fontSize: 18, fontWeight: 900, letterSpacing: 0.2 }}>TradeView</div>
            <div style={{ fontSize: 12, color: "rgba(255,255,255,0.62)" }}>Minimal cockpit service — truthful, fast, and boring in the best way.</div>
          </div>
          {headerRight}
        </div>
        {body}
      </div>
    </div>
  );
}
