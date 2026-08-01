import type { EquityPoint, Position } from '../api'

function formatCurrency(value: number): string {
  return value.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
}

function formatPercent(value: number): string {
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(2)}%`
}

export function StatTiles({ equity, positions }: { equity: EquityPoint[]; positions: Position[] }) {
  const latest = equity[equity.length - 1]
  const first = equity[0]
  const returnPct = latest && first && first.equity !== 0 ? ((latest.equity - first.equity) / first.equity) * 100 : null
  const totalMarketValue = positions.reduce((sum, p) => sum + p.market_value, 0)

  return (
    <div className="stat-tiles">
      <div className="stat-tile hero">
        <span className="stat-label">Equity</span>
        <span className="stat-value">{latest ? formatCurrency(latest.equity) : '—'}</span>
        {returnPct !== null && (
          <span className={`stat-delta ${returnPct >= 0 ? 'positive' : 'negative'}`}>
            {formatPercent(returnPct)} since first snapshot
          </span>
        )}
      </div>
      <div className="stat-tile">
        <span className="stat-label">Cash</span>
        <span className="stat-value">{latest ? formatCurrency(latest.cash) : '—'}</span>
      </div>
      <div className="stat-tile">
        <span className="stat-label">Positions value</span>
        <span className="stat-value">{formatCurrency(totalMarketValue)}</span>
      </div>
      <div className="stat-tile">
        <span className="stat-label">Open positions</span>
        <span className="stat-value">{positions.length}</span>
      </div>
    </div>
  )
}
