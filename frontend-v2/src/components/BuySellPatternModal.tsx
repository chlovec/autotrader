import { useEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import { api, type BuySellPatternReport, type BuySellPatternRun } from '../api'

const WIDTH = 860
const HEIGHT = 300
const MARGIN = { top: 16, right: 16, bottom: 28, left: 64 }
const MARKER = 6

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
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
}

// Triangle pointing up (buy) or down (sell), centered on (x, y) - shape carries the
// buy/sell identity alongside color, so the two never rely on color alone.
function trianglePath(x: number, y: number, up: boolean): string {
  const h = MARKER * 1.6
  return up
    ? `M ${x} ${y - h / 2} L ${x + MARKER} ${y + h / 2} L ${x - MARKER} ${y + h / 2} Z`
    : `M ${x} ${y + h / 2} L ${x + MARKER} ${y - h / 2} L ${x - MARKER} ${y - h / 2} Z`
}

type BuySellPatternModalProps = {
  ticker: string
  title: string
  onClose: () => void
}

// Popped up by a Trading Symbols row's right-click "Buy Sell Pattern" menu - plots the
// ticker's daily closes with the trades the buy-sell-pattern job stored for it
// (jobs/buy_sell_pattern.py). The run picker lists every stored run name for this
// ticker, and its first/last trade dates bound the date inputs so the user can only
// pick a range that has stored results.
export function BuySellPatternModal({ ticker, title, onClose }: BuySellPatternModalProps) {
  const [runs, setRuns] = useState<BuySellPatternRun[] | null>(null)
  const [runName, setRunName] = useState('')
  const [pendingStart, setPendingStart] = useState('')
  const [pendingEnd, setPendingEnd] = useState('')
  const [applied, setApplied] = useState<{ name: string; start: string; end: string } | null>(null)
  const [report, setReport] = useState<BuySellPatternReport | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const [showTable, setShowTable] = useState(false)
  const svgRef = useRef<SVGSVGElement>(null)

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  const selectRun = (run: BuySellPatternRun) => {
    setRunName(run.name)
    setPendingStart(run.first_trade_date)
    setPendingEnd(run.last_trade_date)
    setApplied({ name: run.name, start: run.first_trade_date, end: run.last_trade_date })
  }

  // Defaults to the newest run (the backend orders by last_trade_date desc) over its
  // full stored span.
  useEffect(() => {
    let cancelled = false
    api
      .buySellPatternRuns(ticker)
      .then((data) => {
        if (cancelled) return
        setRuns(data)
        if (data.length > 0) selectRun(data[0])
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [ticker])

  useEffect(() => {
    if (!applied) return
    let cancelled = false
    setLoading(true)
    setError(null)
    api
      .buySellPatternReport(ticker, applied.name, applied.start, applied.end)
      .then((data) => {
        if (!cancelled) setReport(data)
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
  }, [ticker, applied])

  const run = runs?.find((r) => r.name === runName) ?? null
  const rangeError = !run
    ? null
    : !pendingStart || !pendingEnd
      ? 'Pick both a start and an end date.'
      : pendingStart > pendingEnd
        ? 'Start date must not be after end date.'
        : pendingStart < run.first_trade_date || pendingEnd > run.last_trade_date
          ? `Pick dates between ${run.first_trade_date} and ${run.last_trade_date}.`
          : null
  const applyRange = () => {
    if (rangeError || !run) return
    setApplied({ name: run.name, start: pendingStart, end: pendingEnd })
  }

  const innerWidth = WIDTH - MARGIN.left - MARGIN.right
  const innerHeight = HEIGHT - MARGIN.top - MARGIN.bottom

  const plot = useMemo(() => {
    if (!report || report.bars.length === 0) return null

    const times = report.bars.map((b) => new Date(b.date).getTime())
    const minTime = Math.min(...times)
    const maxTime = Math.max(...times)
    const values = [
      ...report.bars.map((b) => b.close),
      ...report.trades.flatMap((t) => [t.buy_price, t.sell_price]),
    ]
    const rawMin = Math.min(...values)
    const rawMax = Math.max(...values)
    const pad = (rawMax - rawMin) * 0.1 || rawMax * 0.05 || 1
    const min = rawMin - pad
    const max = rawMax + pad

    const x = (iso: string) => {
      const t = new Date(iso).getTime()
      return maxTime === minTime ? innerWidth / 2 : ((t - minTime) / (maxTime - minTime)) * innerWidth
    }
    const y = (v: number) => innerHeight - ((v - min) / (max - min)) * innerHeight

    const bars = report.bars.map((b) => ({ ...b, px: x(b.date), py: y(b.close) }))
    const closePath = bars.map((b, i) => `${i === 0 ? 'M' : 'L'} ${b.px.toFixed(1)} ${b.py.toFixed(1)}`).join(' ')
    const trades = report.trades.map((t) => ({
      ...t,
      buyX: x(t.buy_date),
      buyY: y(t.buy_price),
      sellX: x(t.sell_date),
      sellY: y(t.sell_price),
    }))

    const step = niceStep(max - min, 4)
    const ticks: number[] = []
    for (let t = Math.ceil(min / step) * step; t <= max; t += step) ticks.push(t)

    return { bars, closePath, trades, ticks, min, max }
  }, [report, innerWidth, innerHeight])

  function handlePointerMove(e: PointerEvent<SVGRectElement>) {
    if (!plot) return
    const svg = svgRef.current
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    const relX = ((e.clientX - rect.left) / rect.width) * WIDTH - MARGIN.left
    let nearest = 0
    let nearestDist = Infinity
    plot.bars.forEach((b, i) => {
      const dist = Math.abs(b.px - relX)
      if (dist < nearestDist) {
        nearestDist = dist
        nearest = i
      }
    })
    setHoverIndex(nearest)
  }

  const yFor = (v: number) => (plot ? innerHeight - ((v - plot.min) / (plot.max - plot.min)) * innerHeight : 0)
  const hovered = plot && hoverIndex !== null ? plot.bars[hoverIndex] : null
  const hoveredBuys = hovered ? (plot?.trades.filter((t) => t.buy_date === hovered.date) ?? []) : []
  const hoveredSells = hovered ? (plot?.trades.filter((t) => t.sell_date === hovered.date) ?? []) : []
  const totalProfit = report ? report.trades.reduce((sum, t) => sum + t.profit, 0) : 0

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal modal-wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="buy-sell-pattern-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header-row">
          <h2 id="buy-sell-pattern-title" className="modal-title">
            {title}
          </h2>
          <button type="button" className="icon-button modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        {runs === null && !error && <p className="placeholder-note">Loading stored buy/sell patterns...</p>}

        {runs !== null && runs.length === 0 && (
          <p className="placeholder-note">
            No stored buy/sell patterns for {ticker}. Run the "Buy/sell pattern" job for this ticker first.
          </p>
        )}

        {run && (
          <div className="backtest-chart">
            <div className="backtest-chart-toolbar">
              <label className="job-field bsp-run-field">
                Run
                <select
                  value={runName}
                  onChange={(e) => {
                    const next = runs?.find((r) => r.name === e.target.value)
                    if (next) selectRun(next)
                  }}
                >
                  {runs?.map((r) => (
                    <option key={r.name} value={r.name}>
                      {r.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="job-field">
                Start date
                <input
                  type="date"
                  value={pendingStart}
                  min={run.first_trade_date}
                  max={pendingEnd || run.last_trade_date}
                  onChange={(e) => setPendingStart(e.target.value)}
                />
              </label>
              <label className="job-field">
                End date
                <input
                  type="date"
                  value={pendingEnd}
                  min={pendingStart || run.first_trade_date}
                  max={run.last_trade_date}
                  onChange={(e) => setPendingEnd(e.target.value)}
                />
              </label>
              <button type="button" className="job-button" disabled={loading || !!rangeError} onClick={applyRange}>
                {loading ? 'Loading...' : 'Apply range'}
              </button>
              <div className="backtest-chart-legend">
                <span className="backtest-chart-legend-item">
                  <svg width="18" height="10" aria-hidden="true">
                    <line x1="0" y1="5" x2="18" y2="5" className="bsp-close-line" />
                  </svg>
                  Close
                </span>
                <span className="backtest-chart-legend-item">
                  <svg width="14" height="12" aria-hidden="true">
                    <path d={trianglePath(7, 6, true)} className="bsp-buy-marker" />
                  </svg>
                  Buy (day low)
                </span>
                <span className="backtest-chart-legend-item">
                  <svg width="14" height="12" aria-hidden="true">
                    <path d={trianglePath(7, 6, false)} className="bsp-sell-marker" />
                  </svg>
                  Sell (day high)
                </span>
              </div>
            </div>

            <p className="bsp-range-hint">
              Available range for <strong>{run.name}</strong>: {formatDate(run.first_trade_date)} –{' '}
              {formatDate(run.last_trade_date)} · {run.trades.toLocaleString()} stored trade{run.trades === 1 ? '' : 's'}
            </p>

            {rangeError && <p className="job-field-error">{rangeError}</p>}
            {error && <p className="jobs-error">{error}</p>}

            {!error && loading && !plot && <p className="placeholder-note">Loading chart...</p>}
            {!error && !loading && report && !plot && (
              <p className="placeholder-note">No daily bars for {ticker} in this date range.</p>
            )}

            {plot && applied && (
              <>
                <p className="bsp-summary">
                  {applied.start} to {applied.end}: {plot.trades.length} trade{plot.trades.length === 1 ? '' : 's'},{' '}
                  {formatCurrency(totalProfit)} total profit per share
                </p>
                <div className="bsp-chart-wrap">
                  <svg
                    ref={svgRef}
                    viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
                    className="backtest-chart-svg"
                    role="img"
                    aria-label={`${ticker} daily close with buy and sell points from run ${applied.name}`}
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

                      <path d={plot.closePath} className="bsp-close-line" fill="none" />

                      {plot.trades.map((t) => (
                        <line
                          key={`link-${t.buy_date}-${t.sell_date}`}
                          x1={t.buyX}
                          y1={t.buyY}
                          x2={t.sellX}
                          y2={t.sellY}
                          className="bsp-trade-link"
                        />
                      ))}
                      {plot.trades.map((t) => (
                        <g key={`markers-${t.buy_date}-${t.sell_date}`}>
                          <path d={trianglePath(t.buyX, t.buyY + MARKER, true)} className="bsp-buy-marker" />
                          <path d={trianglePath(t.sellX, t.sellY - MARKER, false)} className="bsp-sell-marker" />
                        </g>
                      ))}

                      <text x={0} y={innerHeight + 20} textAnchor="start" className="axis-label">
                        {formatDate(plot.bars[0].date)}
                      </text>
                      <text x={innerWidth} y={innerHeight + 20} textAnchor="end" className="axis-label">
                        {formatDate(plot.bars[plot.bars.length - 1].date)}
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
                    <div
                      className="backtest-chart-tooltip"
                      style={{ left: `${((MARGIN.left + hovered.px) / WIDTH) * 100}%` }}
                    >
                      <strong>{formatDate(hovered.date)}</strong>
                      <span>Close: {formatCurrency(hovered.close)}</span>
                      {hovered.low != null && hovered.high != null && (
                        <span>
                          Low/High: {formatCurrency(hovered.low)} / {formatCurrency(hovered.high)}
                        </span>
                      )}
                      {hoveredBuys.map((t) => (
                        <span key={`buy-${t.buy_date}`}>▲ Buy at {formatCurrency(t.buy_price)}</span>
                      ))}
                      {hoveredSells.map((t) => (
                        <span key={`sell-${t.sell_date}`}>
                          ▼ Sell at {formatCurrency(t.sell_price)} ({formatCurrency(t.profit)})
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                <button type="button" className="link-button" onClick={() => setShowTable((v) => !v)}>
                  {showTable ? 'Hide trades table' : 'Show trades table'}
                </button>

                {showTable && (
                  <table className="backtest-chart-table">
                    <thead>
                      <tr>
                        <th>Buy date</th>
                        <th>Buy price</th>
                        <th>Sell date</th>
                        <th>Sell price</th>
                        <th>Profit</th>
                        <th>Profit %</th>
                      </tr>
                    </thead>
                    <tbody>
                      {plot.trades.length === 0 ? (
                        <tr>
                          <td colSpan={6}>No trades in this range.</td>
                        </tr>
                      ) : (
                        plot.trades.map((t) => (
                          <tr key={`${t.buy_date}-${t.sell_date}`}>
                            <td>{formatDate(t.buy_date)}</td>
                            <td>{formatCurrency(t.buy_price)}</td>
                            <td>{formatDate(t.sell_date)}</td>
                            <td>{formatCurrency(t.sell_price)}</td>
                            <td className="emphasis">{formatCurrency(t.profit)}</td>
                            <td>{((t.profit / t.buy_price) * 100).toFixed(2)}%</td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                )}
              </>
            )}
          </div>
        )}

        {runs === null && error && <p className="jobs-error">{error}</p>}
      </div>
    </div>
  )
}
