import { useEffect, useRef, useState } from 'react'
import { api, MAX_PREDICTION_UPLOAD_BYTES, type PredictionHeader, type PredictionUploadResult } from '../api'
import { csvHeaderNames, predictionHeaderLabel } from '../predictionCsv'

type PredictionCsvUploadProps = {
  // Which count on PredictionHeader reflects this upload's target table.
  countKey: 'hit_rate_rows' | 'test_result_rows' | 'current_result_rows'
  columns: string[]
  upload: (
    predId: number,
    file: File,
    replace: boolean,
    onProgress: (fraction: number) => void,
  ) => Promise<PredictionUploadResult>
}

const MAX_UPLOAD_MB = MAX_PREDICTION_UPLOAD_BYTES / (1024 * 1024)

function formatMB(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function PredictionCsvUpload({ countKey, columns, upload }: PredictionCsvUploadProps) {
  const [headers, setHeaders] = useState<PredictionHeader[] | null>(null)
  const [predId, setPredId] = useState<number | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [replace, setReplace] = useState(false)
  const [uploading, setUploading] = useState(false)
  // Fraction of the file sent so far, or null when no upload is in flight.
  const [progress, setProgress] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<PredictionUploadResult | null>(null)
  const fileInput = useRef<HTMLInputElement | null>(null)
  const [dragActive, setDragActive] = useState(false)
  // dragenter/dragleave also fire for the drop zone's child elements - counting them
  // keeps the highlight from flickering off while the pointer moves over the text.
  const dragDepth = useRef(0)

  // Both the file input and a drop land here. The input's `accept` doesn't filter
  // drops, so the extension is checked here too.
  const pickFile = (next: File | null) => {
    setResult(null)
    if (next && !next.name.toLowerCase().endsWith('.csv')) {
      setError(`${next.name} isn't a .csv file.`)
      return
    }
    if (next && next.size > MAX_PREDICTION_UPLOAD_BYTES) {
      setError(`${next.name} is ${formatMB(next.size)} - the upload limit is ${MAX_UPLOAD_MB} MB.`)
      return
    }
    setError(null)
    setFile(next)
  }

  const loadHeaders = () =>
    api
      .predictionHeaders()
      .then((data) => {
        setHeaders(data)
        setPredId((current) => current ?? data[0]?.pred_id ?? null)
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load prediction headers'))

  useEffect(() => {
    loadHeaders()
  }, [])

  // A file dropped just outside the drop zone would otherwise make the browser open
  // it and navigate away from the page.
  useEffect(() => {
    const block = (event: DragEvent) => {
      if (event.dataTransfer?.types.includes('Files')) event.preventDefault()
    }
    window.addEventListener('dragover', block)
    window.addEventListener('drop', block)
    return () => {
      window.removeEventListener('dragover', block)
      window.removeEventListener('drop', block)
    }
  }, [])

  const selected = headers?.find((h) => h.pred_id === predId) ?? null

  const submit = async () => {
    if (predId == null || !file) return
    setError(null)
    setResult(null)
    if (file.size > MAX_PREDICTION_UPLOAD_BYTES) {
      setError(`${file.name} is ${formatMB(file.size)} - the upload limit is ${MAX_UPLOAD_MB} MB.`)
      return
    }
    setUploading(true)
    setProgress(0)
    try {
      // Only the start of the file is read here, for the header-row check - the
      // upload itself streams the file from disk.
      const present = new Set(csvHeaderNames(await file.slice(0, 64 * 1024).text()))
      const missing = columns.filter((name) => !present.has(name))
      if (missing.length) {
        setError(`CSV is missing column(s): ${missing.join(', ')}`)
        return
      }
      setResult(await upload(predId, file, replace, setProgress))
      setFile(null)
      if (fileInput.current) fileInput.current.value = ''
      loadHeaders()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setUploading(false)
      setProgress(null)
    }
  }

  if (headers && headers.length === 0) {
    return <p className="placeholder-note">No prediction headers yet. Create one on the Prediction Headers page first.</p>
  }

  return (
    <>
      <div className="report-controls">
        <div className="prediction-form-stack">
          <label className="job-field">
            <span className="job-field-label">Prediction</span>
            <select
              value={predId ?? ''}
              disabled={!headers || uploading}
              onChange={(event) => setPredId(Number(event.target.value))}
            >
              {headers?.map((header) => (
                <option key={header.pred_id} value={header.pred_id}>
                  {predictionHeaderLabel(header)}
                </option>
              ))}
            </select>
          </label>
          <div className="job-field">
            <span className="job-field-label">CSV file</span>
            <div
              role="button"
              tabIndex={uploading ? -1 : 0}
              aria-disabled={uploading}
              aria-label={file ? `Selected file ${file.name}. Click to choose a different file.` : 'Choose a CSV file'}
              className={`csv-drop-zone${dragActive ? ' active' : ''}${uploading ? ' disabled' : ''}`}
              onClick={() => !uploading && fileInput.current?.click()}
              onKeyDown={(event) => {
                if (!uploading && (event.key === 'Enter' || event.key === ' ')) {
                  event.preventDefault()
                  fileInput.current?.click()
                }
              }}
              onDragEnter={(event) => {
                event.preventDefault()
                if (uploading) return
                dragDepth.current++
                setDragActive(true)
              }}
              onDragOver={(event) => {
                event.preventDefault()
                event.dataTransfer.dropEffect = uploading ? 'none' : 'copy'
              }}
              onDragLeave={() => {
                dragDepth.current = Math.max(0, dragDepth.current - 1)
                if (dragDepth.current === 0) setDragActive(false)
              }}
              onDrop={(event) => {
                event.preventDefault()
                dragDepth.current = 0
                setDragActive(false)
                if (!uploading) pickFile(event.dataTransfer.files?.[0] ?? null)
              }}
            >
              {file ? (
                <>
                  <span className="csv-drop-zone-file">{file.name}</span>
                  <span className="csv-drop-zone-hint">
                    {formatMB(file.size)} · drop another file or click to replace
                  </span>
                </>
              ) : (
                <>
                  <span className="csv-drop-zone-title">Drag and drop a CSV file here</span>
                  <span className="csv-drop-zone-hint">or click to browse · up to {MAX_UPLOAD_MB} MB</span>
                </>
              )}
            </div>
            <input
              ref={fileInput}
              type="file"
              accept=".csv,text/csv"
              hidden
              disabled={uploading}
              onChange={(event) => pickFile(event.target.files?.[0] ?? null)}
            />
          </div>
          <div className="job-field report-checkbox-field">
            <span className="job-field-label">Existing rows</span>
            <label className="job-run-type-option">
              <input type="checkbox" checked={replace} disabled={uploading} onChange={(event) => setReplace(event.target.checked)} />
              Replace this prediction's existing rows
            </label>
          </div>
        </div>
        {selected && (
          <p className="jobs-page-subtitle">
            {selected[countKey].toLocaleString()} row(s) already uploaded for this prediction.{' '}
            {replace ? 'They will be deleted and replaced by this file.' : 'Matching rows will be overwritten; others are kept.'}
          </p>
        )}
        <div className="report-controls-actions">
          <button
            type="button"
            className="job-button job-button-primary"
            disabled={predId == null || !file || uploading}
            onClick={submit}
          >
            {uploading ? 'Uploading...' : 'Upload'}
          </button>
        </div>
        {progress != null && file && (
          <div className="job-progress">
            <div className="job-progress-row">
              {/* Indeterminate (no value) once sent - the server is then validating and inserting. */}
              <progress className="job-progress-bar" value={progress < 1 ? progress : undefined} max={1} />
              <span className="job-progress-label">
                {progress < 1
                  ? `Sending ${formatMB(file.size * progress)} of ${formatMB(file.size)} (${Math.floor(progress * 100)}%)`
                  : 'Sent - validating and saving rows (a large file can take a few minutes)...'}
              </span>
            </div>
          </div>
        )}
      </div>

      {/* The server lists one bad row per line. */}
      {error && <p className="jobs-error prediction-upload-error">{error}</p>}
      {result && (
        <p className="jobs-success">
          Uploaded {result.rows.toLocaleString()} row(s) across {result.tickers.toLocaleString()} ticker(s) to prediction #
          {result.pred_id}
          {result.replaced ? `, replacing ${result.replaced.toLocaleString()} existing row(s)` : ''}.
        </p>
      )}

      <p className="jobs-page-subtitle">
        Up to {MAX_UPLOAD_MB} MB. Expected columns (extra columns are ignored; blank, NA and NaN are stored as empty):{' '}
        {columns.join(', ')}
      </p>
    </>
  )
}
