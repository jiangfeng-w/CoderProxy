// 模型连通性检测共享逻辑：登录后自动测试 / 手动同步测试复用同一套实现。
// 顺序测试（因上游有并发限制）；失败的写入白名单关闭启用开关，但模型仍保留在列表中。
import { relay, getConfig, updateConfig, type OaiModel } from "./api";
import { store } from "./store";

const DISABLE_ALL = "__none__";

/** 测试单个模型连通性，返回是否成功。 */
export async function testModel(id: string): Promise<boolean> {
  try {
    await relay("POST", "/v1/chat/completions", {
      model: id,
      messages: [{ role: "user", content: "Hi!" }],
      stream: false,
    });
    return true;
  } catch {
    return false;
  }
}

/** 顺序测试所有模型：更新可用状态，失败的写入白名单关闭启用开关。返回失败模型名列表。 */
export async function batchTestAll(models: OaiModel[]): Promise<string[]> {
  const ids = models.map((m) => m.id);
  for (const id of ids) store.modelStatus[id] = "testing";
  const failedIds: string[] = [];
  for (const id of ids) {
    const ok = await testModel(id);
    store.modelStatus[id] = ok ? "available" : "unavailable";
    if (!ok) failedIds.push(id);
  }
  if (failedIds.length === 0) return failedIds;
  try {
    const cfg = await getConfig();
    const w = cfg.model_whitelist ?? [];
    if (w.length === 1 && w[0] === DISABLE_ALL) {
      // 已全部禁用，不动
      return failedIds;
    }
    let next: string[];
    if (w.length === 0) {
      // 全部启用模式：从白名单中排除失败的
      next = ids.filter((x) => !failedIds.includes(x));
      if (next.length === 0) next = [DISABLE_ALL];
    } else {
      next = w.filter((x) => !failedIds.includes(x));
      if (next.length === 0) next = [DISABLE_ALL];
    }
    await updateConfig({ model_whitelist: next });
  } catch {
    /* 白名单写入失败不阻断测试流程 */
  }
  return failedIds;
}
