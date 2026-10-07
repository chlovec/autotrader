import { useEffect, useMemo, useState } from 'react'
import { api, type BuySellPatternTrade } from '../api'
import { ReportGrid, type ReportColumn } from './ReportGrid'
import { SearchableSelect } from './SearchableSelect'

type TradeRow = BuySellPatternTrade

const COLUMNS: ReportColumn<TradeRow>[] = [
  { key: 'buy_datetime', label: 'Buy Date' },
  { key: 'buy_price', label: 'Buy' },
  { key: 'sell_datetime', label: 'Sell Date' },
  { key: 'sell_price', label: 'Sell' },
  { key: 'profit', label: 'Profit' },
  { key: 'profit_pct', label: '% Profit' },
]

// Prices keep up to 6 decimals so a sub-penny buy (the kind that blows up % Profit)
// doesn't render as 0.00.
const PRICE_FORMAT: Intl.NumberFormatOptions = { minimumFractionDigits: 2, maximumFractionDigits: 6 }
const PCT_FORMAT: Intl.NumberFormatOptions = { minimumFractionDigits: 2, maximumFractionDigits: 2 }

function formatCell(row: TradeRow, key: keyof TradeRow): string {
  const value = row[key]
  if (value == null) return ''
  // Naive US/Eastern market time - shown as just the date, never shifted.
  if (key === 'buy_datetime' || key === 'sell_datetime') return (value as string).slice(0, 10)
  if (key === 'profit_pct') return (value as number).toLocaleString(undefined, PCT_FORMAT)
  if (typeof value === 'number') return value.toLocaleString(undefined, PRICE_FORMAT)
  return String(value)
}

function rowKey(row: TradeRow): string {
  return row.buy_datetime
}

function mean(values: number[]): number | null {
  return values.length ? values.reduce((sum, v) => sum + v, 0) / values.length : null
}

function median(values: number[]): number | null {
  if (!values.length) return null
  const sorted = [...values].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

type TradeStats = {
  trades: number
  totalProfit: number
  avgProfit: number | null
  totalProfitPct: number | null
  avgProfitPct: number | null
  // Selected trades left out of the % stats because their buy price is 0.
  zeroBuyTrades: number
  avgBuy: number | null
  minBuy: number
  maxBuy: number
  medianBuy: number | null
  avgSell: number | null
  minSell: number
  maxSell: number
  medianSell: number | null
}

// Same statistics as jobs/buy_sell_pattern_stats.py, over whichever trades are
// selected: % stats sum/average each trade's profit_pct (skipping $0 buys, which have
// none); $ stats are per share.
function computeStats(trades: TradeRow[]): TradeStats | null {
  if (!trades.length) return null
  const buys = trades.map((t) => t.buy_price)
  const sells = trades.map((t) => t.sell_price)
  const profits = trades.map((t) => t.profit)
  const pcts = trades.flatMap((t) => (t.profit_pct == null ? [] : [t.profit_pct]))
  return {
    trades: trades.length,
    totalProfit: profits.reduce((sum, v) => sum + v, 0),
    avgProfit: mean(profits),
    totalProfitPct: pcts.length ? pcts.reduce((sum, v) => sum + v, 0) : null,
    avgProfitPct: mean(pcts),
    zeroBuyTrades: trades.length - pcts.length,
    avgBuy: mean(buys),
    minBuy: Math.min(...buys),
    maxBuy: Math.max(...buys),
    medianBuy: median(buys),
    avgSell: mean(sells),
    minSell: Math.min(...sells),
    maxSell: Math.max(...sells),
    medianSell: median(sells),
  }
}

function formatPrice(value: number | null): string {
  return value == null ? '—' : value.toLocaleString(undefined, PRICE_FORMAT)
}

function formatPct(value: number | null): string {
  return value == null ? '—' : `${value.toLocaleString(undefined, PCT_FORMAT)}%`
}

// Lists every trade the buy-sell-pattern job (jobs/buy_sell_pattern.py) stored for one
// ticker under one pattern - including $0-buy trades the stats skip, so bad bars
// behind an outsized % Profit are easy to spot. Each row has a checkbox; the stats
// above the grid are recomputed in the browser over the selected trades only.
export function BuySellTradesPage() {
  const [ticker, setTicker] = useState('')
  const [patternNames, setPatternNames] = useState<string[] | null>(null)
  const [patternName, setPatternName] = useState('')
  // Both optional - blank means no bound on that side.
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [trades, setTrades] = useState<TradeRow[] | null>(null)
  // The ticker/pattern the current trades belong to - the pickers can change before the next run.
  const [loadedFor, setLoadedFor] = useState<{
    ticker: string
    patternName: string
    startDate: string
    endDate: string
  } | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Every stored pattern name, loaded once on mount regardless of the ticker - starts
  // on the first.
  useEffect(() => {
    api
      .buySellPatternPatternNames()
      .then((names) => {
        setPatternNames(names)
        setPatternName((current) => (names.includes(current) ? current : (names[0] ?? '')))
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load patterns'))
  }, [])

  const rangeError = startDate && endDate && startDate > endDate ? 'Start date must not be after end date.' : null

  // Every trade starts selected.
  const runReport = async () => {
    if (!ticker || !patternName || rangeError) return
    setLoading(true)
    setError(null)
    try {
      const data = await api.buySellPatternTrades(ticker, patternName, startDate, endDate)
      setTrades(data)
      setLoadedFor({ ticker, patternName, startDate, endDate })
      setSelected(new Set(data.map(rowKey)))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load trades')
    } finally {
      setLoading(false)
    }
  }

  const stats = useMemo(
    () => computeStats(trades?.filter((t) => selected.has(rowKey(t))) ?? []),
    [trades, selected],
  )

  return (
    <div className="report-page">
      <h1 className="jobs-page-title">Buy Sell Trades</h1>
      <p className="jobs-page-subtitle">
        Every trade the buy-sell-pattern job stored for a ticker under the chosen pattern, with stats over the
        trades you tick. Leave a date blank for no limit; only trades bought and sold within the dates are listed.
        Profit is per share.
      </p>

      <div className="report-controls">
        <div className="report-controls-fields">
          <div className="job-field report-ticker-type-field">
            <span className="job-field-label">Ticker</span>
            <SearchableSelect
              multiple={false}
              selected={ticker ? [ticker] : []}
              onChange={(values) => setTicker(values[0] ?? '')}
              onSearch={(q) =>
                api
                  .searchTickers(q)
                  .then((matches) => matches.map((t) => ({ value: t.ticker, label: t.name ? `${t.ticker} — ${t.name}` : t.ticker })))
              }
              placeholder="Search tickers..."
            />
          </div>
          <div className="job-field">
            <span className="job-field-label">Pattern</span>
            <select
              value={patternName}
              disabled={!patternNames || patternNames.length === 0}
              onChange={(event) => setPatternName(event.target.value)}
            >
              {!patternNames && <option value="">Loading...</option>}
              {patternNames?.length === 0 && <option value="">No patterns stored yet</option>}
              {patternNames?.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
          <div className="job-field">
            <span className="job-field-label">Start date</span>
            <input type="date" value={startDate} onChange={(event) => setStartDate(event.target.value)} />
          </div>
          <div className="job-field">
            <span className="job-field-label">End date</span>
            <input type="date" value={endDate} onChange={(event) => setEndDate(event.target.value)} />
          </div>
        </div>
        <div className="report-controls-actions">
          <button
            type="button"
            className="job-button job-button-primary"
            disabled={loading || !ticker || !patternName || rangeError != null}
            onClick={runReport}
          >
            {loading ? 'Running...' : 'Run report'}
          </button>
        </div>
      </div>

      {rangeError && <p className="jobs-error">{rangeError}</p>}
      {error && <p className="jobs-error">{error}</p>}
      {!loading && trades && loadedFor && (
        <section className="bst-stats" aria-label="Stats for selected trades">
          <p className="bst-stats-caption">
            {loadedFor.ticker} · {loadedFor.patternName}
            {(loadedFor.startDate || loadedFor.endDate) &&
              ` · ${loadedFor.startDate || 'earliest'} to ${loadedFor.endDate || 'latest'}`}{' '}
            — {selected.size.toLocaleString()} of{' '}
            {trades.length.toLocaleString()} trades selected
            {stats && stats.zeroBuyTrades > 0 && ` (${stats.zeroBuyTrades} with a $0 buy left out of the % stats)`}
          </p>
          {stats ? (
            <dl className="bst-stats-grid">
              <div>
                <dt>Trades</dt>
                <dd>{stats.trades.toLocaleString()}</dd>
              </div>
              <div>
                <dt>Total Profit %</dt>
                <dd>{formatPct(stats.totalProfitPct)}</dd>
              </div>
              <div>
                <dt>Avg Profit %</dt>
                <dd>{formatPct(stats.avgProfitPct)}</dd>
              </div>
              <div>
                <dt>Total Profit / Share</dt>
                <dd>{formatPrice(stats.totalProfit)}</dd>
              </div>
              <div>
                <dt>Avg Profit / Share</dt>
                <dd>{formatPrice(stats.avgProfit)}</dd>
              </div>
              <div>
                <dt>Buy Avg / Median</dt>
                <dd>
                  {formatPrice(stats.avgBuy)} / {formatPrice(stats.medianBuy)}
                </dd>
              </div>
              <div>
                <dt>Buy Min / Max</dt>
                <dd>
                  {formatPrice(stats.minBuy)} / {formatPrice(stats.maxBuy)}
                </dd>
              </div>
              <div>
                <dt>Sell Avg / Median</dt>
                <dd>
                  {formatPrice(stats.avgSell)} / {formatPrice(stats.medianSell)}
                </dd>
              </div>
              <div>
                <dt>Sell Min / Max</dt>
                <dd>
                  {formatPrice(stats.minSell)} / {formatPrice(stats.maxSell)}
                </dd>
              </div>
            </dl>
          ) : (
            <p className="placeholder-note">Tick at least one trade to see its stats.</p>
          )}
        </section>
      )}

      {!loading && trades && loadedFor && (
        <ReportGrid
          columns={COLUMNS}
          rows={trades}
          rowKey={rowKey}
          formatCell={formatCell}
          emptyMessage={
            loadedFor.startDate || loadedFor.endDate
              ? 'No trades for this ticker under this pattern in this date range.'
              : 'No trades stored for this ticker under this pattern.'
          }
          storageKey="buy-sell-trades"
          exportFilename={`buy-sell-trades-${loadedFor.ticker}-${loadedFor.patternName}`}
          exportTitle={`Buy Sell Trades - ${loadedFor.ticker} - ${loadedFor.patternName}`}
          copyable
          rowSelection={{ selected, onChange: setSelected }}
        />
      )}

      {!trades && !loading && !error && (
        <p className="placeholder-note">Pick a ticker and pattern, then run the report.</p>
      )}
    </div>
  )
}
