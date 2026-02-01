import React from 'react';

export function Card(props: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="card">
      <div className="cardHeader">
        <div className="cardTitle">{props.title}</div>
        <div>{props.right}</div>
      </div>
      <div className="cardBody">{props.children}</div>
    </div>
  );
}

export function Chip(props: { text: string; tone?: 'ok' | 'warn' | 'bad' | 'info' }) {
  const cls = props.tone ? `chip ${props.tone}` : 'chip';
  return <span className={cls}>{props.text}</span>;
}
