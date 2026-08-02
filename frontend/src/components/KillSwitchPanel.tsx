import { useState } from 'react'
import type { KillSwitchState } from '../api'
import { api } from '../api'

export function KillSwitchPanel({
  accountId,
  state,
  onChange,
}: {
  accountId: string
  state: KillSwitchState | null
  onChange: (next: KillSwitchState) => void
}) {
  const [busy, setBusy] = useState(false)
  const [reasonInput, setReasonInput] = useState('')

  if (!state) {
    return <p className="empty-note">Loading kill switch status…</p>
  }

  async function toggle() {
    setBusy(true)
    try {
      const next = await api.setAccountKillSwitch(
        accountId, !state!.engaged, state!.engaged ? '' : reasonInput || 'manually engaged from dashboard'
      )
      onChange(next)
      setReasonInput('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`kill-switch ${state.engaged ? 'engaged' : 'disengaged'}`}>
      <div className="kill-switch-status">
        <span className="status-dot" aria-hidden="true" />
        <div>
          <strong>{state.engaged ? 'Trading halted' : 'Trading active'}</strong>
          {state.engaged && state.reason && <p className="kill-switch-reason">{state.reason}</p>}
        </div>
      </div>

      {!state.engaged && (
        <input
          type="text"
          placeholder="Reason for stopping (optional)"
          value={reasonInput}
          onChange={(e) => setReasonInput(e.target.value)}
          className="kill-switch-input"
        />
      )}

      <button type="button" onClick={toggle} disabled={busy} className={state.engaged ? 'btn resume' : 'btn stop'}>
        {busy ? 'Working…' : state.engaged ? 'Resume trading' : 'Stop trading'}
      </button>
    </div>
  )
}
