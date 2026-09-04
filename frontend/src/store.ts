// 全局共享状态：顶栏服务/登录态 + 当前导航页 + 模型可用状态。
import { reactive } from "vue";
import type { RelayStatus, AuthStatus } from "./api";

export const store = reactive({
  relay: { running: false, port: 0, tool_mode: "hybrid" } as RelayStatus,
  auth: { status: "not_logged_in" } as AuthStatus,
  /** 对 agent 的 OpenAI /v1 服务是否可用（relay 侧：已登录 且 未手动停止）。 */
  serviceEnabled: false,
  view: "overview" as string,
  /** 模型可用状态：available / unavailable / testing。跨页面共享，不持久化。 */
  modelStatus: {} as Record<string, string>,
  /** 会话级标记：本轮登录后是否已跑过批量测试，跨组件挂载保持，登出时重置。 */
  batchTested: false,
});

/** 工具模式（tool_mode）在页面上的通俗叫法；代码/API 内部仍用原名 hybrid/strict/passthrough。 */
const TOOL_MODE_LABELS: Record<string, string> = {
  hybrid: "智能适配",
  strict: "严格模式",
  passthrough: "原样转发",
};

export function toolModeLabel(mode?: string): string {
  return TOOL_MODE_LABELS[mode ?? ""] ?? mode ?? "—";
}
