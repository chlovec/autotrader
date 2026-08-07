import { useState } from 'react'
import { api, type Job } from '../api'

type RunJobModalProps = {
  job: Job
  onClose: () => void
  onRun: () => void
}

// Confirmation dialog popped up by JobCard's play button. Confirming calls
// api.triggerJob, which fires the job in the backend outside of its normal schedule.
export function RunJobModal({ job, onClose, onRun }: RunJobModalProps) {
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleConfirm = async () => {
    setRunning(true)
    setError(null)
    try {
      const result = await api.triggerJob(job.name)
      if (result.status === 'already-running') {
        setError(`${job.label} is already running.`)
        setRunning(false)
        return
      }
      onRun()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start job')
      setRunning(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="run-job-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="run-job-title" className="modal-title">
          Run {job.label} now?
        </h2>
        <p className="modal-body">
          This will manually trigger "{job.label}" right away, outside of its normal schedule. {job.description}
        </p>
        {error && <p className="job-field-error">{error}</p>}
        <div className="modal-actions">
          <button type="button" className="job-button job-button-ghost" onClick={onClose} disabled={running}>
            Cancel
          </button>
          <button type="button" className="job-button job-button-primary" onClick={handleConfirm} disabled={running}>
            {running ? 'Starting...' : 'Run job'}
          </button>
        </div>
      </div>
    </div>
  )
}
