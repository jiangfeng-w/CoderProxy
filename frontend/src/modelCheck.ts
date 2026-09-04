// 模型连通性检测共享逻辑：登录后自动测试 / 手动同步测试复用同一套实现。
// 顺序测试（因上游有并发限制）。测试只更新连通状态，不修改白名单——
// 启用与否完全由模型页开关决定（测试与启用开关完全解耦）。
import { relay, type OaiModel } from "./api";
import { store } from "./store";

/** 测试单个模型连通性，返回是否成功。 */
export async function testModel(id: string): Promise<boolean> {
  try {
    await relay("POST", "/v1/chat/completions", {
      model: id,
      messages: [{ role: "user", content: "Hi!" }],
      stream: false,
      // probe：GUI 连通性探测专用标记——relay 跳过白名单启用判定真实转发到上游，
      // 否则「先测后启用」对未启用模型会被本地白名单 404 拦截、永远测不通。
      probe: true,
    });
    return true;
  } catch {
    return false;
  }
}

/** 顺序测试所有模型：更新连通状态（success/failure/testing）。返回失败模型名列表。 */
export async function batchTestAll(models: OaiModel[]): Promise<string[]> {
  const ids = models.map((m) => m.id);
  for (const id of ids) store.modelStatus[id] = "testing";
  const failedIds: string[] = [];
  for (const id of ids) {
    const ok = await testModel(id);
    store.modelStatus[id] = ok ? "success" : "failure";
    if (!ok) failedIds.push(id);
  }
  return failedIds;
}
