import type { View } from '../App'

type SideMenuProps = {
  open: boolean
  onClose: () => void
  activeView: View
  onNavigate: (view: View) => void
}

// Matches index.css's mobile breakpoint for the side menu overlay - below it, picking
// a page should also close the drawer instead of leaving it open over the content.
const MOBILE_QUERY = '(max-width: 768px)'

const NAV_ITEMS: View[] = ['Dashboard', 'Positions', 'Signals', 'Research', 'Jobs', 'Settings']

export function SideMenu({ open, onClose, activeView, onNavigate }: SideMenuProps) {
  const handleNavigate = (view: View) => {
    onNavigate(view)
    if (typeof window !== 'undefined' && window.matchMedia(MOBILE_QUERY).matches) {
      onClose()
    }
  }

  return (
    <>
      {/* Only visible below the mobile breakpoint (see index.css) - tapping it closes the overlay drawer */}
      <div
        className={`side-menu-backdrop${open ? ' visible' : ''}`}
        onClick={onClose}
        aria-hidden="true"
      />
      <nav className={`side-menu${open ? '' : ' collapsed'}`} aria-hidden={!open}>
        <ul className="side-menu-list">
          {NAV_ITEMS.map((item) => (
            <li key={item}>
              <button
                type="button"
                className={`side-menu-item${item === activeView ? ' active' : ''}`}
                aria-current={item === activeView ? 'page' : undefined}
                onClick={() => handleNavigate(item)}
              >
                {item}
              </button>
            </li>
          ))}
        </ul>
      </nav>
    </>
  )
}
