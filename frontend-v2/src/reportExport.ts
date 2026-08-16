// Client-side export for a ReportGrid's currently-visible rows - nothing like this
// existed before (grep found zero CSV/PDF/Blob/download code anywhere in this repo).
// Both exports render each cell through the same `formatCell` the grid already displays
// with (percentages, decimals, "–" for null) so the export matches what's on screen,
// rather than re-deriving a separate "raw value" export format.

import { jsPDF } from 'jspdf'
import autoTable from 'jspdf-autotable'

export type ExportColumn = {
  key: string
  label: string
}

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

// Quotes a field if it contains a comma, quote, or newline, doubling any internal
// quotes - the standard RFC4180 escaping rule.
function csvField(value: string): string {
  if (/[",\n]/.test(value)) return `"${value.replace(/"/g, '""')}"`
  return value
}

export function downloadCsv<T>(
  filename: string,
  columns: ExportColumn[],
  rows: T[],
  formatCell: (row: T, key: Extract<keyof T, string>) => string,
): void {
  const lines = [
    columns.map((col) => csvField(col.label)).join(','),
    ...rows.map((row) =>
      columns.map((col) => csvField(formatCell(row, col.key as Extract<keyof T, string>))).join(','),
    ),
  ]
  triggerDownload(new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' }), filename)
}

export function downloadPdf<T>(
  filename: string,
  title: string,
  columns: ExportColumn[],
  rows: T[],
  formatCell: (row: T, key: Extract<keyof T, string>) => string,
): void {
  // Landscape, since these reports commonly run 15-40 columns wide.
  const doc = new jsPDF({ orientation: 'landscape' })
  doc.setFontSize(12)
  doc.text(title, 14, 12)
  autoTable(doc, {
    startY: 16,
    styles: { fontSize: 7, cellPadding: 2 },
    head: [columns.map((col) => col.label)],
    body: rows.map((row) => columns.map((col) => formatCell(row, col.key as Extract<keyof T, string>))),
  })
  doc.save(filename)
}
