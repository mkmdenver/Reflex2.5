import React, { useEffect, useMemo, useRef, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { AlertTriangle, Activity, Wifi, Database, Gauge, Clock3, ArrowUpDown } from "lucide-react";
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, AreaChart, Area, CartesianGrid, BarChart, Bar } from "recharts";

/**
 * Engineer Cockpit Panel
 * - Polls DataHub API + Metrics sidecar
 * - Shows live health, metrics time-series, and tier controls
 *
 * Env / Props (optional):
 *   - apiBase:   http://localhost:7000 (DataHub API)
 *   - sidecar:   http://localhost:7001 (MetricsHTTP /health)
 */
export default function EngineerPanel({ apiBase = "http://localhost:7000", sidecar = "http://localhost:7001" }: { apiBase?: string; sidecar?: string }) {
  const [health, setHealth] = useState<any>(null);
  const [snap, setSnap] = useState<any>(null);
  const [tiers, setTiers] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [symbolQuery, setSymbolQuery] = useState("");
  const [tierChange, setTierChange] = useState<{ symbol: string; tier: string }>({ symbol: "", tier: "watch" });

  // In-memory time series buffers (session-only)
  const maxPoints = 120; // ~4 minutes at 2s cadence
  const seriesRef = useRef<{ ts: number; wsAge: number; tradesOut?: number; quotesOut?: number }[]>([]);
  const [, force] = useState(0); // to re-render when series updates

  async function fetchHealth() {
    const r = await fetch(`${sidecar}/health`);
    return r.json();
  }
  async function fetchSnap() {
    const r = await fetch(`${apiBase}/v1/metrics`);
    return r.json();
  }
  async function fetchTiers() {
    const r = await fetch(`${apiBase}/v1/tiers`);
    const j = await r.json();
    return j.symbols || [];
  }
  async function postTier(symbol: string, tier: string) {
    const r = await fetch(`${apiBase}/v1/tiers`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, tier }),
    });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(t || `HTTP ${r.status}`);
    }
    return r.json();
  }

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const [h, s] = await Promise.all([fetchHealth(), fetchSnap()]);
        if (!alive) return;
        setHealth(h);
        setSnap(s);
        setError(null);

        // Append to series
        const stat = h?.stat || h; // sidecar returns {ok, stat}
        const now = Date.now();
        const wsAge = Number(stat?.ws_last_msg_age_s ?? 0);
        const tradesOut = Number(s?.registry?.trades_total ?? 0);
        const quotesOut = Number(s?.registry?.quotes_total ?? 0);
        seriesRef.current.push({ ts: now, wsAge, tradesOut, quotesOut });
        if (seriesRef.current.length > maxPoints) seriesRef.current.shift();
        force((n) => n + 1);
      } catch (e: any) {
        if (!alive) return;
        setError(e?.message || String(e));
      }
    };

    // initial fetch
    (async () => {
      setLoading(true);
      try {
        const [h, s, t] = await Promise.all([fetchHealth(), fetchSnap(), fetchTiers()]);
        if (!alive) return;
        setHealth(h);
        setSnap(s);
        setTiers(t);
        setLoading(false);
      } catch (e: any) {
        setError(e?.message || String(e));
        setLoading(false);
      }
    })();

    const id = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [apiBase, sidecar]);

  const filteredTiers = useMemo(() => {
    const q = symbolQuery.trim().toUpperCase();
    if (!q) return tiers;
    return tiers.filter((r) => String(r.symbol || "").toUpperCase().includes(q));
  }, [tiers, symbolQuery]);

  const ready = Boolean(health?.stat?.ready ?? false);
  const wsStatus = health?.stat?.ws_status || "-";
  const wsAge = health?.stat?.ws_last_msg_age_s ?? 0;
  const dbSyms = health?.stat?.symbols_total ?? 0;
  const subsT = health?.stat?.subs_trades ?? 0;
  const subsQ = health?.stat?.subs_quotes ?? 0;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Engineer Panel</h1>
        <Badge variant={ready ? "default" : "destructive"} className="text-sm">
          {ready ? "READY" : "NOT READY"}
        </Badge>
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Wifi className="w-4 h-4" /> <span>{wsStatus}</span>
        </div>
      </div>

      {/* Top Stats */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard icon={<Clock3 className="w-5 h-5" />} label="WS Last Msg Age (s)" value={wsAge} />
        <StatCard icon={<Gauge className="w-5 h-5" />} label="DB Symbols" value={dbSyms} />
        <StatCard icon={<Activity className="w-5 h-5" />} label="Subs: Trades" value={subsT} />
        <StatCard icon={<Activity className="w-5 h-5" />} label="Subs: Quotes" value={subsQ} />
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="col-span-1 lg:col-span-1">
          <CardContent className="p-4">
            <div className="font-medium mb-2">WS Last Message Age</div>
            <div className="h-40">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={seriesRef.current.map((p) => ({ x: p.ts, y: p.wsAge }))}>
                  <defs>
                    <linearGradient id="g1" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="currentColor" stopOpacity={0.4} />
                      <stop offset="95%" stopColor="currentColor" stopOpacity={0.05} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="x" tickFormatter={(v) => new Date(v).toLocaleTimeString()} minTickGap={24} />
                  <YAxis allowDecimals={false} />
                  <Tooltip labelFormatter={(v) => new Date(v).toLocaleTimeString()} />
                  <Area type="monotone" dataKey="y" stroke="currentColor" fill="url(#g1)" />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>

        <Card className="col-span-1 lg:col-span-1">
          <CardContent className="p-4">
            <div className="font-medium mb-2">Registry Counters (session)</div>
            <div className="h-40">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={seriesRef.current.map((p) => ({ x: p.ts, trades: p.tradesOut ?? 0, quotes: p.quotesOut ?? 0 }))}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="x" tickFormatter={(v) => new Date(v).toLocaleTimeString()} minTickGap={24} />
                  <YAxis allowDecimals={false} />
                  <Tooltip labelFormatter={(v) => new Date(v).toLocaleTimeString()} />
                  <Bar dataKey="trades" stackId="a" />
                  <Bar dataKey="quotes" stackId="a" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </CardContent>
        </Card>

        <Card className="col-span-1 lg:col-span-1">
          <CardContent className="p-4">
            <div className="font-medium mb-2">Quick Snapshot</div>
            <div className="grid grid-cols-2 gap-y-2 text-sm">
              <div className="text-muted-foreground">API Base</div>
              <div className="truncate">{apiBase}</div>
              <div className="text-muted-foreground">Sidecar</div>
              <div className="truncate">{sidecar}</div>
              <div className="text-muted-foreground">Uptime (s)</div>
              <div>{health?.stat?.uptime_s ?? "-"}</div>
              <div className="text-muted-foreground">Ready</div>
              <div>{ready ? "true" : "false"}</div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Tier Controls */}
      <Card>
        <CardContent className="p-4 space-y-3">
          <div className="flex items-center justify-between">
            <div className="font-medium">Runtime Tiers</div>
            <div className="flex items-center gap-2">
              <Input placeholder="Filter symbol e.g. AAPL" value={symbolQuery} onChange={(e) => setSymbolQuery(e.target.value)} className="w-56" />
              <Button variant="outline" size="sm" onClick={async () => setTiers(await fetchTiers())}>Refresh</Button>
            </div>
          </div>

          <div className="overflow-auto border rounded-2xl">
            <table className="w-full text-sm">
              <thead className="bg-muted/40">
                <tr>
                  <th className="text-left p-2">Symbol</th>
                  <th className="text-left p-2">DB Tier</th>
                  <th className="text-left p-2">RT Tier</th>
                  <th className="text-left p-2">Change</th>
                </tr>
              </thead>
              <tbody>
                {filteredTiers.slice(0, 200).map((r) => (
                  <tr key={r.symbol} className="border-t">
                    <td className="p-2 font-mono">{r.symbol}</td>
                    <td className="p-2">{r.db_tier || "-"}</td>
                    <td className="p-2">
                      <Badge variant={tierVariant(r.rt_tier)}>{r.rt_tier || "cold"}</Badge>
                    </td>
                    <td className="p-2">
                      <TierChanger symbol={r.symbol} onChanged={async () => setTiers(await fetchTiers())} apiBase={apiBase} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      {error && (
        <div className="flex items-center gap-2 text-destructive">
          <AlertTriangle className="w-4 h-4" /> <span className="text-sm">{error}</span>
        </div>
      )}
    </div>
  );
}

function StatCard({ icon, label, value }: { icon: React.ReactNode; label: string; value: any }) {
  return (
    <Card className="shadow-sm">
      <CardContent className="p-4">
        <div className="flex items-center gap-3">
          <div className="p-2 rounded-xl bg-muted">{icon}</div>
          <div>
            <div className="text-sm text-muted-foreground">{label}</div>
            <div className="text-xl font-semibold">{formatNumber(value)}</div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

function formatNumber(v: any) {
  if (v === null || v === undefined) return "-";
  if (typeof v === "number") return Number.isInteger(v) ? v.toString() : v.toFixed(0);
  const n = Number(v);
  return Number.isFinite(n) ? (Number.isInteger(n) ? n.toString() : n.toFixed(0)) : String(v);
}

function tierVariant(tier?: string) {
  switch ((tier || "cold").toLowerCase()) {
    case "hot":
      return "default";
    case "warm":
      return "secondary";
    case "watch":
      return "outline";
    default:
      return "destructive"; // cold
  }
}

function TierChanger({ symbol, onChanged, apiBase }: { symbol: string; onChanged: () => void; apiBase: string }) {
  const [pending, setPending] = useState(false);
  const [sel, setSel] = useState("watch");
  const doChange = async () => {
    setPending(true);
    try {
      await fetch(`${apiBase}/v1/tiers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol, tier: sel }),
      });
      onChanged();
    } catch (e) {
      console.error(e);
    } finally {
      setPending(false);
    }
  };
  return (
    <div className="flex items-center gap-2">
      <Select value={sel} onValueChange={setSel}>
        <SelectTrigger className="w-[140px]"><SelectValue placeholder="Tier" /></SelectTrigger>
        <SelectContent>
          <SelectItem value="cold">cold</SelectItem>
          <SelectItem value="watch">watch</SelectItem>
          <SelectItem value="warm">warm</SelectItem>
          <SelectItem value="hot">hot</SelectItem>
        </SelectContent>
      </Select>
      <Button size="sm" onClick={doChange} disabled={pending}>
        <ArrowUpDown className="w-4 h-4 mr-1" /> Apply
      </Button>
    </div>
  );
}
