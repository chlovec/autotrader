import type { PredictionHeader } from './api'

// Expected CSV headers - mirror app/main.py's _HIT_RATE_CSV_COLUMNS and
// _PRED_RESULT_CSV_COLUMNS. Checked here too so a wrong file is caught before upload.
export const HIT_RATE_CSV_COLUMNS = [
  'Direction',
  'Ticker',
  'Count of Trading Days',
  'Hit Rate (Count)',
  'Hit Rate (Pcnt)',
  'Pcnt Difference',
]

export const PRED_RESULT_CSV_COLUMNS = [
  'ticker',
  'timestamp',
  'close',
  'prev_timestamp',
  'prev_close',
  'pct_return',
  'p_end',
  'p_start',
  'momentum',
  'volatility',
  'date',
  'pcnt_daily_return',
  'momentum_z',
  'volatility_z',
  'pred',
  'pred_close',
  'pcnt_diff',
]

// Comments can run to several lines - a dropdown option only gets the first,
// shortened, so long notes don't blow out the select's width.
const LABEL_COMMENT_MAX = 60

export function predictionHeaderLabel(header: PredictionHeader): string {
  const lines = header.comments?.trim().split(/\r?\n/) ?? []
  let comment = lines[0]?.trim() ?? ''
  if (comment.length > LABEL_COMMENT_MAX || lines.length > 1) {
    comment = `${comment.slice(0, LABEL_COMMENT_MAX).trimEnd()}…`
  }
  return `#${header.pred_id} — ${header.pred_date}${comment ? ` — ${comment}` : ''}`
}

// Header names from the CSV's first line - just enough parsing for quoted names
// (R's write.csv quotes them), not a general CSV parser.
export function csvHeaderNames(text: string): string[] {
  const firstLine = text.replace(/^﻿/, '').split(/\r?\n/, 1)[0] ?? ''
  const names: string[] = []
  let current = ''
  let quoted = false
  for (let i = 0; i < firstLine.length; i++) {
    const ch = firstLine[i]
    if (quoted) {
      if (ch === '"' && firstLine[i + 1] === '"') {
        current += '"'
        i++
      } else if (ch === '"') {
        quoted = false
      } else {
        current += ch
      }
    } else if (ch === '"') {
      quoted = true
    } else if (ch === ',') {
      names.push(current.trim())
      current = ''
    } else {
      current += ch
    }
  }
  names.push(current.trim())
  return names
}
