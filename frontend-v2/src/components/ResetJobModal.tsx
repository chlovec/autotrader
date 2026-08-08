import { useState } from 'react'
import { api, type Job } from '../api'

type ResetJobModalProps = {
  job: Job
  onClose: () => void
  onReset: (job: Job) => void
}

// Confirmation dialog popped up by JobCard's reset (trash) button. Confirming calls
// api.resetJob, which empties the table(s) this job's data lives in (and, for the
// tickers/news jobs, their sync cursor - see backend-v2 app/main.py's reset_job) -
// there's no undo, so this needs its own explicit confirmation step same as cancel.
export function ResetJobModal({ job, onClose, onReset }: ResetJobModalProps) {
  const [resetting, setResetting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleConfirm = async () => {
    setResetting(true)
    setError(null)
    try {
      const updated = await api.resetJob(job.name)
      onReset(updated)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reset job')
      setResetting(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="reset-job-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="reset-job-title" className="modal-title">
          Reset {job.label}?
        </h2>
        <p className="modal-body">
          This permanently empties the data this job has synced or computed. It cannot be undone - the table only
          fills back up the next time this job runs.
        </p>
        {error && <p className="job-field-error">{error}</p>}
        <div className="modal-actions">
          <button type="button" className="job-button job-button-ghost" onClick={onClose} disabled={resetting}>
            Cancel
          </button>
          <button type="button" className="job-button job-button-danger" onClick={handleConfirm} disabled={resetting}>
            {resetting ? 'Resetting...' : 'Reset job'}
          </button>
        </div>
      </div>
    </div>
  )
}
