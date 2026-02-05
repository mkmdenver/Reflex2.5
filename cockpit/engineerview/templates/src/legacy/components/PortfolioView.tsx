import React from 'react';
import type { AccountSummary } from '@/types';
import { fmt } from '@/utils';

export default function PortfolioView({
    accounts,
    onOpenAccount
}: {
    accounts: AccountSummary[];
    onOpenAccount: (id: string) => void;
}) {
    return (
        <div
            style={{
                display: 'grid',
                gap: 16,
                gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))'
            }}
        >
            {accounts.map(a => (
                <div
                    key={a.account_id}
                    style={{
                        background: '#151515',
                        border: '1px solid #2a2a2a',
                        borderRadius: 12
                    }}
                >
                    <div
                        style={{
                            padding: 14,
                            borderBottom: '1px solid #222'
                        }}
                    >
                        <div
                            style={{
                                display: 'flex',
                                justifyContent: 'space-between'
                            }}
                        >
                            <div style={{ fontWeight: 600 }}>
                                {a.name || a.account_id}
                            </div>
                            <div style={{ fontSize: 12, opacity: 0.7 }}>
                                {a.broker}
                            </div>
                        </div>
                    </div>
                    <div style={{ padding: 14, fontSize: 14 }}>
                        <Row label="Equity" value={`$${fmt(a.equity)}`} />
                        <Row label="Cash" value={`$${fmt(a.cash)}`} />
                        <Row label="Buying Power" value={`$${fmt(a.buying_power)}`} />
                        <Row
                            label="As of"
                            value={
                                a.last_heartbeat_at
                                    ? new Date(a.last_heartbeat_at).toLocaleTimeString()
                                    : '—'
                            }
                        />
                        <div
                            style={{
                                display: 'flex',
                                justifyContent: 'flex-end',
                                paddingTop: 8
                            }}
                        >
                            <button
                                onClick={() => onOpenAccount(a.account_id)}
                                style={btn()}
                            >
                                Open Cockpit →
                            </button>
                        </div>
                    </div>
                </div>
            ))}
        </div>
    );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
    return (
        <div
            style={{
                display: 'flex',
                justifyContent: 'space-between',
                padding: '6px 0',
                color: '#ddd'
            }}
        >
            <span style={{ color: '#9a9a9a' }}>{label}</span>
            <span style={{ fontWeight: 600 }}>{value}</span>
        </div>
    );
}

function btn(): React.CSSProperties {
    return {
        background: '#2b2b2b',
        color: '#fff',
        border: '1px solid #3a3a3a',
        borderRadius: 8,
        padding: '6px 10px',
        cursor: 'pointer'
    };
}
