// Shared by ColumnHeaderMenu.tsx (renders the </<=/>/>= condition rows) and
// ReportGrid.tsx (applies them to filteredRows) - split out from either component file
// so both plain import it rather than one importing runtime values from the other,
// which oxlint's react/only-export-components rule flags as a fast-refresh hazard.
export const NUMERIC_FILTER_OPS = ['<', '<=', '>', '>='] as const
export type NumericFilterOp = (typeof NUMERIC_FILTER_OPS)[number]
export type NumericCondition = { op: NumericFilterOp; value: number }

export function isNumericFilterOp(value: unknown): value is NumericFilterOp {
  return typeof value === 'string' && (NUMERIC_FILTER_OPS as readonly string[]).includes(value)
}

export function matchesCondition(value: number, condition: NumericCondition): boolean {
  switch (condition.op) {
    case '<':
      return value < condition.value
    case '<=':
      return value <= condition.value
    case '>':
      return value > condition.value
    case '>=':
      return value >= condition.value
  }
}
