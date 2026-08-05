type SideMenuProps = {
  open: boolean
}

const NAV_ITEMS = ['Dashboard', 'Positions', 'Signals', 'Research', 'Settings']

export function SideMenu({ open }: SideMenuProps) {
  return (
    <nav className={`side-menu${open ? '' : ' collapsed'}`} aria-hidden={!open}>
      <ul className="side-menu-list">
        {NAV_ITEMS.map((item) => (
          <li key={item}>
            <a className="side-menu-item" href="#">
              {item}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  )
}
