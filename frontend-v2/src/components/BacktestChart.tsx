import { useEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import { api, type BacktestPoint } from '../api'

const WIDTH = 860
const HEIGHT = 260
const MARGIN = { top: 16, right: 16, bottom: 28, left: 64 }

// Matches app/main.py's BACKTEST_REPORT_DEFAULT_DAYS - kept in sync manually since the
// backend only applies this default when start_date/end_date are omitted, but the date
// inputs below need concrete values to display on first render.
const DEFAULT_RANGE_DAYS = 15

function toISODate(d: Date): string {
  return d.toISOString().slice(0, 10)
}

// end = yesterday (UTC), same reasoning as compute_market_state_backtest's own
// default: "today" isn't a fair comparison point, since a trading day's actual close
// isn't known until it's over.
function defaultDateRange(): { start: string; end: string } {
  const end = new Date()
  end.setUTCDate(end.getUTCDate() - 1)
  const start = new Date(end)
  start.setUTCDate(start.getUTCDate() - DEFAULT_RANGE_DAYS)
  return { start: toISODate(start), end: toISODate(end) }
}

function niceStep(range: number, ticks: number): number {
  const rough = range / ticks || 1
  const magnitude = 10 ** Math.floor(Math.log10(rough))
  const residual = rough / magnitude
  if (residual > 5) return 10 * magnitude
  if (residual > 2) return 5 * magnitude
  if (residual > 1) return 2 * magnitude
  return magnitude
}

function formatCurrency(value: number): string {
  return value.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 2 })
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })
}

// Plotted at the top of TickerDetailsModal - shows a single ticker's backtest
// (predicted_exit_price) vs realized (actual_exit_price) closing price per
// evaluated_date, for a caller-adjustable date range (default: trailing 15 days).
export function BacktestChart({ ticker }: { ticker: string }) {
  const defaults = useMemo(defaultDateRange, [])
  const [startDate, setStartDate] = useState(defaults.start)
  const [endDate, setEndDate] = useState(defaults.end)
  const [pendingStart, setPendingStart] = useState(defaults.start)
  const [pendingEnd, setPendingEnd] = useState(defaults.end)
  const [points, setPoints] = useState<BacktestPoint[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const [showTable, setShowTable] = useState(false)
  const svgRef = useRef<SVGSVGElement>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .backtestReport(ticker, startDate, endDate)
      .then((data) => {
        if (!cancelled) setPoints(data)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [ticker, startDate, endDate])

  const rangeInvalid = pendingStart > pendingEnd
  const applyRange = () => {
    if (rangeInvalid) return
    setStartDate(pendingStart)
    setEndDate(pendingEnd)
  }

  const innerWidth = WIDTH - MARGIN.left - MARGIN.right
  const innerHeight = HEIGHT - MARGIN.top - MARGIN.bottom

  const plot = useMemo(() => {
    if (!points || points.length === 0) return null

    const times = points.map((p) => new Date(p.evaluated_date).getTime())
    const values = points.flatMap((p) => [p.predicted_exit_price, p.actual_exit_price])
    const minTime = Math.min(...times)
    const maxTime = Math.max(...times)
    const rawMin = Math.min(...values)
    const rawMax = Math.max(...values)
    const pad = (rawMax - rawMin) * 0.15 || rawMax * 0.05 || 1
    const min = rawMin - pad
    const max = rawMax + pad

    const x = (t: number) => (maxTime === minTime ? innerWidth / 2 : ((t - minTime) / (maxTime - minTime)) * innerWidth)
    const y = (v: number) => innerHeight - ((v - min) / (max - min)) * innerHeight

    const rows = points.map((p, i) => ({
      ...p,
      px: x(times[i]),
      predictedPy: y(p.predicted_exit_price),
      actualPy: y(p.actual_exit_price),
    }))
    const predictedPath = rows.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.px.toFixed(1)} ${p.predictedPy.toFixed(1)}`).join(' ')
    const actualPath = rows.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.px.toFixed(1)} ${p.actualPy.toFixed(1)}`).join(' ')

    const step = niceStep(max - min, 4)
    const firstTick = Math.ceil(min / step) * step
    const ticks: number[] = []
    for (let t = firstTick; t <= max; t += step) ticks.push(t)

    return { rows, predictedPath, actualPath, ticks, min, max }
  }, [points, innerWidth, innerHeight])

  function handlePointerMove(e: PointerEvent<SVGRectElement>) {
    if (!plot) return
    const svg = svgRef.current
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    const relX = ((e.clientX - rect.left) / rect.width) * WIDTH - MARGIN.left
    let nearest = 0
    let nearestDist = Infinity
    plot.rows.forEach((p, i) => {
      const dist = Math.abs(p.px - relX)
      if (dist < nearestDist) {
        nearestDist = dist
        nearest = i
      }
    })
    setHoverIndex(nearest)
  }

  const yFor = (v: number) => (plot ? innerHeight - ((v - plot.min) / (plot.max - plot.min)) * innerHeight : 0)
  const hovered = plot && hoverIndex !== null ? plot.rows[hoverIndex] : null
  const last = plot ? plot.rows[plot.rows.length - 1] : null

  return (
    <div className="backtest-chart">
      <div className="backtest-chart-toolbar">
        <label className="job-field">
          Start date (UTC)
          <input type="date" value={pendingStart} max={pendingEnd} onChange={(e) => setPendingStart(e.target.value)} />
        </label>
        <label className="job-field">
          End date (UTC)
          <input type="date" value={pendingEnd} min={pendingStart} onChange={(e) => setPendingEnd(e.target.value)} />
        </label>
        <button type="button" className="job-button" disabled={loading || rangeInvalid} onClick={applyRange}>
          {loading ? 'Loading...' : 'Apply range'}
        </button>
        <div className="backtest-chart-legend">
          <span className="backtest-chart-legend-item">
            <svg width="18" height="10" aria-hidden="true">
              <line x1="0" y1="5" x2="18" y2="5" className="backtest-actual-line" />
            </svg>
            Actual
          </span>
          <span className="backtest-chart-legend-item">
            <svg width="18" height="10" aria-hidden="true">
              <line x1="0" y1="5" x2="18" y2="5" className="backtest-predicted-line" />
            </svg>
            Predicted (backtest)
          </span>
        </div>
      </div>

      {rangeInvalid && <p className="job-field-error">Start date must not be after end date.</p>}
      {error && <p className="jobs-error">{error}</p>}

      {!error && loading && !plot && <p className="placeholder-note">Loading backtest data...</p>}

      {!error && !loading && !plot && (
        <p className="placeholder-note">No backtest data for {ticker} in this date range.</p>
      )}

      {plot && last && (
        <>
          <svg
            ref={svgRef}
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            className="backtest-chart-svg"
            role="img"
            aria-label={`${ticker} backtest predicted vs actual exit price`}
          >
            <g transform={`translate(${MARGIN.left}, ${MARGIN.top})`}>
              {plot.ticks.map((t) => (
                <g key={t}>
                  <line x1={0} x2={innerWidth} y1={yFor(t)} y2={yFor(t)} className="gridline" />
                  <text x={-8} y={yFor(t)} textAnchor="end" dominantBaseline="middle" className="axis-label">
                    {formatCurrency(t)}
                  </text>
                </g>
              ))}

              {hovered && <line x1={hovered.px} x2={hovered.px} y1={0} y2={innerHeight} className="crosshair" />}

              <path d={plot.actualPath} className="backtest-actual-line" fill="none" />
              <path d={plot.predictedPath} className="backtest-predicted-line" fill="none" />

              <circle cx={last.px} cy={last.actualPy} r={4} className="backtest-actual-dot" />
              <circle cx={last.px} cy={last.predictedPy} r={4} className="backtest-predicted-dot" />
              {hovered && hovered !== last && (
                <>
                  <circle cx={hovered.px} cy={hovered.actualPy} r={4} className="backtest-actual-dot" />
                  <circle cx={hovered.px} cy={hovered.predictedPy} r={4} className="backtest-predicted-dot" />
                </>
              )}

              <text x={0} y={innerHeight + 20} textAnchor="start" className="axis-label">
                {formatDate(plot.rows[0].evaluated_date)}
              </text>
              <text x={innerWidth} y={innerHeight + 20} textAnchor="end" className="axis-label">
                {formatDate(last.evaluated_date)}
              </text>

              <rect
                x={0}
                y={0}
                width={innerWidth}
                height={innerHeight}
                fill="transparent"
                onPointerMove={handlePointerMove}
                onPointerLeave={() => setHoverIndex(null)}
              />
            </g>
          </svg>

          {hovered && (
            <div className="backtest-chart-tooltip" style={{ left: `${((MARGIN.left + hovered.px) / WIDTH) * 100}%` }}>
              <strong>{formatDate(hovered.evaluated_date)}</strong>
              <span>Actual: {formatCurrency(hovered.actual_exit_price)}</span>
              <span>Predicted: {formatCurrency(hovered.predicted_exit_price)}</span>
              <span>{hovered.predicted_correct ? 'Prediction correct' : 'Prediction incorrect'}</span>
            </div>
          )}

          <button type="button" className="link-button" onClick={() => setShowTable((v) => !v)}>
            {showTable ? 'Hide data table' : 'Show data table'}
          </button>

          {showTable && (
            <table className="backtest-chart-table">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Actual</th>
                  <th>Predicted</th>
                  <th>Error %</th>
                  <th>Correct</th>
                </tr>
              </thead>
              <tbody>
                {[...plot.rows].reverse().map((p) => (
                  <tr key={p.evaluated_date}>
                    <td>{formatDate(p.evaluated_date)}</td>
                    <td className="emphasis">{formatCurrency(p.actual_exit_price)}</td>
                    <td>{formatCurrency(p.predicted_exit_price)}</td>
                    <td>{(p.price_error_pct * 100).toFixed(2)}%</td>
                    <td>{p.predicted_correct ? 'Yes' : 'No'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  )
}
