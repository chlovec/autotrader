import { useState } from 'react'
import { Header } from './components/Header'
import { SideMenu } from './components/SideMenu'

function App() {
  const [menuOpen, setMenuOpen] = useState(true)
  const [authenticated, setAuthenticated] = useState(false)
  const [unreadCount] = useState(3)

  return (
    <div className="app-shell">
      <Header
        menuOpen={menuOpen}
        onToggleMenu={() => setMenuOpen((open) => !open)}
        authenticated={authenticated}
        onToggleAuth={() => setAuthenticated((value) => !value)}
        unreadCount={unreadCount}
      />
      <div className="app-body">
        <SideMenu open={menuOpen} />
        <main className="app-content">
          <p className="placeholder-note">Main content goes here.</p>
        </main>
      </div>
    </div>
  )
}

export default App
