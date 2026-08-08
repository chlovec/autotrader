import { useEffect, useRef, useState } from 'react'

type Position = { top: number; left: number }

// Shared by ColumnHeaderMenu and ReportGrid's column-visibility toolbar button: a
// button that opens a dropdown portaled into document.body, positioned via
// getBoundingClientRect from the trigger rather than as an absolutely-positioned
// descendant of it - both live inside a scrolling `overflow: auto` grid, which would
// otherwise clip the dropdown for a trigger near either edge. Closes on outside click
// or on any scroll (rather than tracking position live).
export function useAnchoredDropdown(width: number) {
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState<Position | null>(null)
  const anchorRef = useRef<HTMLButtonElement>(null)
  const dropdownRef = useRef<HTMLDivElement>(null)

  const openMenu = () => {
    const rect = anchorRef.current?.getBoundingClientRect()
    if (rect) {
      // Right-aligned under the trigger when there's room; clamped to the viewport
      // otherwise so a column near either edge doesn't run off-screen.
      const left = Math.max(4, Math.min(rect.right - width, window.innerWidth - width - 4))
      setPosition({ top: rect.bottom + 4, left })
    }
    setOpen(true)
  }

  const closeMenu = () => setOpen(false)
  const toggleMenu = () => (open ? closeMenu() : openMenu())

  useEffect(() => {
    if (!open) return
    const close = (event: Event) => {
      const target = event.target as Node
      if (anchorRef.current?.contains(target) || dropdownRef.current?.contains(target)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', close)
    // The position above is captured once, on open - rather than tracking it live,
    // scrolling the grid (the capture-phase listener catches that even though native
    // scroll events don't bubble) just closes the menu.
    document.addEventListener('scroll', close, true)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('scroll', close, true)
    }
  }, [open])

  return { open, position, anchorRef, dropdownRef, openMenu, closeMenu, toggleMenu }
}
