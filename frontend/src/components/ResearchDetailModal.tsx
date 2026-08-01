import { useEffect } from 'react'
import type { ResearchResult } from '../api'

export function ResearchDetailModal({ result, onClose }: { result: ResearchResult; onClose: () => void }) {
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="research-modal-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h3 id="research-modal-title">{result.symbol}</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>

        <dl className="modal-scores">
          <div>
            <dt>Combined score</dt>
            <dd>{result.combined_score.toFixed(1)}</dd>
          </div>
          <div>
            <dt>Technical score</dt>
            <dd>{result.technical_score.toFixed(1)}</dd>
          </div>
          <div>
            <dt>News score</dt>
            <dd>{result.news_score.toFixed(1)}</dd>
          </div>
        </dl>

        <p className="modal-status">
          {result.selected ? 'Selected for the trading watchlist' : 'Not selected this run'} · as of{' '}
          {new Date(result.run_at).toLocaleString()}
        </p>

        <p className="modal-rationale">{result.rationale}</p>
      </div>
    </div>
  )
}
