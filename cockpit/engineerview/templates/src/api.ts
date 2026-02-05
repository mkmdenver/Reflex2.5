import type { MarketSessionDoc,AccountSummary,CapabilityDoc,PositionRow,OrderRow,CreateOrderRequest,CreateOrderResponse } from './types';
const PORT = (import.meta as any).env.VITE_BROKERVIEW_PORT ?? '7010';
const API_BASE=(import.meta as any).env.VITE_API_BASE ?? ('http://127.0.0.1:'+PORT);
async function j<T>(u:string,i?:RequestInit){const r=await fetch(API_BASE+u,i); if(!r.ok) throw new Error(`${r.status}`); return r.json();}
export const api={session:()=>j<MarketSessionDoc>('/v1/market/session'),accounts:()=>j<AccountSummary[]>('/v1/accounts'),caps:(id:string)=>j<CapabilityDoc>(`/v1/accounts/${encodeURIComponent(id)}/capabilities`),positions:(id:string)=>j<PositionRow[]>(`/v1/positions?account=${encodeURIComponent(id)}`),orders:(id:string,s='working')=>j<OrderRow[]>(`/v1/orders?account=${encodeURIComponent(id)}&status=${s}`),createOrder:(p:CreateOrderRequest)=>j<CreateOrderResponse>('/v1/orders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)}),};
export const API_BASE_URL=API_BASE;
