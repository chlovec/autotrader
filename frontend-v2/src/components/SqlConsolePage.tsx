import { useState } from 'react'
import { api, type AdhocQueryResult } from '../api'

// Leading keyword that doesn't mutate/create/drop anything - matches app/main.py's
// run_adhoc_query, which decides "rows" vs "statement" from CursorResult.returns_rows
// rather than sniffing the SQL text itself. This is only used client-side to decide
// whether the confirm step below is worth showing; it doesn't gate what the backend
// will run.
const READ_ONLY_KEYWORDS = ['select', 'with', 'pragma', 'explain']

function isReadOnly(sql: string): boolean {
  const firstWord = sql.trim().toLowerCase().match(/^[a-z]+/)?.[0]
  return firstWord != null && READ_ONLY_KEYWORDS.includes(firstWord)
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  return String(value)
}

export function SqlConsolePage() {
  const [sql, setSql] = useState('')
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<AdhocQueryResult | null>(null)
  const [confirming, setConfirming] = useState(false)

  const execute = async () => {
    setRunning(true)
    setError(null)
    try {
      const res = await api.runAdhocQuery(sql)
      setResult(res)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Query failed')
      setResult(null)
    } finally {
      setRunning(false)
    }
  }

  const handleRunClick = () => {
    if (!sql.trim() || running) return
    if (isReadOnly(sql)) {
      void execute()
    } else {
      setConfirming(true)
    }
  }

  const handleConfirm = () => {
    setConfirming(false)
    void execute()
  }

  return (
    <div className="report-page">
      <h1 className="jobs-page-title">SQL Console</h1>
      <p className="jobs-page-subtitle">
        Run one ad-hoc SQL statement against backend_v2.db. SELECT (and WITH/PRAGMA/EXPLAIN) statements display their
        results below; CREATE/UPDATE/DELETE and other statements run directly against the database and report how
        many rows were affected. There is no undo.
      </p>

      {error && <p className="jobs-error">{error}</p>}

      <textarea
        className="sql-console-input"
        value={sql}
        onChange={(event) => setSql(event.target.value)}
        onKeyDown={(event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
            event.preventDefault()
            handleRunClick()
          }
        }}
        placeholder="SELECT * FROM tickers LIMIT 100;"
        spellCheck={false}
        rows={8}
      />

      <div className="sql-console-toolbar">
        <button
          type="button"
          className="job-button job-button-primary"
          onClick={handleRunClick}
          disabled={!sql.trim() || running}
        >
          {running ? 'Running...' : 'Run (⌘/Ctrl + Enter)'}
        </button>
      </div>

      {result && result.kind === 'statement' && (
        <p className="placeholder-note">
          Statement executed successfully.{' '}
          {result.rowcount != null ? `${result.rowcount} row(s) affected.` : ''}
        </p>
      )}

      {result && result.kind === 'rows' && (
        <div className="report-grid">
          <p className="placeholder-note">
            {result.row_count} row(s) returned
            {result.truncated ? ' (truncated - add a LIMIT to your query to see fewer/more rows)' : ''}.
          </p>
          <div className="report-table-wrap">
            <table className="job-history-table report-table">
              <thead>
                <tr>
                  {result.columns.map((col) => (
                    <th key={col}>{col}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {result.rows.length === 0 ? (
                  <tr>
                    <td className="report-empty-cell" colSpan={result.columns.length || 1}>
                      No rows returned.
                    </td>
                  </tr>
                ) : (
                  // Index as key: rows from an arbitrary ad-hoc query have no stable identity.
                  result.rows.map((row, i) => (
                    <tr key={i}>
                      {result.columns.map((col) => (
                        <td key={col}>{formatCell(row[col])}</td>
                      ))}
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {confirming && (
        <div className="modal-backdrop" onClick={() => setConfirming(false)}>
          <div
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="sql-console-confirm-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 id="sql-console-confirm-title" className="modal-title">
              Run this statement?
            </h2>
            <p className="modal-body">
              This doesn't look like a read-only query. It will run directly against backend_v2.db and cannot be
              undone.
            </p>
            <div className="modal-actions">
              <button type="button" className="job-button job-button-ghost" onClick={() => setConfirming(false)}>
                Cancel
              </button>
              <button type="button" className="job-button job-button-danger" onClick={handleConfirm}>
                Run statement
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
