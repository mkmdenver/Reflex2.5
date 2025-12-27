async function jget(url){ const r = await fetch(url); return r.json(); }

function steamGauge(el, value, min, max) {
  const pct = Math.max(0, Math.min(1, (value - min) / (max - min)));
  const start = Math.PI * 0.75, end = Math.PI * 2.25;
  const angle = start + pct * (end - start);
  const r = 40, cx = 50, cy = 50;
  const x = cx + r * Math.cos(angle), y = cy + r * Math.sin(angle);
  const largeArc = pct > 0.5 ? 1 : 0;
  el.innerHTML = `<svg width="100" height="60" viewBox="0 0 100 60">
    <path d="M ${cx - r*Math.cos(start)} ${cy - r*Math.sin(start)} A ${r} ${r} 0 ${largeArc} 1 ${x} ${y}" stroke="#333" stroke-width="6" fill="none"/>
    <circle cx="${cx}" cy="${cy}" r="3" fill="#333"/></svg>`;
}

async function refreshAll(){
  try{
    const m = await jget('/v1/cockpit/metrics');
    const now = Date.now()/1000;
    const wsAge = now - (m.ws_last_msg_epoch || now);
    steamGauge(document.getElementById('g_ws_last'), Math.max(0, 60 - wsAge), 0, 60);
    document.getElementById('v_ws_last').textContent = `${wsAge.toFixed(1)}s ago`;

    steamGauge(document.getElementById('g_in_q'), m.ws_in_q_depth || 0, 0, 50000);
    document.getElementById('v_in_q').textContent = m.ws_in_q_depth ?? '--';

    steamGauge(document.getElementById('g_out_q'), m.ws_out_q_depth || 0, 0, 5000);
    document.getElementById('v_out_q').textContent = m.ws_out_q_depth ?? '--';

    const dbAge = now - (m.db_last_write_epoch || now);
    steamGauge(document.getElementById('g_db_last'), Math.max(0, 60 - dbAge), 0, 60);
    document.getElementById('v_db_last').textContent = `${dbAge.toFixed(1)}s ago`;

    steamGauge(document.getElementById('g_pnl'), (m.eval?.pnl ?? 0), -10000, 10000);
    document.getElementById('v_pnl').textContent = (m.eval?.pnl ?? 0).toFixed(2);

    steamGauge(document.getElementById('g_dd'), (m.eval?.drawdown ?? 0), 0, 50);
    document.getElementById('v_dd').textContent = ((m.eval?.drawdown ?? 0)).toFixed(2) + '%';

    // tiers
    const rows = m.tiers || [];
    const html = ['<table><thead><tr><th>Symbol</th><th>db_tier</th><th>rt_tier</th></tr></thead><tbody>',
      ...rows.map(r => `<tr><td>${r.symbol}</td><td>${r.db_tier}</td><td>${r.rt_tier}</td></tr>`),
      '</tbody></table>'].join('');
    document.getElementById('tiers_table').innerHTML = html;

    // KPI
    const k = m.kpi_1m || [];
    const khtml = ['<table><thead><tr><th>Bucket</th><th>Dataset</th><th>Rows/min</th></tr></thead><tbody>',
      ...k.map(r => `<tr><td>${r.bucket}</td><td>${r.dataset}</td><td>${r.rows_ingested}</td></tr>`),
      '</tbody></table>'].join('');
    document.getElementById('kpi_box').innerHTML = khtml;

    // Controls -> set default UI values (only first time)
    if (!refreshAll._inited){
      const st = m.controls || {};
      document.getElementById('ctl_throttle').value = st.throttle ?? 100;
      document.getElementById('ctl_torque').value = st.torque ?? 1.0;
      document.getElementById('ctl_gear').value = st.gear ?? 1;
      document.getElementById('ctl_mix').value = st.mix ?? 'default';
      document.getElementById('ctl_paused').checked = !!st.paused;
      refreshAll._inited = true;
    }
  }catch(e){}
}

async function applyControls(){
  const payload = {
    throttle: parseInt(document.getElementById('ctl_throttle').value,10),
    torque: parseFloat(document.getElementById('ctl_torque').value),
    gear: parseInt(document.getElementById('ctl_gear').value,10),
    mix: document.getElementById('ctl_mix').value,
    paused: document.getElementById('ctl_paused').checked,
  };
  await fetch('/v1/cockpit/controls', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
}

document.addEventListener('DOMContentLoaded', ()=>{
  document.getElementById('btn_apply').addEventListener('click', applyControls);
  setInterval(refreshAll, 1500);
  refreshAll();
});
