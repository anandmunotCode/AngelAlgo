/* ═════════════════════════════════════════════════════════════════
   DELTA NEUTRAL NIFTY — REAL-TIME FRONTEND CLIENT ENGINE
   ═════════════════════════════════════════════════════════════════ */

// Initialize Socket.io client
const socket = io();

// UI Elements Cache
const elements = {
  connectionPill: document.getElementById('connectionPill'),
  statusText: document.getElementById('statusText'),
  modeBadge: document.getElementById('modeBadge'),
  modeText: document.getElementById('modeText'),
  liveClock: document.getElementById('liveClock'),
  
  // Hero KPI Card Values
  totalPnl: document.getElementById('totalPnl'),
  roiBadge: document.getElementById('roiBadge'),
  realizedPnl: document.getElementById('realizedPnl'),
  unrealizedPnl: document.getElementById('unrealizedPnl'),
  
  spotPrice: document.getElementById('spotPrice'),
  expiryBadge: document.getElementById('expiryBadge'),
  
  deployedMargin: document.getElementById('deployedMargin'),
  straddlePhaseBadge: document.getElementById('straddlePhaseBadge'),
  straddleSlLimit: document.getElementById('straddleSlLimit'),
  marginSource: document.getElementById('marginSource'),
  
  netDelta: document.getElementById('netDelta'),
  netTheta: document.getElementById('netTheta'),
  netGamma: document.getElementById('netGamma'),
  netVega: document.getElementById('netVega'),
  equilibriumBadge: document.getElementById('equilibriumBadge'),
  
  // Gauges
  surgeProgressBar: document.getElementById('surgeProgressBar'),
  surgeStatusTag: document.getElementById('surgeStatusTag'),
  maxSurgeVal: document.getElementById('maxSurgeVal'),
  
  straddleSlProgressBar: document.getElementById('straddleSlProgressBar'),
  straddleRiskTag: document.getElementById('straddleRiskTag'),
  straddleCurrentLoss: document.getElementById('straddleCurrentLoss'),
  straddleMaxLoss: document.getElementById('straddleMaxLoss'),
  
  // Tables & Logs
  activeLegsCount: document.getElementById('activeLegsCount'),
  matrixTableBody: document.getElementById('matrixTableBody'),
  adjustmentTimeline: document.getElementById('adjustmentTimeline'),
  adjCountBadge: document.getElementById('adjCountBadge'),
  auditLogsBox: document.getElementById('auditLogsBox'),
  tradesCountBadge: document.getElementById('tradesCountBadge'),
  latencyVal: document.getElementById('latencyVal')
};

// State Variables
let lastUpdateTime = Date.now();
let prevSpotPrice = null;

// Currency Formatter
function formatINR(val, showSign = false) {
  const num = parseFloat(val) || 0.0;
  const formatted = Math.abs(num).toLocaleString('en-IN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
  if (showSign) {
    return (num > 0 ? '+₹' : (num < 0 ? '-₹' : '₹')) + formatted;
  }
  return '₹' + formatted;
}

// Live Clock in IST
function updateLiveClock() {
  const now = new Date();
  const options = { timeZone: 'Asia/Kolkata', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' };
  elements.liveClock.textContent = now.toLocaleTimeString('en-GB', options) + ' IST';
}
setInterval(updateLiveClock, 1000);
updateLiveClock();

// WebSocket Connection Handlers
socket.on('connect', () => {
  elements.statusText.textContent = 'LIVE WS CONNECTED';
  elements.connectionPill.style.background = 'rgba(16, 185, 129, 0.12)';
  elements.connectionPill.style.color = '#10b981';
  elements.connectionPill.style.borderColor = 'rgba(16, 185, 129, 0.25)';
});

socket.on('disconnect', () => {
  elements.statusText.textContent = 'DISCONNECTED (RECONNECTING...)';
  elements.connectionPill.style.background = 'rgba(244, 63, 94, 0.12)';
  elements.connectionPill.style.color = '#f43f5e';
  elements.connectionPill.style.borderColor = 'rgba(244, 63, 94, 0.25)';
});

// WebSocket Stream Receiver
socket.on('update', (data) => {
  const now = Date.now();
  const latency = now - lastUpdateTime;
  lastUpdateTime = now;
  elements.latencyVal.textContent = `${Math.min(latency, 25)}ms`;

  if (!data || !data.position) {
    return;
  }

  const pos = data.position;
  const trades = data.trades || [];

  renderTerminalState(pos, trades);
});

// Main Render Function
function renderTerminalState(pos, trades) {
  // 1. Mode Badge (Live vs Paper Simulation)
  const isPending = pos.status === 'PENDING' || !pos.legs || pos.legs.length === 0;
  const isPaper = pos.is_paper_trading !== false && pos.trading_mode !== 'LIVE_TRADING';
  
  if (isPending) {
    elements.modeBadge.className = 'mode-badge paper-mode';
    elements.modeBadge.innerHTML = `<span class="mode-icon">⏳</span><span class="mode-text">${isPaper ? 'PAPER MODE (STANDBY)' : 'LIVE MODE (STANDBY)'}</span>`;
  } else if (isPaper) {
    elements.modeBadge.className = 'mode-badge paper-mode';
    elements.modeBadge.innerHTML = '<span class="mode-icon">🧪</span><span class="mode-text">PAPER SIMULATION</span>';
  } else {
    elements.modeBadge.className = 'mode-badge live-mode';
    elements.modeBadge.innerHTML = '<span class="mode-icon">🔥</span><span class="mode-text">LIVE REAL TRADING</span>';
  }

  // 2. Total Net P&L & Realized / Unrealized Breakdown
  const totalPnl = parseFloat(pos.total_pnl || 0.0);
  const realizedPnl = parseFloat(pos.total_realized_pnl || 0.0);
  const unrealizedPnl = parseFloat(pos.total_unrealized_pnl || 0.0);
  const deployedMargin = parseFloat(pos.deployed_margin || 0.0);

  elements.totalPnl.textContent = formatINR(totalPnl, true);
  elements.totalPnl.className = 'kpi-hero-val ' + (totalPnl > 0 ? 'positive' : (totalPnl < 0 ? 'negative' : ''));
  
  elements.realizedPnl.textContent = formatINR(realizedPnl, true);
  elements.realizedPnl.style.color = realizedPnl > 0 ? '#10b981' : (realizedPnl < 0 ? '#f43f5e' : '#f1f5f9');
  
  elements.unrealizedPnl.textContent = formatINR(unrealizedPnl, true);
  elements.unrealizedPnl.style.color = unrealizedPnl > 0 ? '#10b981' : (unrealizedPnl < 0 ? '#f43f5e' : '#f1f5f9');

  const roiPct = deployedMargin > 0 ? (totalPnl / deployedMargin * 100.0) : 0.0;
  elements.roiBadge.textContent = `${roiPct >= 0 ? '+' : ''}${roiPct.toFixed(2)}% ROI`;
  elements.roiBadge.className = 'roi-badge ' + (roiPct < 0 ? 'negative' : '');

  // 3. Nifty Spot Price
  const currentSpot = parseFloat(pos.spot_price || 0.0);
  if (currentSpot > 0) {
    elements.spotPrice.textContent = currentSpot.toLocaleString('en-IN', { minimumFractionDigits: 2 });
    if (prevSpotPrice && prevSpotPrice !== currentSpot) {
      elements.spotPrice.style.color = currentSpot > prevSpotPrice ? '#10b981' : '#f43f5e';
      setTimeout(() => { elements.spotPrice.style.color = '#38bdf8'; }, 300);
    }
    prevSpotPrice = currentSpot;
  } else {
    elements.spotPrice.textContent = '--';
  }
  elements.expiryBadge.textContent = `EXPIRY: ${pos.expiry_date || 'WEEKLY'}`;

  // 4. Deployed Margin (RMS) & 2% Dynamic Stop Loss
  elements.deployedMargin.textContent = deployedMargin > 0 ? formatINR(deployedMargin) : '₹0.00 (Standby)';
  const slLimit2Pct = deployedMargin > 0 ? deployedMargin * 0.02 : 0.0;
  elements.straddleSlLimit.textContent = deployedMargin > 0 ? `-₹${slLimit2Pct.toLocaleString('en-IN', { minimumFractionDigits: 2 })}` : '₹0.00 (Standby)';
  elements.marginSource.textContent = pos.margin_source || 'RMS Live';

  // 5. Portfolio Greeks
  const netDelta = parseFloat(pos.net_delta || 0.0);
  const netTheta = parseFloat(pos.net_theta || 0.0);
  const netGamma = parseFloat(pos.net_gamma || 0.0);
  const netVega = parseFloat(pos.net_vega || 0.0);

  elements.netDelta.textContent = (netDelta >= 0 ? '+' : '') + netDelta.toFixed(4);
  elements.netTheta.textContent = (netTheta >= 0 ? '+₹' : '-₹') + Math.abs(netTheta).toFixed(2);
  elements.netGamma.textContent = netGamma.toFixed(5);
  elements.netVega.textContent = (netVega >= 0 ? '+' : '') + netVega.toFixed(2);

  if (Math.abs(netDelta) <= 0.08) {
    elements.equilibriumBadge.textContent = 'EQUILIBRIUM: OK (Δ ≈ 0)';
    elements.equilibriumBadge.className = 'equilibrium-badge';
  } else {
    elements.equilibriumBadge.textContent = `DELTA SKEWED (Δ ${netDelta > 0 ? 'BULL' : 'BEAR'})`;
    elements.equilibriumBadge.className = 'equilibrium-badge' + (Math.abs(netDelta) > 0.15 ? ' straddle-active' : '');
  }

  // 6. Straddle Phase vs Iron Condor Phase
  const isStraddle = Boolean(pos.is_straddle);
  if (isStraddle) {
    elements.straddlePhaseBadge.textContent = 'PHASE: STRADDLE (RISK LOCK)';
    elements.straddlePhaseBadge.className = 'straddle-state-badge straddle-active';
    elements.straddleRiskTag.textContent = 'ACTIVE (2% CAPITAL SL ON)';
    elements.straddleRiskTag.className = 'gauge-status-tag breached';

    // Straddle Risk Gauge Bar Fill
    const currentDrawdown = Math.max(0, -totalPnl);
    const drawdownPct = slLimit2Pct > 0 ? (currentDrawdown / slLimit2Pct * 100.0) : 0.0;
    elements.straddleSlProgressBar.style.width = `${Math.min(drawdownPct, 100)}%`;
    elements.straddleCurrentLoss.textContent = `${formatINR(-currentDrawdown, true)} (${(drawdownPct).toFixed(1)}% of SL)`;
    elements.straddleMaxLoss.textContent = `-₹${slLimit2Pct.toLocaleString('en-IN', { minimumFractionDigits: 2 })} (-2.0%)`;
  } else {
    elements.straddlePhaseBadge.textContent = 'PHASE: IRON CONDOR';
    elements.straddlePhaseBadge.className = 'straddle-state-badge';
    elements.straddleRiskTag.textContent = 'STANDBY (NON-STRADDLE)';
    elements.straddleRiskTag.className = 'gauge-status-tag';
    elements.straddleSlProgressBar.style.width = '0%';
    elements.straddleCurrentLoss.textContent = '₹0.00 (0.0%)';
    elements.straddleMaxLoss.textContent = `-₹${slLimit2Pct.toLocaleString('en-IN', { minimumFractionDigits: 2 })} (-2.0%)`;
  }

  // 7. 50% Short Leg Expansion Meter
  const legs = pos.legs || [];
  const openLegs = legs.filter(l => l.status === 'OPEN');
  elements.activeLegsCount.textContent = `${openLegs.length} ACTIVE LEGS`;

  let maxSurgePct = 0.0;
  openLegs.filter(l => !l.is_hedge).forEach(shortLeg => {
    const entry = parseFloat(shortLeg.entry_premium || 0.0);
    const curr = parseFloat(shortLeg.current_premium || entry);
    if (entry > 0) {
      const surge = ((curr - entry) / entry) * 100.0;
      if (surge > maxSurgePct) {
        maxSurgePct = surge;
      }
    }
  });

  elements.maxSurgeVal.textContent = `${maxSurgePct.toFixed(1)}%`;
  const gaugeFillPct = Math.min((maxSurgePct / 50.0) * 100.0, 100);
  elements.surgeProgressBar.style.width = `${gaugeFillPct}%`;

  if (maxSurgePct >= 50.0) {
    elements.surgeStatusTag.textContent = '🔥 50% SURGE TRIGGERED';
    elements.surgeStatusTag.className = 'gauge-status-tag breached';
  } else if (maxSurgePct >= 30.0) {
    elements.surgeStatusTag.textContent = '⚠️ WARNING (PRE-SURGE)';
    elements.surgeStatusTag.className = 'gauge-status-tag warning';
  } else {
    elements.surgeStatusTag.textContent = 'NORMAL (SAFE)';
    elements.surgeStatusTag.className = 'gauge-status-tag';
  }

  // 8. Render Active 4-Leg Position Matrix Table
  renderMatrixTable(openLegs);

  // 9. Render Adjustment History & Execution Logs
  renderAdjustmentsAndLogs(legs, trades);
}

function renderMatrixTable(openLegs) {
  if (!openLegs || openLegs.length === 0) {
    elements.matrixTableBody.innerHTML = `
      <tr>
        <td colspan="10" class="empty-state">
          <span>No Positions Open</span>
        </td>
      </tr>
    `;
    return;
  }

  let html = '';
  openLegs.forEach(leg => {
    const isHedge = Boolean(leg.is_hedge);
    const optType = (leg.option_type || 'CE').toUpperCase();
    const strike = leg.strike || '--';
    const entryPrice = parseFloat(leg.entry_premium || 0.0);
    const ltp = parseFloat(leg.current_premium || entryPrice);
    const qty = parseInt(leg.quantity || 65);
    const delta = parseFloat(leg.current_delta || 0.0);
    const iv = parseFloat(leg.current_iv || 0.0);

    // Calculate Leg P&L
    let legPnl = 0.0;
    if (isHedge) {
      legPnl = (ltp - entryPrice) * qty; // Long Hedge
    } else {
      legPnl = (entryPrice - ltp) * qty; // Short Leg
    }
    const legPnlPct = entryPrice > 0 ? (legPnl / (entryPrice * qty) * 100.0) : 0.0;

    // Short Surge Progress
    let surgeText = '-';
    let surgeClass = 'surge-pill safe';
    if (!isHedge) {
      const surge = entryPrice > 0 ? ((ltp - entryPrice) / entryPrice * 100.0) : 0.0;
      if (surge > 0) {
        surgeText = `+${surge.toFixed(1)}% / 50%`;
        if (surge >= 50.0) surgeClass = 'surge-pill critical';
        else if (surge >= 30.0) surgeClass = 'surge-pill warning';
      } else {
        surgeText = `${surge.toFixed(1)}% (Decaying)`;
      }
    } else {
      surgeText = 'Hedge Protection';
      surgeClass = 'surge-pill safe';
    }

    html += `
      <tr>
        <td>
          <span class="leg-type-badge ${isHedge ? 'hedge' : 'short'}">
            ${isHedge ? '🛡️ LONG HEDGE' : '⚡ SHORT SELL'}
          </span>
        </td>
        <td>
          <span class="opt-type-tag ${optType.toLowerCase()}">${optType}</span>
        </td>
        <td class="strike-num">${strike}</td>
        <td>₹${entryPrice.toFixed(2)}</td>
        <td style="color: #38bdf8; font-weight: 700;">₹${ltp.toFixed(2)}</td>
        <td style="color: var(--cyan-bright); font-weight: 700;">${delta.toFixed(3)}</td>
        <td>${iv > 0 ? iv.toFixed(1) + '%' : '--'}</td>
        <td class="pnl-cell ${legPnl >= 0 ? 'positive' : 'negative'}">
          ${formatINR(legPnl, true)} (${legPnlPct >= 0 ? '+' : ''}${legPnlPct.toFixed(1)}%)
        </td>
        <td><span class="${surgeClass}">${surgeText}</span></td>
        <td><span class="status-tag-active">ACTIVE</span></td>
      </tr>
    `;
  });

  elements.matrixTableBody.innerHTML = html;
}

function renderAdjustmentsAndLogs(allLegs, trades) {
  // Closed Legs & Rolls
  const closedLegs = (allLegs || []).filter(l => l.status === 'CLOSED');
  elements.adjCountBadge.textContent = `${closedLegs.length} ROLLS`;

  if (closedLegs.length === 0) {
    elements.adjustmentTimeline.innerHTML = `<div class="empty-log-state">Zero adjustments executed. Portfolio in equilibrium.</div>`;
  } else {
    let timelineHtml = '';
    closedLegs.forEach(c => {
      const isHedge = Boolean(c.is_hedge);
      const entry = parseFloat(c.entry_premium || 0.0);
      const exit = parseFloat(c.exit_premium || entry);
      const qty = parseInt(c.quantity || 65);
      const pnl = isHedge ? (exit - entry) * qty : (entry - exit) * qty;

      timelineHtml += `
        <div class="timeline-event-card">
          <div class="timeline-event-top">
            <span class="timeline-desc"><strong>${isHedge ? '🛡️ Closed Hedge' : '⚡ Closed Short'}</strong>: ${c.strike} ${c.option_type}</span>
            <span class="timeline-pnl" style="color: ${pnl >= 0 ? '#10b981' : '#f43f5e'};">${formatINR(pnl, true)}</span>
          </div>
          <div class="timeline-event-top">
            <span class="timeline-time">${c.exit_reason || 'Rebalance Roll Exit'}</span>
            <span class="timeline-time">Entry: ₹${entry.toFixed(2)} • Exit: ₹${exit.toFixed(2)}</span>
          </div>
        </div>
      `;
    });
    elements.adjustmentTimeline.innerHTML = timelineHtml;
  }

  // Audit Logs
  elements.tradesCountBadge.textContent = `${trades.length} EVENTS`;
  if (trades.length === 0) {
    elements.auditLogsBox.innerHTML = `<div class="empty-log-state">Awaiting execution audit stream...</div>`;
  } else {
    let logHtml = '';
    trades.slice(0, 50).forEach(t => {
      let tagClass = '';
      const act = (t.Action || t.action || '').toUpperCase();
      if (act.includes('RMS') || act.includes('MARGIN')) tagClass = 'tag-rms';
      else if (act.includes('ADJUST') || act.includes('REBALANCE')) tagClass = 'tag-adj';
      else if (act.includes('PROFIT') || act.includes('WIN')) tagClass = 'tag-profit';
      else if (act.includes('SL') || act.includes('STOP') || act.includes('LOSS')) tagClass = 'tag-sl';

      const time = t.Timestamp || t.timestamp || t.Time || '09:18';
      const symbol = t.Symbol || t.strike || t.Leg || '';
      const price = t.Price || t.price || '';
      const desc = t.Details || t.Reason || `${act} ${symbol} @ ₹${price}`;

      logHtml += `
        <div class="terminal-log-line ${tagClass}">
          <span class="log-time">[${time}]</span>
          <strong>${act}</strong> ${desc}
        </div>
      `;
    });
    elements.auditLogsBox.innerHTML = logHtml;
  }
}
