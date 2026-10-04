import { useEffect, useMemo, useRef, useState } from 'react'
import { adhocExportDownloadUrl, api, type AdhocExport, type AdhocExportFormat, type AdhocQueryResult } from '../api'
import { loadReportProfiles, upsertReportProfile, deleteReportProfile, type ReportProfile } from '../reportProfiles'
import { ReportGrid, type ReportColumn } from './ReportGrid'

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

type ResultRow = Record<string, unknown>

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  return String(value)
}

function formatElapsed(ms: number): string {
  if (ms < 1000) return `${ms.toFixed(ms < 10 ? 1 : 0)} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)} s`
  const minutes = Math.floor(ms / 60_000)
  return `${minutes}m ${((ms % 60_000) / 1000).toFixed(1)}s`
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`
}

// A plain link to the saved file rather than fetch + Blob: the backend serves it as an
// attachment, so the browser streams it straight to disk without navigating away or
// holding the whole export in memory.
// Id the backend registers a running query/export under, so cancel_adhoc_query can
// interrupt it. randomUUID needs a secure context (localhost counts), hence the fallback.
function newQueryId(): string {
  return typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function startDownload(id: string): void {
  const anchor = document.createElement('a')
  anchor.href = adhocExportDownloadUrl(id)
  anchor.click()
}

export function SqlConsolePage() {
  const [sql, setSql] = useState('')
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<AdhocQueryResult | null>(null)
  const [confirming, setConfirming] = useState(false)
  // The SQL text that produced `result` - what the export buttons re-run, so editing
  // the textarea after a run can't silently export a different query than the one on
  // screen. runId remounts ReportGrid per run so sort/filter state keyed to the last
  // result's columns doesn't carry over to a query with different ones.
  const [resultSql, setResultSql] = useState('')
  const [runId, setRunId] = useState(0)
  const [exporting, setExporting] = useState<AdhocExportFormat | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const [lastExport, setLastExport] = useState<AdhocExport | null>(null)
  // Id of the query or export currently running on the backend - what the Cancel
  // buttons abort. Only one runs at a time: Run and the export buttons are disabled
  // while either is in flight.
  const [activeQueryId, setActiveQueryId] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState(false)

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
    setExportError(null)
    setLastExport(null)
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
    setExportError(null)
    setLastExport(null)
    const queryId = newQueryId()
    setActiveQueryId(queryId)
    try {
      const res = await api.runAdhocQuery(sql, queryId)
      setResult(res)
      setResultSql(sql)
      setRunId((id) => id + 1)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Query failed')
      setResult(null)
    } finally {
      setRunning(false)
      setActiveQueryId(null)
      setCancelling(false)
    }
  }

  // Interrupts the running query/export on the backend; the request that started it
  // then fails with "Query cancelled." and resets the page through its own finally.
  // If it had already finished (cancelled: false), its result just arrives as normal.
  const handleCancel = async () => {
    if (!activeQueryId || cancelling) return
    setCancelling(true)
    try {
      const { cancelled } = await api.cancelAdhocQuery(activeQueryId)
      if (!cancelled) setCancelling(false)
    } catch {
      setCancelling(false)
    }
  }

  // The export endpoint runs resultSql again with no row cap and saves the result to a
  // file on the backend, so it holds everything the query returns (its own LIMIT, if
  // any, still applies) - not just the rows the grid shows, and not affected by the
  // grid's sort/filter. The file stays downloadable until cleanup-query-exports
  // deletes it.
  const handleExport = async (format: AdhocExportFormat) => {
    setExporting(format)
    setExportError(null)
    const queryId = newQueryId()
    setActiveQueryId(queryId)
    try {
      const saved = await api.exportAdhocQuery(resultSql, format, queryId)
      setLastExport(saved)
      startDownload(saved.id)
    } catch (err) {
      setExportError(err instanceof Error ? err.message : 'Export failed')
    } finally {
      setExporting(null)
      setActiveQueryId(null)
      setCancelling(false)
    }
  }

  const gridColumns = useMemo<ReportColumn<ResultRow>[]>(
    () => (result?.kind === 'rows' ? result.columns.map((col) => ({ key: col, label: col })) : []),
    [result],
  )
  // Ad-hoc rows have no stable identity, so key each one by its position in the
  // result as returned (not as sorted/filtered by the grid).
  const rowKeys = useMemo(
    () => new Map(result?.kind === 'rows' ? result.rows.map((row, i) => [row, String(i)]) : []),
    [result],
  )

  const handleRunClick = () => {
    if (!sql.trim() || running || exporting != null) return
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
          disabled={!sql.trim() || running || exporting != null}
        >
          {running ? 'Running...' : 'Run (⌘/Ctrl + Enter)'}
        </button>
        {running && (
          <button
            type="button"
            className="job-button job-button-danger"
            disabled={cancelling || !activeQueryId}
            onClick={() => void handleCancel()}
          >
            {cancelling ? 'Cancelling...' : 'Cancel query'}
          </button>
        )}
      </div>

      {result && result.kind === 'statement' && (
        <p className="placeholder-note">
          Statement executed successfully in {formatElapsed(result.elapsed_ms)}.{' '}
          {result.rowcount != null ? `${result.rowcount} row(s) affected.` : ''}
        </p>
      )}

      {result && result.kind === 'rows' && (
        <>
          <div className="sql-console-result-bar">
            <p className="placeholder-note">
              {result.row_count} row(s) returned in {formatElapsed(result.elapsed_ms)}
              {result.truncated
                ? ` - showing the first ${result.row_count}; the export includes every row the query returns`
                : ''}
              .
            </p>
            <div className="sql-console-export-buttons">
              <span className="tooltip-anchor">
                <button
                  type="button"
                  className="job-button"
                  disabled={exporting != null || running}
                  onClick={() => void handleExport('csv')}
                >
                  {exporting === 'csv' ? 'Exporting...' : 'Export CSV'}
                </button>
                <span className="tooltip-bubble tooltip-bubble-right" role="tooltip">
                  Re-runs the query and downloads every row it returns as a .csv with a header row - all columns,
                  ignoring the grid's sort, filters and hidden columns.
                </span>
              </span>
              <span className="tooltip-anchor">
                <button
                  type="button"
                  className="job-button"
                  disabled={exporting != null || running}
                  onClick={() => void handleExport('json')}
                >
                  {exporting === 'json' ? 'Exporting...' : 'Export JSON'}
                </button>
                <span className="tooltip-bubble tooltip-bubble-right" role="tooltip">
                  Re-runs the query and downloads every row it returns as a .json array of objects keyed by column
                  name - ignoring the grid's sort, filters and hidden columns.
                </span>
              </span>
              {exporting != null && (
                <button
                  type="button"
                  className="job-button job-button-danger"
                  disabled={cancelling || !activeQueryId}
                  onClick={() => void handleCancel()}
                >
                  {cancelling ? 'Cancelling...' : 'Cancel export'}
                </button>
              )}
            </div>
          </div>
          {exportError && <p className="jobs-error">{exportError}</p>}
          {lastExport && (
            <p className="placeholder-note">
              Exported {lastExport.row_count.toLocaleString()} row(s) to {lastExport.filename} (
              {formatBytes(lastExport.size_bytes)}) in {formatElapsed(lastExport.elapsed_ms)}. It stays on the server
              until about {new Date(lastExport.delete_after).toLocaleString()} -{' '}
              <a href={adhocExportDownloadUrl(lastExport.id)}>download again</a>.
            </p>
          )}
          <ReportGrid<ResultRow>
            key={runId}
            columns={gridColumns}
            rows={result.rows}
            rowKey={(row) => rowKeys.get(row) ?? ''}
            formatCell={(row, key) => formatCell(row[key])}
            emptyMessage="No rows returned."
            copyable
          />
        </>
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
