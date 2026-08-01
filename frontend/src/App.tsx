import { useEffect, useState } from 'react'
import './App.css'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

function App() {
  const [status, setStatus] = useState<'checking' | 'connected' | 'unreachable'>('checking')

  useEffect(() => {
    fetch(`${API_BASE}/health`)
      .then((res) => (res.ok ? setStatus('connected') : setStatus('unreachable')))
      .catch(() => setStatus('unreachable'))
  }, [])

  return (
    <main className="dashboard">
      <h1>Autotrader Dashboard</h1>
      <p>
        Backend status: <strong>{status}</strong>
      </p>
      <p className="note">
        This is a skeleton. Positions, equity curve, trade log, and the kill switch land in Phase 6.
      </p>
    </main>
  )
}

export default App
