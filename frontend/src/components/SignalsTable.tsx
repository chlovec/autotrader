import type { Signal } from '../api'

export function SignalsTable({ signals }: { signals: Signal[] }) {
  if (signals.length === 0) {
    return <p className="empty-note">No signals yet.</p>
  }

  return (
    <table>
      <thead>
        <tr>
          <th>Time</th>
          <th>Symbol</th>
          <th>Strategy</th>
          <th>Action</th>
          <th>Reason</th>
        </tr>
      </thead>
      <tbody>
        {signals.map((s) => (
          <tr key={s.id}>
            <td>{new Date(s.timestamp).toLocaleString()}</td>
            <td className="emphasis">{s.symbol}</td>
            <td>{s.strategy_name}</td>
            <td>{s.action.toUpperCase()}</td>
            <td>{s.reason}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
