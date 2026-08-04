import { useEffect, useState } from 'react'
import type { AccountDetail } from '../api'
import { api } from '../api'

const REBALANCE_STRATEGY_NAME = 'rebalancing_portfolio'

const STRATEGY_OPTIONS: { value: string; label: string }[] = [
  { value: 'ma_crossover', label: 'Moving Average Crossover' },
  { value: 'mean_reversion', label: 'Mean Reversion' },
  { value: 'regime_switching', label: 'Regime Switching' },
  { value: REBALANCE_STRATEGY_NAME, label: 'Rebalancing Portfolio' },
]

// Mirrors each Python strategy class's constructor defaults (engine/strategy.py) - used
// to seed the form when switching to a strategy other than the account's current one,
// since that strategy's own saved params don't apply to a different shape.
const STRATEGY_FIELDS: Record<string, { key: string; label: string }[]> = {
  ma_crossover: [
    { key: 'short_window', label: 'Short window (days)' },
    { key: 'long_window', label: 'Long window (days)' },
  ],
  mean_reversion: [
    { key: 'period', label: 'RSI period (days)' },
    { key: 'oversold', label: 'Oversold threshold' },
    { key: 'overbought', label: 'Overbought threshold' },
  ],
  regime_switching: [
    { key: 'adx_period', label: 'ADX period (days)' },
    { key: 'adx_trend_threshold', label: 'ADX trend threshold' },
    { key: 'ma_short', label: 'Short MA window (days)' },
    { key: 'ma_long', label: 'Long MA window (days)' },
    { key: 'rsi_period', label: 'RSI period (days)' },
  ],
}

const STRATEGY_DEFAULTS: Record<string, Record<string, number>> = {
  ma_crossover: { short_window: 20, long_window: 50 },
  mean_reversion: { period: 14, oversold: 30, overbought: 70 },
  regime_switching: { adx_period: 14, adx_trend_threshold: 25, ma_short: 20, ma_long: 50, rsi_period: 14 },
}

// Same seed engine/accounts.py uses for a freshly-created account defaulted to this strategy.
const REBALANCE_DEFAULT_WEIGHTS: Record<string, number> = { SPY: 1 / 3, TLT: 1 / 3, GLD: 1 / 3 }

const WEIGHT_TOLERANCE = 0.001

interface WeightRow {
  symbol: string
  weight: string
}

function strategyLabel(name: string): string {
  return STRATEGY_OPTIONS.find((opt) => opt.value === name)?.label ?? name
}

function defaultFieldsFor(name: string): Record<string, string> {
  return Object.fromEntries(Object.entries(STRATEGY_DEFAULTS[name] ?? {}).map(([k, v]) => [k, String(v)]))
}

function fieldsFromAccountParams(name: string, rawParams: Record<string, unknown>): Record<string, string> {
  const defaults = defaultFieldsFor(name)
  return Object.fromEntries(Object.keys(defaults).map((k) => [k, rawParams[k] !== undefined ? String(rawParams[k]) : defaults[k]]))
}

function weightRowsFrom(weights: Record<string, number>): WeightRow[] {
  return Object.entries(weights).map(([symbol, weight]) => ({ symbol, weight: String(weight) }))
}

// Sorts object keys before stringifying so a same-content-different-key-order comparison
// (e.g. after a poll refetch rebuilds the params object) doesn't register as a false dirty.
function stableStringify(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b))
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${stableStringify(v)}`).join(',')}}`
  }
  return JSON.stringify(value)
}

export function StrategyPanel({ account, onChange }: { account: AccountDetail; onChange: (next: AccountDetail) => void }) {
  const [strategyName, setStrategyName] = useState(account.strategy_name)
  const [fields, setFields] = useState<Record<string, string>>(() => fieldsFromAccountParams(account.strategy_name, account.strategy_params))
  const [weightRows, setWeightRows] = useState<WeightRow[]>(() =>
    weightRowsFrom((account.strategy_params.target_weights as Record<string, number>) ?? REBALANCE_DEFAULT_WEIGHTS),
  )
  const [immediate, setImmediate] = useState(false)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [cancelBusy, setCancelBusy] = useState(false)

  const paramsKey = stableStringify(account.strategy_params)
  useEffect(() => {
    setStrategyName(account.strategy_name)
    if (account.strategy_name === REBALANCE_STRATEGY_NAME) {
      setWeightRows(weightRowsFrom((account.strategy_params.target_weights as Record<string, number>) ?? REBALANCE_DEFAULT_WEIGHTS))
    } else {
      setFields(fieldsFromAccountParams(account.strategy_name, account.strategy_params))
    }
    // paramsKey is a stable, order-independent stand-in for account.strategy_params so this
    // only re-syncs when the account's live params actually change, not on every poll tick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [account.id, account.strategy_name, paramsKey])

  function onStrategyNameChange(next: string) {
    setStrategyName(next)
    if (next === account.strategy_name) {
      if (next === REBALANCE_STRATEGY_NAME) {
        setWeightRows(weightRowsFrom((account.strategy_params.target_weights as Record<string, number>) ?? REBALANCE_DEFAULT_WEIGHTS))
      } else {
        setFields(fieldsFromAccountParams(next, account.strategy_params))
      }
    } else if (next === REBALANCE_STRATEGY_NAME) {
      setWeightRows(weightRowsFrom(REBALANCE_DEFAULT_WEIGHTS))
    } else {
      setFields(defaultFieldsFor(next))
    }
  }

  function updateWeightRow(index: number, patch: Partial<WeightRow>) {
    setWeightRows((rows) => rows.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  function removeWeightRow(index: number) {
    setWeightRows((rows) => rows.filter((_, i) => i !== index))
  }

  function addWeightRow() {
    setWeightRows((rows) => [...rows, { symbol: '', weight: '0' }])
  }

  function buildParams(): Record<string, unknown> {
    if (strategyName === REBALANCE_STRATEGY_NAME) {
      const target_weights: Record<string, number> = {}
      for (const row of weightRows) {
        const symbol = row.symbol.trim().toUpperCase()
        if (!symbol) continue
        target_weights[symbol] = Number(row.weight) || 0
      }
      return { target_weights }
    }
    const result: Record<string, number> = {}
    for (const field of STRATEGY_FIELDS[strategyName] ?? []) {
      result[field.key] = Number(fields[field.key])
    }
    return result
  }

  const weightSum = weightRows.reduce((sum, row) => sum + (Number(row.weight) || 0), 0)
  const weightsValid = strategyName !== REBALANCE_STRATEGY_NAME || (weightRows.length > 0 && Math.abs(weightSum - 1) < WEIGHT_TOLERANCE)
  const fieldsValid =
    strategyName === REBALANCE_STRATEGY_NAME ||
    (STRATEGY_FIELDS[strategyName] ?? []).every((f) => fields[f.key] !== undefined && fields[f.key] !== '' && !Number.isNaN(Number(fields[f.key])))
  const dirty = strategyName !== account.strategy_name || stableStringify(buildParams()) !== stableStringify(account.strategy_params)

  async function save() {
    setBusy(true)
    try {
      const updated = await api.setAccountStrategy(account.id, strategyName, buildParams(), immediate)
      onChange(updated)
      setImmediate(false)
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setBusy(false)
    }
  }

  async function cancelPending() {
    setCancelBusy(true)
    try {
      onChange(await api.cancelPendingAccountStrategy(account.id))
    } finally {
      setCancelBusy(false)
    }
  }

  return (
    <div className="strategy-panel">
      {account.pending_strategy_name && (
        <p className="strategy-pending-note">
          Pending: switching to <strong>{strategyLabel(account.pending_strategy_name)}</strong> at the next trading cycle.
          <button type="button" className="btn-small" disabled={cancelBusy} onClick={cancelPending}>
            {cancelBusy ? 'Canceling…' : 'Cancel pending change'}
          </button>
        </p>
      )}

      <label className="strategy-field">
        <span>Strategy</span>
        <select value={strategyName} onChange={(e) => onStrategyNameChange(e.target.value)}>
          {STRATEGY_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </label>

      {strategyName === REBALANCE_STRATEGY_NAME ? (
        <div className="weight-table">
          {weightRows.map((row, i) => (
            <div className="weight-row" key={i}>
              <input
                type="text"
                placeholder="Symbol"
                value={row.symbol}
                onChange={(e) => updateWeightRow(i, { symbol: e.target.value.toUpperCase() })}
              />
              <input type="number" min="0" max="1" step="0.01" value={row.weight} onChange={(e) => updateWeightRow(i, { weight: e.target.value })} />
              <button type="button" className="btn-small" onClick={() => removeWeightRow(i)}>
                Remove
              </button>
            </div>
          ))}
          <button type="button" className="btn-small" onClick={addWeightRow}>
            Add symbol
          </button>
          <p className={`weight-total${weightsValid ? '' : ' invalid'}`}>
            Total: {weightSum.toFixed(2)}
            {!weightsValid && ' — must sum to 1.00'}
          </p>
        </div>
      ) : (
        <div className="strategy-params">
          {(STRATEGY_FIELDS[strategyName] ?? []).map((field) => (
            <label className="strategy-field" key={field.key}>
              <span>{field.label}</span>
              <input
                type="number"
                step="any"
                value={fields[field.key] ?? ''}
                onChange={(e) => setFields((f) => ({ ...f, [field.key]: e.target.value }))}
              />
            </label>
          ))}
        </div>
      )}

      <label className="strategy-immediate">
        <input type="checkbox" checked={immediate} onChange={(e) => setImmediate(e.target.checked)} />
        Apply immediately (otherwise takes effect at the next trading cycle)
      </label>

      <button type="button" className="btn-small" disabled={!dirty || !weightsValid || !fieldsValid || busy} onClick={save}>
        {busy ? 'Saving…' : saved ? 'Saved' : 'Save strategy'}
      </button>
    </div>
  )
}
