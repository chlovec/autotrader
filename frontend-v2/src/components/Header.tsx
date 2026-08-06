import { BellIcon, LoginIcon, LogoutIcon, MenuIcon, SearchIcon } from './icons'

type HeaderProps = {
  menuOpen: boolean
  onToggleMenu: () => void
  authenticated: boolean
  onToggleAuth: () => void
  unreadCount: number
}

export function Header({
  menuOpen,
  onToggleMenu,
  authenticated,
  onToggleAuth,
  unreadCount,
}: HeaderProps) {
  return (
    <header className="app-header">
      <button
        type="button"
        className="icon-button"
        aria-label={menuOpen ? 'Collapse side menu' : 'Expand side menu'}
        aria-pressed={menuOpen}
        onClick={onToggleMenu}
      >
        <MenuIcon className="icon" />
      </button>

      <span className="app-name">Autotrader</span>

      <div className="header-search">
        <SearchIcon className="icon header-search-icon" />
        <input type="search" placeholder="Search" aria-label="Search" />
      </div>

      <button type="button" className="icon-button auth-button" onClick={onToggleAuth}>
        {authenticated ? <LogoutIcon className="icon" /> : <LoginIcon className="icon" />}
        <span className="auth-button-label">{authenticated ? 'Log out' : 'Log in'}</span>
      </button>

      <button type="button" className="icon-button notification-button" aria-label="Notifications">
        <BellIcon className="icon" />
        {unreadCount > 0 && (
          <span className="notification-badge">{unreadCount > 99 ? '99+' : unreadCount}</span>
        )}
      </button>
    </header>
  )
}
