import { useEffect, useState } from 'react'
import { api, type Job } from '../api'
import { JobCard } from './JobCard'
import type { SelectOption } from './SearchableSelect'

const POLL_INTERVAL_MS = 2000

export function JobsPage() {
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Fetched once here rather than per-card - both jobs' Ticker type select(s) share
  // the same small, slow-changing list.
  const [tickerTypeOptions, setTickerTypeOptions] = useState<SelectOption[]>([])

  const loadJobs = async () => {
    try {
      const next = await api.jobs()
      setJobs(next)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load jobs')
    }
  }

  useEffect(() => {
    loadJobs()
    api
      .tickerTypes()
      .then((types) => setTickerTypeOptions(types.map((type) => ({ value: type, label: type }))))
      .catch(() => setTickerTypeOptions([]))
  }, [])

  // While any job is running, poll so status/last-run update without a manual refresh.
  const anyRunning = jobs?.some((job) => job.running) ?? false
  useEffect(() => {
    if (!anyRunning) return
    const id = window.setInterval(loadJobs, POLL_INTERVAL_MS)
    return () => window.clearInterval(id)
  }, [anyRunning])

  const handleSaved = (updated: Job) => {
    setJobs((prev) => prev?.map((job) => (job.name === updated.name ? updated : job)) ?? prev)
  }

  return (
    <div className="jobs-page">
      <h1 className="jobs-page-title">Jobs</h1>
      <p className="jobs-page-subtitle">
        Configure backend-v2's scheduled data-sync jobs, or trigger one manually.
      </p>

      {error && <p className="jobs-error">{error}</p>}

      {!jobs && !error && <p className="placeholder-note">Loading jobs...</p>}

      {jobs && (
        <div className="jobs-list">
          {jobs.map((job) => (
            <JobCard key={job.name} job={job} onSaved={handleSaved} tickerTypeOptions={tickerTypeOptions} />
          ))}
        </div>
      )}
    </div>
  )
}
