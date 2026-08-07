import { useState } from 'react'
import { api, type Job } from '../api'

type CancelJobModalProps = {
  job: Job
  onClose: () => void
  onCancelled: () => void
}

// Confirmation dialog popped up by JobCard's stop button. Confirming calls
// api.cancelJob, which requests cancellation in the backend - the run stops at its
// next checkpoint (see backend-v2 jobs/control.py), not necessarily instantly.
export function CancelJobModal({ job, onClose, onCancelled }: CancelJobModalProps) {
  const [cancelling, setCancelling] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleConfirm = async () => {
    setCancelling(true)
    setError(null)
    try {
      await api.cancelJob(job.name)
      onCancelled()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to cancel job')
      setCancelling(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cancel-job-title"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="cancel-job-title" className="modal-title">
          Cancel {job.label}?
        </h2>
        <p className="modal-body">
          This stops the run in progress at its next safe checkpoint. Progress already made (e.g. tickers already
          synced) is kept - it isn't rolled back - but anything not yet reached won't happen this run.
        </p>
        {error && <p className="job-field-error">{error}</p>}
        <div className="modal-actions">
          <button type="button" className="job-button job-button-ghost" onClick={onClose} disabled={cancelling}>
            Keep running
          </button>
          <button type="button" className="job-button job-button-danger" onClick={handleConfirm} disabled={cancelling}>
            {cancelling ? 'Cancelling...' : 'Cancel job'}
          </button>
        </div>
      </div>
    </div>
  )
}
