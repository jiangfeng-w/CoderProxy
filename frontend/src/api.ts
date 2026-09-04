// 前端 → Tauri 壳 → relay 的 IPC 封装。
// Rust 侧只暴露 relay（GET/POST 转发 /v1/*）、relay_status、relay_stop、relay_restart。
import { invoke } from "@tauri-apps/api/core";
import { openUrl } from "@tauri-apps/plugin-opener";

export interface RelayStatus {
  running: boolean;
  port?: number;
  api_key?: string;
  tool_mode?: string;
}

export interface AuthStatus {
  status: "logged_in" | "pending" | "failed" | "not_logged_in";
  account?: { id?: string; label?: string };
  error?: string;
  authorize_url?: string;
  expires_in?: number;
}

export interface Config {
  base_url: string;
  host: string;
  port: number;
  api_key: string;
  tool_mode: string;
  model_whitelist: string[];
}

export interface OaiModel {
  id: string;
  name?: string;
  context_window?: number | null;
  supports_reasoning?: boolean;
  is_multimodal?: boolean;
}

export interface MonitorEvent {
  id: number;
  ts: string;
  kind: string;
  data: Record<string, unknown>;
}

export type MonitorStats = Record<string, number>;

/** 通用中继：只放行 /v1/*，仅 GET/POST。 */
export async function relay(
  method: "GET" | "POST",
  path: string,
  body?: Record<string, unknown>
): Promise<any> {
  return invoke("relay", { method, path, body: body ?? null });
}

/** Tauri 壳命令。 */
export const relayStatus = (): Promise<RelayStatus> => invoke("relay_status");
export const relayStop = (): Promise<void> => invoke("relay_stop");
export const relayRestart = (): Promise<{ restarting: boolean }> =>
  invoke("relay_restart");

/** 托盘/关窗相关：隐藏主窗口或真正退出。 */
export const windowHide = (): Promise<void> => invoke("window_hide");
export const appExit = (): Promise<void> => invoke("app_exit");

/** /v1/auth/* */
export const authStatus = (): Promise<AuthStatus> => relay("GET", "/v1/auth/status");
export const authLoginStart = (): Promise<AuthStatus> => relay("POST", "/v1/auth/login/start");
export const authLogout = (): Promise<{ status: string }> => relay("POST", "/v1/auth/logout");
export const authSync = (): Promise<{ status: string; models: string[] }> =>
  relay("POST", "/v1/auth/sync");

/** /v1/auth/config */
export const getConfig = (): Promise<Config> => relay("GET", "/v1/auth/config");
export const updateConfig = (patch: Record<string, unknown>): Promise<Config> =>
  relay("POST", "/v1/auth/config", patch);

/** /v1/models
 * all=true 时请求完整目录（GUI 管理用，不受白名单过滤）；
 * 默认 false 保持 OpenAI 兼容语义（只返回已启用模型）。
 */
export const getModels = (all = false): Promise<{ object: string; data: OaiModel[] }> =>
  relay("GET", all ? "/v1/models?all=1" : "/v1/models");

/** /v1/monitor/* */
export const getStats = (): Promise<MonitorStats> => relay("GET", "/v1/monitor/stats");
export const getEvents = (afterId = 0, limit = 200): Promise<{
  events: MonitorEvent[];
  stats: MonitorStats;
}> => relay("GET", `/v1/monitor/events?after_id=${afterId}&limit=${limit}`);
export const clearMonitor = (): Promise<{ status: string }> => relay("POST", "/v1/monitor/clear");

export { openUrl };

/** 打开系统浏览器授权页（登录 pending 时用）。 */
export function openAuthorizeUrl(url: string): void {
  void openUrl(url);
}
