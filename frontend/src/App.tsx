import { useState } from 'react'
import { AccountDashboard } from './AccountDashboard'
import { MainDashboard } from './MainDashboard'
import { useRoute } from './router'

function App() {
  const [connected, setConnected] = useState(false)
  const route = useRoute()

  if (route.name === 'account') {
    return <AccountDashboard accountId={route.accountId} />
  }

  return <MainDashboard connected={connected} onConnectedChange={setConnected} />
}

export default App
