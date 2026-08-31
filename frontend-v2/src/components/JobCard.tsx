import { useEffect, useState, type FormEvent } from 'react'
import {
  api,
  START_TIME_OPTIONS,
  type Job,
  type JobRun,
  type RunType,
  type ScheduleIntervalUnit,
  type TickerTypeOption,
} from '../api'
import { CancelJobModal } from './CancelJobModal'
import { ChevronIcon, DragHandleIcon, EyeIcon, InfoIcon, PauseIcon, PlayIcon, StopIcon, TrashIcon } from './icons'
import { ResetJobModal } from './ResetJobModal'
import { RunJobModal } from './RunJobModal'
import { SearchableSelect, type SelectOption } from './SearchableSelect'

type JobCardProps = {
  job: Job
  onSaved: (job: Job) => void
  onRun: () => void
  // Drag-to-reorder wiring, owned by JobsPage (it needs every visible card's dragged/
  // drag-over state at once to compute the drop target) - this card only reports drag
  // handle events and renders the resulting dragging/dragOver state, same split as
  // every other JobCard control that mutates shared list state (e.g. onSaved/onRun).
  dragging: boolean
  dragOver: boolean
  onDragStart: () => void
  onDragEnter: () => void
  onDragEnd: () => void
  onDrop: () => void
}

function tickerTypeLabel(t: TickerTypeOption): string {
  const detail = [t.asset_class, t.description].filter(Boolean).join(': ')
  return detail ? `${t.code} — ${detail}` : t.code
}

function searchTickerTypeOptions(q: string): Promise<SelectOption[]> {
  return api.searchTickerTypes(q).then((matches) => matches.map((t) => ({ value: t.code, label: tickerTypeLabel(t) })))
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

// started_at is naive-UTC, same "Z" append as formatTimestamp needs to parse it
// correctly in the browser's local timezone.
function elapsedSeconds(startedAt: string): number {
  return (Date.now() - new Date(`${startedAt}Z`).getTime()) / 1000
}

// Rate-based ETA from elapsed time / units completed so far - the same estimator
// shape a download progress bar uses. No backend field needed: progress_completed/
// progress_total (reported live by the job - see jobs/control.py's
// report_job_progress) plus started_at (already on every JobRun) are enough to derive
// both "time spent" and "time remaining" purely client-side, recomputed on every
// JobsPage poll tick (see JobsPage.tsx's POLL_INTERVAL_MS).
function formatEta(startedAt: string, completed: number, total: number): string {
  if (completed <= 0) return 'estimating...'
  if (completed >= total) return 'finishing up...'
  const remaining = (elapsedSeconds(startedAt) / completed) * (total - completed)
  return formatDuration(remaining)
}

function runStatusLabel(status: JobRun['status']): string {
  if (status === 'in_progress') return 'In progress'
  if (status === 'failed') return 'Failed'
  if (status === 'cancelled') return 'Cancelled'
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
  if (job.running && job.paused) return <span className="job-status-badge paused">Paused</span>
  if (job.running) return <span className="job-status-badge running">Running</span>
  if (!job.last_run) return <span className="job-status-badge idle">Never run</span>
  if (job.last_run.status === 'failed') return <span className="job-status-badge failed">Failed</span>
  if (job.last_run.status === 'cancelled') return <span className="job-status-badge cancelled">Cancelled</span>
  return <span className="job-status-badge succeeded">Succeeded</span>
}

export function JobCard({
  job,
  onSaved,
  onRun,
  dragging,
  dragOver,
  onDragStart,
  onDragEnter,
  onDragEnd,
  onDrop,
}: JobCardProps) {
  const [runType, setRunType] = useState<RunType>(job.run_type)
  const [scheduleIntervalUnit, setScheduleIntervalUnit] = useState<ScheduleIntervalUnit>(job.schedule_interval_unit)
  const [scheduleIntervalValue, setScheduleIntervalValue] = useState(job.schedule_interval_value)
  const [startTime, setStartTime] = useState(job.start_time)
  const [tickerTypes, setTickerTypes] = useState<string[]>(() => parseCsv(job.ticker_types))
  const [tickers, setTickers] = useState<string[]>(() => parseCsv(job.tickers))
  const [multiplier, setMultiplier] = useState(job.multiplier ?? 1)
  const [timespan, setTimespan] = useState(job.timespan ?? 'day')
  const [backfillDays, setBackfillDays] = useState(job.backfill_days ?? 730)
  // Default 1 ("through yesterday") matches backend-v2 jobs/sync_bars.py's
  // DEFAULT_END_DATE_OFFSET_DAYS.
  const [barsEndDateOffsetDays, setBarsEndDateOffsetDays] = useState(job.bars_end_date_offset_days ?? 1)
  const [snapshotTypes, setSnapshotTypes] = useState<string[]>(() => parseCsv(job.snapshot_types))
  const [averageVolumeStartDate, setAverageVolumeStartDate] = useState(job.average_volume_start_date ?? '')
  const [averageVolumeDaysInterval, setAverageVolumeDaysInterval] = useState(job.average_volume_days_interval ?? 50)
  const [backtestStartDate, setBacktestStartDate] = useState(job.backtest_start_date ?? '')
  const [backtestEndDate, setBacktestEndDate] = useState(job.backtest_end_date ?? '')
  const [predictionStartDate, setPredictionStartDate] = useState(job.prediction_start_date ?? '')
  // Default 1 ("tomorrow") matches backend-v2 jobs/predict_market_state.py's
  // DEFAULT_PREDICTED_DATE_OFFSET_DAYS.
  const [predictedDateOffsetDays, setPredictedDateOffsetDays] = useState(job.predicted_date_offset_days ?? 1)
  const [mcmcNumSimulations, setMcmcNumSimulations] = useState(job.mcmc_num_simulations ?? 2000)
  const [ohlcBarsStartDate, setOhlcBarsStartDate] = useState(job.ohlc_bars_start_date ?? '')
  const [ohlcBarsEndDate, setOhlcBarsEndDate] = useState(job.ohlc_bars_end_date ?? '')
  // Default 8000 matches backend-v2 jobs/sync_ohlc_bars.py's DEFAULT_LIMIT.
  const [ohlcBarsLimit, setOhlcBarsLimit] = useState(job.ohlc_bars_limit ?? 8000)
  const [ohlcUpdateStartDate, setOhlcUpdateStartDate] = useState(job.ohlc_update_start_date ?? '')
  const [ohlcUpdateEndDate, setOhlcUpdateEndDate] = useState(job.ohlc_update_end_date ?? '')
  const [lstmTrainStartDate, setLstmTrainStartDate] = useState(job.lstm_train_start_date ?? '')
  const [lstmTrainEndDate, setLstmTrainEndDate] = useState(job.lstm_train_end_date ?? '')
  // Defaults match backend-v2 jobs/lstm_common.py's DEFAULT_EPOCHS/
  // DEFAULT_LOOKBACK_DAYS/DEFAULT_LEARNING_RATE/DEFAULT_BATCH_SIZE.
  const [lstmEpochs, setLstmEpochs] = useState(job.lstm_epochs ?? 5)
  const [lstmLookbackDays, setLstmLookbackDays] = useState(job.lstm_lookback_days ?? 60)
  const [lstmLearningRate, setLstmLearningRate] = useState(job.lstm_learning_rate ?? 0.001)
  const [lstmBatchSize, setLstmBatchSize] = useState(job.lstm_batch_size ?? 256)
  // Default 3 matches backend-v2 jobs/lstm_common.py's DEFAULT_WALKFORWARD_NUM_FOLDS.
  const [lstmWalkforwardNumFolds, setLstmWalkforwardNumFolds] = useState(job.lstm_walkforward_num_folds ?? 3)
  const [lstmModelVersionId, setLstmModelVersionId] = useState(job.lstm_model_version_id ?? '')
  // Default 1.0 matches backend-v2 jobs/prediction_accuracy.py's
  // DEFAULT_PASS_THRESHOLD_STD.
  const [predictionAccuracyPassThresholdStd, setPredictionAccuracyPassThresholdStd] = useState(
    job.prediction_accuracy_pass_threshold_std ?? 1.0,
  )

  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [showHistory, setShowHistory] = useState(false)
  const [history, setHistory] = useState<JobRun[] | null>(null)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [runModalOpen, setRunModalOpen] = useState(false)
  const [cancelModalOpen, setCancelModalOpen] = useState(false)
  const [resetModalOpen, setResetModalOpen] = useState(false)
  const [controlBusy, setControlBusy] = useState(false)
  const [controlError, setControlError] = useState<string | null>(null)
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
        start_time: startTime,
        ...(job.has_ticker_type_filter || job.has_ticker_selector ? { ticker_types: toCsv(tickerTypes) } : {}),
        ...(job.has_ticker_selector ? { tickers: toCsv(tickers) } : {}),
        ...(job.has_bars_fields
          ? {
              multiplier,
              timespan,
              backfill_days: backfillDays,
              bars_end_date_offset_days: barsEndDateOffsetDays,
            }
          : {}),
        ...(job.has_snapshot_type_filter ? { snapshot_types: toCsv(snapshotTypes) } : {}),
        ...(job.has_average_volume_fields
          ? {
              average_volume_start_date: averageVolumeStartDate || null,
              average_volume_days_interval: averageVolumeDaysInterval,
            }
          : {}),
        ...(job.has_backtest_fields
          ? {
              backtest_start_date: backtestStartDate || null,
              backtest_end_date: backtestEndDate || null,
            }
          : {}),
        ...(job.has_prediction_start_date_field ? { prediction_start_date: predictionStartDate || null } : {}),
        ...(job.has_predicted_date_offset_field ? { predicted_date_offset_days: predictedDateOffsetDays } : {}),
        ...(job.has_monte_carlo_fields ? { mcmc_num_simulations: mcmcNumSimulations } : {}),
        ...(job.has_ohlc_bars_fields
          ? {
              ohlc_bars_start_date: ohlcBarsStartDate || null,
              ohlc_bars_end_date: ohlcBarsEndDate || null,
              ohlc_bars_limit: ohlcBarsLimit,
            }
          : {}),
        ...(job.has_ohlc_update_fields
          ? {
              ohlc_update_start_date: ohlcUpdateStartDate || null,
              ohlc_update_end_date: ohlcUpdateEndDate || null,
            }
          : {}),
        ...(job.has_lstm_training_fields
          ? {
              lstm_train_start_date: lstmTrainStartDate || null,
              lstm_train_end_date: lstmTrainEndDate || null,
              lstm_epochs: lstmEpochs,
              lstm_lookback_days: lstmLookbackDays,
              lstm_learning_rate: lstmLearningRate,
              lstm_batch_size: lstmBatchSize,
            }
          : {}),
        ...(job.has_lstm_walkforward_fields ? { lstm_walkforward_num_folds: lstmWalkforwardNumFolds } : {}),
        ...(job.has_lstm_inference_fields
          ? { lstm_model_version_id: lstmModelVersionId === '' ? null : Number(lstmModelVersionId) }
          : {}),
        ...(job.has_prediction_accuracy_fields
          ? { prediction_accuracy_pass_threshold_std: predictionAccuracyPassThresholdStd }
          : {}),
      })
      onSaved(updated)
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  const handlePause = async () => {
    setControlBusy(true)
    setControlError(null)
    try {
      await api.pauseJob(job.name)
      onRun()
    } catch (err) {
      setControlError(err instanceof Error ? err.message : 'Failed to pause job')
    } finally {
      setControlBusy(false)
    }
  }

  const handleResume = async () => {
    setControlBusy(true)
    setControlError(null)
    try {
      await api.resumeJob(job.name)
      onRun()
    } catch (err) {
      setControlError(err instanceof Error ? err.message : 'Failed to resume job')
    } finally {
      setControlBusy(false)
    }
  }

  const handleHide = async () => {
    setControlBusy(true)
    setControlError(null)
    try {
      onSaved(await api.hideJob(job.name))
    } catch (err) {
      setControlError(err instanceof Error ? err.message : 'Failed to hide job')
    } finally {
      setControlBusy(false)
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
    <section
      className={`job-card${dragging ? ' job-card-dragging' : ''}${dragOver ? ' job-card-drag-over' : ''}`}
      onDragOver={(event) => event.preventDefault()}
      onDragEnter={onDragEnter}
      onDrop={(event) => {
        event.preventDefault()
        onDrop()
      }}
    >
      <div className="job-card-header">
        <div className="job-card-header-left">
          <button
            type="button"
            className="icon-button job-drag-handle"
            aria-label={`Drag to reorder ${job.label} job.`}
            draggable
            onDragStart={onDragStart}
            onDragEnd={onDragEnd}
          >
            <DragHandleIcon className="icon" />
          </button>
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
              className="icon-button job-play-button job-cancel-button"
              aria-label={`Reset ${job.label} job.`}
              disabled={controlBusy || job.running}
              onClick={() => setResetModalOpen(true)}
            >
              <TrashIcon className="icon" />
            </button>
            <span className="tooltip-bubble tooltip-bubble-right" role="tooltip">
              Reset {job.label} job - empties its data.
            </span>
          </span>
          <span className="tooltip-anchor">
            <button
              type="button"
              className="icon-button job-play-button"
              aria-label={`Hide ${job.label} job.`}
              disabled={controlBusy}
              onClick={handleHide}
            >
              <EyeIcon className="icon" />
            </button>
            <span className="tooltip-bubble tooltip-bubble-right" role="tooltip">
              Hide {job.label} job.
            </span>
          </span>
          {!job.running && (
            <span className="tooltip-anchor">
              <button
                type="button"
                className="icon-button job-play-button"
                aria-label={`Run ${job.label} job now.`}
                onClick={() => setRunModalOpen(true)}
              >
                <PlayIcon className="icon" />
              </button>
              <span className="tooltip-bubble tooltip-bubble-right" role="tooltip">
                Run {job.label} job now.
              </span>
            </span>
          )}
          {job.running && (
            <>
              <span className="tooltip-anchor">
                <button
                  type="button"
                  className="icon-button job-play-button"
                  aria-label={job.paused ? `Resume ${job.label} job.` : `Pause ${job.label} job.`}
                  disabled={controlBusy}
                  onClick={job.paused ? handleResume : handlePause}
                >
                  {job.paused ? <PlayIcon className="icon" /> : <PauseIcon className="icon" />}
                </button>
                <span className="tooltip-bubble tooltip-bubble-right" role="tooltip">
                  {job.paused ? `Resume ${job.label} job.` : `Pause ${job.label} job.`}
                </span>
              </span>
              <span className="tooltip-anchor">
                <button
                  type="button"
                  className="icon-button job-play-button job-cancel-button"
                  aria-label={`Cancel ${job.label} job.`}
                  disabled={controlBusy}
                  onClick={() => setCancelModalOpen(true)}
                >
                  <StopIcon className="icon" />
                </button>
                <span className="tooltip-bubble tooltip-bubble-right" role="tooltip">
                  Cancel {job.label} job.
                </span>
              </span>
            </>
          )}
        </div>
      </div>
      {job.running && job.last_run?.progress_total != null && job.last_run.progress_completed != null && (
        <div className="job-progress">
          <div className="job-progress-row">
            <progress
              className="job-progress-bar"
              value={job.last_run.progress_completed}
              max={job.last_run.progress_total}
            />
            <span className="job-progress-label">
              {job.last_run.progress_completed.toLocaleString()} / {job.last_run.progress_total.toLocaleString()} (
              {Math.round((job.last_run.progress_completed / job.last_run.progress_total) * 100)}%)
            </span>
          </div>
          <span className="job-progress-timing">
            {formatDuration(elapsedSeconds(job.last_run.started_at))} elapsed · ~
            {formatEta(job.last_run.started_at, job.last_run.progress_completed, job.last_run.progress_total)}{' '}
            remaining
          </span>
        </div>
      )}
      {controlError && <p className="job-field-error job-control-error">{controlError}</p>}

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
                <label className="job-field">
                  Start time (UTC)
                  <select value={startTime} disabled={runType === 'manual'} onChange={(e) => setStartTime(e.target.value)}>
                    {START_TIME_OPTIONS.map((option) => (
                      <option key={option} value={option}>
                        {option}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <p className="job-field-hint">
                The schedule's first run lands on this UTC time of day; every run after that follows "run every"
                above from there - e.g. start 00:15 + every 1 hour fires at :15 past every hour.
              </p>
            </div>

            <div className="job-form-section">
              <h3 className="job-form-section-title">Run parameters</h3>

              {!job.has_ticker_selector && job.has_ticker_type_filter && (
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
                    onSearch={searchTickerTypeOptions}
                    placeholder="Search ticker types..."
                  />
                </div>
              )}

              {!job.has_ticker_selector &&
                !job.has_ticker_type_filter &&
                !job.has_snapshot_type_filter &&
                !job.has_average_volume_fields &&
                !job.has_backtest_fields &&
                !job.has_prediction_start_date_field &&
                !job.has_predicted_date_offset_field &&
                !job.has_monte_carlo_fields &&
                !job.has_ohlc_bars_fields &&
                !job.has_ohlc_update_fields &&
                !job.has_lstm_training_fields &&
                !job.has_lstm_walkforward_fields &&
                !job.has_lstm_inference_fields &&
                !job.has_prediction_accuracy_fields && (
                  <p className="job-field-hint">This job has no run parameters to configure.</p>
                )}

              {job.has_snapshot_type_filter && (
                <>
                  <div className="job-field">
                    <span className="job-field-label">Snapshot types</span>
                    <div className="job-run-type-options">
                      {job.snapshot_type_options.map((type) => (
                        <label key={type} className="job-run-type-option">
                          <input
                            type="checkbox"
                            checked={snapshotTypes.includes(type)}
                            onChange={(e) =>
                              setSnapshotTypes((prev) =>
                                e.target.checked ? [...prev, type] : prev.filter((t) => t !== type),
                              )
                            }
                          />
                          {type}
                        </label>
                      ))}
                    </div>
                  </div>
                  <p className="job-field-hint">Leave every type unchecked to sync all of them.</p>
                </>
              )}

              {job.has_ticker_selector && (
                <>
                  <div className="job-field-row">
                    <div className="job-field">
                      <span className="job-field-label">Ticker types</span>
                      <SearchableSelect
                        multiple
                        selected={tickerTypes}
                        onChange={setTickerTypes}
                        onSearch={searchTickerTypeOptions}
                        placeholder="Search ticker types..."
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
                </>
              )}

              {job.has_bars_fields && (
                <>
                  <div className="job-field-row">
                    <label className="job-field">
                      Multiplier
                      <input
                        type="number"
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
                        min={0}
                        value={backfillDays}
                        onChange={(e) => setBackfillDays(Number(e.target.value))}
                      />
                    </label>
                    <label className="job-field">
                      End date offset (days before today, UTC)
                      <input
                        type="number"
                        min={0}
                        step={1}
                        value={barsEndDateOffsetDays}
                        onChange={(e) => setBarsEndDateOffsetDays(Number(e.target.value))}
                      />
                    </label>
                  </div>
                  <p className="job-field-hint">
                    Syncs through today minus this many days - e.g. 1 (the default) for yesterday, 0 for today.
                  </p>
                </>
              )}

              {job.has_average_volume_fields && (
                <>
                  <div className="job-field-row">
                    <label className="job-field">
                      Start date (UTC)
                      <input
                        type="date"
                        value={averageVolumeStartDate}
                        onChange={(e) => setAverageVolumeStartDate(e.target.value)}
                      />
                    </label>
                    <label className="job-field">
                      Days interval
                      <input
                        type="number"
                        min={1}
                        value={averageVolumeDaysInterval}
                        onChange={(e) => setAverageVolumeDaysInterval(Number(e.target.value))}
                      />
                    </label>
                  </div>
                  <p className="job-field-hint">
                    Leave start date blank to default to yesterday (UTC) at run time. Days interval defaults to 50.
                  </p>
                </>
              )}

              {job.has_backtest_fields && (
                <>
                  <div className="job-field-row">
                    <label className="job-field">
                      Start date (UTC)
                      <input
                        type="date"
                        value={backtestStartDate}
                        onChange={(e) => setBacktestStartDate(e.target.value)}
                      />
                    </label>
                    <label className="job-field">
                      End date (UTC)
                      <input type="date" value={backtestEndDate} onChange={(e) => setBacktestEndDate(e.target.value)} />
                    </label>
                  </div>
                  <p className="job-field-hint">
                    Leave end date blank to default to yesterday (UTC) at run time, and start date blank to default
                    to 90 days before that.
                  </p>
                </>
              )}

              {job.has_prediction_start_date_field && (
                <>
                  <div className="job-field-row">
                    <label className="job-field">
                      Start date (UTC)
                      <input
                        type="date"
                        value={predictionStartDate}
                        onChange={(e) => setPredictionStartDate(e.target.value)}
                      />
                    </label>
                  </div>
                  <p className="job-field-hint">Leave blank to default to tomorrow (UTC) at run time.</p>
                </>
              )}

              {job.has_predicted_date_offset_field && (
                <>
                  <div className="job-field-row">
                    <label className="job-field">
                      Predicted date offset (days from today, UTC)
                      <input
                        type="number"
                        step={1}
                        value={predictedDateOffsetDays}
                        onChange={(e) => setPredictedDateOffsetDays(Number(e.target.value))}
                      />
                    </label>
                  </div>
                  <p className="job-field-hint">
                    Predicted date = today + this many days - e.g. 1 for tomorrow (the default), 0 for today, -1 for
                    yesterday. Shared by both phases of this job: the Markov chain prediction runs first, then a
                    Monte Carlo simulation over that same chain for the same predicted date.
                  </p>
                </>
              )}

              {job.has_monte_carlo_fields && (
                <>
                  <div className="job-field-row">
                    <label className="job-field">
                      Simulated paths
                      <input
                        type="number"
                        min={1}
                        value={mcmcNumSimulations}
                        onChange={(e) => setMcmcNumSimulations(Number(e.target.value))}
                      />
                    </label>
                  </div>
                  <p className="job-field-hint">Simulated paths per ticker defaults to 2000.</p>
                </>
              )}

              {job.has_ohlc_bars_fields && (
                <>
                  <div className="job-field-row">
                    <label className="job-field">
                      Start date (UTC)
                      <input
                        type="date"
                        value={ohlcBarsStartDate}
                        onChange={(e) => setOhlcBarsStartDate(e.target.value)}
                      />
                    </label>
                    <label className="job-field">
                      End date (UTC)
                      <input type="date" value={ohlcBarsEndDate} onChange={(e) => setOhlcBarsEndDate(e.target.value)} />
                    </label>
                    <label className="job-field">
                      Limit
                      <input
                        type="number"
                        min={1}
                        max={10000}
                        value={ohlcBarsLimit}
                        onChange={(e) => setOhlcBarsLimit(Number(e.target.value))}
                      />
                    </label>
                  </div>
                  <p className="job-field-hint">
                    Leave start date blank to default to 2 years before today (UTC), and end date blank to default
                    to today - end date can't be after today. Limit (max 10000) caps how many tickers this run
                    selects; a backlog larger than that needs more than one run to fully catch up.
                  </p>
                </>
              )}

              {job.has_ohlc_update_fields && (
                <>
                  <div className="job-field-row">
                    <label className="job-field">
                      Start date (UTC)
                      <input
                        type="date"
                        value={ohlcUpdateStartDate}
                        onChange={(e) => setOhlcUpdateStartDate(e.target.value)}
                      />
                    </label>
                    <label className="job-field">
                      End date (UTC)
                      <input
                        type="date"
                        value={ohlcUpdateEndDate}
                        onChange={(e) => setOhlcUpdateEndDate(e.target.value)}
                      />
                    </label>
                  </div>
                  <p className="job-field-hint">
                    Both required - unlike every other date field on this page, neither defaults to anything.
                    Every run re-fetches and overwrites this exact range for the selected tickers (or every
                    ticker, if none is selected above), regardless of what's already synced.
                  </p>
                </>
              )}

              {job.has_lstm_training_fields && (
                <>
                  <div className="job-field-row">
                    <label className="job-field">
                      Start date (UTC)
                      <input
                        type="date"
                        value={lstmTrainStartDate}
                        onChange={(e) => setLstmTrainStartDate(e.target.value)}
                      />
                    </label>
                    <label className="job-field">
                      End date (UTC)
                      <input
                        type="date"
                        value={lstmTrainEndDate}
                        onChange={(e) => setLstmTrainEndDate(e.target.value)}
                      />
                    </label>
                  </div>
                  <div className="job-field-row">
                    <label className="job-field">
                      Epochs
                      <input
                        type="number"
                        min={1}
                        value={lstmEpochs}
                        onChange={(e) => setLstmEpochs(Number(e.target.value))}
                      />
                    </label>
                    <label className="job-field">
                      Lookback days
                      <input
                        type="number"
                        min={2}
                        value={lstmLookbackDays}
                        onChange={(e) => setLstmLookbackDays(Number(e.target.value))}
                      />
                    </label>
                  </div>
                  <div className="job-field-row">
                    <label className="job-field">
                      Learning rate
                      <input
                        type="number"
                        min={0}
                        step={0.0001}
                        value={lstmLearningRate}
                        onChange={(e) => setLstmLearningRate(Number(e.target.value))}
                      />
                    </label>
                    <label className="job-field">
                      Batch size
                      <input
                        type="number"
                        min={1}
                        value={lstmBatchSize}
                        onChange={(e) => setLstmBatchSize(Number(e.target.value))}
                      />
                    </label>
                  </div>
                  <p className="job-field-hint">
                    Leave end date blank to default to yesterday (UTC) at run time, and start date blank to default
                    to 730 days before that. Trains a pooled model across every selected ticker at once - not one
                    model per ticker - so scoping down Tickers/Ticker types above is the fastest way to get a
                    first timing comparison against the other LSTM training job.
                  </p>
                </>
              )}

              {job.has_lstm_walkforward_fields && (
                <>
                  <div className="job-field-row">
                    <label className="job-field">
                      Number of folds
                      <input
                        type="number"
                        min={1}
                        value={lstmWalkforwardNumFolds}
                        onChange={(e) => setLstmWalkforwardNumFolds(Number(e.target.value))}
                      />
                    </label>
                  </div>
                  <p className="job-field-hint">
                    Retrains from scratch at this many rolling cutoffs across the date range above and evaluates
                    each on the block of days up to the next cutoff - more folds means a slower but more rigorous
                    run. Only the final fold's model is kept for inference.
                  </p>
                </>
              )}

              {job.has_lstm_inference_fields && (
                <>
                  <div className="job-field-row">
                    <label className="job-field">
                      Model version (optional)
                      <input
                        type="number"
                        min={1}
                        placeholder="Latest"
                        value={lstmModelVersionId}
                        onChange={(e) => setLstmModelVersionId(e.target.value === '' ? '' : Number(e.target.value))}
                      />
                    </label>
                  </div>
                  <p className="job-field-hint">
                    Leave blank to use whichever trained lstm_model_versions row is most recent, from either
                    training job; set it to compare the "holdout" and "walkforward" flavors' predictions
                    head-to-head. See the Predicted date offset field above for which session this run targets.
                  </p>
                </>
              )}

              {job.has_prediction_accuracy_fields && (
                <>
                  <div className="job-field-row">
                    <label className="job-field">
                      Pass threshold (std devs)
                      <input
                        type="number"
                        min={0}
                        step={0.1}
                        value={predictionAccuracyPassThresholdStd}
                        onChange={(e) => setPredictionAccuracyPassThresholdStd(Number(e.target.value))}
                      />
                    </label>
                  </div>
                  <p className="job-field-hint">
                    A source's prediction passes when the actual price lands within this many standard deviations
                    of that source's predicted exit price - the standard deviation used is the ticker's own
                    historical return volatility, not any model's own self-reported confidence. Use Tickers/Ticker
                    types above to scope which tickers get scored.
                  </p>
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

      {runModalOpen && <RunJobModal job={job} onClose={() => setRunModalOpen(false)} onRun={onRun} />}
      {cancelModalOpen && (
        <CancelJobModal job={job} onClose={() => setCancelModalOpen(false)} onCancelled={onRun} />
      )}
      {resetModalOpen && (
        <ResetJobModal job={job} onClose={() => setResetModalOpen(false)} onReset={onSaved} />
      )}
    </section>
  )
}
