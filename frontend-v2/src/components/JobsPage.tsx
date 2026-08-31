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
  const [draggedJob, setDraggedJob] = useState<string | null>(null)
  const [dragOverJob, setDragOverJob] = useState<string | null>(null)

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

  const handleDragEnd = () => {
    setDraggedJob(null)
    setDragOverJob(null)
  }

  // Reorders just the visible cards (dragged/target are both drawn from visibleJobs),
  // then splices that new order back into the full job list in place of the old
  // visible ordering - so hidden jobs (never rendered as cards, and thus never
  // draggable) keep their existing relative slots instead of getting shuffled to the
  // end. api.reorderJobs needs every job name, visible or not, since sort_order is
  // stored per job on the backend (see app/main.py's reorder_jobs).
  const handleDrop = async (targetName: string) => {
    const sourceName = draggedJob
    handleDragEnd()
    if (!jobs || !sourceName || sourceName === targetName) return

    const fromIndex = visibleJobs.findIndex((job) => job.name === sourceName)
    const toIndex = visibleJobs.findIndex((job) => job.name === targetName)
    if (fromIndex === -1 || toIndex === -1) return

    const reorderedVisible = [...visibleJobs]
    const [moved] = reorderedVisible.splice(fromIndex, 1)
    reorderedVisible.splice(toIndex, 0, moved)

    let nextVisible = 0
    const merged = jobs.map((job) => (job.hidden ? job : reorderedVisible[nextVisible++]))

    setJobs(merged)
    try {
      setJobs(await api.reorderJobs(merged.map((job) => job.name)))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reorder jobs')
      loadJobs()
    }
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
        <>
          <div className="jobs-page-toolbar">
            <JobVisibilityMenu jobs={jobs} onToggle={handleToggleVisibility} />
          </div>
          <div className="jobs-list">
            {visibleJobs.map((job) => (
              <JobCard
                key={job.name}
                job={job}
                onSaved={handleSaved}
                onRun={handleRun}
                dragging={draggedJob === job.name}
                dragOver={dragOverJob === job.name && draggedJob !== job.name}
                onDragStart={() => setDraggedJob(job.name)}
                onDragEnter={() => draggedJob && draggedJob !== job.name && setDragOverJob(job.name)}
                onDragEnd={handleDragEnd}
                onDrop={() => handleDrop(job.name)}
              />
            ))}
          </div>
        </>
      )}
    </div>
  )
}
