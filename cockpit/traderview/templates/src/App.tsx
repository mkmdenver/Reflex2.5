import React, { useEffect, useMemo, useState } from "react";

// TraderView UI (v0): Plan Monitor — static dummy data.
// Keep styling aligned with BrokerView for muscle memory.

type TabKey = "monitor" | "debug";

type PlanState =
  | "ARMED"
  | "ENTRY_PENDING"
  | "IN_POSITION"
  | "EXITING"
  | "PAUSED"
  | "SALVAGE_NEEDS_ACK"
  | "DONE";

type StopKind = "FIX" | "TRL" | "SVG";

type PlanRow = {
  plan_id: string;
  account_id: string;
  symbol: string;
  state: PlanState;
  qty: number;
  avg_price: number | null;
  mark: number | null;
  stop_price: number | null;
  stop_kind: StopKind;
  target_price: number | null;
  unrealized_pl: number | null;
  last_event: string;
  age_s: number;
};

// ---------------------------------------------------------------------------
// Market clock (NY time) + optional server-sync offset (proxy via /v1/time)
// ---------------------------------------------------------------------------

type TimeSyncDoc = {
  server_utc_ms: number;
  server_iso?: string;
  source?: string;
};

function formatNyTime(d: Date): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour12: false,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(d);
}

function MarketClock() {
  const [offsetMs, setOffsetMs] = useState<number>(0);
  const [syncInfo, setSyncInfo] = useState<{ ok: boolean; rttMs?: number; source?: string } | null>(null);

  useEffect(() => {
    let cancelled = false;

    const syncOnce = async () => {
      const t0 = Date.now();
      try {
        const res = await fetch("/v1/time");
        if (!res.ok) throw new Error(`time sync failed: ${res.status}`);
        const data = (await res.json()) as TimeSyncDoc;
        const t1 = Date.now();
        const rtt = t1 - t0;

        const serverMs = Number(data.server_utc_ms);
        if (!Number.isFinite(serverMs)) throw new Error("bad server_utc_ms");

        const estClientAtServerReply = t0 + rtt / 2.0;
        const off = serverMs - estClientAtServerReply;

        if (cancelled) return;
        setOffsetMs(off);
        setSyncInfo({ ok: true, rttMs: Math.round(rtt), source: data.source || "server" });
      } catch {
        if (cancelled) return;
        setSyncInfo({ ok: false });
      }
    };

    syncOnce();
    const t = setInterval(syncOnce, 30_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const [, setTick] = useState<number>(0);
  useEffect(() => {
    const t = setInterval(() => setTick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, []);

  const nowSynced = new Date(Date.now() + offsetMs);
  const ny = formatNyTime(nowSynced);

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <span style={{ ...pill, borderColor: "#334155", color: "#e5e7eb" }}>
        NY: <strong>{ny}</strong>
      </span>
      <span style={smallText}>
        {syncInfo?.ok ? `SYNC (${syncInfo.source}, rtt≈${syncInfo.rttMs}ms)` : "LOCAL (unsynced)"}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Styling (mirrors BrokerView)
// ---------------------------------------------------------------------------

const pageStyle: React.CSSProperties = {
  fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
  margin: 0,
  padding: 0,
  background: "#050816",
  color: "#f9fafb",
  minHeight: "100vh",
};

const appShell: React.CSSProperties = {
  maxWidth: 1400,
  margin: "0 auto",
  padding: "16px 24px 40px",
};

const headerRow: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  marginBottom: 16,
};

const titleStyle: React.CSSProperties = {
  fontSize: 24,
  fontWeight: 700,
};

const subtitleStyle: React.CSSProperties = {
  fontSize: 13,
  color: "#9ca3af",
};

const tabsRow: React.CSSProperties = {
  display: "flex",
  gap: 8,
  marginBottom: 16,
  borderBottom: "1px solid #111827",
  paddingBottom: 4,
};

const tabButton = (active: boolean): React.CSSProperties => ({
  padding: "6px 14px",
  borderRadius: 999,
  border: "none",
  cursor: "pointer",
  fontSize: 13,
  background: active ? "#10b981" : "transparent",
  color: active ? "#0b1120" : "#e5e7eb",
  boxShadow: active ? "0 0 0 1px #064e3b" : "none",
});

const card: React.CSSProperties = {
  background: "radial-gradient(circle at top left, rgba(16,185,129,0.12), transparent 55%), #020617",
  borderRadius: 16,
  border: "1px solid rgba(15,23,42,0.9)",
  boxShadow: "0 18px 35px rgba(15,23,42,0.7)",
  padding: 16,
};

const cardHeaderRow: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  marginBottom: 10,
};

const cardTitle: React.CSSProperties = {
  fontSize: 14,
  fontWeight: 600,
};

const smallText: React.CSSProperties = {
  fontSize: 11,
  color: "#6b7280",
};

const pill: React.CSSProperties = {
  fontSize: 11,
  padding: "2px 8px",
  borderRadius: 999,
  background: "rgba(15,23,42,0.9)",
  border: "1px solid rgba(55,65,81,0.7)",
};

const thStyle: React.CSSProperties = {
  textAlign: "left",
  padding: "4px 6px",
  fontSize: 11,
  fontWeight: 600,
  color: "#9ca3af",
};

const tdStyle: React.CSSProperties = {
  padding: "4px 6px",
  fontSize: 12,
  borderBottom: "1px solid #020617",
};

const badge = (color: string): React.CSSProperties => ({
  fontSize: 11,
  padding: "2px 8px",
  borderRadius: 999,
  border: `1px solid ${color}`,
  color,
  background: "rgba(15,23,42,0.9)",
});

const ghostButton: React.CSSProperties = {
  fontSize: 12,
  padding: "6px 10px",
  borderRadius: 999,
  border: "1px solid rgba(55,65,81,0.8)",
  background: "rgba(15,23,42,0.9)",
  color: "#e5e7eb",
  cursor: "pointer",
};

const dangerButton: React.CSSProperties = {
  fontSize: 12,
  padding: "6px 10px",
  borderRadius: 999,
  border: "1px solid rgba(248,113,113,0.7)",
  background: "rgba(248,113,113,0.06)",
  color: "#fecaca",
  cursor: "pointer",
};

function formatCurrency(value: any): string {
  const n = Number(value ?? 0);
  if (!isFinite(n)) return "-";
  return n.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  });
}

function formatNumber(value: any, decimals = 2): string {
  const n = Number(value ?? 0);
  if (!isFinite(n)) return "-";
  return n.toLocaleString("en-US", { maximumFractionDigits: decimals });
}

function formatAge(secs: number): string {
  const s = Math.max(0, Math.floor(secs));
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m <= 0) return `${r}s`;
  return `${m}m ${r}s`;
}

function stateColor(state: PlanState): string {
  switch (state) {
    case "IN_POSITION":
      return "#22c55e";
    case "ENTRY_PENDING":
      return "#0ea5e9";
    case "EXITING":
      return "#f97316";
    case "PAUSED":
      return "#e5e7eb";
    case "SALVAGE_NEEDS_ACK":
      return "#fb7185";
    case "DONE":
      return "#6b7280";
    case "ARMED":
    default:
      return "#a3e635";
  }
}

function stopTagColor(kind: StopKind): string {
  switch (kind) {
    case "TRL":
      return "#0ea5e9";
    case "SVG":
      return "#fb7185";
    case "FIX":
    default:
      return "#e5e7eb";
  }
}

function makeDummyPlans(nowMs: number): PlanRow[] {
  const now = nowMs / 1000;
  return [
    {
      plan_id: "SPY-R1",
      account_id: "alpaca:paper",
      symbol: "SPY",
      state: "IN_POSITION",
      qty: 12,
      avg_price: 487.2,
      mark: 488.1,
      stop_price: 486.4,
      stop_kind: "TRL",
      target_price: 490.8,
      unrealized_pl: (488.1 - 487.2) * 12,
      last_event: "STOP_TRAILED → 486.40",
      age_s: now - (now - 14 * 60 - 22),
    },
    {
      plan_id: "MSFT-ORB",
      account_id: "alpaca:paper",
      symbol: "MSFT",
      state: "ENTRY_PENDING",
      qty: 1,
      avg_price: null,
      mark: 432.7,
      stop_price: 429.0,
      stop_kind: "FIX",
      target_price: 438.0,
      unrealized_pl: null,
      last_event: "ENTRY_SUBMITTED (leg 1)",
      age_s: 63,
    },
    {
      plan_id: "NVDA-SVG",
      account_id: "alpaca:paper",
      symbol: "NVDA",
      state: "SALVAGE_NEEDS_ACK",
      qty: -5,
      avg_price: 492.35,
      mark: 490.9,
      stop_price: 497.0,
      stop_kind: "SVG",
      target_price: null,
      unrealized_pl: (490.9 - 492.35) * -5,
      last_event: "IMPORTED ON RESTART",
      age_s: 7 * 60 + 8,
    },
  ];
}

export default function App() {
  const [activeTab, setActiveTab] = useState<TabKey>("monitor");
  const [apiLog, setApiLog] = useState<string[]>([]);

  const [nowMs, setNowMs] = useState<number>(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const plans = useMemo(() => makeDummyPlans(nowMs), [nowMs]);

  const logApi = (msg: string) => {
    setApiLog((prev) => {
      const next = [...prev, `[${new Date().toLocaleTimeString()}] ${msg}`];
      if (next.length > 120) next.shift();
      return next;
    });
  };

  const onKill = (p: PlanRow) => {
    logApi(`KILL plan_id=${p.plan_id} (placeholder)`);
    alert(`Kill plan (placeholder): ${p.plan_id}`);
  };

  const onFlatten = (p: PlanRow) => {
    logApi(`FLATTEN plan_id=${p.plan_id} (placeholder)`);
    alert(`Flatten plan (placeholder): ${p.plan_id}`);
  };

  const onInspect = (p: PlanRow) => {
    logApi(`INSPECT plan_id=${p.plan_id} (placeholder)`);
    alert(`Inspect plan (placeholder): ${p.plan_id}`);
  };

  const renderMonitor = () => (
    <div style={card}>
      <div style={cardHeaderRow}>
        <div style={cardTitle}>Plans Monitor</div>
        <span style={smallText}>Static dummy data (TraderView v0)</span>
      </div>

      <div style={{ maxHeight: 520, overflow: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ borderBottom: "1px solid #111827" }}>
              <th style={thStyle}>Plan</th>
              <th style={thStyle}>State</th>
              <th style={thStyle}>Position</th>
              <th style={thStyle}>Mark</th>
              <th style={thStyle}>Stop</th>
              <th style={thStyle}>Target</th>
              <th style={thStyle}>P&amp;L</th>
              <th style={thStyle}>Age</th>
              <th style={thStyle}>Last event</th>
              <th style={thStyle}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {plans.map((p) => (
              <tr key={p.plan_id}>
                <td style={tdStyle}>
                  <div style={{ fontWeight: 600 }}>{p.plan_id}</div>
                  <div style={smallText}>{p.account_id}</div>
                </td>
                <td style={tdStyle}>
                  <span style={badge(stateColor(p.state))}>{p.state}</span>
                </td>
                <td style={tdStyle}>
                  <div>{p.symbol}</div>
                  <div style={smallText}>
                    {p.qty >= 0 ? "+" : ""}{formatNumber(p.qty, 0)}
                    {p.avg_price != null ? ` @ ${formatCurrency(p.avg_price)}` : ""}
                  </div>
                </td>
                <td style={tdStyle}>{p.mark != null ? formatCurrency(p.mark) : "—"}</td>
                <td style={tdStyle}>
                  {p.stop_price != null ? (
                    <span style={badge(stopTagColor(p.stop_kind))}>
                      {formatCurrency(p.stop_price)} {p.stop_kind}
                    </span>
                  ) : "—"}
                </td>
                <td style={tdStyle}>{p.target_price != null ? formatCurrency(p.target_price) : "—"}</td>
                <td style={tdStyle}>
                  {p.unrealized_pl != null ? (
                    <span style={badge((p.unrealized_pl ?? 0) >= 0 ? "#22c55e" : "#f97316")}> 
                      {formatCurrency(p.unrealized_pl)}
                    </span>
                  ) : "—"}
                </td>
                <td style={tdStyle}>{formatAge(p.age_s)}</td>
                <td style={tdStyle}>{p.last_event}</td>
                <td style={tdStyle}>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    <button style={dangerButton} onClick={() => onKill(p)} title="Kill plan">
                      ⛔
                    </button>
                    <button style={dangerButton} onClick={() => onFlatten(p)} title="Flatten">
                      ⚡
                    </button>
                    <button style={ghostButton} onClick={() => onInspect(p)} title="Inspect">
                      🔍
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ marginTop: 10, ...smallText }}>
        Next: wire to Trader /v1/plans + event cursor feed (no SSE), and make kill/flatten real.
      </div>
    </div>
  );

  const renderDebug = () => (
    <div style={card}>
      <div style={cardHeaderRow}>
        <div style={cardTitle}>Debug log</div>
        <span style={smallText}>Local UI-only (placeholder)</span>
      </div>
      <pre
        style={{
          background: "#020617",
          borderRadius: 8,
          padding: 8,
          maxHeight: 320,
          overflow: "auto",
          fontSize: 11,
        }}
      >
        {apiLog.join("
") || "No actions yet."}
      </pre>
    </div>
  );

  return (
    <div style={pageStyle}>
      <div style={appShell}>
        <header style={headerRow}>
          <div>
            <div style={titleStyle}>Reflex TraderView</div>
            <div style={subtitleStyle}>Plan runtime monitor + per-plan kill controls.</div>
          </div>
          <div>
            <span style={smallText}>
              Instance: <strong>live</strong> • <MarketClock />
            </span>
          </div>
        </header>

        <div style={tabsRow}>
          <button style={tabButton(activeTab === "monitor")} onClick={() => setActiveTab("monitor")}>
            Plans Monitor
          </button>
          <button style={tabButton(activeTab === "debug")} onClick={() => setActiveTab("debug")}>
            Debug
          </button>
        </div>

        {activeTab === "monitor" && renderMonitor()}
        {activeTab === "debug" && renderDebug()}
      </div>
    </div>
  );
}
