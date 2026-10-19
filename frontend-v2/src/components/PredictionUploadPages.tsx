import { useState } from 'react'
import { api, type PredictionResultKind } from '../api'
import { HIT_RATE_CSV_COLUMNS, PRED_RESULT_CSV_COLUMNS } from '../predictionCsv'
import { PredictionCsvUpload } from './PredictionCsvUpload'

export function UploadTickerPredictionsPage() {
  return (
    <div className="report-page">
      <h1 className="jobs-page-title">Upload Ticker Hit Rates</h1>
      <p className="jobs-page-subtitle">Upload a CSV of per-ticker hit rates for a prediction set.</p>
      <PredictionCsvUpload countKey="hit_rate_rows" columns={HIT_RATE_CSV_COLUMNS} upload={api.uploadPredictionHitRates} />
    </div>
  )
}

export function UploadPredictionResultsPage() {
  const [kind, setKind] = useState<PredictionResultKind>('test')
  return (
    <div className="report-page">
      <h1 className="jobs-page-title">Upload Prediction Results</h1>
      <p className="jobs-page-subtitle">Upload a CSV of test or current prediction results for a prediction set.</p>
      <div className="report-controls">
        <div className="prediction-form-stack">
          <label className="job-field">
            <span className="job-field-label">Results type</span>
            <select value={kind} onChange={(event) => setKind(event.target.value as PredictionResultKind)}>
              <option value="test">Test prediction results</option>
              <option value="current">Current prediction results</option>
            </select>
          </label>
        </div>
      </div>
      {/* Keyed by kind so switching type clears any picked file and last result. */}
      <PredictionCsvUpload
        key={kind}
        countKey={kind === 'test' ? 'test_result_rows' : 'current_result_rows'}
        columns={PRED_RESULT_CSV_COLUMNS}
        upload={(predId, file, replace, onProgress) => api.uploadPredictionResults(predId, kind, file, replace, onProgress)}
      />
    </div>
  )
}
