// src/context/components/orders/OrderTable.tsx
// Version: Reflex 2.4 — show ORDER_METRICS columns + Time column
// Date: 2025-12-23

import React from "react";
import { useStateContext } from "@/context/StateContext";

export function fmtTs(ts?: string) {
  if (!ts) return "-";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);

  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export default function OrderTable() {
  const { state } = useStateContext();
  const { orders, metricsByOrderId } = state as any;

  const rows = Object.values(orders || {}).sort((a: any, b: any) => {
    const ta = new Date(a?.submitted_at || a?.created_at || 0).getTime();
    const tb = new Date(b?.submitted_at || b?.created_at || 0).getTime();
    return tb - ta; // newest first
  });

  return (
    <table className="w-full text-sm border">
      <thead>
        <tr className="bg-gray-200 text-left">
          <th>Time</th>
          <th>Symbol</th>
          <th>Qty</th>
          <th>Side</th>
          <th>Type</th>
          <th>Status</th>
          <th>Submit→Ack (ms)</th>
          <th>Submit→Fill (ms)</th>
        </tr>
      </thead>

      <tbody>
        {rows.map((order: any) => {
          const m = metricsByOrderId?.[order?.id]?.metrics || {};
          const ts = order?.submitted_at || order?.created_at;

          return (
            <tr
              key={order?.id || `${order?.symbol}-${ts}`}
              className="border-t"
              title={order?.id || ""}
            >
              <td>{fmtTs(ts)}</td>
              <td>{order?.symbol}</td>
              <td>{order?.qty}</td>
              <td>{order?.side}</td>
              <td>{order?.type}</td>
              <td>{order?.status}</td>
              <td>{m.submit_to_ack_ms ?? "-"}</td>
              <td>{m.submit_to_done_ms ?? "-"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
