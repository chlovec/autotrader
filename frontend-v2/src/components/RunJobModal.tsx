import { useState } from 'react'
import { api, type Job, type JobRunOverrides } from '../api'

type RunJobModalProps = {
  job: Job
  // Whatever JobCard's fields currently show, saved or not - sent as a one-time
  // override for just this run (see api.ts's JobRunOverrides) so a manual run never
  // silently falls back to a stale saved JobConfig field.
  overrides: JobRunOverrides
  onClose: () => void
  onRun: () => void
}

// Confirmation dialog popped up by JobCard's play button. Confirming calls
// api.triggerJob, which fires the job in the backend outside of its normal schedule.
//
// For buy-sell-pattern, a name already used in buy_sell_patterns comes back as a
// 'name-conflict' - the dialog then asks for a different name or confirmation to
// replace that name's rows, and re-sends with whichever was chosen.
export function RunJobModal({ job, overrides, onClose, onRun }: RunJobModalProps) {
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflictName, setConflictName] = useState<string | null>(null)
  const [newName, setNewName] = useState('')

  const trigger = async (runOverrides: JobRunOverrides) => {
    setRunning(true)
    setError(null)
    try {
      const result = await api.triggerJob(job.name, runOverrides)
      if (result.status === 'already-running') {
        setError(`${job.label} is already running.`)
        setRunning(false)
        return
      }
      if (result.status === 'name-conflict') {
        setConflictName(result.name)
        setNewName('')
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

  const handleConfirm = () => trigger(overrides)
  const handleReplace = () =>
    trigger({ ...overrides, buy_sell_pattern_name: conflictName, buy_sell_pattern_replace: true })
  const handleRename = () => trigger({ ...overrides, buy_sell_pattern_name: newName.trim() })

  if (conflictName !== null) {
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
            Name "{conflictName}" already exists
          </h2>
          <p className="modal-body">
            buy_sell_patterns already has rows named "{conflictName}". Enter a different name, or replace the existing
            rows with this run's output.
          </p>
          <label className="job-field">
            New name
            <input type="text" value={newName} onChange={(e) => setNewName(e.target.value)} disabled={running} />
          </label>
          {error && <p className="job-field-error">{error}</p>}
          <div className="modal-actions">
            <button type="button" className="job-button job-button-ghost" onClick={onClose} disabled={running}>
              Cancel
            </button>
            <button type="button" className="job-button" onClick={handleReplace} disabled={running}>
              Replace existing
            </button>
            <button
              type="button"
              className="job-button job-button-primary"
              onClick={handleRename}
              disabled={running || !newName.trim() || newName.trim() === conflictName}
            >
              {running ? 'Starting...' : 'Run with new name'}
            </button>
          </div>
        </div>
      </div>
    )
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
