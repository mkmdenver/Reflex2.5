import React, { useEffect, useState } from "react";
import { api } from "@/api";
import type {
  AccountSummary,
  CapabilityDoc,
  OrderRow,
  PositionRow,
  SessionState,
} from "@/types";
import CapabilityChips from "./CapabilityChips";
import OrderTicket from "./OrderTicket";
import { fmt } from "@/utils";

// MM-DD HH:MM:SS (24h) in New York (market) time
function fmtTs(ts?: string | null) {
  if (!ts) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);

  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).formatToParts(d);

  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "??";
  return `${get("month")}-${get("day")} ${get("hour")}:${get("minute")}:${get("second")}`;
}

export default function AccountCockpit({
  accountId,
  session,
  accounts,
  onChangeAccount,
}: {
  accountId: string;
  session: SessionState;
  accounts: AccountSummary[];
  onChangeAccount: (id: string) => void;
}) {
  const [caps, setCaps] = useState<CapabilityDoc | null>(null);
  const [positions, setPositions] = useState<PositionRow[]>([]);
  const [orders, setOrders] = useState<OrderRow[]>([]);

  useEffect(() => {
    setCaps(null);
    api.caps(accountId).then(setCaps);
    api.positions(accountId).then(setPositions);
    api.orders(accountId).then(setOrders);
  }, [accountId, session]);

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div
        style={{
          background: "#151515",
          border: "1px solid #2a2a2a",
          borderRadius: 12,
          padding: 14,
        }}
      >
        <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
          <label>
            <span style={{ fontSize: 12, color: "#9a9a9a", marginRight: 8 }}>
              Account
            </span>
            <select
              value={accountId}
              onChange={(e) => onChangeAccount(e.target.value)}
            >
              {accounts.map((a) => (
                <option key={a.account_id} value={a.account_id}>
                  {a.name || a.account_id}
                </option>
              ))}
            </select>
          </label>
          <div style={{ fontSize: 12, color: "#9a9a9a" }}>Capability Matrix</div>
          <CapabilityChips caps={caps} />
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gap: 16,
          gridTemplateColumns: "minmax(0, 1fr) 420px",
        }}
      >
        <div style={{ display: "grid", gap: 16 }}>
          <Card title="Positions">
            <table style={{ width: "100%", fontSize: 14 }}>
              <thead style={{ color: "#aaa" }}>
                <tr style={{ borderBottom: "1px solid #222" }}>
                  <th className="th">Symbol</th>
                  <th className="th">Qty</th>
                  <th className="th">Avg</th>
                  <th className="th">Mark</th>
                  <th className="th">P&amp;L</th>
                  <th className="th">Actions</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((p) => (
                  <tr key={p.symbol} style={{ borderBottom: "1px solid #1b1b1b" }}>
                    <td className="td">{p.symbol}</td>
                    <td className="td">{p.qty}</td>
                    <td className="td">${fmt(p.avg_price)}</td>
                    <td className="td">${fmt(p.market_price)}</td>
                    <td
                      className="td"
                      style={{ color: p.unrealized_pl >= 0 ? "#32d583" : "#ff6b6b" }}
                    >
                      {p.unrealized_pl >= 0 ? "+" : ""}
                      {fmt(p.unrealized_pl)}
                    </td>
                    <td className="td">
                      <div style={{ display: "flex", gap: 8 }}>
                        <button style={btn("secondary")}>Add TP/SL</button>
                        <button style={btn("danger")}>Flatten</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>

          <Card title="Working Orders">
            <table style={{ width: "100%", fontSize: 14 }}>
              <thead style={{ color: "#aaa" }}>
                <tr style={{ borderBottom: "1px solid #222" }}>
                  <th className="th">Time</th>
                  <th className="th">Symbol</th>
                  <th className="th">Side</th>
                  <th className="th">Type</th>
                  <th className="th">Params</th>
                  <th className="th">Status</th>
                  <th className="th">Mode</th>
                  <th className="th">Note</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((o: any) => (
                  <tr key={o.id} style={{ borderBottom: "1px solid #1b1b1b" }}>
                    <td className="td mono" title={o.client_order_id || o.id}>
                      {fmtTs(o.submitted_at)}
                    </td>
                    <td className="td">{o.symbol}</td>
                    <td className="td">{String(o.side || "").toUpperCase()}</td>
                    <td className="td">{String(o.type || "").toUpperCase()}</td>
                    <td className="td">
                      {o.type === "limit" && <>LMT {o.limit_price}</>}
                      {o.type === "stop" && <>STP {o.stop_price}</>}
                      {o.type === "stop_limit" && (
                        <>
                          STP {o.stop_price} / LMT {o.limit_price}
                        </>
                      )}
                    </td>
                    <td className="td">{o.status}</td>
                    <td className="td">{o.hybrid ? "Hybrid" : "Native"}</td>
                    <td className="td">{o.note || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </div>

        <OrderTicket
          accountId={accountId}
          session={session}
          caps={caps}
          onSubmitted={(o) => setOrders((prev) => [o, ...prev])}
        />
      </div>
    </div>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ background: "#151515", border: "1px solid #2a2a2a", borderRadius: 12 }}>
      <div style={{ padding: 12, borderBottom: "1px solid #222", fontWeight: 700 }}>
        {title}
      </div>
      <div style={{ padding: 12 }}>{children}</div>
    </div>
  );
}

function btn(kind?: "secondary" | "danger") {
  const bg = kind === "danger" ? "#b42318" : kind === "secondary" ? "#2b2b2b" : "#3d59ff";
  return {
    background: bg,
    color: "#fff",
    border: "1px solid #3a3a3a",
    borderRadius: 8,
    padding: "6px 10px",
    cursor: "pointer",
  } as React.CSSProperties;
}
