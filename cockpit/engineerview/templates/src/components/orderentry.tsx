import React, { useEffect, useState } from "react";

type AccountSummary = {
  account_id: string;
  label: string;
  broker: string;
  account_type: string;
  status: string;
  currency: string;
  cash: number;
  equity: number;
  buying_power: number;
};

type OrderCreateRequest = {
  account_id: string;
  symbol: string;
  side: "buy" | "sell";
  qty: number;
  type: "market" | "limit";
  limit_price?: number;
  time_in_force: "day" | "gtc" | "ioc" | "fok";
  extended_hours: boolean;
  exec_at?: string; // ISO string
};

type OrderCreateResponse = {
  ok: boolean;
  message?: string;
  order?: {
    order_id: string;
    status: string;
  };
};

interface Props {
  traderOnline: boolean;
}

export const OrderEntry: React.FC<Props> = ({ traderOnline }) => {
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [loadingAccounts, setLoadingAccounts] = useState(false);

  const [form, setForm] = useState<OrderCreateRequest>({
    account_id: "",
    symbol: "",
    side: "buy",
    qty: 100,
    type: "market",
    time_in_force: "day",
    extended_hours: false,
  });

  const [submitting, setSubmitting] = useState(false);
  const [resultMsg, setResultMsg] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    const fetchAccounts = async () => {
      setLoadingAccounts(true);
      try {
        const resp = await fetch("/v1/accounts");
        const data = await resp.json();
        if (data && Array.isArray(data.accounts)) {
          setAccounts(data.accounts);
          if (!form.account_id && data.accounts.length > 0) {
            setForm((f) => ({ ...f, account_id: data.accounts[0].account_id }));
          }
        }
      } catch (err) {
        console.error("Failed to load accounts", err);
      } finally {
        setLoadingAccounts(false);
      }
    };
    fetchAccounts();
  }, []);

  const updateField = <K extends keyof OrderCreateRequest>(
    key: K,
    value: OrderCreateRequest[K],
  ) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  const onSubmit: React.FormEventHandler = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    setResultMsg(null);
    setErrorMsg(null);

    try {
      const payload: OrderCreateRequest = { ...form };

      if (payload.type === "market") {
        delete payload.limit_price;
      }

      const resp = await fetch("/v1/orders/place", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const data: OrderCreateResponse = await resp.json();
      if (!resp.ok || !data.ok) {
        setErrorMsg(data.message || `Order rejected (${resp.status})`);
      } else if (data.order) {
        setResultMsg(
          `Order ${data.order.order_id} accepted with status ${data.order.status}`,
        );
        // FIX: broadcast that an order was submitted
        window.dispatchEvent(
          new CustomEvent("reflex:order_submitted", { detail: data.order }),
        );
      } else {
        setResultMsg("Order accepted.");
        // FIX: broadcast that an order was submitted
        window.dispatchEvent(
          new CustomEvent("reflex:order_submitted", { detail: { ok: true } }),
        );
      }
    } catch (err: any) {
      console.error(err);
      setErrorMsg(err?.message || "Order failed.");
    } finally {
      setSubmitting(false);
      // FIX: nudge the rest of the UI to refresh orders immediately
      window.dispatchEvent(new Event("reflex:orders_refresh"));
    }
  };

  return (
    <div className="flex flex-col gap-3 p-3 border rounded-xl">
      <div className="flex justify-between items-center">
        <h2 className="text-lg font-semibold">Manual Order Entry</h2>
        <span
          className={
            "text-xs px-2 py-1 rounded-full " +
            (traderOnline ? "bg-green-200" : "bg-red-200")
          }
        >
          Trader {traderOnline ? "ONLINE" : "OFFLINE"}
        </span>
      </div>

      <form className="grid grid-cols-2 gap-3" onSubmit={onSubmit}>
        <label className="flex flex-col text-sm">
          Account
          <select
            className="mt-1 border rounded px-2 py-1"
            value={form.account_id}
            onChange={(e) => updateField("account_id", e.target.value)}
            disabled={loadingAccounts || submitting}
          >
            {accounts.map((a) => (
              <option key={a.account_id} value={a.account_id}>
                {a.account_id} ({a.status})
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col text-sm">
          Symbol
          <input
            className="mt-1 border rounded px-2 py-1 uppercase"
            value={form.symbol}
            onChange={(e) => updateField("symbol", e.target.value.toUpperCase())}
            disabled={submitting}
          />
        </label>

        <label className="flex flex-col text-sm">
          Side
          <select
            className="mt-1 border rounded px-2 py-1"
            value={form.side}
            onChange={(e) => updateField("side", e.target.value as "buy" | "sell")}
            disabled={submitting}
          >
            <option value="buy">Buy</option>
            <option value="sell">Sell</option>
          </select>
        </label>

        <label className="flex flex-col text-sm">
          Quantity
          <input
            className="mt-1 border rounded px-2 py-1"
            type="number"
            min={1}
            value={form.qty}
            onChange={(e) =>
              updateField("qty", Number(e.target.value) || (0 as any))
            }
            disabled={submitting}
          />
        </label>

        <label className="flex flex-col text-sm">
          Order Type
          <select
            className="mt-1 border rounded px-2 py-1"
            value={form.type}
            onChange={(e) =>
              updateField("type", e.target.value as "market" | "limit")
            }
            disabled={submitting}
          >
            <option value="market">Market</option>
            <option value="limit">Limit</option>
          </select>
        </label>

        <label className="flex flex-col text-sm">
          Limit Price
          <input
            className="mt-1 border rounded px-2 py-1"
            type="number"
            step="0.01"
            value={form.limit_price ?? ""}
            onChange={(e) =>
              updateField(
                "limit_price",
                e.target.value === "" ? undefined : Number(e.target.value),
              )
            }
            disabled={submitting || form.type === "market"}
          />
        </label>

        <label className="flex flex-col text-sm">
          Time in Force
          <select
            className="mt-1 border rounded px-2 py-1"
            value={form.time_in_force}
            onChange={(e) =>
              updateField(
                "time_in_force",
                e.target.value as OrderCreateRequest["time_in_force"],
              )
            }
            disabled={submitting}
          >
            <option value="day">DAY</option>
            <option value="gtc">GTC</option>
            <option value="ioc">IOC</option>
            <option value="fok">FOK</option>
          </select>
        </label>

        <label className="flex flex-col text-sm">
          Exec at (optional, UTC ISO)
          <input
            className="mt-1 border rounded px-2 py-1"
            placeholder="2025-11-13T16:05:00Z"
            value={form.exec_at ?? ""}
            onChange={(e) =>
              updateField("exec_at", e.target.value || undefined)
            }
            disabled={submitting}
          />
        </label>

        <label className="flex flex-row items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={form.extended_hours}
            onChange={(e) => updateField("extended_hours", e.target.checked)}
            disabled={submitting}
          />
          Allow extended hours
        </label>

        <div className="flex items-end justify-end col-span-2">
          <button
            type="submit"
            className="px-4 py-2 rounded bg-blue-500 text-white disabled:opacity-60"
            disabled={submitting || !form.symbol || !form.account_id || !traderOnline}
          >
            {submitting ? "Placing…" : "Submit Order"}
          </button>
        </div>
      </form>

      {resultMsg && <div className="text-sm text-green-700">{resultMsg}</div>}
      {errorMsg && <div className="text-sm text-red-700">{errorMsg}</div>}
    </div>
  );
};
