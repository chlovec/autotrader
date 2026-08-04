export const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

export interface Position {
  symbol: string
  qty: number
  avg_entry_price: number
  market_value: number
  unrealized_pl: number
}

export interface EquityPoint {
  timestamp: string
  equity: number
  cash: number
}

export interface Trade {
  id: number
  broker_order_id: string
  symbol: string
  side: 'buy' | 'sell'
  qty: number
  fill_price: number | null
  status: string
  submitted_at: string
}

export interface Signal {
  id: number
  symbol: string
  strategy_name: string
  action: 'buy' | 'sell' | 'hold'
  reason: string
  timestamp: string
}

export interface SystemEvent {
  id: number
  account_id: string | null
  level: 'info' | 'warning' | 'error' | 'critical'
  source: string
  message: string
  timestamp: string
}

export interface KillSwitchState {
  engaged: boolean
  reason: string
}

export interface ResearchResult {
  id: number
  run_at: string
  symbol: string
  technical_score: number
  news_score: number
  combined_score: number
  rationale: string
  selected: boolean
}

export interface PaginatedResearchResults {
  items: ResearchResult[]
  total: number
  selected_total: number
  page: number
  page_size: number
}

export interface ResearchScheduleState {
  enabled: boolean
  selected_count: number
}

export interface BlocklistedSymbol {
  symbol: string
  blocklisted_at: string
}

export interface ResearchStatus {
  running: boolean
}

export type TriggerResearchResult = { status: 'started' } | { status: 'already-running' }

export interface AccountSummary {
  id: string
  display_name: string
  broker: string
  active: boolean
  strategy_name: string
  pending_strategy_name: string | null
  equity: number | null
  cash: number | null
  unrealized_pl: number | null
}

export interface AccountDetail {
  id: string
  display_name: string
  broker: string
  active: boolean
  strategy_name: string
  strategy_params: Record<string, unknown>
  pending_strategy_name: string | null
  pending_strategy_params: Record<string, unknown> | null
  max_position_size_usd: number
  max_daily_loss_usd: number
  max_total_exposure_usd: number
  kill_switch_engaged: boolean
  kill_switch_reason: string
}

export interface TradingLimits {
  max_position_size_usd: number
  max_daily_loss_usd: number
  max_total_exposure_usd: number
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`)
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`)
  return res.json()
}

async function postJSON<T>(path: string, params: Record<string, string | number | boolean> = {}, method: string = 'POST'): Promise<T> {
  const query = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)])).toString()
  const res = await fetch(`${API_BASE}${path}${query ? `?${query}` : ''}`, { method })
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`)
  return res.json()
}

async function deleteJSON<T>(path: string, params: Record<string, string | number | boolean> = {}): Promise<T> {
  const query = new URLSearchParams(Object.entries(params).map(([k, v]) => [k, String(v)])).toString()
  const res = await fetch(`${API_BASE}${path}${query ? `?${query}` : ''}`, { method: 'DELETE' })
  if (!res.ok) throw new Error(`${path} failed: ${res.status}`)
  return res.json()
}

export function accountWsUrl(accountId: string): string {
  return `${API_BASE.replace(/^http/, 'ws')}/ws/accounts/${accountId}`
}

export const api = {
  health: () => getJSON<{ status: string }>('/health'),
  events: () => getJSON<SystemEvent[]>('/events'),
  clearEvent: (id: number) => deleteJSON<{ cleared: number }>(`/events/${id}`),
  clearEvents: (scope?: { accountId?: string; unassigned?: boolean }) =>
    deleteJSON<{ cleared: number }>(
      '/events',
      scope?.unassigned ? { unassigned: true } : scope?.accountId ? { account_id: scope.accountId } : {},
    ),
  research: (page: number, pageSize: number) => getJSON<PaginatedResearchResults>(`/research?page=${page}&page_size=${pageSize}`),
  researchSchedule: () => getJSON<ResearchScheduleState>('/research/schedule'),
  setResearchSchedule: (next: ResearchScheduleState) => postJSON<ResearchScheduleState>('/research/schedule', { ...next }),
  researchStatus: () => getJSON<ResearchStatus>('/research/status'),
  triggerResearch: async (): Promise<TriggerResearchResult> => {
    const res = await fetch(`${API_BASE}/research/run`, { method: 'POST' })
    if (res.status === 409) return { status: 'already-running' }
    if (!res.ok) throw new Error(`research trigger failed: ${res.status}`)
    return res.json()
  },

  researchUniverse: () => getJSON<string[]>('/research/universe'),
  blocklist: () => getJSON<BlocklistedSymbol[]>('/blocklist'),
  addToBlocklist: (symbol: string) => postJSON<BlocklistedSymbol>('/blocklist', { symbol }),
  removeFromBlocklist: (symbol: string) => deleteJSON<{ removed: string }>(`/blocklist/${encodeURIComponent(symbol)}`),

  accounts: () => getJSON<AccountSummary[]>('/accounts'),
  account: (id: string) => getJSON<AccountDetail>(`/accounts/${id}`),
  activateAccount: (id: string) => postJSON<{ active: boolean }>(`/accounts/${id}/activate`),
  deactivateAccount: (id: string) => postJSON<{ active: boolean }>(`/accounts/${id}/deactivate`),
  setAccountLimits: (id: string, limits: TradingLimits) =>
    postJSON<TradingLimits>(`/accounts/${id}/limits`, { ...limits }, 'PATCH'),
  setAccountStrategy: (id: string, strategyName: string, strategyParams: Record<string, unknown>, immediate: boolean) =>
    postJSON<AccountDetail>(
      `/accounts/${id}/strategy`,
      { strategy_name: strategyName, strategy_params: JSON.stringify(strategyParams), immediate },
      'PATCH',
    ),
  cancelPendingAccountStrategy: (id: string) => deleteJSON<AccountDetail>(`/accounts/${id}/strategy/pending`),
  accountKillSwitch: (id: string) => getJSON<KillSwitchState>(`/accounts/${id}/kill-switch`),
  setAccountKillSwitch: (id: string, engaged: boolean, reason: string) =>
    postJSON<KillSwitchState>(`/accounts/${id}/kill-switch`, { engaged, reason }),
  accountPositions: (id: string) => getJSON<Position[]>(`/accounts/${id}/positions`),
  accountEquity: (id: string) => getJSON<EquityPoint[]>(`/accounts/${id}/equity`),
  accountTrades: (id: string) => getJSON<Trade[]>(`/accounts/${id}/trades`),
  accountSignals: (id: string) => getJSON<Signal[]>(`/accounts/${id}/signals`),
}
