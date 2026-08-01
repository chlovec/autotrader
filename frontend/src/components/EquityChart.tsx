import { useMemo, useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import type { EquityPoint } from '../api'

const WIDTH = 760
const HEIGHT = 260
const MARGIN = { top: 16, right: 16, bottom: 28, left: 64 }

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
  return value.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

export function EquityChart({ data }: { data: EquityPoint[] }) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const [showTable, setShowTable] = useState(false)
  const svgRef = useRef<SVGSVGElement>(null)

  const innerWidth = WIDTH - MARGIN.left - MARGIN.right
  const innerHeight = HEIGHT - MARGIN.top - MARGIN.bottom

  const plot = useMemo(() => {
    if (data.length === 0) return null

    const times = data.map((d) => new Date(d.timestamp).getTime())
    const values = data.map((d) => d.equity)
    const minTime = Math.min(...times)
    const maxTime = Math.max(...times)
    const rawMin = Math.min(...values)
    const rawMax = Math.max(...values)
    const pad = (rawMax - rawMin) * 0.15 || rawMax * 0.05 || 100
    const min = rawMin - pad
    const max = rawMax + pad

    const x = (t: number) => (maxTime === minTime ? innerWidth / 2 : ((t - minTime) / (maxTime - minTime)) * innerWidth)
    const y = (v: number) => innerHeight - ((v - min) / (max - min)) * innerHeight

    const points = data.map((d, i) => ({ ...d, px: x(times[i]), py: y(d.equity) }))
    const path = points.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.px.toFixed(1)} ${p.py.toFixed(1)}`).join(' ')

    const step = niceStep(max - min, 4)
    const firstTick = Math.ceil(min / step) * step
    const ticks: number[] = []
    for (let t = firstTick; t <= max; t += step) ticks.push(t)

    return { points, path, ticks, min, max }
  }, [data, innerWidth, innerHeight])

  if (!plot) {
    return (
      <div className="chart-empty">
        <p>No equity data yet — it'll appear after the first trade.</p>
      </div>
    )
  }

  const { points, path, ticks, min, max } = plot
  const last = points[points.length - 1]
  const hovered = hoverIndex !== null ? points[hoverIndex] : null

  function handlePointerMove(e: PointerEvent<SVGRectElement>) {
    const svg = svgRef.current
    if (!svg) return
    const rect = svg.getBoundingClientRect()
    const relX = ((e.clientX - rect.left) / rect.width) * WIDTH - MARGIN.left
    let nearest = 0
    let nearestDist = Infinity
    points.forEach((p, i) => {
      const dist = Math.abs(p.px - relX)
      if (dist < nearestDist) {
        nearestDist = dist
        nearest = i
      }
    })
    setHoverIndex(nearest)
  }

  const yFor = (v: number) => innerHeight - ((v - min) / (max - min)) * innerHeight

  return (
    <div className="equity-chart">
      <svg ref={svgRef} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="equity-chart-svg" role="img" aria-label="Portfolio equity over time">
        <g transform={`translate(${MARGIN.left}, ${MARGIN.top})`}>
          {ticks.map((t) => (
            <g key={t}>
              <line x1={0} x2={innerWidth} y1={yFor(t)} y2={yFor(t)} className="gridline" />
              <text x={-8} y={yFor(t)} textAnchor="end" dominantBaseline="middle" className="axis-label">
                {formatCurrency(t)}
              </text>
            </g>
          ))}

          {hovered && <line x1={hovered.px} x2={hovered.px} y1={0} y2={innerHeight} className="crosshair" />}

          <path d={path} className="equity-line" fill="none" />

          <circle cx={last.px} cy={last.py} r={5} className="equity-dot" />
          {hovered && hovered !== last && <circle cx={hovered.px} cy={hovered.py} r={5} className="equity-dot" />}

          <text x={last.px} y={last.py - 12} textAnchor="end" className="end-label">
            {formatCurrency(last.equity)}
          </text>

          <text x={0} y={innerHeight + 20} textAnchor="start" className="axis-label">
            {formatDate(points[0].timestamp)}
          </text>
          <text x={innerWidth} y={innerHeight + 20} textAnchor="end" className="axis-label">
            {formatDate(last.timestamp)}
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
          className="equity-tooltip"
          style={{ left: `${((MARGIN.left + hovered.px) / WIDTH) * 100}%` }}
        >
          <strong>{formatCurrency(hovered.equity)}</strong>
          <span>{formatDate(hovered.timestamp)}</span>
        </div>
      )}

      <button type="button" className="link-button" onClick={() => setShowTable((v) => !v)}>
        {showTable ? 'Hide data table' : 'Show data table'}
      </button>

      {showTable && (
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Equity</th>
              <th>Cash</th>
            </tr>
          </thead>
          <tbody>
            {[...data].reverse().map((d) => (
              <tr key={d.timestamp}>
                <td>{new Date(d.timestamp).toLocaleString()}</td>
                <td className="emphasis">{formatCurrency(d.equity)}</td>
                <td>{formatCurrency(d.cash)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
