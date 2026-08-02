import { useEffect, useState } from 'react'
import type { AccountDetail } from '../api'
import { api } from '../api'

export function TradingLimitsPanel({ account, onChange }: { account: AccountDetail; onChange: (next: AccountDetail) => void }) {
  const [maxPositionSize, setMaxPositionSize] = useState(String(account.max_position_size_usd))
  const [maxDailyLoss, setMaxDailyLoss] = useState(String(account.max_daily_loss_usd))
  const [maxTotalExposure, setMaxTotalExposure] = useState(String(account.max_total_exposure_usd))
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    setMaxPositionSize(String(account.max_position_size_usd))
    setMaxDailyLoss(String(account.max_daily_loss_usd))
    setMaxTotalExposure(String(account.max_total_exposure_usd))
  }, [account.max_position_size_usd, account.max_daily_loss_usd, account.max_total_exposure_usd])

  const dirty =
    Number(maxPositionSize) !== account.max_position_size_usd ||
    Number(maxDailyLoss) !== account.max_daily_loss_usd ||
    Number(maxTotalExposure) !== account.max_total_exposure_usd

  async function save() {
    setBusy(true)
    try {
      const limits = await api.setAccountLimits(account.id, {
        max_position_size_usd: Number(maxPositionSize),
        max_daily_loss_usd: Number(maxDailyLoss),
        max_total_exposure_usd: Number(maxTotalExposure),
      })
      onChange({ ...account, ...limits })
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="limits-panel">
      <label className="limits-field">
        <span>Max position size (USD)</span>
        <input type="number" min="0" step="1" value={maxPositionSize} onChange={(e) => setMaxPositionSize(e.target.value)} />
      </label>
      <label className="limits-field">
        <span>Max daily loss (USD)</span>
        <input type="number" min="0" step="1" value={maxDailyLoss} onChange={(e) => setMaxDailyLoss(e.target.value)} />
      </label>
      <label className="limits-field">
        <span>Max total open positions (USD)</span>
        <input type="number" min="0" step="1" value={maxTotalExposure} onChange={(e) => setMaxTotalExposure(e.target.value)} />
        <span className="limits-hint">0 = no cap. Blocks new buys once total open-position value hits this - sells always free up room.</span>
      </label>
      <button type="button" className="btn-small" disabled={busy || !dirty} onClick={save}>
        {busy ? 'Saving…' : saved ? 'Saved' : 'Save limits'}
      </button>
    </div>
  )
}
