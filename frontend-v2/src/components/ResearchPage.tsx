import { useEffect, useState } from 'react'
import { api, type JobRun, type ResearchPickRow, type ResearchPicksResult, type TriggerJobResult } from '../api'
import { ReportGrid, type ReportColumn } from './ReportGrid'

const RESEARCH_PICKS_JOB_NAME = 'research-picks'

const COLUMNS: ReportColumn<ResearchPickRow>[] = [
  { key: 'rank', label: 'Rank' },
  { key: 'ticker', label: 'Ticker' },
  { key: 'name', label: 'Name' },
  { key: 'predicted_direction', label: 'Direction' },
  { key: 'markov_expected_return', label: 'Markov Expected Return' },
  { key: 'mcmc_expected_return', label: 'MCMC Expected Return' },
  { key: 'markov_state_confidence', label: 'Markov Confidence' },
  { key: 'mcmc_state_confidence', label: 'MCMC Confidence' },
  { key: 'market_cap', label: 'Market Cap' },
  { key: 'average_volume', label: 'Average Volume' },
  { key: 'markov_win_rate', label: 'Markov Win Rate' },
  { key: 'mcmc_win_rate', label: 'MCMC Win Rate' },
  { key: 'backtest_win_rate', label: 'Backtest Win Rate' },
  { key: 'rsi_value', label: 'RSI' },
  { key: 'news_sentiment_lean', label: 'News Sentiment Lean' },
  { key: 'score', label: 'Score' },
  { key: 'comment', label: 'Comment' },
]

const PERCENT_FIELDS = new Set<keyof ResearchPickRow>([
  'markov_expected_return',
  'mcmc_expected_return',
  'markov_state_confidence',
  'mcmc_state_confidence',
  'markov_win_rate',
  'mcmc_win_rate',
  'backtest_win_rate',
])

function formatCell(row: ResearchPickRow, key: keyof ResearchPickRow): string {
  const value = row[key]
  if (value == null) return '–'
  if (PERCENT_FIELDS.has(key)) return `${(Number(value) * 100).toFixed(2)}%`
  if (key === 'score') return Number(value).toFixed(3)
  if (typeof value === 'number') return value.toLocaleString(undefined, { maximumFractionDigits: 4 })
  return String(value)
}

function rowKey(row: ResearchPickRow): string {
  return row.ticker
}

function formatGeneratedAt(value: string | null): string {
  return value ? new Date(`${value}Z`).toLocaleString() : '–'
}

function formatRunOption(run: JobRun): string {
  const started = new Date(`${run.started_at}Z`).toLocaleString()
  return `${started} (${run.status})`
}

export function ResearchPage() {
  const [result, setResult] = useState<ResearchPicksResult | null>(null)
  const [runs, setRuns] = useState<JobRun[]>([])
  const [selectedRunId, setSelectedRunId] = useState<number | ''>('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [triggering, setTriggering] = useState(false)
  const [triggerMessage, setTriggerMessage] = useState<string | null>(null)

  const loadPicks = async (runId?: number) => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.researchPicks(runId)
      setResult(data)
      if (data.run_id != null) setSelectedRunId(data.run_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load research picks')
    } finally {
      setLoading(false)
    }
  }

  const loadRuns = async () => {
    try {
      setRuns(await api.jobRuns(RESEARCH_PICKS_JOB_NAME, 20))
    } catch {
      setRuns([])
    }
  }

  useEffect(() => {
    loadPicks()
    loadRuns()
  }, [])

  const handleRunSelect = (value: string) => {
    if (!value) return
    const runId = Number(value)
    setSelectedRunId(runId)
    loadPicks(runId)
  }

  const handleRunNow = async () => {
    setTriggering(true)
    setTriggerMessage(null)
    try {
      const outcome: TriggerJobResult = await api.triggerJob(RESEARCH_PICKS_JOB_NAME)
      setTriggerMessage(
        outcome.status === 'already-running' ? 'Already running - check back shortly.' : 'Started - check back shortly.',
      )
    } catch (err) {
      setTriggerMessage(err instanceof Error ? err.message : 'Failed to start the job')
    } finally {
      setTriggering(false)
    }
  }

  return (
    <div className="report-page">
      <h1 className="jobs-page-title">Research</h1>
      <p className="jobs-page-subtitle">
        A manually-triggered shortlist of up to 20 liquid common-stock candidates whose Markov and Monte Carlo
        predictions agree on direction, scored by a composite of expected-return magnitude, confidence, win-rate/
        backtest track record, RSI, and recent news sentiment. This is a research shortlist for further
        investigation - not a trading signal.
      </p>

      <div className="report-controls">
        <div className="report-controls-actions">
          <button type="button" className="job-button job-button-primary" disabled={triggering} onClick={handleRunNow}>
            {triggering ? 'Starting...' : 'Run now'}
          </button>
          <div className="job-field">
            <span className="job-field-label">View past run</span>
            <select value={selectedRunId} onChange={(event) => handleRunSelect(event.target.value)}>
              <option value="">Latest</option>
              {runs.map((run) => (
                <option key={run.id} value={run.id}>
                  {formatRunOption(run)}
                </option>
              ))}
            </select>
          </div>
        </div>
        {triggerMessage && <p className="placeholder-note">{triggerMessage}</p>}
      </div>

      {error && <p className="jobs-error">{error}</p>}

      {!loading && result && (
        <>
          <p className="placeholder-note">
            Run {result.run_id ?? '–'} · generated {formatGeneratedAt(result.generated_at)} · {result.rows.length} pick(s)
          </p>
          <ReportGrid
            columns={COLUMNS}
            rows={result.rows}
            rowKey={rowKey}
            formatCell={formatCell}
            emptyMessage="No research picks yet - run the job from the button above."
            storageKey="research-picks"
          />
        </>
      )}

      {loading && <p className="placeholder-note">Loading...</p>}
    </div>
  )
}
