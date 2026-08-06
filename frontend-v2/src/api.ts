// backend-v2's API (app/main.py), launched via bin/restart-v2.sh's run_jobs.py -
// separate from v1's frontend/src/api.ts, which points at the repo-root backend instead.
export const API_BASE = import.meta.env.VITE_BACKEND_V2_URL ?? 'http://localhost:8001'

export type RunType = 'manual' | 'auto'
export type ScheduleIntervalUnit = 'minutes' | 'hours' | 'days'

export interface JobRun {
  id: number
  trigger: RunType
  status: 'in_progress' | 'completed' | 'failed'
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
  run_type: RunType
  schedule_interval_unit: ScheduleIntervalUnit
  schedule_interval_value: number
  next_run_time: string | null
  ticker_types: string | null
  tickers: string | null
  multiplier: number | null
  timespan: string | null
  backfill_days: number | null
  running: boolean
  last_run: JobRun | null
}

export interface JobConfigInput {
  run_type: RunType
  schedule_interval_unit: ScheduleIntervalUnit
  schedule_interval_value: number
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
  tickerTypes: () => getJSON<string[]>('/ticker-types'),
  searchTickers: (q: string, limit = 20) =>
    getJSON<TickerOption[]>(`/tickers/search?q=${encodeURIComponent(q)}&limit=${limit}`),
}
