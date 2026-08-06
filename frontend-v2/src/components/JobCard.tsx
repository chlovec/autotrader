import { useEffect, useState, type FormEvent } from 'react'
import { api, type Job, type JobRun, type RunType, type ScheduleIntervalUnit } from '../api'
import { ChevronIcon, InfoIcon, PlayIcon } from './icons'
import { JobSetupModal } from './JobSetupModal'
import { SearchableSelect, type SelectOption } from './SearchableSelect'

type JobCardProps = {
  job: Job
  onSaved: (job: Job) => void
  tickerTypeOptions: SelectOption[]
}

function parseCsv(value: string | null): string[] {
  if (!value) return []
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

function toCsv(values: string[]): string | null {
  return values.join(',') || null
}

function formatTimestamp(value: string): string {
  return new Date(`${value}Z`).toLocaleString()
}

function formatRunTypeLabel(runType: RunType): string {
  return runType === 'auto' ? 'Auto' : 'Manual'
}

function formatDuration(seconds: number | null): string {
  if (seconds == null) return '–'
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  const remainder = Math.round(seconds % 60)
  return `${minutes}m ${remainder}s`
}

function runStatusLabel(status: JobRun['status']): string {
  if (status === 'in_progress') return 'In progress'
  if (status === 'failed') return 'Failed'
  return 'Completed'
}

// next_run_time is already null for a "manual" job (see app/main.py's
// _next_run_time) - it's an aware ISO timestamp (unlike started_at/finished_at, which
// are naive-UTC and need formatTimestamp's manual "Z" append) straight from the live
// APScheduler job, so this is display-only formatting, no date math.
function formatNextRunTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '–'
}

function StatusBadge({ job }: { job: Job }) {
  if (job.running) return <span className="job-status-badge running">Running</span>
  if (!job.last_run) return <span className="job-status-badge idle">Never run</span>
  if (job.last_run.status === 'failed') return <span className="job-status-badge failed">Failed</span>
  return <span className="job-status-badge succeeded">Succeeded</span>
}

export function JobCard({ job, onSaved, tickerTypeOptions }: JobCardProps) {
  const [runType, setRunType] = useState<RunType>(job.run_type)
  const [scheduleIntervalUnit, setScheduleIntervalUnit] = useState<ScheduleIntervalUnit>(job.schedule_interval_unit)
  const [scheduleIntervalValue, setScheduleIntervalValue] = useState(job.schedule_interval_value)
  const [tickerTypes, setTickerTypes] = useState<string[]>(() => parseCsv(job.ticker_types))
  const [tickers, setTickers] = useState<string[]>(() => parseCsv(job.tickers))
  const [multiplier, setMultiplier] = useState(job.multiplier ?? 1)
  const [timespan, setTimespan] = useState(job.timespan ?? 'day')
  const [backfillDays, setBackfillDays] = useState(job.backfill_days ?? 730)

  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [showHistory, setShowHistory] = useState(false)
  const [history, setHistory] = useState<JobRun[] | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [setupOpen, setSetupOpen] = useState(false)
  const [collapsed, setCollapsed] = useState(true)

  // Discards the cached run history once a new run finishes, so re-expanding shows it
  // instead of a stale list from before the last run finished.
  useEffect(() => {
    setHistory(null)
  }, [job.last_run?.id])

  const handleSave = async (event: FormEvent) => {
    event.preventDefault()
    setSaving(true)
    setSaveError(null)
    try {
      const updated = await api.updateJobConfig(job.name, {
        run_type: runType,
        schedule_interval_unit: scheduleIntervalUnit,
        schedule_interval_value: scheduleIntervalValue,
        ticker_types: toCsv(tickerTypes),
        ...(job.has_bars_fields
          ? {
              tickers: toCsv(tickers),
              multiplier,
              timespan,
              backfill_days: backfillDays,
            }
          : {}),
      })
      onSaved(updated)
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  const toggleHistory = async () => {
    const next = !showHistory
    setShowHistory(next)
    if (next && history === null) {
      setHistoryLoading(true)
      try {
        setHistory(await api.jobRuns(job.name, 10))
      } catch {
        setHistory([])
      } finally {
        setHistoryLoading(false)
      }
    }
  }

  return (
    <section className="job-card">
      <div className="job-card-header">
        <div className="job-card-header-left">
          <button
            type="button"
            className="icon-button job-collapse-button"
            aria-expanded={!collapsed}
            aria-label={collapsed ? `Expand ${job.label}` : `Collapse ${job.label}`}
            onClick={() => setCollapsed((value) => !value)}
          >
            <ChevronIcon className={`icon${collapsed ? ' job-collapse-icon-collapsed' : ''}`} />
          </button>
          <div className="job-card-title-row">
            <h2 className="job-card-title">{job.label}</h2>
            <span className="job-info tooltip-anchor" tabIndex={0}>
              <InfoIcon className="icon job-info-icon" />
              <span className="tooltip-bubble" role="tooltip">
                {job.description}
              </span>
            </span>
          </div>
        </div>
        <div className="job-card-header-actions">
          <StatusBadge job={job} />
          <span className="tooltip-anchor">
            <button
              type="button"
              className="icon-button job-play-button"
              aria-label={`Configure and run ${job.label} job.`}
              onClick={() => setSetupOpen(true)}
            >
              <PlayIcon className="icon" />
            </button>
            <span className="tooltip-bubble tooltip-bubble-right" role="tooltip">
              Configure and run {job.label} job.
            </span>
          </span>
        </div>
      </div>

      {!collapsed && (
        <>
          <dl className="job-last-run-grid">
            <div className="job-last-run-item">
              <dt>Last run mode</dt>
              <dd>{job.last_run ? formatRunTypeLabel(job.last_run.trigger) : '–'}</dd>
            </div>
            <div className="job-last-run-item">
              <dt>Last run start</dt>
              <dd>{job.last_run ? formatTimestamp(job.last_run.started_at) : '–'}</dd>
            </div>
            <div className="job-last-run-item">
              <dt>Last run status</dt>
              <dd>{job.last_run ? runStatusLabel(job.last_run.status) : '–'}</dd>
            </div>
            <div className="job-last-run-item">
              <dt>Last run duration</dt>
              <dd>{job.last_run ? formatDuration(job.last_run.duration_seconds) : '–'}</dd>
            </div>
            <div className="job-last-run-item">
              <dt>Next scheduled run</dt>
              <dd>{formatNextRunTime(job.next_run_time)}</dd>
            </div>
          </dl>
          {job.last_run?.status === 'failed' && job.last_run.error && (
            <p className="job-last-run-error">{job.last_run.error}</p>
          )}
          {job.last_run?.status === 'completed' && job.last_run.result_summary && (
            <p className="job-last-run">{job.last_run.result_summary}</p>
          )}

          <form className="job-card-form" onSubmit={handleSave}>
            <div className="job-form-section">
              <h3 className="job-form-section-title">Schedule</h3>

              <div className="job-field job-field-run-type">
                <span className="job-field-label">Run type</span>
                <div className="job-run-type-options">
                  <label className="job-run-type-option">
                    <input
                      type="radio"
                      name={`run-type-${job.name}`}
                      checked={runType === 'manual'}
                      onChange={() => setRunType('manual')}
                    />
                    Manual
                  </label>
                  <label className="job-run-type-option">
                    <input
                      type="radio"
                      name={`run-type-${job.name}`}
                      checked={runType === 'auto'}
                      onChange={() => setRunType('auto')}
                    />
                    Auto
                  </label>
                </div>
              </div>
              <p className="job-field-hint">
                Manual jobs can only be run from this dashboard. Auto jobs also run on the schedule below.
              </p>

              <div className="job-field-row">
                <label className="job-field">
                  Run every
                  <input
                    type="number"
                    min={1}
                    value={scheduleIntervalValue}
                    disabled={runType === 'manual'}
                    onChange={(e) => setScheduleIntervalValue(Number(e.target.value))}
                  />
                </label>
                <label className="job-field">
                  Interval unit
                  <select
                    value={scheduleIntervalUnit}
                    disabled={runType === 'manual'}
                    onChange={(e) => setScheduleIntervalUnit(e.target.value as ScheduleIntervalUnit)}
                  >
                    <option value="minutes">Minutes</option>
                    <option value="hours">Hours</option>
                    <option value="days">Days</option>
                  </select>
                </label>
              </div>
            </div>

            <div className="job-form-section">
              <h3 className="job-form-section-title">Run parameters</h3>

              {!job.has_bars_fields && (
                // A plain div, not <label> - a native <label> forwards clicks on any
                // non-form-control descendant (like a dropdown <li>) to the first
                // labelable element inside it (button, input, ...) in DOM order. Once
                // a chip's remove <button> existed before the <input> in that order,
                // clicking an unrelated dropdown option was being silently replayed as
                // a click on that button too, undoing the just-made selection.
                <div className="job-field">
                  <span className="job-field-label">Ticker type</span>
                  <SearchableSelect
                    multiple={false}
                    selected={tickerTypes}
                    onChange={setTickerTypes}
                    options={tickerTypeOptions}
                    placeholder="Any type"
                  />
                </div>
              )}

              {job.has_bars_fields && (
                <>
                  <div className="job-field-row">
                    <div className="job-field">
                      <span className="job-field-label">Ticker types</span>
                      <SearchableSelect
                        multiple
                        selected={tickerTypes}
                        onChange={setTickerTypes}
                        options={tickerTypeOptions}
                        placeholder="Any type"
                      />
                    </div>
                    <div className="job-field">
                      <span className="job-field-label">Tickers</span>
                      <SearchableSelect
                        multiple
                        selected={tickers}
                        onChange={setTickers}
                        onSearch={(q) =>
                          api
                            .searchTickers(q)
                            .then((matches) =>
                              matches.map((t) => ({
                                value: t.ticker,
                                label: t.name ? `${t.ticker} — ${t.name}` : t.ticker,
                              })),
                            )
                        }
                        placeholder="Search tickers..."
                      />
                    </div>
                  </div>
                  <p className="job-field-hint">
                    Leave both blank to sync every ticker. Specify one or the other, not both.
                  </p>

                  <div className="job-field-row">
                    <label className="job-field">
                      Multiplier
                      <input
                        type="number"
                        min={1}
                        value={multiplier}
                        onChange={(e) => setMultiplier(Number(e.target.value))}
                      />
                    </label>
                    <label className="job-field">
                      Timespan
                      <input type="text" value={timespan} onChange={(e) => setTimespan(e.target.value)} />
                    </label>
                    <label className="job-field">
                      Backfill days
                      <input
                        type="number"
                        min={1}
                        value={backfillDays}
                        onChange={(e) => setBackfillDays(Number(e.target.value))}
                      />
                    </label>
                  </div>
                </>
              )}
            </div>

            {saveError && <p className="job-field-error">{saveError}</p>}

            <div className="job-card-actions">
              <button type="submit" className="job-button" disabled={saving}>
                {saving ? 'Saving...' : 'Save'}
              </button>
              <button type="button" className="job-button job-button-ghost" onClick={toggleHistory}>
                {showHistory ? 'Hide history' : 'Show history'}
              </button>
            </div>
          </form>

          {showHistory && (
            <div className="job-history">
              {historyLoading && <p className="placeholder-note">Loading history...</p>}
              {!historyLoading && history && history.length === 0 && <p className="placeholder-note">No runs yet.</p>}
              {!historyLoading && history && history.length > 0 && (
                <table className="job-history-table">
                  <thead>
                    <tr>
                      <th>Started</th>
                      <th>Mode</th>
                      <th>Status</th>
                      <th>Duration</th>
                      <th>Result</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((run) => (
                      <tr key={run.id}>
                        <td>{formatTimestamp(run.started_at)}</td>
                        <td>{formatRunTypeLabel(run.trigger)}</td>
                        <td>{runStatusLabel(run.status)}</td>
                        <td>{formatDuration(run.duration_seconds)}</td>
                        <td>{run.status === 'failed' ? run.error : run.result_summary}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </>
      )}

      {setupOpen && <JobSetupModal job={job} onClose={() => setSetupOpen(false)} />}
    </section>
  )
}
