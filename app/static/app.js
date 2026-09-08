async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error('Request failed');
  }
  return response.json();
}

function renderSummary(summary) {
  const cards = document.getElementById('summaryCards');
  const items = [
    { label: 'Open positions', value: summary.open_positions ?? 0 },
    { label: 'Signals logged', value: summary.signals_logged ?? 0 },
    { label: 'Rejected', value: summary.signals_rejected ?? 0 },
    { label: 'Net P/L', value: `${Number(summary.net_profit_after_fees ?? 0).toFixed(2)}` },
  ];

  cards.innerHTML = items.map((item) => `
    <div class="card">
      <h3>${item.label}</h3>
      <p class="value">${item.value}</p>
    </div>
  `).join('');

  const badge = document.getElementById('modeBadge');
  badge.textContent = summary.execution_mode || 'paper';
}

function renderSignals(signals) {
  const container = document.getElementById('signalsTable');
  if (!signals.length) {
    container.innerHTML = '<p>No signals yet.</p>';
    return;
  }

  const rows = signals.slice(0, 8).map((signal) => `
    <tr>
      <td>${signal.strategy_name}</td>
      <td>${signal.symbol}</td>
      <td>${signal.action}</td>
      <td>${signal.direction || '-'}</td>
      <td>${signal.status}</td>
    </tr>
  `).join('');

  container.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Strategy</th>
          <th>Symbol</th>
          <th>Action</th>
          <th>Direction</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

async function loadDashboard() {
  try {
    const [summary, signals] = await Promise.all([
      fetchJson('/api/summary'),
      fetchJson('/api/signals?limit=10'),
    ]);
    renderSummary(summary);
    renderSignals(signals);
  } catch (error) {
    document.getElementById('summaryCards').innerHTML = '<div class="card"><h3>Dashboard</h3><p class="value">offline</p></div>';
  }
}

loadDashboard();
