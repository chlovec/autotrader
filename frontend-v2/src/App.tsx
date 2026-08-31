import { useState } from 'react'
import { Header } from './components/Header'
import { SideMenu } from './components/SideMenu'
import { JobsPage } from './components/JobsPage'
import { TopMoversPage } from './components/TopMoversPage'
import { TradingSymbolsPage } from './components/TradingSymbolsPage'
import { StaleTickersPage } from './components/StaleTickersPage'
import { MarketDirectionPage } from './components/MarketDirectionPage'
import { Next10DayPredictionsPage } from './components/Next10DayPredictionsPage'
import { MarketPredictionsPage } from './components/MarketPredictionsPage'
import { MarketPredictionsPerformancePage } from './components/MarketPredictionsPerformancePage'
import { PredictionComparisonPage } from './components/PredictionComparisonPage'
import { PredictionAccuracyPage } from './components/PredictionAccuracyPage'
import { ResearchPage } from './components/ResearchPage'
import { TickerTypesPage } from './components/TickerTypesPage'
import { SqlConsolePage } from './components/SqlConsolePage'

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
  | 'Market Direction'
  | 'Next 10 Day Predictions'
  | 'Market Predictions'
  | 'Market Prediction Performance'
  | 'Prediction Comparison'
  | 'Prediction Accuracy'
  | 'Settings'
  | 'Ticker Types'
  | 'SQL Console'

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
          ) : activeView === 'Market Direction' ? (
            <MarketDirectionPage />
          ) : activeView === 'Next 10 Day Predictions' ? (
            <Next10DayPredictionsPage />
          ) : activeView === 'Market Predictions' ? (
            <MarketPredictionsPage />
          ) : activeView === 'Market Prediction Performance' ? (
            <MarketPredictionsPerformancePage />
          ) : activeView === 'Prediction Comparison' ? (
            <PredictionComparisonPage />
          ) : activeView === 'Prediction Accuracy' ? (
            <PredictionAccuracyPage />
          ) : activeView === 'Research' ? (
            <ResearchPage />
          ) : activeView === 'Ticker Types' ? (
            <TickerTypesPage />
          ) : activeView === 'SQL Console' ? (
            <SqlConsolePage />
          ) : (
            <p className="placeholder-note">{activeView} goes here.</p>
          )}
        </main>
      </div>
    </div>
  )
}

export default App
