import type { Position } from '../api'

function formatCurrency(value: number): string {
  return value.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 })
}

export function PositionsTable({ positions }: { positions: Position[] }) {
  if (positions.length === 0) {
    return <p className="empty-note">No open positions yet.</p>
  }

  return (
    <table>
      <thead>
        <tr>
          <th>Symbol</th>
          <th>Qty</th>
          <th>Avg entry</th>
          <th>Market value</th>
          <th>Unrealized P&amp;L</th>
        </tr>
      </thead>
      <tbody>
        {positions.map((p) => (
          <tr key={p.symbol}>
            <td className="emphasis">{p.symbol}</td>
            <td>{p.qty}</td>
            <td>{formatCurrency(p.avg_entry_price)}</td>
            <td>{formatCurrency(p.market_value)}</td>
            <td className={p.unrealized_pl >= 0 ? 'positive' : 'negative'}>
              {p.unrealized_pl >= 0 ? '+' : ''}
              {formatCurrency(p.unrealized_pl)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
