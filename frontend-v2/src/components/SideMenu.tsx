import { useState } from 'react'
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

type NavItem = {
  view: View
  children?: View[]
}

const NAV_ITEMS: NavItem[] = [
  { view: 'Dashboard' },
  { view: 'Positions' },
  { view: 'Signals' },
  { view: 'Research' },
  { view: 'Jobs' },
  {
    view: 'Analytics',
    children: [
      'Top Movers',
      'Trading Symbols',
      'Stale Tickers',
      'Market Direction',
      'Next 10 Day Predictions',
      'Market Predictions',
      'Market Prediction Performance',
      'Prediction Comparison',
      'Prediction Accuracy',
    ],
  },
  {
    view: 'Settings',
    children: ['Ticker Types', 'SQL Console'],
  },
]

export function SideMenu({ open, onClose, activeView, onNavigate }: SideMenuProps) {
  const [expanded, setExpanded] = useState<Set<View>>(() => {
    const parent = NAV_ITEMS.find((item) => item.children?.includes(activeView))
    return new Set(parent ? [parent.view] : [])
  })

  const handleNavigate = (view: View) => {
    onNavigate(view)
    if (typeof window !== 'undefined' && window.matchMedia(MOBILE_QUERY).matches) {
      onClose()
    }
  }

  const toggleExpanded = (view: View) => {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(view)) {
        next.delete(view)
      } else {
        next.add(view)
      }
      return next
    })
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
          {NAV_ITEMS.map((item) => {
            const hasChildren = !!item.children?.length
            const isExpanded = expanded.has(item.view)
            return (
              <li key={item.view}>
                <button
                  type="button"
                  className={`side-menu-item${item.view === activeView ? ' active' : ''}${hasChildren ? ' has-children' : ''}`}
                  aria-current={item.view === activeView ? 'page' : undefined}
                  aria-expanded={hasChildren ? isExpanded : undefined}
                  onClick={() => (hasChildren ? toggleExpanded(item.view) : handleNavigate(item.view))}
                >
                  {item.view}
                  {hasChildren && (
                    <span className={`side-menu-chevron${isExpanded ? ' expanded' : ''}`} aria-hidden="true">
                      ▸
                    </span>
                  )}
                </button>
                {hasChildren && (
                  <ul className={`side-menu-sublist${isExpanded ? '' : ' collapsed'}`}>
                    {item.children!.map((child) => (
                      <li key={child}>
                        <button
                          type="button"
                          className={`side-menu-item side-menu-subitem${child === activeView ? ' active' : ''}`}
                          aria-current={child === activeView ? 'page' : undefined}
                          onClick={() => handleNavigate(child)}
                        >
                          {child}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            )
          })}
        </ul>
      </nav>
    </>
  )
}
