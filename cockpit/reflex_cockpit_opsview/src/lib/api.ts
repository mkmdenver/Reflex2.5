export type TraderHealth = {
  ok: boolean;
  adapters?: string[];
  portfolio_accounts?: string[];
  last_poll_at?: string | null;
  last_poll_error?: string | null;
  orders_last_sync_at?: string | null;
  orders_last_sync_error?: string | null;
};

export type PortfolioOverview = {
  overview: Record<
    string,
    {
      balances: { cash: number; equity: number; buying_power: number; updated_at: any };
      positions: Array<{
        account_id: string;
        symbol: string;
        qty: number;
        avg_price: number;
        market_price: number;
        market_value: number;
        unrealized_pl: number;
        unrealized_plpc: number;
        side: string;
        stop_price?: number | null;
        target_price?: number | null;
      }>;
      broker_id?: string | null;
      kind?: string | null;
    }
  >;
  count: number;
};

export type IntentDoc = {
  intent_id: string;
  trade_id: string;
  created_ts: number;
  account_id: string;
  symbol: string;
  side: string;
  source: string;
  strategy_id?: string | null;
  trigger?: any;
  raw?: any;
};

export type TradeDoc = {
  trade_id: string;
  intent_id: string;
  created_ts: number;
  account_id: string;
  symbol: string;
  side: string;
  state: string;
  strategy_id?: string | null;
  source?: string;
  risk?: any;
  plan?: any;
  qty?: number;
};

export type Bars1mResp = {
  ok: boolean;
  symbol: string;
  count: number;
  bars: Array<{ ts: number; close: number; volume: number }>;
};

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
  });

  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${txt}`);
  }

  return (await res.json()) as T;
}

// NOTE: In dev we use Vite proxies:
//   /trader  -> http://127.0.0.1:7002
//   /datahub -> http://127.0.0.1:7001

export const api = {
  trader: {
    health: () => fetchJson<TraderHealth>('/trader/v1/health'),
    overview: () => fetchJson<PortfolioOverview>('/trader/v1/portfolio/overview'),
    intents: (limit = 50) => fetchJson<{ ok: boolean; intents: IntentDoc[] }>(`/trader/v1/intents?limit=${limit}`),
    trades: (status: 'planned' | 'active' | 'closed' = 'active') =>
      fetchJson<{ ok: boolean; trades: TradeDoc[] }>(`/trader/v1/trades?status=${status}`),
    eventsUrl: () => '/trader/v1/events',
  },
  datahub: {
    bars1m: (symbol: string, limit = 240) =>
      fetchJson<Bars1mResp>(`/datahub/v1/history/bars1m?symbol=${encodeURIComponent(symbol)}&limit=${limit}`),
  },
};

export const getTraderHealth = () => api.trader.health();
export const getPortfolioOverview = () => api.trader.overview();
export const getIntents = (limit = 50) => api.trader.intents(limit);
export const getTrades = (status: 'planned' | 'active' | 'closed' = 'active') => api.trader.trades(status);
export const getBars1m = (symbol: string, limit = 240) => api.datahub.bars1m(symbol, limit);
