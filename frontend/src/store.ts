// 全局共享状态：顶栏服务/登录态 + 当前导航页。
import { reactive } from "vue";
import type { RelayStatus, AuthStatus } from "./api";

export const store = reactive({
  relay: { running: false, port: 0, tool_mode: "hybrid" } as RelayStatus,
  auth: { status: "not_logged_in" } as AuthStatus,
  view: "overview" as string,
});
