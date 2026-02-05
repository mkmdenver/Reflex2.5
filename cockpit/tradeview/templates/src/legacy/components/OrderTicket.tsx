import React, { useMemo, useState } from "react";
import type { CapabilityDoc, CreateOrderRequest, OrderRow, SessionState } from "@/types";
import { api } from "@/api";

export default function OrderTicket({ accountId, session, caps, onSubmitted }: { accountId: string; session: SessionState; caps: CapabilityDoc | null; onSubmitted: (o: OrderRow) => void }) {
  const [symbol, setSymbol] = useState("AAPL");
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [type, setType] = useState<OrderRow["type"]>("limit");
  const [qty, setQty] = useState<number>(100);
  const [limit, setLimit] = useState<string>("");
  const [stop, setStop] = useState<string>("");
  const [tif, setTif] = useState<"day" | "gtc" | "ioc">("day");
  const [ext, setExt] = useState<boolean>(false);
  const [goodAfter, setGoodAfter] = useState<string>("");
  const [cancelAt, setCancelAt] = useState<string>("");
  const [tp, setTp] = useState<string>("");
  const [sl, setSl] = useState<string>("");
  const [slOff, setSlOff] = useState<string>("");
  const [simulate, setSim] = useState(true);
  const [note, setNote] = useState<string>("testing");

  const nativeAllowed = useMemo(() => {
    if (!caps) return undefined;
    switch (type) { case "market": return caps.native.market; case "limit": return caps.native.limit; case "stop": return caps.native.stop; case "stop_limit": return caps.native.stop_limit; case "trailing": return caps.native.trailing; }
  }, [caps, type]);

  const hybrid = nativeAllowed === false && simulate;

  async function submit() {
    const payload: CreateOrderRequest = {
      account_id: accountId, symbol, side, type, qty,
      limit_price: limit ? Number(limit) : undefined, stop_price: stop ? Number(stop) : undefined,
      tif, extended_hours: ext,
      bracket: (tp || sl || slOff) ? { take_profit: tp ? Number(tp) : undefined, stop_loss: sl ? Number(sl) : undefined, stop_limit_offset: slOff ? Number(slOff) : undefined } : undefined,
      advanced: { simulate_if_unsupported: simulate, good_after: goodAfter || undefined, cancel_if_not_filled_at: cancelAt || undefined },
      note: note || "testing",
    };
    try {
      const res = await api.createOrder(payload);
      onSubmitted({ id: res.order_id, account_id: accountId, symbol, side, type, status: "pending", limit_price: payload.limit_price, stop_price: payload.stop_price, tif, extended_hours: ext, hybrid, note });
    } catch {
      const tmp = `tmp_${Date.now()}`;
      onSubmitted({ id: tmp, account_id: accountId, symbol, side, type, status: "pending", limit_price: payload.limit_price, stop_price: payload.stop_price, tif, extended_hours: ext, hybrid, note });
    }
  }

  const preflightText = nativeAllowed === undefined ? "Checking venue rules…" : nativeAllowed ? "Will submit as Native broker order." : !simulate ? "Unsupported now. Enable Hybrid to simulate." : `Will submit as Hybrid (simulated) because ${type.toUpperCase()} isn't native in ${session}.`;

  return (
    <div style={{ background: "#151515", border: "1px solid #2a2a2a", borderRadius: 12, padding: 14 }}>
      <div style={{ fontWeight: 700, marginBottom: 8 }}>Order Entry</div>
      <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(4,1fr)" }}>
        <Field label="Symbol"><input value={symbol} onChange={e => setSymbol(e.target.value.toUpperCase())} /></Field>
        <Field label="Side"><select value={side} onChange={e => setSide(e.target.value as any)}><option value="buy">Buy</option><option value="sell">Sell</option></select></Field>
        <Field label="Qty"><input type="number" value={qty} onChange={e => setQty(Number(e.target.value))} /></Field>
        <Field label="Type"><select value={type} onChange={e => setType(e.target.value as any)}>
          <option value="market">Market</option><option value="limit">Limit</option><option value="stop">Stop</option><option value="stop_limit">Stop-Limit</option><option value="trailing">Trailing</option>
        </select></Field>
      </div>
      <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(4,1fr)", marginTop: 8 }}>
        {(type === "limit" || type === "stop_limit") && <Field label="Limit"><input value={limit} onChange={e => setLimit(e.target.value)} placeholder="189.10" /></Field>}
        {(type === "stop" || type === "stop_limit") && <Field label="Stop"><input value={stop} onChange={e => setStop(e.target.value)} placeholder="187.90" /></Field>}
        <Field label="TIF"><select value={tif} onChange={e => setTif(e.target.value as any)}><option value="day">DAY</option><option value="gtc">GTC</option><option value="ioc">IOC</option></select></Field>
        <Field label="Extended Hours"><input type="checkbox" checked={ext} onChange={e => setExt(e.target.checked)} /> Enable</Field>
      </div>
      <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(3,1fr)", marginTop: 8 }}>
        <Field label="Good After (UTC ISO)"><input value={goodAfter} onChange={e => setGoodAfter(e.target.value)} placeholder="2025-11-11T14:25:00Z" /></Field>
        <Field label="Cancel If Not Filled (UTC ISO)"><input value={cancelAt} onChange={e => setCancelAt(e.target.value)} placeholder="2025-11-11T15:00:00Z" /></Field>
        <Field label="Hybrid if unsupported"><input type="checkbox" checked={simulate} onChange={e => setSim(e.target.checked)} /></Field>
      </div>
      <div style={{ display: "grid", gap: 8, gridTemplateColumns: "repeat(3,1fr)", marginTop: 8 }}>
        <Field label="Bracket Take Profit"><input value={tp} onChange={e => setTp(e.target.value)} placeholder="191.90" /></Field>
        <Field label="Bracket Stop Loss"><input value={sl} onChange={e => setSl(e.target.value)} placeholder="187.90" /></Field>
        <Field label="SL Limit Offset"><input value={slOff} onChange={e => setSlOff(e.target.value)} placeholder="0.05" /></Field>
      </div>
      <div style={{ marginTop: 8, padding: 8, border: "1px solid #333", borderRadius: 8, background: "#111" }}>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>Preflight</div>
        <div style={{ fontSize: 13, opacity: 0.9 }}>{preflightText}</div>
      </div>
      <div style={{ marginTop: 8, display: "grid", gap: 8, gridTemplateColumns: "2fr 1fr" }}>
        <Field label="Order Note"><input value={note} onChange={e => setNote(e.target.value)} /></Field>
        <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "flex-end", gap: 8 }}>
          <button style={btn("secondary")}>Preview</button>
          <button style={btn()} onClick={submit}>Submit {hybrid ? "(Hybrid)" : ""}</button>
        </div>
      </div>
    </div>
  );
}
function Field({ label, children }: { label: string; children: React.ReactNode }) { return (<label style={{ display: "grid", gap: 6 }}><span style={{ fontSize: 12, color: "#9a9a9a" }}>{label}</span><div style={{ display: "flex" }}>{children}</div></label>); }
function btn(variant?: "secondary") { return { background: variant ? "#2b2b2b" : "#3d59ff", color: "#fff", border: "1px solid #3a3a3a", borderRadius: 8, padding: "8px 12px", cursor: "pointer" } as React.CSSProperties; }
