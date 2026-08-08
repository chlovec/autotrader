// Persists a report page's filter/control values (as opposed to ReportGrid's own
// "Save view", which only covers sort/filter/freeze/hide/width on the grid itself) so
// a page can restore them into its controls on the next visit. Saving is opt-in via an
// explicit "Save parameters" button - loading happens automatically on mount, but never
// auto-runs the report, since the last run may have been against stale/expensive
// filters the user wouldn't want fired off without confirming first.
function storageKey(id: string): string {
  return `report-params:${id}`
}

export function loadReportParams<T>(id: string): Partial<T> | null {
  try {
    const raw = window.localStorage.getItem(storageKey(id))
    if (!raw) return null
    return JSON.parse(raw) as Partial<T>
  } catch {
    return null
  }
}

export function saveReportParams<T>(id: string, params: T): void {
  try {
    window.localStorage.setItem(storageKey(id), JSON.stringify(params))
  } catch {
    // Storage full/unavailable (private browsing, quota) - saving is a nice-to-have,
    // not worth surfacing an error over.
  }
}
