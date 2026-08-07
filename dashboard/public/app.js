const socket = io();

// Clock
function updateClock() {
  const now = new Date();
  const options = { timeZone: 'Asia/Kolkata', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' };
  document.getElementById('liveClock').innerText = now.toLocaleTimeString('en-US', options) + ' IST';
}
setInterval(updateClock, 1000);
updateClock();

socket.on('connect', () => {
  const sb = document.getElementById('statusBadge');
  sb.innerText = 'LIVE CONNECTED';
  sb.style.background = 'rgba(16, 185, 129, 0.15)';
  sb.style.color = '#10b981';
});

socket.on('disconnect', () => {
  const sb = document.getElementById('statusBadge');
  sb.innerText = 'DISCONNECTED';
  sb.style.background = 'rgba(244, 63, 94, 0.15)';
  sb.style.color = '#f43f5e';
});

socket.on('update', (data) => {
  if (!data || !data.position) return;
  const pos = data.position;
  const trades = data.trades || [];

  // Expiry & Spot
  document.getElementById('expiryDate').innerText = `Expiry: ${pos.expiry_date || '--'}`;
  if (pos.spot_price) {
    document.getElementById('spotPrice').innerText = Number(pos.spot_price).toFixed(2);
  }

  // Calculate total P&L
  let realizedPnl = 0;
  let openPnl = 0;
  const legs = pos.legs || [];

  legs.forEach(leg => {
    if (leg.status === 'CLOSED') {
      realizedPnl += (leg.pnl || 0);
    } else if (leg.status === 'OPEN') {
      const entryP = leg.entry_premium || 0;
      const currP = leg.current_premium || entryP;
      const lotSize = 65;
      if (leg.is_hedge) {
        openPnl += (currP - entryP) * lotSize;
      } else {
        openPnl += (entryP - currP) * lotSize;
      }
    }
  });

  const totalPnl = realizedPnl + openPnl;

  // Format P&L
  const pnlElem = document.getElementById('totalPnl');
  pnlElem.innerText = (totalPnl >= 0 ? '+' : '') + `₹${totalPnl.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  pnlElem.className = 'card-value pnl-value ' + (totalPnl >= 0 ? 'positive' : 'negative');

  document.getElementById('pnlBreakdown').innerText = 
    `Realized: ₹${realizedPnl.toFixed(2)} | Unrealized: ₹${openPnl.toFixed(2)}`;

  // Net Delta & Greeks (Use Python calculated portfolio net_delta if available)
  let netDelta = pos.net_delta !== undefined ? pos.net_delta : 0.0;
  let openLegs = legs.filter(l => l.status === 'OPEN');
  let closedLegs = legs.filter(l => l.status === 'CLOSED');

  document.getElementById('netDelta').innerText = (netDelta >= 0 ? '+' : '') + netDelta.toFixed(4);
  document.getElementById('adjCount').innerText = `Adjustments: ${pos.adjustment_count || 0}`;

  // Delta Gauge Fill (0.00 to 0.10 trigger line) - Dynamically resets to 0% after rebalance!
  const absDelta = Math.abs(netDelta);
  document.getElementById('absDeltaVal').innerText = absDelta.toFixed(4);
  const fillPct = Math.min(100, (absDelta / 0.10) * 100);
  document.getElementById('deltaGaugeFill').style.width = `${fillPct}%`;

  const trigStatus = document.getElementById('deltaTriggerStatus');
  if (absDelta >= 0.10) {
    trigStatus.innerText = '🚨 TRIGGER BREACHED (ADJUSTING)';
    trigStatus.className = 'trigger-label breached';
  } else {
    trigStatus.innerText = 'NEUTRAL (OK)';
    trigStatus.className = 'trigger-label';
  }

  // Straddle Status
  const straddleElem = document.getElementById('straddleStatus');
  if (pos.is_straddle_reached) {
    straddleElem.innerText = 'STRADDLE: REACHED ⛔';
    straddleElem.style.color = '#f43f5e';
  } else {
    straddleElem.innerText = 'STRADDLE: NO';
    straddleElem.style.color = '#9ca3af';
  }

  // Active Position Table
  const tbody = document.getElementById('legsTableBody');
  if (openLegs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="text-center muted">No active legs in position</td></tr>';
  } else {
    tbody.innerHTML = openLegs.map(l => {
      const typeClass = l.is_hedge ? 'type-long' : 'type-short';
      const optClass = l.option_type === 'CE' ? 'opt-ce' : 'opt-pe';
      const entryP = l.entry_premium || 0;
      const currP = l.current_premium || entryP;
      const lotSize = 65;
      const legPnl = l.is_hedge ? (currP - entryP) * lotSize : (entryP - currP) * lotSize;
      const pnlClass = legPnl >= 0 ? 'positive' : 'negative';

      return `
        <tr>
          <td class="${typeClass}">${l.is_hedge ? 'BUY (HEDGE)' : 'SELL (SHORT)'}</td>
          <td class="${optClass}">${l.option_type}</td>
          <td>${l.strike}</td>
          <td>₹${entryP.toFixed(2)}</td>
          <td>₹${currP.toFixed(2)}</td>
          <td>${l.current_delta ? l.current_delta.toFixed(4) : '--'}</td>
          <td class="${pnlClass}">${legPnl >= 0 ? '+' : ''}₹${legPnl.toFixed(2)}</td>
          <td><span class="badge status-badge">OPEN</span></td>
        </tr>
      `;
    }).join('');
  }

  // Closed / Exited Legs Table
  const closedTbody = document.getElementById('closedLegsTableBody');
  if (closedTbody) {
    if (closedLegs.length === 0) {
      closedTbody.innerHTML = '<tr><td colspan="8" class="text-center muted">No exited legs yet</td></tr>';
    } else {
      closedTbody.innerHTML = closedLegs.map(l => {
        const typeClass = l.is_hedge ? 'type-long' : 'type-short';
        const optClass = l.option_type === 'CE' ? 'opt-ce' : 'opt-pe';
        const entryP = l.entry_premium || 0;
        const exitP = l.exit_premium || 0;
        const pnl = l.pnl || 0;
        const pnlClass = pnl >= 0 ? 'positive' : 'negative';

        return `
          <tr>
            <td class="${typeClass}">${l.is_hedge ? 'BUY (HEDGE)' : 'SELL (SHORT)'}</td>
            <td class="${optClass}">${l.option_type}</td>
            <td>${l.strike}</td>
            <td>₹${entryP.toFixed(2)}</td>
            <td>₹${exitP.toFixed(2)}</td>
            <td class="${pnlClass}">${pnl >= 0 ? '+' : ''}₹${pnl.toFixed(2)}</td>
            <td>${l.exit_time || '--'}</td>
            <td><span class="badge status-badge">EXITED</span></td>
          </tr>
        `;
      }).join('');
    }
  }

  // Adjustments Timeline
  const adjs = pos.adjustments || [];
  const adjContainer = document.getElementById('adjTimeline');
  if (adjs.length === 0) {
    adjContainer.innerHTML = '<div class="muted text-center p-3">No adjustments executed yet</div>';
  } else {
    adjContainer.innerHTML = adjs.slice().reverse().map(a => `
      <div class="timeline-item">
        <div class="timeline-time">${a.time}</div>
        <div><strong>Reason:</strong> ${a.reason}</div>
        <div><strong>Action:</strong> Closed PnL: ₹${(a.pnl_booked || 0).toFixed(2)} | New Short Strike: ${a.new_short_strike} (₹${a.new_short_premium})</div>
      </div>
    `).join('');
  }

  // Trade Logs
  const logContainer = document.getElementById('tradeLogs');
  if (trades.length === 0) {
    logContainer.innerHTML = '<div class="muted text-center p-3">No trade logs found</div>';
  } else {
    logContainer.innerHTML = trades.map(t => `
      <div class="timeline-item">
        <div class="timeline-time">${t.exit_time || t.entry_time}</div>
        <div><strong>${t.leg_type} ${t.option_type} @ ${t.strike}</strong> | Exit Prem: ₹${t.exit_premium} | PnL: ₹${t.pnl_inr}</div>
      </div>
    `).join('');
  }
});
