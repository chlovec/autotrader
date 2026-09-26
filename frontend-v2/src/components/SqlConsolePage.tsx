import { useEffect, useRef, useState } from 'react'
import { api, type AdhocQueryResult } from '../api'
import { loadReportProfiles, upsertReportProfile, deleteReportProfile, type ReportProfile } from '../reportProfiles'

// Leading keyword that doesn't mutate/create/drop anything - matches app/main.py's
// run_adhoc_query, which decides "rows" vs "statement" from CursorResult.returns_rows
// rather than sniffing the SQL text itself. This is only used client-side to decide
// whether the confirm step below is worth showing; it doesn't gate what the backend
// will run.
const READ_ONLY_KEYWORDS = ['select', 'with', 'pragma', 'explain']

// reportProfiles.ts storage key/namespace for this page's saved queries - a browser-
// local, named-preset store (see MarketPredictionsPerformancePage.tsx for the same
// load/update/save-as/delete pattern this page reuses). `view` is always null here:
// that field exists for pages that pair a profile with a ReportGrid's saved column
// layout, which the SQL console's plain results table doesn't have.
const SAVED_QUERIES_ID = 'sql-console'

type SavedSqlParams = { sql: string }

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

  // Named, saved queries - see SAVED_QUERIES_ID above.
  const [savedQueries, setSavedQueries] = useState<ReportProfile<SavedSqlParams>[]>(() =>
    loadReportProfiles<SavedSqlParams>(SAVED_QUERIES_ID),
  )
  const [activeQueryName, setActiveQueryName] = useState<string | null>(null)
  const [savingAsNewQuery, setSavingAsNewQuery] = useState(false)
  const [newQueryNameInput, setNewQueryNameInput] = useState('')
  const [queryJustSaved, setQueryJustSaved] = useState(false)
  const querySavedFlashTimeout = useRef<number | null>(null)
  useEffect(
    () => () => {
      if (querySavedFlashTimeout.current) window.clearTimeout(querySavedFlashTimeout.current)
    },
    [],
  )

  const flashQuerySaved = () => {
    setQueryJustSaved(true)
    if (querySavedFlashTimeout.current) window.clearTimeout(querySavedFlashTimeout.current)
    querySavedFlashTimeout.current = window.setTimeout(() => setQueryJustSaved(false), 1500)
  }

  // Populates the editor from a saved query - never runs it, same "loading never
  // auto-runs" convention as the report pages' own profile loads. Nothing is written
  // back to storage here, so editing afterward can't affect the saved query until
  // Update/Save as... is clicked again.
  const handleLoadQuery = (name: string) => {
    const query = savedQueries.find((q) => q.name === name)
    if (!query) return
    setSql(query.params.sql)
    setActiveQueryName(query.name)
    setResult(null)
    setError(null)
  }

  const handleUpdateActiveQuery = () => {
    if (!activeQueryName) return
    upsertReportProfile<SavedSqlParams>(SAVED_QUERIES_ID, {
      name: activeQueryName,
      params: { sql },
      view: null,
      updatedAt: new Date().toISOString(),
    })
    setSavedQueries(loadReportProfiles<SavedSqlParams>(SAVED_QUERIES_ID))
    flashQuerySaved()
  }

  const handleSaveAsNewQuery = () => {
    const name = newQueryNameInput.trim()
    if (!name) return
    upsertReportProfile<SavedSqlParams>(SAVED_QUERIES_ID, {
      name,
      params: { sql },
      view: null,
      updatedAt: new Date().toISOString(),
    })
    setSavedQueries(loadReportProfiles<SavedSqlParams>(SAVED_QUERIES_ID))
    setActiveQueryName(name)
    setNewQueryNameInput('')
    setSavingAsNewQuery(false)
    flashQuerySaved()
  }

  const handleDeleteActiveQuery = () => {
    if (!activeQueryName) return
    if (!window.confirm(`Delete saved query "${activeQueryName}"?`)) return
    deleteReportProfile(SAVED_QUERIES_ID, activeQueryName)
    setSavedQueries(loadReportProfiles<SavedSqlParams>(SAVED_QUERIES_ID))
    setActiveQueryName(null)
  }

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
    <div className="report-page sql-console-page">
      <h1 className="jobs-page-title">SQL Console</h1>
      <p className="jobs-page-subtitle">
        Run one ad-hoc SQL statement against backend_v2.db. SELECT (and WITH/PRAGMA/EXPLAIN) statements display their
        results below; CREATE/UPDATE/DELETE and other statements run directly against the database and report how
        many rows were affected. There is no undo.
      </p>

      <div className="report-profiles-bar">
        <span className="job-field-label">Saved query</span>
        <select
          className="report-profile-select"
          value={activeQueryName ?? ''}
          onChange={(event) => (event.target.value ? handleLoadQuery(event.target.value) : setActiveQueryName(null))}
        >
          <option value="">— Select a saved query —</option>
          {[...savedQueries]
            .sort((a, b) => a.name.localeCompare(b.name))
            .map((query) => (
              <option key={query.name} value={query.name}>
                {query.name}
              </option>
            ))}
        </select>
        <button type="button" className="job-button" disabled={!activeQueryName} onClick={handleUpdateActiveQuery}>
          {queryJustSaved && !savingAsNewQuery ? 'Saved' : 'Update'}
        </button>
        {savingAsNewQuery ? (
          <>
            <input
              type="text"
              className="report-profile-name-input"
              placeholder="Query name"
              value={newQueryNameInput}
              autoFocus
              onChange={(event) => setNewQueryNameInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault()
                  handleSaveAsNewQuery()
                } else if (event.key === 'Escape') {
                  setSavingAsNewQuery(false)
                  setNewQueryNameInput('')
                }
              }}
            />
            <button
              type="button"
              className="job-button job-button-primary"
              disabled={!newQueryNameInput.trim()}
              onClick={handleSaveAsNewQuery}
            >
              {queryJustSaved ? 'Saved' : 'Confirm'}
            </button>
            <button
              type="button"
              className="job-button"
              onClick={() => {
                setSavingAsNewQuery(false)
                setNewQueryNameInput('')
              }}
            >
              Cancel
            </button>
          </>
        ) : (
          <button type="button" className="job-button" disabled={!sql.trim()} onClick={() => setSavingAsNewQuery(true)}>
            Save as...
          </button>
        )}
        <button
          type="button"
          className="job-button job-button-danger"
          disabled={!activeQueryName}
          onClick={handleDeleteActiveQuery}
        >
          Delete
        </button>
      </div>

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
