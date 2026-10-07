// 模型连通性手动测试逻辑：模型页点击状态胶囊 / 批量测试入口复用同一套实现。
// 顺序测试（因上游有并发限制）。测试只更新连通状态，不修改白名单——
// 启用与否完全由模型页开关决定（测试与启用开关完全解耦）。
// 本地修订（2026-10-07）：移除「登录拿到模型后自动批量测试」，连通性测试只由用户手动触发。
import { relay } from './api'

/** 测试单个模型连通性，返回是否成功。 */
export async function testModel(id: string): Promise<boolean> {
  try {
    await relay('POST', '/v1/chat/completions', {
      model: id,
      messages: [{ role: 'user', content: 'Hi!' }],
      stream: false,
      // probe：GUI 连通性探测专用标记——relay 跳过白名单启用判定真实转发到上游，
      // 否则「先测后启用」对未启用模型会被本地白名单 404 拦截、永远测不通。
      probe: true
    })
    return true
  } catch {
    return false
  }
}
