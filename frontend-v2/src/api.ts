// backend-v2's API (app/main.py), launched via bin/restart-v2.sh's run_jobs.py -
// separate from v1's frontend/src/api.ts, which points at the repo-root backend instead.
export const API_BASE = import.meta.env.VITE_BACKEND_V2_URL ?? 'http://localhost:8001'

export type RunType = 'manual' | 'auto'
export type ScheduleIntervalUnit = 'minutes' | 'hours' | 'days'

// Quarter-hour UTC time-of-day slots, "00:00".."23:45" - mirrors backend-v2's
// jobs/registry.py START_TIME_OPTIONS, which app/main.py validates JobConfigIn.start_time
// against. Generated rather than fetched: it's static and fully determined by this one
// formula, so there's nothing a network round trip would keep in sync that this doesn't
// already guarantee by construction.
export const START_TIME_OPTIONS: string[] = Array.from({ length: 24 * 4 }, (_, i) => {
  const hour = Math.floor(i / 4)
  const minute = (i % 4) * 15
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
})

export interface JobRun {
  id: number
  trigger: RunType
  status: 'in_progress' | 'completed' | 'failed' | 'cancelled'
  started_at: string
  finished_at: string | null
  duration_seconds: number | null
  result_summary: string | null
  error: string | null
}

export interface Job {
  name: string
  label: string
  description: string
  has_bars_fields: boolean
  has_ticker_type_filter: boolean
  has_ticker_selector: boolean
  run_type: RunType
  schedule_interval_unit: ScheduleIntervalUnit
  schedule_interval_value: number
  start_time: string
  next_run_time: string | null
  ticker_types: string | null
  tickers: string | null
  multiplier: number | null
  timespan: string | null
  backfill_days: number | null
  running: boolean
  // Only meaningful while running - a job that isn't running can't be paused. Reflects
  // a pause *request*, not confirmation the run has actually parked at a checkpoint
  // (see backend-v2 jobs/control.py) - in practice that lag is well under a second.
  paused: boolean
  last_run: JobRun | null
}

export interface JobConfigInput {
  run_type: RunType
  schedule_interval_unit: ScheduleIntervalUnit
  schedule_interval_value: number
  start_time: string
  ticker_types?: string | null
  tickers?: string | null
  multiplier?: number | null
  timespan?: string | null
  backfill_days?: number | null
}

export interface TickerOption {
  ticker: string
  name: string | null
}

export interface TickerTypeOption {
  code: string
  asset_class: string
  description: string | null
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`)
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`)
  return res.json()
}

async function putJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const detail = await res.json().catch(() => null)
    throw new Error(detail?.detail ?? `${path} failed: ${res.status}`)
  }
  return res.json()
}

async function postJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { method: 'POST' })
  if (!res.ok) {
    const detail = await res.json().catch(() => null)
    throw new Error(detail?.detail ?? `${path} failed: ${res.status}`)
  }
  return res.json()
}

export type TriggerJobResult = { status: 'started' } | { status: 'already-running' }

export const api = {
  jobs: () => getJSON<Job[]>('/jobs'),
  job: (name: string) => getJSON<Job>(`/jobs/${name}`),
  jobRuns: (name: string, limit = 10) => getJSON<JobRun[]>(`/jobs/${name}/runs?limit=${limit}`),
  updateJobConfig: (name: string, config: JobConfigInput) => putJSON<Job>(`/jobs/${name}/config`, config),
  triggerJob: async (name: string): Promise<TriggerJobResult> => {
    const res = await fetch(`${API_BASE}/jobs/${name}/run`, { method: 'POST' })
    if (res.status === 409) return { status: 'already-running' }
    if (!res.ok) throw new Error(`trigger ${name} failed: ${res.status}`)
    return res.json()
  },
  pauseJob: (name: string) => postJSON<{ status: string }>(`/jobs/${name}/pause`),
  resumeJob: (name: string) => postJSON<{ status: string }>(`/jobs/${name}/resume`),
  cancelJob: (name: string) => postJSON<{ status: string }>(`/jobs/${name}/cancel`),
  searchTickers: (q: string, limit = 20) =>
    getJSON<TickerOption[]>(`/tickers/search?q=${encodeURIComponent(q)}&limit=${limit}`),
  searchTickerTypes: (q: string, limit = 20) =>
    getJSON<TickerTypeOption[]>(`/ticker-types/search?q=${encodeURIComponent(q)}&limit=${limit}`),
}
