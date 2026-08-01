import type { Trade } from '../api'

function formatCurrency(value: number | null): string {
  if (value === null) return '—'
  return value.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 })
}

export function TradesTable({ trades }: { trades: Trade[] }) {
  if (trades.length === 0) {
    return <p className="empty-note">No trades yet.</p>
  }

  return (
    <table>
      <thead>
        <tr>
          <th>Time</th>
          <th>Symbol</th>
          <th>Side</th>
          <th>Qty</th>
          <th>Fill price</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {trades.map((t) => (
          <tr key={t.id}>
            <td>{new Date(t.submitted_at).toLocaleString()}</td>
            <td className="emphasis">{t.symbol}</td>
            <td>{t.side.toUpperCase()}</td>
            <td>{t.qty}</td>
            <td>{formatCurrency(t.fill_price)}</td>
            <td>{t.status}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
