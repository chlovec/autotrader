import type { Job } from '../api'

type JobSetupModalProps = {
  job: Job
  onClose: () => void
}

// Placeholder popped up by JobCard's play button. Will eventually let a user edit the
// job's schedule or trigger a manual run from here; for now it's just a stand-in so
// the play button has somewhere to go - see the Jobs feature's follow-up iteration.
export function JobSetupModal({ job, onClose }: JobSetupModalProps) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="job-setup-title" onClick={(e) => e.stopPropagation()}>
        <h2 id="job-setup-title" className="modal-title">
          Set up {job.label}
        </h2>
        <p className="modal-body">
          Schedule setup and manual run controls for this job are coming in a future update.
        </p>
        <div className="modal-actions">
          <button type="button" className="job-button" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  )
}
