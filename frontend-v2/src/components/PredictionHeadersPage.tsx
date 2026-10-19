import { useEffect, useState } from 'react'
import { api, type PredictionHeader, type PredictionHeaderInput } from '../api'
import { predictionHeaderLabel } from '../predictionCsv'

// Form buffer shared by the "new header" form and a row being edited - ref_pred_id is
// kept as the select's string value ('' for none).
type Draft = { pred_date: string; comments: string; ref_pred_id: string }

const EMPTY_DRAFT: Draft = { pred_date: new Date().toISOString().slice(0, 10), comments: '', ref_pred_id: '' }

function toInput(draft: Draft): PredictionHeaderInput {
  return {
    pred_date: draft.pred_date,
    comments: draft.comments.trim() || null,
    ref_pred_id: draft.ref_pred_id === '' ? null : Number(draft.ref_pred_id),
  }
}

function toDraft(header: PredictionHeader): Draft {
  return {
    pred_date: header.pred_date,
    comments: header.comments ?? '',
    ref_pred_id: header.ref_pred_id == null ? '' : String(header.ref_pred_id),
  }
}

type RefSelectProps = {
  value: string
  headers: PredictionHeader[]
  // A header can't reference itself, so it's left out of its own options.
  excludeId?: number
  disabled?: boolean
  onChange: (value: string) => void
}

function RefSelect({ value, headers, excludeId, disabled, onChange }: RefSelectProps) {
  return (
    <select value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
      <option value="">None</option>
      {headers
        .filter((h) => h.pred_id !== excludeId)
        .map((h) => (
          <option key={h.pred_id} value={h.pred_id}>
            {predictionHeaderLabel(h)}
          </option>
        ))}
    </select>
  )
}

export function PredictionHeadersPage() {
  const [headers, setHeaders] = useState<PredictionHeader[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT)
  const [creating, setCreating] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editDraft, setEditDraft] = useState<Draft>(EMPTY_DRAFT)
  const [saving, setSaving] = useState(false)

  const load = () => {
    setLoading(true)
    setError(null)
    api
      .predictionHeaders()
      .then(setHeaders)
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load prediction headers'))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  const create = async () => {
    if (!draft.pred_date) {
      setError('Prediction date is required.')
      return
    }
    setCreating(true)
    setError(null)
    try {
      const created = await api.createPredictionHeader(toInput(draft))
      setHeaders((prev) => [created, ...(prev ?? [])])
      setDraft(EMPTY_DRAFT)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create prediction header')
    } finally {
      setCreating(false)
    }
  }

  const startEdit = (header: PredictionHeader) => {
    setEditingId(header.pred_id)
    setEditDraft(toDraft(header))
    setError(null)
  }

  const saveEdit = async () => {
    if (editingId == null) return
    if (!editDraft.pred_date) {
      setError('Prediction date is required.')
      return
    }
    setSaving(true)
    setError(null)
    try {
      const updated = await api.updatePredictionHeader(editingId, toInput(editDraft))
      setHeaders((prev) => prev?.map((h) => (h.pred_id === updated.pred_id ? updated : h)) ?? prev)
      setEditingId(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save prediction header')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="report-page">
      <h1 className="jobs-page-title">Prediction Headers</h1>
      <p className="jobs-page-subtitle">
        Each prediction set's date, comments and optional reference prediction. Ticker predictions and test/current
        results are uploaded against one of these.
      </p>

      <div className="report-controls">
        <div className="prediction-form-stack">
          <label className="job-field">
            <span className="job-field-label">Prediction date</span>
            <input
              type="date"
              value={draft.pred_date}
              disabled={creating}
              onChange={(event) => setDraft((d) => ({ ...d, pred_date: event.target.value }))}
            />
          </label>
          <label className="job-field">
            <span className="job-field-label">Comments</span>
            <textarea
              rows={4}
              value={draft.comments}
              disabled={creating}
              placeholder="Optional - drag the corner to expand"
              onChange={(event) => setDraft((d) => ({ ...d, comments: event.target.value }))}
            />
          </label>
          <label className="job-field">
            <span className="job-field-label">Reference prediction</span>
            <RefSelect
              value={draft.ref_pred_id}
              headers={headers ?? []}
              disabled={creating}
              onChange={(value) => setDraft((d) => ({ ...d, ref_pred_id: value }))}
            />
          </label>
        </div>
        <div className="report-controls-actions">
          <button type="button" className="job-button job-button-primary" disabled={creating} onClick={create}>
            {creating ? 'Creating...' : 'Create prediction header'}
          </button>
        </div>
      </div>

      {error && <p className="jobs-error">{error}</p>}

      {loading && <p className="placeholder-note">Loading prediction headers...</p>}

      {!loading && headers && headers.length === 0 && <p className="placeholder-note">No prediction headers yet.</p>}

      {!loading && headers && headers.length > 0 && (
        <div className="report-grid">
          <div className="report-table-wrap">
            <table className="job-history-table report-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Prediction Date</th>
                  <th>Comments</th>
                  <th>Reference</th>
                  <th>Ticker Predictions</th>
                  <th>Test Results</th>
                  <th>Current Results</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {headers.map((header) => {
                  const editing = editingId === header.pred_id
                  return (
                    <tr key={header.pred_id}>
                      <td>{header.pred_id}</td>
                      <td>
                        {editing ? (
                          <input
                            type="date"
                            value={editDraft.pred_date}
                            disabled={saving}
                            onChange={(event) => setEditDraft((d) => ({ ...d, pred_date: event.target.value }))}
                          />
                        ) : (
                          header.pred_date
                        )}
                      </td>
                      <td className="prediction-header-comments">
                        {editing ? (
                          <textarea
                            className="prediction-header-comments-input"
                            rows={3}
                            value={editDraft.comments}
                            disabled={saving}
                            onChange={(event) => setEditDraft((d) => ({ ...d, comments: event.target.value }))}
                          />
                        ) : (
                          header.comments ?? '—'
                        )}
                      </td>
                      <td>
                        {editing ? (
                          <RefSelect
                            value={editDraft.ref_pred_id}
                            headers={headers}
                            excludeId={header.pred_id}
                            disabled={saving}
                            onChange={(value) => setEditDraft((d) => ({ ...d, ref_pred_id: value }))}
                          />
                        ) : header.ref_pred_id == null ? (
                          '—'
                        ) : (
                          `#${header.ref_pred_id}`
                        )}
                      </td>
                      <td>{header.hit_rate_rows.toLocaleString()}</td>
                      <td>{header.test_result_rows.toLocaleString()}</td>
                      <td>{header.current_result_rows.toLocaleString()}</td>
                      <td>
                        {editing ? (
                          <div className="report-controls-actions">
                            <button type="button" className="job-button job-button-primary" disabled={saving} onClick={saveEdit}>
                              {saving ? 'Saving...' : 'Save'}
                            </button>
                            <button type="button" className="job-button" disabled={saving} onClick={() => setEditingId(null)}>
                              Cancel
                            </button>
                          </div>
                        ) : (
                          <button
                            type="button"
                            className="job-button"
                            disabled={editingId != null}
                            onClick={() => startEdit(header)}
                          >
                            Edit
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
