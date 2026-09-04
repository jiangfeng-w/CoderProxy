// 全局共享状态：顶栏服务/登录态 + 当前导航页 + 模型可用状态。
import { reactive } from "vue";
import type { RelayStatus, AuthStatus } from "./api";

export const store = reactive({
  relay: { running: false, port: 0, tool_mode: "hybrid" } as RelayStatus,
  auth: { status: "not_logged_in" } as AuthStatus,
  view: "overview" as string,
  /** 模型可用状态：available / unavailable / testing。跨页面共享，不持久化。 */
  modelStatus: {} as Record<string, string>,
  /** 会话级标记：本轮登录后是否已跑过批量测试，跨组件挂载保持，登出时重置。 */
  batchTested: false,
});
