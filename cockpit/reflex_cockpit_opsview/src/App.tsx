import React from 'react';
import { NavLink, Route, Routes } from 'react-router-dom';
import ModelView from './views/ModelView';
import RiskView from './views/RiskView';
import EngineerView from './views/EngineerView';

function TopNav() {
  return (
    <div className="nav">
      <div className="brand">
        <div className="logo">R</div>
        <div>
          <div className="brandTitle">Reflex OpsView</div>
          <div className="brandSub">Model · Risk · Engineer</div>
        </div>
      </div>

      <div className="navLinks">
        <NavLink to="/models" className={({ isActive }) => (isActive ? 'navLink navLinkActive' : 'navLink')}>ModelView</NavLink>
        <NavLink to="/risk" className={({ isActive }) => (isActive ? 'navLink navLinkActive' : 'navLink')}>Portfolio/Risk</NavLink>
        <NavLink to="/engineer" className={({ isActive }) => (isActive ? 'navLink navLinkActive' : 'navLink')}>Engineer</NavLink>
      </div>

      <div className="navRight">
        <EnvBadge />
      </div>
    </div>
  );
}

function EnvBadge() {
  const trader = (import.meta.env.VITE_TRADER_BASE as string) || '(via /trader proxy)';
  const datahub = (import.meta.env.VITE_DATAHUB_BASE as string) || '(via /datahub proxy)';
  return (
    <div className="badgeRow">
      <span className="badge">Trader: {trader}</span>
      <span className="badge">DataHub: {datahub}</span>
    </div>
  );
}

export default function App() {
  return (
    <div className="app">
      <TopNav />
      <div className="content">
        <Routes>
          <Route path="/" element={<ModelView />} />
          <Route path="/models" element={<ModelView />} />
          <Route path="/risk" element={<RiskView />} />
          <Route path="/engineer" element={<EngineerView />} />
        </Routes>
      </div>
    </div>
  );
}
