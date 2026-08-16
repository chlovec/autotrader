// Named, multi-entry presets for a report page - a step up from reportParams.ts's
// single auto-loaded slot. Each profile bundles a page's filter params (whatever shape
// the page defines) alongside a raw copy of ReportGrid's own "Save view" JSON (sort/
// filter/freeze/hide/columnWidths - see ReportGrid.tsx's readRawGridView), so loading a
// profile restores both in one step. Loading only ever populates in-memory state (see
// the calling page's handleLoadProfile) - nothing here is touched again until the user
// explicitly saves, same "load doesn't mutate storage" contract reportParams.ts already
// has for its own single slot.

function storageKey(id: string): string {
  return `report-profiles:${id}`
}

export type ReportProfile<Params> = {
  name: string
  params: Params
  // Raw ReportGrid SavedView JSON (see ReportGrid.tsx's readRawGridView/writeRawGridView) -
  // opaque here since this module doesn't know a page's column-key type. null if the
  // profile was saved before the grid ever mounted (no view to capture yet).
  view: unknown | null
  updatedAt: string
}

export function loadReportProfiles<Params>(id: string): ReportProfile<Params>[] {
  try {
    const raw = window.localStorage.getItem(storageKey(id))
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? (parsed as ReportProfile<Params>[]) : []
  } catch {
    return []
  }
}

function saveAll<Params>(id: string, profiles: ReportProfile<Params>[]): void {
  try {
    window.localStorage.setItem(storageKey(id), JSON.stringify(profiles))
  } catch {
    // Storage full/unavailable (private browsing, quota) - saving is a nice-to-have,
    // not worth surfacing an error over, same reasoning as reportParams.ts.
  }
}

// Upserts by name - saving under an existing name silently overwrites it, same
// create-or-update semantics as reportParams.ts's single-slot save.
export function upsertReportProfile<Params>(id: string, profile: ReportProfile<Params>): void {
  const profiles = loadReportProfiles<Params>(id)
  const index = profiles.findIndex((p) => p.name === profile.name)
  if (index === -1) profiles.push(profile)
  else profiles[index] = profile
  saveAll(id, profiles)
}

export function deleteReportProfile(id: string, name: string): void {
  saveAll(
    id,
    loadReportProfiles(id).filter((p) => p.name !== name),
  )
}
