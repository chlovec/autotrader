import { useState } from 'react'
import type { AccountSummary } from '../api'
import { api } from '../api'
import { navigate } from '../router'

function formatCurrency(value: number | null): string {
  if (value === null) return '—'
  return value.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 })
}

export function AccountsTable({ accounts, onChange }: { accounts: AccountSummary[]; onChange: () => void }) {
  const [busyId, setBusyId] = useState<string | null>(null)

  if (accounts.length === 0) {
    return <p className="empty-note">No accounts configured yet - see .env.example's ACCOUNT_IDS.</p>
  }

  async function toggleActive(account: AccountSummary, event: React.MouseEvent) {
    event.stopPropagation()
    setBusyId(account.id)
    try {
      if (account.active) await api.deactivateAccount(account.id)
      else await api.activateAccount(account.id)
      onChange()
    } finally {
      setBusyId(null)
    }
  }

  return (
    <table>
      <thead>
        <tr>
          <th>Account</th>
          <th>Broker</th>
          <th>Strategy</th>
          <th>Equity</th>
          <th>Cash</th>
          <th>Unrealized P&amp;L</th>
          <th>Status</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {accounts.map((a) => (
          <tr
            key={a.id}
            className="clickable-row"
            tabIndex={0}
            onClick={() => navigate(`/accounts/${a.id}`)}
            onKeyDown={(e) => e.key === 'Enter' && navigate(`/accounts/${a.id}`)}
          >
            <td className="emphasis">{a.display_name}</td>
            <td>{a.broker}</td>
            <td>
              {a.strategy_name}
              {a.pending_strategy_name && <span className="badge badge-pending"> → {a.pending_strategy_name}</span>}
            </td>
            <td>{formatCurrency(a.equity)}</td>
            <td>{formatCurrency(a.cash)}</td>
            <td className={a.unrealized_pl !== null ? (a.unrealized_pl >= 0 ? 'positive' : 'negative') : undefined}>
              {a.unrealized_pl !== null && a.unrealized_pl >= 0 ? '+' : ''}
              {formatCurrency(a.unrealized_pl)}
            </td>
            <td>
              <span className={`badge ${a.active ? 'badge-selected' : 'badge-inactive'}`}>{a.active ? 'Active' : 'Inactive'}</span>
            </td>
            <td>
              <button type="button" className="btn-small" disabled={busyId === a.id} onClick={(e) => toggleActive(a, e)}>
                {busyId === a.id ? 'Working…' : a.active ? 'Deactivate' : 'Activate'}
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
