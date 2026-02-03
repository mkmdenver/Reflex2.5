import React, { useEffect, useMemo, useRef, useState } from "react";
import { OrderEntry } from "./components/orderentry";

type Json = any;

type TabKey = "trade" | "accounts" | "debug";


function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";

  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) {
    // If it's not a valid date, just show the raw value so we see what's wrong
    return String(value);
  }

  return dt.toLocaleString("en-US", {
    timeZone: "America/New_York",   // always EST/ET across the app
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}


function fmtTsNY(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);

  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).formatToParts(d);

  const get = (t: string) => parts.find(p => p.type === t)?.value ?? "??";
  return `${get("month")}-${get("day")} ${get("hour")}:${get("minute")}:${get("second")}`;
}

// ---------------------------------------------------------------------------
// Market clock (NY time) + optional server-sync offset
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

// Simple session classifier (NO holiday calendar; shows a safety note)
function nySessionState(nowUtc: Date): { state: "PRE" | "OPEN" | "POST" | "CLOSED"; note?: string } {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "short",
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  }).formatToParts(nowUtc);

  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const wd = get("weekday");
  const hour = Number(get("hour"));
  const minute = Number(get("minute"));
  const isWeekend = wd === "Sat" || wd === "Sun";

  const minutes = hour * 60 + minute;

  if (isWeekend) return { state: "CLOSED", note: "Weekend (holiday schedule unknown)" };

  // Typical US equities schedule (no holiday/early close logic)
  const preStart = 4 * 60;
  const openStart = 9 * 60 + 30;
  const openEnd = 16 * 60;
  const postEnd = 20 * 60;

  if (minutes >= preStart && minutes < openStart) return { state: "PRE", note: "Holiday schedule unknown" };
  if (minutes >= openStart && minutes < openEnd) return { state: "OPEN", note: "Holiday schedule unknown" };
  if (minutes >= openEnd && minutes < postEnd) return { state: "POST", note: "Holiday schedule unknown" };
  return { state: "CLOSED", note: "Holiday schedule unknown" };
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
  const sess = nySessionState(nowSynced);

  const sessColor =
    sess.state === "OPEN" ? "#22c55e" :
    sess.state === "PRE" ? "#0ea5e9" :
    sess.state === "POST" ? "#f97316" :
    "#ef4444";

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <span style={{ ...pill, borderColor: "#334155", color: "#e5e7eb" }}>
        NY: <strong>{ny}</strong>
      </span>
      <span style={{ ...pill, borderColor: sessColor, color: sessColor }}>
        {sess.state}
      </span>
      <span style={smallText}>
        {syncInfo?.ok ? `SYNC (${syncInfo.source}, rtt≈${syncInfo.rttMs}ms)` : "LOCAL (unsynced)"}
        {sess.note ? ` • ${sess.note}` : ""}
      </span>
    </div>
  );
}


interface LoadState {
  loading: boolean;
  error: string | null;
}

interface MarketSession {
  state?: string;
  open_time?: string | null;
  close_time?: string | null;
  note?: string;
  [k: string]: any;
}

interface AccountSummary {
  account_id: string;
  cash?: string | number | null;
  equity?: string | number | null;
  buying_power?: string | number | null;
  status?: string;
  currency?: string;
  note?: string;
  updated_at?: string | null;
}

interface Position {
  symbol: string;
  qty: number;
  avg_price: number | null;
  market_price: number | null;
  stop_price?: number | null;
  target_price?: number | null;
  unrealized_pl: number | null;
  side?: string | null;
  account_id?: string | null;

  // Optional
  // Single-letter provenance tag from PTI (Trader-managed)
  gen_id?: string | null;

  // Optional (not always provided by Trader). BrokerView will also maintain a local
  // first-seen timestamp so we can produce a stable "entered" sort under load.
  opened_at?: string | null;
  entered_at?: string | null;
}

interface Order {
  id: string;
  client_order_id?: string | null;
  account_id: string;
  symbol: string;
  qty: number;
  side: string;
  type: string;
  limit_price?: number | null;
  stop_price?: number | null;
  time_in_force?: string | null;
  status: string;
  created_at?: string | null;
  updated_at?: string | null;
  submitted_at?: string | null;
  filled_qty?: number | null;
  avg_fill_price?: number | null;
  raw?: Json;
}

// Stable identity for matching active vs closed (prefer client_order_id)
function orderKey(o: Order): string {
  return (o.client_order_id || o.id || "").toString();
}


// De-dupe Active Orders: if both a local echo (PENDING_LOCAL) and broker-truth exist for the same
// client_order_id, prefer the broker-truth row and hide the local echo row.
function dedupeActiveOrders(items: Order[]): Order[] {
  const out: Order[] = [];
  const byCid = new Map<string, Order>();

  for (const o of items || []) {
    const cid = (o.client_order_id || "").toString();
    if (!cid) {
      out.push(o);
      continue;
    }
    const cur = byCid.get(cid);
    if (!cur) {
      byCid.set(cid, o);
      continue;
    }
    const curIsLocal = (cur.status || "").toLowerCase() === "pending_local";
    const oIsLocal = (o.status || "").toLowerCase() === "pending_local";
    if (curIsLocal && !oIsLocal) {
      byCid.set(cid, o);
    }
  }

  out.push(...byCid.values());
  return out;
}

interface OrdersResponse {
  orders: Order[];
}

interface PositionsResponse {
  positions: Position[];
}

interface AccountsResponse {
  accounts: AccountSummary[];
}

type ExitTag = "stop" | "target";

type ClosedPositionRow = {
  trade_id?: string | null;
  account_id?: string | null;
  gen_id?: string | null;
  symbol: string;
  side: "LONG" | "SHORT";
  qty: number;
  entry_price: number | null;
  exit_price: number | null;
  pnl: number | null;
  exit_ts: string | null;
  close_reason: string | null;
};

const API_BASE = "";

const pageStyle: React.CSSProperties = {
  fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
  margin: 0,
  padding: 0,
  background: "#050816",
  color: "#f9fafb",
  minHeight: "100vh",
};

const appShell: React.CSSProperties = {
  maxWidth: 1550,
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

const gridRow: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "1.9fr 2.1fr",
  gap: 16,
  alignItems: "flex-start",
};

const card: React.CSSProperties = {
  background:
    "radial-gradient(circle at top left, rgba(16,185,129,0.12), transparent 55%), #020617",
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

const pill: React.CSSProperties = {
  fontSize: 11,
  padding: "2px 8px",
  borderRadius: 999,
  background: "rgba(15,23,42,0.9)",
  border: "1px solid rgba(55,65,81,0.7)",
};

const labelStyle: React.CSSProperties = {
  fontSize: 12,
  color: "#9ca3af",
  marginBottom: 4,
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "6px 8px",
  borderRadius: 8,
  border: "1px solid #1f2937",
  background: "#020617",
  color: "#f9fafb",
  fontSize: 13,
  outline: "none",
};

const selectStyle: React.CSSProperties = {
  ...inputStyle,
  paddingRight: 24,
};

const smallText: React.CSSProperties = {
  fontSize: 11,
  color: "#6b7280",
};

const badge = (color: string): React.CSSProperties => ({
  fontSize: 11,
  padding: "2px 8px",
  borderRadius: 999,
  border: `1px solid ${color}`,
  color,
  background: "rgba(15,23,42,0.9)",
});

const dangerButton: React.CSSProperties = {
  fontSize: 12,
  padding: "6px 10px",
  borderRadius: 999,
  border: "1px solid rgba(248,113,113,0.7)",
  background: "rgba(248,113,113,0.06)",
  color: "#fecaca",
  cursor: "pointer",
};

const tinyDangerButton: React.CSSProperties = {
  ...dangerButton,
  fontSize: 11,
  padding: "4px 8px",
};

const primaryButton: React.CSSProperties = {
  fontSize: 13,
  padding: "7px 14px",
  borderRadius: 999,
  border: "none",
  background:
    "linear-gradient(135deg, #10b981, #22c55e 30%, #a3e635 90%, #f97316)",
  color: "#05101b",
  fontWeight: 600,
  cursor: "pointer",
  boxShadow:
    "0 0 0 1px rgba(16,185,129,0.6), 0 12px 25px rgba(15,23,42,0.9)",
};

const ghostButton: React.CSSProperties = {
  fontSize: 12,
  padding: "6px 10px",
  borderRadius: 999,
  border: "1px solid rgba(55,65,81,0.8)",
  background: "rgba(15,23,42,0.9)",
  color: "#e5e7eb",
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
  return n.toLocaleString("en-US", {
    maximumFractionDigits: decimals,
  });
}

function formatPct(value: any): string {
  const n = Number(value ?? 0);
  if (!isFinite(n)) return "-";
  return `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
}

async function fetchJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(
      `Request failed ${res.status} ${res.statusText}: ${text}`.slice(0, 300)
    );
  }
  return (await res.json()) as T;
}

export default function App() {
  const [activeTab, setActiveTab] = useState<TabKey>("trade");

  const [session, setSession] = useState<MarketSession | null>(null);
  const [sessionState, setSessionState] = useState<LoadState>({
    loading: true,
    error: null,
  });

  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [accountsLoad, setAccountsLoad] = useState<LoadState>({
    loading: true,
    error: null,
  });

  const [positions, setPositions] = useState<Position[]>([]);
  const [positionsLoad, setPositionsLoad] = useState<LoadState>({
    loading: true,
    error: null,
  });

  // -------------------------------------------------------------------------
  // UI-side stability: remember when we first saw each open position so we can
  // sort by "entered" even if the backend snapshot lacks timestamps.
  // Key is account_id:symbol (one live position per symbol per account).
  // -------------------------------------------------------------------------
  const positionFirstSeenRef = useRef<Map<string, number>>(new Map());

  type PositionSort = "entered" | "symbol" | "pnl";
  const [positionSort, setPositionSort] = useState<PositionSort>("entered");

  type OrdersSort = "time" | "symbol" | "status";
  const [activeOrdersSort, setActiveOrdersSort] = useState<OrdersSort>("time");
  const [closedOrdersSort, setClosedOrdersSort] = useState<OrdersSort>("time");

  const [activeOrders, setActiveOrders] = useState<Order[]>([]);
  const [closedOrders, setClosedOrders] = useState<Order[]>([]);
  const [ordersLoad, setOrdersLoad] = useState<LoadState>({
    loading: true,
    error: null,
  });

  type ClosedPosScope = "today" | "all";
  const [closedPosScope, setClosedPosScope] = useState<ClosedPosScope>("today");
  const [closedPositionRows, setClosedPositionRows] = useState<ClosedPositionRow[]>([]);
  const [closedPositionsLoad, setClosedPositionsLoad] = useState<LoadState>({
    loading: true,
    error: null,
  });

  const [apiLog, setApiLog] = useState<string[]>([]);

  const [selectedAccount, setSelectedAccount] = useState<string>("alpaca:paper");
  const [symbol, setSymbol] = useState("SPY");
  const [qty, setQty] = useState("1");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [orderType, setOrderType] = useState<
    "market" | "limit" | "stop" | "stop_limit"
  >("market");
  const [limitPrice, setLimitPrice] = useState("");
  const [stopPrice, setStopPrice] = useState("");
  const [timeInForce, setTimeInForce] = useState("day");
  const [extendedHours, setExtendedHours] = useState(false);
  const [executeAt, setExecuteAt] = useState("");  // TODO: scheduling not wired to backend yet
  const [clientNote, setClientNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitSuccess, setSubmitSuccess] = useState<string | null>(null);

  const [lastRawSession, setLastRawSession] = useState<Json | null>(null);
  const [lastRawAccounts, setLastRawAccounts] = useState<Json | null>(null);
  const [lastRawPositions, setLastRawPositions] = useState<Json | null>(null);
  const [lastRawOrders, setLastRawOrders] = useState<Json | null>(null);

  useEffect(() => {
    let cancelled = false;

    const logApi = (msg: string) => {
      setApiLog((prev) => {
        const next = [...prev, `[${new Date().toLocaleTimeString()}] ${msg}`];
        if (next.length > 80) next.shift();
        return next;
      });
    };

    const loadSession = async () => {
      try {
        setSessionState((s) => ({ ...s, loading: true, error: null }));
        logApi("GET /v1/market/session");
        const data = await fetchJson<Json>("/v1/market/session");
        if (cancelled) return;
        setLastRawSession(data);
        setSession(data);
        setSessionState({ loading: false, error: null });
      } catch (err: any) {
        if (cancelled) return;
        setSessionState({
          loading: false,
          error: err?.message || String(err),
        });
      }
    };

    const loadAccounts = async () => {
      try {
        setAccountsLoad((s) => ({ ...s, loading: true, error: null }));
        logApi("GET /v1/accounts");
        const data = await fetchJson<AccountsResponse>("/v1/accounts");
        if (cancelled) return;
        setLastRawAccounts(data);
        setAccounts(data.accounts ?? []);
        setAccountsLoad({ loading: false, error: null });

        if (!selectedAccount && data.accounts && data.accounts.length > 0) {
          setSelectedAccount(data.accounts[0].account_id);
        }
      } catch (err: any) {
        if (cancelled) return;
        setAccountsLoad({
          loading: false,
          error: err?.message || String(err),
        });
      }
    };

    const loadPositions = async () => {
      try {
        setPositionsLoad((s) => ({ ...s, loading: true, error: null }));
        logApi(`GET /v1/positions?account=${encodeURIComponent(selectedAccount)}`);
        const data = await fetchJson<PositionsResponse>(`/v1/positions?account=${encodeURIComponent(selectedAccount)}`);
        if (cancelled) return;
        setLastRawPositions(data);
        const items = (data.positions ?? []) as Position[];

        // Maintain a local "first-seen" timestamp per position identity so the UI can
        // keep a stable, deterministic sort even when the backend doesn't provide an
        // explicit entry timestamp.
        const now = Date.now();
        for (const p of items) {
          const key = `${p.account_id || ""}:${(p.symbol || "").toUpperCase()}`;
          if (!positionFirstSeenRef.current.has(key)) {
            positionFirstSeenRef.current.set(key, now);
          }
        }

        setPositions(items);
        setPositionsLoad({ loading: false, error: null });
      } catch (err: any) {
        if (cancelled) return;
        setPositionsLoad({
          loading: false,
          error: err?.message || String(err),
        });
      }
    };

    const loadOrders = async () => {
      try {
        setOrdersLoad((s) => ({ ...s, loading: true, error: null }));
        logApi("GET /v1/orders?status=active");
        const active = await fetchJson<OrdersResponse>(
          `/v1/orders?status=active&account=${encodeURIComponent(selectedAccount)}`
        );
        if (cancelled) return;
        logApi("GET /v1/orders?status=closed");
        const closed = await fetchJson<OrdersResponse>(
          `/v1/orders?status=closed&account=${encodeURIComponent(selectedAccount)}`
        );
        if (cancelled) return;
        setLastRawOrders({ active, closed });
        setActiveOrders(active.orders ?? []);
        setClosedOrders(closed.orders ?? []);
        setOrdersLoad({ loading: false, error: null });
      } catch (err: any) {
        if (cancelled) return;
        setOrdersLoad({
          loading: false,
          error: err?.message || String(err),
        });
      }
    };



    const loadClosedPositions = async () => {
      try {
        setClosedPositionsLoad((s) => ({ ...s, loading: true, error: null }));
        logApi(`GET /v1/closed_positions?scope=${closedPosScope}`);
        const data = await fetchJson<{ closed_positions?: ClosedPositionRow[] }>(
          `/v1/closed_positions?scope=${encodeURIComponent(closedPosScope)}&account=${encodeURIComponent(selectedAccount)}&limit=1000`
        );
        if (cancelled) return;
        setClosedPositionRows((data as any).closed_positions ?? (data as any).items ?? []);
        setClosedPositionsLoad({ loading: false, error: null });
      } catch (err: any) {
        if (cancelled) return;
        setClosedPositionsLoad({
          loading: false,
          error: err?.message || String(err),
        });
      }
    };
    // Fast truth path: Trader events (proxied by BrokerView).
    const es = new EventSource("/events");



    // Refresh orders immediately when local UI submits an order
    const onOrdersRefresh = () => {
      loadOrders();
    loadClosedPositions();
    };
    window.addEventListener("reflex:orders_refresh", onOrdersRefresh);

    es.onmessage = (e) => {
      try {
        const evt = JSON.parse(e.data);
        const t = String(evt?.type || "").toUpperCase();
        if (t.includes("ORDER") || t.includes("BROKER") || t.includes("FLATTEN") || t.includes("CANCEL")) {
          loadAccounts();
          loadPositions();
          loadOrders();
    loadClosedPositions();
        }
      } catch {
        // ignore keepalive/pings
      }
    };

    loadSession();
    loadAccounts();
    loadPositions();
    loadOrders();
    loadClosedPositions();

    const timer = setInterval(() => {
      loadSession();
      loadAccounts();
      loadPositions();
      loadOrders();
    loadClosedPositions();
    }, 3000);

    return () => {
      cancelled = true;
      clearInterval(timer);
      window.removeEventListener("reflex:orders_refresh", onOrdersRefresh);
      try { es.close(); } catch {}

    };
  }, [selectedAccount, closedPosScope]);
  const closedKeys = useMemo(() => {
    const s = new Set<string>();
    for (const o of closedOrders) {
      const k = orderKey(o);
      if (k) s.add(k);
      if (o.id) s.add(String(o.id));
    }
    return s;
  }, [closedOrders]);

  const exitBySymbol = useMemo(() => {
    const m = new Map<string, { tag: ExitTag; limit_price: number | null; status: string | null; client_order_id: string | null }>();
    for (const o of activeOrders) {
      const cid = (o.client_order_id || "").toString();
      if (!cid.includes(":exit:")) continue;
      const sym = (o.symbol || "").toString().toUpperCase();
      if (!sym) continue;
      const tag: ExitTag | null = cid.includes(":exit:stop") ? "stop" : cid.includes(":exit:target") ? "target" : null;
      if (!tag) continue;
      m.set(sym, {
        tag,
        limit_price: o.limit_price != null ? Number(o.limit_price) : null,
        status: o.status ? String(o.status) : null,
        client_order_id: cid || null,
      });
    }
    return m;
  }, [activeOrders]);

  const closedPositions = useMemo((): ClosedPositionRow[] => {
    const rows = closedPositionRows || [];
    // UI safety: keep deterministic sort (newest first) and local account filter as a backstop.
    const out = rows.filter((r: any) => !selectedAccount || !r.account_id || r.account_id === selectedAccount);
    out.sort((a: any, b: any) => String(b.exit_ts || "").localeCompare(String(a.exit_ts || "")));
    return out;
  }, [closedPositionRows, selectedAccount]);




  const sessionLabel = useMemo(() => {
    if (!session) return "Unknown";
    const state = (session.state ?? "").toUpperCase();
    if (state === "OPEN") return "REGULAR SESSION";
    if (state === "PRE") return "PRE-MARKET";
    if (state === "POST") return "AFTER HOURS";
    return state || "Unknown";
  }, [session]);

  const sessionColor = useMemo(() => {
    if (!session) return "#6b7280";
    const state = (session.state ?? "").toUpperCase();
    if (state === "OPEN") return "#22c55e";
    if (state === "PRE") return "#0ea5e9";
    if (state === "POST") return "#f97316";
    if (state === "CLOSED") return "#ef4444";
    return "#6b7280";
  }, [session]);

  const accountOptions = useMemo(
    () => accounts.map((a) => ({ value: a.account_id, label: a.account_id })),
    [accounts]
  );

  const selectedAccountSummary: AccountSummary | undefined = useMemo(
    () => accounts.find((a) => a.account_id === selectedAccount),
    [accounts, selectedAccount]
  );

  const submitDisabled = useMemo(() => {
    if (!symbol.trim()) return true;
    const qtyNum = Number(qty);
    if (!Number.isFinite(qtyNum) || qtyNum <= 0) return true;

    if (orderType === "limit" && limitPrice.trim() === "") return true;
    if (
      (orderType === "stop" || orderType === "stop_limit") &&
      stopPrice.trim() === ""
    )
      return true;

    if (orderType === "stop_limit") {
      if (limitPrice.trim() === "" || stopPrice.trim() === "") return true;
    }

    return false;
  }, [symbol, qty, orderType, limitPrice, stopPrice]);

  const handleSubmit = async (evt: React.FormEvent) => {
    evt.preventDefault();
    setSubmitting(true);
    setSubmitError(null);
    setSubmitSuccess(null);

    try {
      const payload: any = {
        account_id: selectedAccount,
        symbol: symbol.trim().toUpperCase(),
        qty: Number(qty),
        side,
        type: orderType,
        time_in_force: timeInForce,
        extended_hours: extendedHours,
      };

      // DEBUG (qty mismatch hunting): log the exact payload we are sending
      try {
        console.log("[ORDER_SUBMIT payload]", payload);
        setApiLog((prev) => {
          const next = [...prev, `[${new Date().toLocaleTimeString()}] POST /v1/orders payload=${JSON.stringify(payload)}`];
          if (next.length > 80) next.shift();
          return next;
        });
      } catch {
        // ignore log errors
      }




      if (orderType === "limit" || orderType === "stop_limit") {
        payload.limit_price = Number(limitPrice);
      }
      if (orderType === "stop" || orderType === "stop_limit") {
        payload.stop_price = Number(stopPrice);
      }

      if (clientNote.trim()) {
        payload.client_note = clientNote.trim();
      }

      // NOTE: executeAt is not yet sent; backend doesn't support scheduling yet.
      // When we wire scheduling, we'll add e.g.:
      // if (executeAt.trim()) payload.execute_at = executeAt.trim();

      const res = await fetch(`${API_BASE}/v1/orders`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const text = await res.text();
        throw new Error(
          `Order failed: ${res.status} ${res.statusText}: ${text}`.slice(
            0,
            400
          )
        );
      }

      const data = await res.json();
      setSubmitSuccess(
        `Order accepted: ${data?.id || data?.status || "OK"}`
      );
      setQty("1");
      if (orderType === "market") {
        setLimitPrice("");
        setStopPrice("");
      }

      try {
        const refreshed = await fetchJson<OrdersResponse>(
          `/v1/orders?status=active&account=${encodeURIComponent(selectedAccount)}`
        );
        setActiveOrders(refreshed.orders ?? []);
      } catch {
        // ignore refresh errors
      }
    } catch (err: any) {
      setSubmitError(err?.message || String(err));
    } finally {
      setSubmitting(false);
    }
  };

  const handleFlattenAll = async () => {
    try {
      const res = await fetch("/v1/accounts/flatten", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: selectedAccount }),
      });
      if (!res.ok) throw new Error(await res.text());
      await res.json();
      // refresh
      window.dispatchEvent(new Event("reflex:orders_refresh"));
    } catch (e: any) {
      alert(`Flatten failed: ${e?.message || String(e)}`);
    }
  };

  const handleCancelAllOrders = async () => {
    try {
      const res = await fetch("/v1/accounts/cancel_all", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: selectedAccount }),
      });
      if (!res.ok) throw new Error(await res.text());
      await res.json();
      // refresh
      window.dispatchEvent(new Event("reflex:orders_refresh"));
    } catch (e: any) {
      alert(`Cancel-all failed: ${e?.message || String(e)}`);
    }
  };

  const handleFlattenSymbol = async (symbol: string) => {
    try {
      const res = await fetch("/v1/positions/flatten", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: selectedAccount, symbol }),
      });
      if (!res.ok) throw new Error(await res.text());
      await res.json();
      window.dispatchEvent(new Event("reflex:orders_refresh"));
    } catch (e: any) {
      alert(`Flatten ${symbol} failed: ${e?.message || String(e)}`);
    }
  };


  const renderSessionPill = () => (
    <span style={{ ...pill, borderColor: sessionColor, color: sessionColor }}>
      {sessionLabel}
    </span>
  );

  const renderAccountsSummary = () => {
    if (accountsLoad.loading && accounts.length === 0) {
      return <div style={smallText}>Loading accounts…</div>;
    }
    if (accountsLoad.error) {
      return (
        <div style={{ ...smallText, color: "#fecaca" }}>
          Accounts error: {accountsLoad.error}
        </div>
      );
    }
    if (!accounts.length) {
      return <div style={smallText}>No accounts discovered yet.</div>;
    }

    return (
      <div style={{ marginTop: 8 }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ borderBottom: "1px solid #111827" }}>
              <th style={thStyle}>Account</th>
              <th style={thStyle}>Type</th>
              <th style={thStyle}>Cash</th>
              <th style={thStyle}>Equity</th>
              <th style={thStyle}>Buying Power</th>
              <th style={thStyle}>Updated</th>
            </tr>
          </thead>
          <tbody>
            {accounts.map((acct) => {
              const isSim =
                acct.account_id.toLowerCase().includes("sim") ||
                acct.account_id.toLowerCase().includes("paper") ||
                acct.status?.toLowerCase() === "sim";
              return (
                <tr
                  key={acct.account_id}
                  style={{
                    borderBottom: "1px solid #020617",
                    background:
                      acct.account_id === selectedAccount
                        ? "rgba(16,185,129,0.09)"
                        : "transparent",
                  }}
                >
                  <td style={tdStyle}>{acct.account_id}</td>
                  <td style={tdStyle}>
                    <span
                      style={badge(isSim ? "#7dd3fc" : "#22c55e")}
                    >
                      {isSim ? "Sim / Paper" : "Live"}
                    </span>
                  </td>
                  <td style={tdStyle}>
                    {formatCurrency(acct.cash)}
                  </td>
                  <td style={tdStyle}>
                    {formatCurrency(acct.equity)}
                  </td>
                  <td style={tdStyle}>
                    {formatCurrency(acct.buying_power)}
                  </td>
                  <td className="text-xs text-muted">
                    {acct.updated_at
                      ? formatDateTime(acct.updated_at)
                      : "—"}
                  </td>

                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    );
  };

  const renderPositions = () => {
    // Keep the panel height stable (avoid collapse/flash when empty)
    const panel = (content: React.ReactNode) => (
      <div style={{ height: 300, overflowY: "auto", overflowX: "hidden", marginTop: 6 }}>
        {content}
      </div>
    );

    if (positionsLoad.loading && positions.length === 0) {
      return panel(<div style={smallText}>Loading positions…</div>);
    }
    const rawErr = positionsLoad.error;
    const posErrMsg = rawErr
      ? (String(rawErr).includes("502") || String(rawErr).toLowerCase().includes("trader unavailable")
          ? "Positions temporarily unavailable (retrying…) — showing last known data."
          : `Positions error: ${rawErr}`)
      : null;

    // If we have no cached positions, show the error inline.
    if (rawErr && positions.length === 0) {
      return panel(
        <div style={{ ...smallText, color: "#fecaca" }}>
          {posErrMsg}
        </div>
      );
    }

    const filtered = positions.filter(
      (p) => !selectedAccount || p.account_id === selectedAccount
    );

    const posEnteredMs = (p: Position): number => {
      const iso = p.entered_at || p.opened_at || null;
      if (iso) {
        const d = new Date(iso);
        if (!Number.isNaN(d.getTime())) return d.getTime();
      }
      const key = `${p.account_id || ""}:${(p.symbol || "").toUpperCase()}`;
      return positionFirstSeenRef.current.get(key) ?? 0;
    };

    const sorted = [...filtered].sort((a, b) => {
      if (positionSort === "symbol") {
        return (a.symbol || "").localeCompare(b.symbol || "");
      }
      if (positionSort === "pnl") {
        const ap = Number(a.unrealized_pl ?? 0);
        const bp = Number(b.unrealized_pl ?? 0);
        // Highest P&L first
        if (bp !== ap) return bp - ap;
        return (a.symbol || "").localeCompare(b.symbol || "");
      }
      // Default: entered time (most recent first)
      const at = posEnteredMs(a);
      const bt = posEnteredMs(b);
      if (bt !== at) return bt - at;
      return (a.symbol || "").localeCompare(b.symbol || "");
    });

    if (!filtered.length) {
      return panel(<div style={smallText}>No open positions.</div>);
    }

    return panel(
      <div>
        {posErrMsg ? <div style={{ ...smallText, color: "#fecaca", marginBottom: 6 }}>{posErrMsg}</div> : null}
      <table style={{ width: "100%", borderCollapse: "collapse", tableLayout: "fixed" }}>
        <colgroup>
          <col style={{ width: "12%" }} />
          <col style={{ width: "6%" }} />
          <col style={{ width: "8%" }} />
          <col style={{ width: "8%" }} />
          <col style={{ width: "12%" }} />
          <col style={{ width: "12%" }} />
          <col style={{ width: "12%" }} />
          <col style={{ width: "12%" }} />
          <col style={{ width: "12%" }} />
          <col style={{ width: "6%" }} />
        </colgroup>
          <thead>
            <tr style={{ borderBottom: "1px solid #111827" }}>
              <th style={thStyle}>Symbol</th>
              <th style={thStyle}>PTI</th>
              <th style={thStyle}>Side</th>
              <th style={thStyle}>Qty</th>
              <th style={thStyle}>Avg Price</th>
              <th style={thStyle}>Stop</th>
              <th style={thStyle}>Target</th>
              <th style={thStyle}>Mkt Price</th>
              <th style={thStyle}>Unrealized P&amp;L</th>
              <th style={thStyle}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((p) => (
              <tr key={`${p.account_id || ""}:${(p.symbol || "").toUpperCase()}`}>
                <td style={tdStyle}>{p.symbol}</td>
                <td style={tdStyle}>
                  <span style={badge("#94a3b8")}>{(p.gen_id ?? "-").toString()}</span>
                </td>
                <td style={tdStyle}>
                  <span
                    style={badge(
                      (p.side ?? "").toLowerCase() === "short" ? "#fb7185" : "#22c55e"
                    )}
                  >
                    {(p.side ?? "long").toUpperCase()}
                  </span>
                </td>
                <td style={tdStyle}>{formatNumber(p.qty, 0)}</td>
                <td style={tdStyle}>{formatCurrency(p.avg_price)}</td>
                <td
                  style={{
                    ...tdStyle,
                    ...(exitBySymbol.get((p.symbol || "").toUpperCase())?.tag === "stop"
                      ? {
                          outline: "1px solid #f97316",
                          outlineOffset: -1,
                        }
                      : null),
                  }}
                  title={(() => {
                    const ex = exitBySymbol.get((p.symbol || "").toUpperCase());
                    if (!ex || ex.tag !== "stop") return "";
                    const lp = ex.limit_price != null ? ` @ ${formatCurrency(ex.limit_price)}` : "";
                    return `Exit working (STOP${lp})`;
                  })()}
                >
                  {(() => {
                    const sym = (p.symbol || "").toUpperCase();
                    const ex = exitBySymbol.get(sym);
                    const base = p.stop_price != null ? formatCurrency(p.stop_price) : "-";
                    if (ex && ex.tag === "stop") {
                      // Keep columns stable: no long text, just a small badge.
                      return (
                        <span>
                          {base}{" "}
                          <span style={badge("#f97316")}>EXIT</span>
                        </span>
                      );
                    }
                    return base;
                  })()}
                </td>
                <td
                  style={{
                    ...tdStyle,
                    ...(exitBySymbol.get((p.symbol || "").toUpperCase())?.tag === "target"
                      ? {
                          outline: "1px solid #f97316",
                          outlineOffset: -1,
                        }
                      : null),
                  }}
                  title={(() => {
                    const ex = exitBySymbol.get((p.symbol || "").toUpperCase());
                    if (!ex || ex.tag !== "target") return "";
                    const lp = ex.limit_price != null ? ` @ ${formatCurrency(ex.limit_price)}` : "";
                    return `Exit working (TARGET${lp})`;
                  })()}
                >
                  {(() => {
                    const sym = (p.symbol || "").toUpperCase();
                    const ex = exitBySymbol.get(sym);
                    const base = p.target_price != null ? formatCurrency(p.target_price) : "-";
                    if (ex && ex.tag === "target") {
                      return (
                        <span>
                          {base}{" "}
                          <span style={badge("#f97316")}>EXIT</span>
                        </span>
                      );
                    }
                    return base;
                  })()}
                </td>
                <td style={tdStyle}>{formatCurrency(p.market_price)}</td>
                <td style={tdStyle}>
                  <span style={badge((p.unrealized_pl ?? 0) >= 0 ? "#22c55e" : "#f97316")}>
                    {formatCurrency(p.unrealized_pl)}
                  </span>
                </td>
                <td style={tdStyle}>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    <button
                      type="button"
                      style={tinyDangerButton}
                      onClick={() => handleFlattenSymbol(p.symbol)}
                      title="Flatten this symbol (market order, best-effort)"
                    >
                      Flatten
                    </button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>

      </table>
      </div>
    );
  };

  type OrdersTableMode = "active" | "closed";

  function orderTsMs(o: Order, mode: OrdersTableMode): number {
    const raw: any = (o as any).raw || null;

    // For CLOSED orders, "Executed" must mean filled/executed time, and must NOT be
    // distorted by later reconciliation updates.
    const candidates =
      mode === "closed"
        ? [
            raw?.filled_at,
            raw?.filled_at_utc,
            raw?.filled_at_iso,
            (o as any).filled_at,
            o.submitted_at,
            o.created_at,
            // NOTE: updated_at intentionally excluded for closed sort
          ]
        : [
            raw?.filled_at,
            raw?.filled_at_utc,
            raw?.filled_at_iso,
            (o as any).filled_at,
            o.updated_at,
            o.submitted_at,
            o.created_at,
          ];

    for (const v of candidates.filter(Boolean)) {
      const t = Date.parse(String(v));
      if (Number.isFinite(t)) return t;
    }
    return 0;
  }

  const orderFillPrice = (o: Order): number | null => {
    const raw: any = (o as any).raw || null;
    const v = o.avg_fill_price ?? raw?.avg_fill_price ?? raw?.filled_avg_price ?? raw?.filled_avg_price_per_share;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  };

  const renderOrdersTable = (orders: Order[], mode: OrdersTableMode) => {
    // Keep panel height stable (avoid collapse/flash when empty)
    const panel = (content: React.ReactNode) => (
      <div style={{ height: 260, overflowY: "auto", overflowX: "hidden", marginTop: 6 }}>
        {content}
      </div>
    );

    if (!orders.length) {
      return panel(<div style={smallText}>No orders.</div>);
    }

    const sortKey = mode === "active" ? activeOrdersSort : closedOrdersSort;
    const sorted = [...orders].sort((a, b) => {
      if (sortKey === "symbol") {
        return (a.symbol || "").localeCompare(b.symbol || "");
      }
      if (sortKey === "status") {
        const as = (a.status || "").localeCompare(b.status || "");
        if (as !== 0) return as;
        return (a.symbol || "").localeCompare(b.symbol || "");
      }
      // Default: time (most recent first)
      const at = orderTsMs(a,mode);
      const bt = orderTsMs(b,mode);
      if (bt !== at) return bt - at;
      return (a.symbol || "").localeCompare(b.symbol || "");
    });

    return panel(
      <table style={{ width: "100%", borderCollapse: "collapse", tableLayout: "fixed" }}>
          <thead>
            <tr style={{ borderBottom: "1px solid #111827" }}>
              <th style={thStyle}>Time</th>
              <th style={thStyle}>Symbol</th>
              <th style={thStyle}>Side</th>
              <th style={thStyle}>Qty</th>
              <th style={thStyle}>Type</th>
              <th style={thStyle}>Limit</th>
              <th style={thStyle}>Stop</th>
              {mode === "closed" && <th style={thStyle}>Fill</th>}
              <th style={thStyle}>Status</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((o) => (
              <tr key={orderKey(o)}>
                <td style={tdStyle} title={o.client_order_id || o.id}>
                  {fmtTsNY(
                    (() => {
                      const raw: any = (o as any).raw || null;
                      if (mode === "closed") {
                        return (
                          raw?.filled_at ||
                          raw?.filled_at_utc ||
                          raw?.filled_at_iso ||
                          (o as any).filled_at ||
                          o.updated_at ||
                          o.submitted_at ||
                          o.created_at
                        );
                      }
                      return o.submitted_at || o.updated_at || o.created_at;
                    })()
                  )}
                </td>
                <td style={tdStyle}>{o.symbol}</td>
                <td style={tdStyle}>
                  <span
                    style={badge(
                      o.side?.toLowerCase() === "sell"
                        ? "#fb7185"
                        : "#22c55e"
                    )}
                  >
                    {o.side?.toUpperCase()}
                  </span>
                </td>
                <td style={tdStyle}>{formatNumber(o.qty, 0)}</td>
                <td style={tdStyle}>{o.type}</td>
                <td style={tdStyle}>
                  {o.limit_price != null ? formatCurrency(o.limit_price) : "-"}
                </td>
                <td style={tdStyle}>
                  {o.stop_price != null ? formatCurrency(o.stop_price) : "-"}
                </td>
                {mode === "closed" && (
                  <td style={tdStyle}>
                    {orderFillPrice(o) != null ? formatCurrency(orderFillPrice(o)) : "-"}
                  </td>
                )}
                <td style={tdStyle}>
                  <span
                    style={badge(
                      o.status?.toLowerCase() === "filled"
                        ? "#22c55e"
                        : "#e5e7eb"
                    )}
                  >
                    {o.status?.toUpperCase()}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
      </table>
    );
  };

  const renderOrderEntry = () => (
    <form onSubmit={handleSubmit}>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1.1fr 1.1fr 0.8fr",
          gap: 12,
          marginBottom: 12,
        }}
      >
        <div>
          <div style={labelStyle}>Account</div>
          <select
            style={selectStyle}
            value={selectedAccount}
            onChange={(e) => setSelectedAccount(e.target.value)}
          >
            {accountOptions.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          <div style={smallText}>
            Balances:{" "}
            {selectedAccountSummary
              ? `${formatCurrency(
                  selectedAccountSummary.equity
                )} equity • ${formatCurrency(
                  selectedAccountSummary.buying_power
                )} BP`
              : "—"}
          </div>
        </div>
        <div>
          <div style={labelStyle}>Symbol</div>
          <input
            style={inputStyle}
            value={symbol}
            onChange={(e) => setSymbol(e.target.value)}
            placeholder="Ticker (e.g. SPY)"
          />
        </div>
        <div>
          <div style={labelStyle}>Quantity</div>
          <input
            style={inputStyle}
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            placeholder="Size"
          />
          <div style={smallText}>
            For small-cap scalp system we’ll usually size by risk/unit.
          </div>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1.2fr 1.3fr 0.9fr",
          gap: 12,
          marginBottom: 12,
        }}
      >
        <div>
          <div style={labelStyle}>Side</div>
          <div style={{ display: "flex", gap: 8 }}>
            <button
              type="button"
              style={{
                ...ghostButton,
                background:
                  side === "buy" ? "rgba(16,185,129,0.15)" : ghostButton.background,
                borderColor:
                  side === "buy" ? "#22c55e" : (ghostButton as any).borderColor,
              }}
              onClick={() => setSide("buy")}
            >
              Buy
            </button>
            <button
              type="button"
              style={{
                ...ghostButton,
                background:
                  side === "sell"
                    ? "rgba(248,113,113,0.15)"
                    : ghostButton.background,
                borderColor:
                  side === "sell"
                    ? "#fb7185"
                    : (ghostButton as any).borderColor,
              }}
              onClick={() => setSide("sell")}
            >
              Sell / Short
            </button>
          </div>
        </div>
        <div>
          <div style={labelStyle}>Order Type</div>
          <select
            style={selectStyle}
            value={orderType}
            onChange={(e) =>
              setOrderType(
                e.target.value as "market" | "limit" | "stop" | "stop_limit"
              )
            }
          >
            <option value="market">Market</option>
            <option value="limit">Limit</option>
            <option value="stop">Stop</option>
            <option value="stop_limit">Stop Limit</option>
          </select>
        </div>
        <div>
          <div style={labelStyle}>Time in Force</div>
          <select
            style={selectStyle}
            value={timeInForce}
            onChange={(e) => setTimeInForce(e.target.value)}
          >
            <option value="day">DAY</option>
            <option value="gtc">GTC</option>
            <option value="opg">OPG</option>
            <option value="cls">CLS</option>
            <option value="ioc">IOC</option>
            <option value="fok">FOK</option>
          </select>
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
          gap: 12,
          marginBottom: 12,
        }}
      >
        <div>
          <div style={labelStyle}>Limit price</div>
          <input
            style={inputStyle}
            value={limitPrice}
            onChange={(e) => setLimitPrice(e.target.value)}
            placeholder="Only used for limit / stop-limit"
          />
        </div>
        <div>
          <div style={labelStyle}>Stop price</div>
          <input
            style={inputStyle}
            value={stopPrice}
            onChange={(e) => setStopPrice(e.target.value)}
            placeholder="Only used for stop / stop-limit"
          />
        </div>
        <div>
          <div style={labelStyle}>Extended hours</div>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              checked={extendedHours}
              onChange={(e) => setExtendedHours(e.target.checked)}
            />
            <span style={smallText}>
              Allow pre-market / post-market if broker supports it.
            </span>
          </div>
        </div>
      </div>

      <details style={{ marginTop: 10 }}>
        <summary style={{ ...smallText, cursor: "pointer", userSelect: "none" }}>
          Advanced (optional)
        </summary>
        <div style={{ marginTop: 10 }}>
          <div style={labelStyle}>Execute at (optional)</div>
          <input
            style={inputStyle}
            type="text"
            value={executeAt}
            onChange={(e) => setExecuteAt(e.target.value)}
            placeholder="e.g. 2025-11-13T09:35:00 or leave blank for now"
          />
          <div style={smallText}>
            UI-only for now.
          </div>
        </div>

        <div style={{ marginTop: 12 }}>
          <div style={labelStyle}>Client note</div>
          <input
            style={inputStyle}
            value={clientNote}
            onChange={(e) => setClientNote(e.target.value)}
            placeholder="Optional tag/reason for this order"
          />
        </div>
      </details>

      {submitError && (
        <div
          style={{
            marginTop: 8,
            ...smallText,
            color: "#fecaca",
          }}
        >
          {submitError}
        </div>
      )}
      {submitSuccess && (
        <div
          style={{
            marginTop: 8,
            ...smallText,
            color: "#bbf7d0",
          }}
        >
          {submitSuccess}
        </div>
      )}

      <div
        style={{
          marginTop: 16,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <button
          type="submit"
          style={{
            ...primaryButton,
            opacity: submitDisabled || submitting ? 0.5 : 1,
            cursor: submitDisabled || submitting ? "default" : "pointer",
          }}
          disabled={submitDisabled || submitting}
        >
          {submitting ? "Sending…" : "Submit order"}
        </button>
        <div />
      </div>
    </form>
  );

  const renderTradeTab = () => (
    <div style={gridRow}>
      {/* LEFT COLUMN: Order entry + orders */}
      <div style={{ display: "grid", gap: 16 }}>
        <div style={card}>
          <div style={cardHeaderRow}>
            <div style={cardTitle}>Order entry</div>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              {renderSessionPill()}
            </div>
          </div>
          {renderOrderEntry()}
        </div>

        <div style={card}>
          <div style={cardHeaderRow}>
            <div style={cardTitle}>Active orders</div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <label style={smallText}>
                Sort
                <select
                  style={{ ...selectStyle, width: 140, marginLeft: 8, padding: "4px 8px" }}
                  value={activeOrdersSort}
                  onChange={(e) => setActiveOrdersSort(e.target.value as any)}
                >
                  <option value="time">Submitted</option>
                  <option value="symbol">Symbol</option>
                  <option value="status">Status</option>
                </select>
              </label>
              <span style={smallText}>Polling /v1/orders?status=active</span>
              <button type="button" style={dangerButton} onClick={handleCancelAllOrders} title="Cancel ALL active orders (account)">
                Cancel active
              </button>
            </div>
          </div>
          {ordersLoad.error ? (
            <div style={{ ...smallText, color: "#fecaca" }}>
              Orders error: {ordersLoad.error}
            </div>
          ) : (
            renderOrdersTable(
              dedupeActiveOrders(activeOrders)
                .filter((o) => !selectedAccount || o.account_id === selectedAccount)
                .filter((o) => {
                  const k = orderKey(o);
                  return !(k && closedKeys.has(k)) && !(o.id && closedKeys.has(String(o.id)));
                }),
              "active"
            )
          )}
        </div>

        <div style={card}>
          <div style={cardHeaderRow}>
            <div style={cardTitle}>Closed / canceled / rejected orders</div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <label style={smallText}>
                Sort
                <select
                  style={{ ...selectStyle, width: 140, marginLeft: 8, padding: "4px 8px" }}
                  value={closedOrdersSort}
                  onChange={(e) => setClosedOrdersSort(e.target.value as any)}
                >
                  <option value="time">Executed</option>
                  <option value="symbol">Symbol</option>
                  <option value="status">Status</option>
                </select>
              </label>
              <span style={smallText}>Polling /v1/orders?status=closed</span>
            </div>
          </div>
          {ordersLoad.error ? (
            <div style={{ ...smallText, color: "#fecaca" }}>
              Orders error: {ordersLoad.error}
            </div>
          ) : (
            renderOrdersTable(
              closedOrders.filter((o) => !selectedAccount || o.account_id === selectedAccount),
              "closed"
            )
          )}
        </div>
      </div>

      {/* RIGHT COLUMN: Positions + Closed positions */}
      <div style={{ display: "grid", gap: 16 }}>
        <div style={card}>
          <div style={cardHeaderRow}>
            <div style={cardTitle}>Positions</div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <label style={smallText}>
                Sort
                <select
                  style={{ ...selectStyle, width: 160, marginLeft: 8, padding: "4px 8px" }}
                  value={positionSort}
                  onChange={(e) => setPositionSort(e.target.value as any)}
                >
                  <option value="entered">Entered</option>
                  <option value="symbol">Symbol</option>
                  <option value="pnl">P&amp;L</option>
                </select>
              </label>
              <span style={smallText}>Source: Trader /v1/portfolio/positions</span>
              <button type="button" style={dangerButton} onClick={handleFlattenAll} title="Flatten ALL positions (account)">
                Flatten all
              </button>
              <button type="button" style={dangerButton} onClick={handleCancelAllOrders} title="Cancel ALL active orders (account)">
                Cancel orders
              </button>
            </div>
          </div>
          {renderPositions()}
        </div>

        <div style={card}>
          <div style={cardHeaderRow}>
            <div style={cardTitle}>Closed positions</div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <label style={smallText}>
                Scope
                <select
                  style={{ ...selectStyle, width: 120, marginLeft: 8, padding: "4px 8px" }}
                  value={closedPosScope}
                  onChange={(e) => setClosedPosScope(e.target.value as any)}
                >
                  <option value="today">Today</option>
                  <option value="all">All</option>
                </select>
              </label>
              <span style={smallText}>Source: Trader /v1/portfolio/closed_positions</span>
            </div>
          </div>
          {closedPositionsLoad.error ? (
            <div style={{ ...smallText, color: "#fecaca" }}>Closed positions error: {closedPositionsLoad.error}</div>
          ) : closedPositions.length ? (
            <div style={{ height: 240, overflowY: "auto", overflowX: "hidden", marginTop: 6 }}>
              <table style={{ width: "100%", borderCollapse: "collapse", tableLayout: "fixed" }}>
                <colgroup>
                  <col style={{ width: "18%" }} />
                  <col style={{ width: "14%" }} />
                  <col style={{ width: "6%" }} />
                  <col style={{ width: "8%" }} />
                  <col style={{ width: "14%" }} />
                  <col style={{ width: "14%" }} />
                  <col style={{ width: "12%" }} />
                  <col style={{ width: "14%" }} />
                </colgroup>
                <thead>
                  <tr style={{ borderBottom: "1px solid #111827" }}>
                    <th style={thStyle}>Time</th>
                    <th style={thStyle}>Symbol</th>
                    <th style={thStyle}>PTI</th>
                    <th style={thStyle}>Qty</th>
                    <th style={thStyle}>Entry</th>
                    <th style={thStyle}>Exit</th>
                    <th style={thStyle}>P/L</th>
                    <th style={thStyle}>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {closedPositions
                    .filter((r: any) => !selectedAccount || (r.account_id && r.account_id === selectedAccount))
                    .map((r, idx) => (
                      <tr key={`closed:${r.symbol}:${r.exit_ts || ""}:${r.qty}:${idx}`}>
                        <td style={tdStyle}>{fmtTsNY(r.exit_ts || "")}</td>
                        <td style={tdStyle}>{r.symbol}</td>
                        <td style={tdStyle}><span style={badge("#94a3b8")}>{(r.gen_id ?? "-").toString()}</span></td>
                        <td style={tdStyle}>{formatNumber(r.qty, 0)}</td>
                        <td style={tdStyle}>{r.entry_price != null ? formatCurrency(r.entry_price) : "-"}</td>
                        <td style={tdStyle}>{r.exit_price != null ? formatCurrency(r.exit_price) : "-"}</td>
                        <td style={{ ...tdStyle, color: r.pnl != null ? (r.pnl >= 0 ? "#bbf7d0" : "#fecaca") : (tdStyle as any).color }}>
                          {r.pnl != null ? formatCurrency(r.pnl) : "-"}
                        </td>
                        <td style={tdStyle}>
                          <span style={badge(String((r.close_reason || "")).toUpperCase().startsWith("STOP") ? "#f97316" : String((r.close_reason || "")).toUpperCase().startsWith("TARGET") ? "#22c55e" : "#e5e7eb")}>
                            {(r.close_reason || "CLOSED").toString()}
                          </span>
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div style={smallText}>No closed positions yet.</div>
          )}
        </div>
      </div>
    </div>
  );

  const renderAccountsTab = () => (
    <div style={card}>
      <div style={cardHeaderRow}>
        <div style={cardTitle}>Accounts overview</div>
        <span style={smallText}>
          Trader-driven portfolio snapshots (balances only for now).
        </span>
      </div>
      {renderAccountsSummary()}
    </div>
  );

  const renderDebugTab = () => (
    <div style={{ display: "grid", gap: 16 }}>
      <div style={card}>
        <div style={cardHeaderRow}>
          <div style={cardTitle}>API heartbeat</div>
        </div>
        <pre
          style={{
            background: "#020617",
            borderRadius: 8,
            padding: 8,
            maxHeight: 220,
            overflow: "auto",
            fontSize: 11,
          }}
        >
          {apiLog.join("\n") || "No API calls logged yet."}
        </pre>
      </div>
      <div style={card}>
        <div style={cardHeaderRow}>
          <div style={cardTitle}>Raw payloads</div>
        </div>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
            gap: 12,
            fontSize: 11,
          }}
        >
          <div>
            <div style={labelStyle}>Session</div>
            <pre style={preBox}>
              {JSON.stringify(lastRawSession, null, 2)}
            </pre>
          </div>
          <div>
            <div style={labelStyle}>Accounts</div>
            <pre style={preBox}>
              {JSON.stringify(lastRawAccounts, null, 2)}
            </pre>
          </div>
          <div>
            <div style={labelStyle}>Positions</div>
            <pre style={preBox}>
              {JSON.stringify(lastRawPositions, null, 2)}
            </pre>
          </div>
          <div>
            <div style={labelStyle}>Orders</div>
            <pre style={preBox}>
              {JSON.stringify(lastRawOrders, null, 2)}
            </pre>
          </div>
        </div>
      </div>
    </div>
  );

  return (
    <div style={pageStyle}>
      <div style={appShell}>
        <header style={headerRow}>
          <div>
            <div style={titleStyle}>Reflex Broker Cockpit</div>
            <div style={subtitleStyle}>
              Live accounts, local sim, and intent-driven auto trading.
            </div>
          </div>
          <div>
            <span style={smallText}>
              Instance: <strong>live</strong> • <MarketClock />
            </span>
          </div>
        </header>

        <div style={tabsRow}>
          <button
            style={tabButton(activeTab === "trade")}
            onClick={() => setActiveTab("trade")}
          >
            Trade
          </button>
          <button
            style={tabButton(activeTab === "accounts")}
            onClick={() => setActiveTab("accounts")}
          >
            Accounts
          </button>
          <button
            style={tabButton(activeTab === "debug")}
            onClick={() => setActiveTab("debug")}
          >
            Util / Debug
          </button>
        </div>

        {activeTab === "trade" && renderTradeTab()}
        {activeTab === "accounts" && renderAccountsTab()}
        {activeTab === "debug" && renderDebugTab()}
      </div>
    </div>
  );
}

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

const preBox: React.CSSProperties = {
  background: "#020617",
  borderRadius: 8,
  padding: 8,
  maxHeight: 160,
  overflow: "auto",
};
