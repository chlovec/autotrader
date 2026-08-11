import { useState } from 'react'
import { Header } from './components/Header'
import { SideMenu } from './components/SideMenu'
import { JobsPage } from './components/JobsPage'
import { TopMoversPage } from './components/TopMoversPage'
import { TradingSymbolsPage } from './components/TradingSymbolsPage'
import { StaleTickersPage } from './components/StaleTickersPage'
import { Next10DayPredictionsPage } from './components/Next10DayPredictionsPage'
import { MarketPredictionsPage } from './components/MarketPredictionsPage'

// Below this width the side menu renders as an overlay drawer (see index.css) and
// should start closed; at/above it, it renders inline and should start open.
const DESKTOP_MENU_QUERY = '(min-width: 769px)'

export type View =
  | 'Dashboard'
  | 'Positions'
  | 'Signals'
  | 'Research'
  | 'Jobs'
  | 'Analytics'
  | 'Top Movers'
  | 'Trading Symbols'
  | 'Stale Tickers'
  | 'Next 10 Day Predictions'
  | 'Market Predictions'
  | 'Settings'

function App() {
  const [menuOpen, setMenuOpen] = useState(
    () => typeof window !== 'undefined' && window.matchMedia(DESKTOP_MENU_QUERY).matches,
  )
  const [authenticated, setAuthenticated] = useState(false)
  const [unreadCount] = useState(3)
  const [activeView, setActiveView] = useState<View>('Dashboard')

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
        <SideMenu open={menuOpen} onClose={() => setMenuOpen(false)} activeView={activeView} onNavigate={setActiveView} />
        <main className="app-content">
          {activeView === 'Jobs' ? (
            <JobsPage />
          ) : activeView === 'Top Movers' ? (
            <TopMoversPage />
          ) : activeView === 'Trading Symbols' ? (
            <TradingSymbolsPage />
          ) : activeView === 'Stale Tickers' ? (
            <StaleTickersPage />
          ) : activeView === 'Next 10 Day Predictions' ? (
            <Next10DayPredictionsPage />
          ) : activeView === 'Market Predictions' ? (
            <MarketPredictionsPage />
          ) : (
            <p className="placeholder-note">{activeView} goes here.</p>
          )}
        </main>
      </div>
    </div>
  )
}

export default App
