import { useEffect, useState } from 'react'

// Hand-rolled rather than a routing library - same call this project already made for
// EquityChart.tsx (see ARCHITECTURE.md): exactly two routes doesn't justify a dependency.

export type Route = { name: 'main' } | { name: 'account'; accountId: string }

function parseRoute(pathname: string): Route {
  const match = pathname.match(/^\/accounts\/([^/]+)\/?$/)
  if (match) return { name: 'account', accountId: decodeURIComponent(match[1]) }
  return { name: 'main' }
}

export function navigate(path: string): void {
  window.history.pushState(null, '', path)
  // pushState doesn't fire popstate itself - dispatch manually so every useRoute() hook
  // (there's only ever one mounted, but this keeps the contract honest) re-renders.
  window.dispatchEvent(new PopStateEvent('popstate'))
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.pathname))

  useEffect(() => {
    const onPopState = () => setRoute(parseRoute(window.location.pathname))
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  return route
}
