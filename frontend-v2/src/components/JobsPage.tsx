import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { api, type Job } from '../api'
import { JobCard } from './JobCard'
import { useAnchoredDropdown } from './useAnchoredDropdown'

const POLL_INTERVAL_MS = 2000
const VISIBILITY_MENU_WIDTH = 260

// Mirrors ReportGrid's "Columns" toolbar button/dropdown (see ReportGrid.tsx's
// ColumnsMenu) - a compact, scrollable list of every job as a checkbox row, checked
// meaning visible. This is the only way back to unhide a job, since a hidden job's own
// card (and its Hide button) doesn't render at all once hidden - same as a hidden
// column's own header/menu disappearing along with it there.
function JobVisibilityMenu({ jobs, onToggle }: { jobs: Job[]; onToggle: (job: Job) => void }) {
  const { open, position, anchorRef, dropdownRef, toggleMenu } = useAnchoredDropdown(VISIBILITY_MENU_WIDTH)
  const hiddenCount = jobs.filter((job) => job.hidden).length

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        className={`job-button jobs-visibility-button${hiddenCount > 0 ? ' jobs-visibility-button-active' : ''}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={toggleMenu}
      >
        Show/hide jobs{hiddenCount > 0 ? ` (${hiddenCount} hidden)` : ''}
      </button>
      {open &&
        position &&
        createPortal(
          <div
            ref={dropdownRef}
            className="report-menu-dropdown"
            role="menu"
            style={{ position: 'fixed', top: position.top, left: position.left, width: VISIBILITY_MENU_WIDTH }}
          >
            <div className="report-menu-section-title">Show / hide jobs</div>
            <ul className="report-filter-list">
              {jobs.map((job) => (
                <li key={job.name}>
                  <label className="report-filter-option">
                    <input type="checkbox" checked={!job.hidden} onChange={() => onToggle(job)} />
                    <span>{job.label}</span>
                  </label>
                </li>
              ))}
            </ul>
          </div>,
          document.body,
        )}
    </>
  )
}

export function JobsPage() {
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [error, setError] = useState<string | null>(null)

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

  const handleRun = () => {
    loadJobs()
  }

  const handleToggleVisibility = async (job: Job) => {
    try {
      handleSaved(job.hidden ? await api.unhideJob(job.name) : await api.hideJob(job.name))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update job visibility')
    }
  }

  const visibleJobs = jobs?.filter((job) => !job.hidden) ?? []

  return (
    <div className="jobs-page">
      <h1 className="jobs-page-title">Jobs</h1>
      <p className="jobs-page-subtitle">
        Configure backend-v2's scheduled data-sync jobs, or trigger one manually.
      </p>

      {error && <p className="jobs-error">{error}</p>}

      {!jobs && !error && <p className="placeholder-note">Loading jobs...</p>}

      {jobs && (
        <>
          <div className="jobs-page-toolbar">
            <JobVisibilityMenu jobs={jobs} onToggle={handleToggleVisibility} />
          </div>
          <div className="jobs-list">
            {visibleJobs.map((job) => (
              <JobCard key={job.name} job={job} onSaved={handleSaved} onRun={handleRun} />
            ))}
          </div>
        </>
      )}
    </div>
  )
}
