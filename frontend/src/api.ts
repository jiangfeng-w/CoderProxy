// 前端 → Tauri 壳 → relay 的 IPC 封装。
// Rust 侧只暴露 relay（GET/POST 转发 /v1/*）、relay_status、relay_stop、relay_restart。
import { invoke } from '@tauri-apps/api/core'
import { openUrl } from '@tauri-apps/plugin-opener'

export interface RelayStatus {
  running: boolean
  port?: number
}

export interface AuthStatus {
  status: 'logged_in' | 'pending' | 'failed' | 'not_logged_in'
  account?: { id?: string; label?: string }
  error?: string
  authorize_url?: string
  expires_in?: number
}

export interface Config {
  base_url: string
  host: string
  port: number
  api_key: string
  tool_mode: string
  model_whitelist: string[]
  /** 每模型默认思考强度（agent 未显式传思考参数时下发；缺省按 none=关 处理） */
  thinking_defaults: Record<string, string>
  /** agent 不传思考参数时的兜底策略：default=假关（按每模型默认值）/ off=真关（一律关思考） */
  thinking_unset_mode: 'default' | 'off'
}

export interface OaiModel {
  id: string
  name?: string
  context_window?: number | null
  supports_reasoning?: boolean
  /** 思考强度档位（上游 thinkingLevels 的 level，即 reasoning_effort 合法取值） */
  reasoning_efforts?: string[]
  /** 档位 → 上游中文标签（低/高/极 等，各模型文案可能不同） */
  reasoning_labels?: Record<string, string>
  is_multimodal?: boolean
}

export interface MonitorEvent {
  id: number
  ts: string
  kind: string
  data: Record<string, unknown>
}

export type MonitorStats = Record<string, number>

/** 当前鉴权 key（前端 config store 装载/回写后同步）。壳不再持有配置/密钥镜像，
 * 每次 invoke("relay", …) 携带，转发时作为 Bearer 交给 relay 校验（D3）。 */
let _authKey = ''

/** 由 config store 在装载/回写成功后同步（key 的唯一真值 = relay 磁盘持久化值）。 */
export function setAuthKey(key: string): void {
  _authKey = key
}

/** 通用中继：只放行 /v1/*，支持 GET/POST/DELETE。
 * body 用 object（非 Record）：接口入参（如 CustomProviderInput）无索引签名也能直传。 */
export async function relay(method: 'GET' | 'POST' | 'DELETE', path: string, body?: object): Promise<any> {
  return invoke('relay', { method, path, body: body ?? null, apiKey: _authKey })
}

/** Tauri 壳命令。 */
export const relayStatus = (): Promise<RelayStatus> => invoke('relay_status')
export const relayStop = (): Promise<void> => invoke('relay_stop')
export const relayRestart = (): Promise<{ restarting: boolean }> => invoke('relay_restart')

/** 托盘/关窗相关：隐藏主窗口或真正退出。 */
export const windowHide = (): Promise<void> => invoke('window_hide')
export const appExit = (): Promise<void> => invoke('app_exit')

/** /v1/auth/* */
export const authStatus = (): Promise<AuthStatus> => relay('GET', '/v1/auth/status')
export const authLoginStart = (): Promise<AuthStatus> => relay('POST', '/v1/auth/login/start')
export const authLogout = (): Promise<{ status: string }> => relay('POST', '/v1/auth/logout')
export const authSync = (): Promise<{ status: string; models: string[] }> => relay('POST', '/v1/auth/sync')

/** /v1/service — 对 agent 的 OpenAI 服务开关（进程常驻，仅开关 /v1，不动登录/配置/日志底座）。 */
export const serviceStatus = (): Promise<{ enabled: boolean }> => relay('GET', '/v1/service')
export const serviceDisable = (): Promise<{ enabled: boolean; status: string }> => relay('POST', '/v1/service/disable')
export const serviceEnable = (): Promise<{ enabled: boolean; status: string }> => relay('POST', '/v1/service/enable')

/** /v1/auth/config */
export const getConfig = (): Promise<Config> => relay('GET', '/v1/auth/config')
export const updateConfig = (patch: Record<string, unknown>): Promise<Config> => relay('POST', '/v1/auth/config', patch)

/** /v1/models
 * all=true 时请求完整目录（GUI 管理用，不受白名单过滤）；
 * 默认 false 保持 OpenAI 兼容语义（只返回已启用模型）。
 */
export const getModels = (all = false): Promise<{ object: string; data: OaiModel[] }> => relay('GET', all ? '/v1/models?all=1' : '/v1/models')

/** /v1/monitor/* */
export const getStats = (): Promise<MonitorStats> => relay('GET', '/v1/monitor/stats')
export const getEvents = (
  afterId = 0,
  limit = 200
): Promise<{
  events: MonitorEvent[]
  stats: MonitorStats
}> => relay('GET', `/v1/monitor/events?after_id=${afterId}&limit=${limit}`)
export const clearMonitor = (): Promise<{ status: string }> => relay('POST', '/v1/monitor/clear')

export { openUrl }

/** 打开系统浏览器授权页（登录 pending 时用）。 */
export function openAuthorizeUrl(url: string): void {
  void openUrl(url)
}

// ─────────────────────────── /v1/logs（M7：SQLite 持久化历史日志）───────────────────────────

/** M6 落库行：detail 为 JSON 兜底（chat_error 的 error、chat_request 的 tools 等）。 */
export interface LogRow {
  id: number
  ts: string
  kind: string
  model?: string | null
  stream?: number | null
  prompt_tokens?: number | null
  completion_tokens?: number | null
  cached_tokens?: number | null
  reasoning_tokens?: number | null
  total_tokens?: number | null
  duration_ms?: number | null
  detail?: Record<string, unknown> | string | null
}

export interface LogsResult {
  rows: LogRow[]
  total: number
  kinds: string[]
  models: string[]
}

export interface LogsQuery {
  limit?: number
  offset?: number
  kind?: string
  model?: string
  timeFrom?: string // UTC ISO；由筛选控件（本地时间）转换后传入
  timeTo?: string
}

function logsQueryString(q: LogsQuery): string {
  const p = new URLSearchParams()
  if (q.limit !== undefined) p.set('limit', String(q.limit))
  if (q.offset !== undefined) p.set('offset', String(q.offset))
  if (q.kind) p.set('kind', q.kind)
  if (q.model) p.set('model', q.model)
  if (q.timeFrom) p.set('time_from', q.timeFrom)
  if (q.timeTo) p.set('time_to', q.timeTo)
  const s = p.toString()
  return s ? `?${s}` : ''
}

/** 分页 + 多条件筛选；rows 按 id 倒序（最新在前）。 */
export const fetchLogs = (q: LogsQuery = {}): Promise<LogsResult> => relay('GET', `/v1/logs${logsQueryString(q)}`)

/** 类型 distinct（筛选项下拉）。 */
export const fetchLogKinds = (): Promise<{ kinds: string[] }> => relay('GET', '/v1/logs/kinds')

/** 模型 distinct（筛选项下拉）。 */
export const fetchLogModels = (): Promise<{ models: string[] }> => relay('GET', '/v1/logs/models')

/** 按条件清空（无参数 = 全清）。 */
export const deleteLogs = (q: LogsQuery = {}): Promise<{ status: string; deleted: number }> => relay('DELETE', `/v1/logs${logsQueryString(q)}`)

// ─────────────────────────── /v1/providers/custom（供应商页：自定义供应商配置管理）───────────────────────────

/** 自定义供应商（API 响应视图：api_key 永不回显，以 has_api_key 表达「是否已设置」）。 */
export interface CustomProvider {
  id: string
  name: string
  base_url: string
  enabled: boolean
  models: string[]
  has_api_key: boolean
}

/** 新增/更新入参：api_key 仅在显式传入时覆盖（键缺省 = 保留旧值，空串 = 清除）。 */
export interface CustomProviderInput {
  name?: string
  base_url?: string
  api_key?: string
  enabled?: boolean
  models?: string[]
}

export const listCustomProviders = (): Promise<{ providers: CustomProvider[] }> => relay('GET', '/v1/providers/custom')
export const createCustomProvider = (body: CustomProviderInput): Promise<{ provider: CustomProvider }> => relay('POST', '/v1/providers/custom', body)
/** 更新用 POST 而非 PUT：Tauri 壳 relay 转发只放行 GET/POST/DELETE。 */
export const updateCustomProvider = (id: string, body: CustomProviderInput): Promise<{ provider: CustomProvider }> => relay('POST', `/v1/providers/custom/${id}`, body)
export const deleteCustomProvider = (id: string): Promise<{ status: string }> => relay('DELETE', `/v1/providers/custom/${id}`)

// ─────────────────────────── /v1/platforms（WorkBuddy 多账号登录与签到）───────────────────────────

/** 平台元信息（GET /v1/platforms）。 */
export interface PlatformInfo {
  id: string
  display_name: string
  supports_checkin: boolean
  supports_quota: boolean
}

/** WorkBuddy 账号视图（后端不回传任何 token）。 */
/** 逐包配额明细（quota.packages，后端归一：到期已转 CN 时间串，cycle=周期刷新包）。 */
export interface WbQuotaPackage {
  name: string
  total: number
  used: number
  remaining: number
  expire_at: string
  cycle: boolean
}

export interface WbAccount {
  uid: string
  nickname: string
  email: string
  domain: string
  status: string
  checkin: { last_checkin_date?: string; streak_days?: number; today_checked_in?: boolean }
  quota: {
    total?: number
    remaining?: number
    used?: number
    expire_at?: string
    fetched_at?: string
    packages?: WbQuotaPackage[]
  }
}

export interface WbCheckinResult {
  success: boolean
  message: string
  streak_days?: number
  credit?: number | null
  today_checked_in?: boolean
}

export interface WbLoginStart {
  login_id: string
  authorize_url: string
  expires_in: number
}

export interface WbLoginStatus {
  status: 'pending' | 'success' | 'failed'
  uid?: string
  error?: string
}

export const listPlatforms = (): Promise<{ platforms: PlatformInfo[] }> => relay('GET', '/v1/platforms')
export const listWbAccounts = (): Promise<{ accounts: WbAccount[] }> => relay('GET', '/v1/platforms/workbuddy/accounts')
export const wbLoginStart = (): Promise<WbLoginStart> => relay('POST', '/v1/platforms/workbuddy/login/start')
export const wbLoginStatus = (loginId: string): Promise<WbLoginStatus> => relay('GET', `/v1/platforms/workbuddy/login/status?login_id=${encodeURIComponent(loginId)}`)
export const wbLoginCancel = (loginId: string): Promise<{ status: string }> => relay('POST', `/v1/platforms/workbuddy/login/cancel?login_id=${encodeURIComponent(loginId)}`)
export const wbDeleteAccount = (uid: string): Promise<{ status: string }> => relay('DELETE', `/v1/platforms/workbuddy/accounts/${uid}`)
export const wbCheckin = (uid: string): Promise<{ result: WbCheckinResult; account: WbAccount }> => relay('POST', `/v1/platforms/workbuddy/accounts/${uid}/checkin`)
export const wbCheckinStatus = (uid: string): Promise<WbAccount> => relay('GET', `/v1/platforms/workbuddy/accounts/${uid}/checkin-status`)
export const wbRefreshQuota = (uid: string): Promise<WbAccount> => relay('POST', `/v1/platforms/workbuddy/accounts/${uid}/quota`)

// ─────────────────────────── /v1/stats（M8：Token 统计页）───────────────────────────

/** 统计维度：模型 / 类型 / 按天 / 按小时。 */
export type StatsGroupBy = 'model' | 'kind' | 'day' | 'hour'

/** /v1/stats 的聚合行：key 为维度取值，数值均为汇总。 */
export interface StatsRow {
  key: string
  requests: number
  success: number
  failed: number
  prompt_tokens: number
  completion_tokens: number
  cached_tokens: number
  reasoning_tokens: number
  total_tokens: number
}

/** /v1/stats 的未分组总量（前端指标卡合计）。 */
export type StatsTotal = Omit<StatsRow, 'key'>

export interface StatsResult {
  rows: StatsRow[]
  total: StatsTotal
}

export interface StatsQuery {
  groupBy: StatsGroupBy
  model?: string
  kind?: string
  timeFrom?: string // UTC ISO；由筛选控件（本地时间）转换后传入
  timeTo?: string
}

function statsQueryString(q: StatsQuery): string {
  const p = new URLSearchParams()
  p.set('group_by', q.groupBy)
  if (q.model) p.set('model', q.model)
  if (q.kind) p.set('kind', q.kind)
  if (q.timeFrom) p.set('time_from', q.timeFrom)
  if (q.timeTo) p.set('time_to', q.timeTo)
  const s = p.toString()
  return s ? `?${s}` : ''
}

/** 按维度聚合 token/请求数（模型/类型/按天/按小时）。 */
export const fetchStats = (q: StatsQuery): Promise<StatsResult> => relay('GET', `/v1/stats${statsQueryString(q)}`)
