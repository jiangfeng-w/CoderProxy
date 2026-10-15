# dev 与安装版并行运行（开发身份隔离）

> **状态**：方案已定稿（2026-10-16；用户拍板：**单独立项小 spec** + **含端口引导**）
> **优先级**：交付（开发体验；对最终用户零影响）
> **来源**：用户需求「dev 和已安装版的单实例分开——安装版在跑反代供 agent 用模型时，启动 dev 测试会被挡；但安装版又不能关（模型在跑）」
> **前置**：M5 单实例（tauri-plugin-single-instance）/ M6 数据目录指针（`data_path.txt`）/ M10 数据目录查看修改

## 1. 背景与根因（已核到源码级，非推测）

现象：安装版运行中，`tauri dev` 启动失败。逐条根因：

| # | 事实 | 证据 |
|---|---|---|
| 1 | 单实例互斥键 = **identifier 派生**：`CreateMutexW("{identifier}-sim")`；第二实例向第一实例窗口发 `WM_COPYDATA` 后 **直接 `exit(0)`** | `tauri-plugin-single-instance-2.4.4/src/platform_impl/windows.rs`（互斥名构造 / 二实例处置）；`semver` feature 未启用（互斥名无版本后缀） |
| 2 | dev 与安装版**共用 identifier** `com.coderproxy.desktop` → 同一把锁 | `src-tauri/tauri.conf.json`；`lib.rs` 注册插件处 |
| 3 | **只去掉插件也不行**：`app_data_dir` 与 WebView2 用户数据目录同样按 identifier 派生——两进程会共用同一份数据与浏览器 profile | `tauri/src/path/desktop.rs`（`data_dir()/identifier`）；`tauri/src/manager/webview.rs`（Windows 强制 WebView2 `data_directory = LocalData/identifier`） |
| 4 | 数据目录另有**共享指针** `data_path.txt`：任意版本（便携/安装/dev）启动都命中**同一真实数据目录**（M6 §2.6 有意设计）→ dev 直接指向安装版生产数据 | `lib.rs::resolve_data_dir_from` |
| 5 | 同数据目录 → 同 `relay_state.json` 的 `config.port`（3601）→ dev relay 绑同端口被 `_check_port_available` 拒绝（SystemExit）；且两 relay **并发读写同一批 JSON（无跨进程锁）**→ 状态互踩、dev 测试改动污染生产配置 | `storage.py::get_port`、`cli.py::_cmd_run` |

**现场实证（2026-10-16，config 合并探针）**：以 `tauri dev -c <tmp1> -c <tmp2>`（仅改 productName/version，**不改 identifier**）启动，dev 正常编译运行且日志 `[shell] sidecar 就绪 port=3601`——证实：① `-c` 多文件合并机制可用；② **即使 productName 不同**，identifier 未变 → dev 仍命中 prod 数据指针目录、读到 prod `relay_state.json` 的持久化端口 3601（根因 2/4/5 当场复现）。探针已即时终止，prod 业务文件（JSON/主 DB/日志行）经只读核对无写入。

**结论**：「把单实例分开」的正确形态 = **给 dev 一个独立身份（identifier）**——互斥锁 / AppData / 数据指针 / WebView2 profile **全部随 identifier 自动隔离**；唯一不在该派生链上的只剩**端口**，需单独引导。

## 2. 设计方案

### 2.1 dev 身份隔离（核心）

新增 `src-tauri/tauri.dev.conf.json` overlay（**仅 dev 显式传入，不进任何 build 命令**）：

```json
{
  "identifier": "com.coderproxy.desktop.dev",
  "app": {
    "windows": [
      { "title": "CoderProxy（开发版）", "width": 960, "height": 680,
        "minWidth": 800, "minHeight": 600 }
    ]
  }
}
```

- **合并语义已核实**：`tauri dev --config` 是**合并**到默认配置（CLI help 原文「merge with the default configuration file」，多 `-c` 按序合并）；`tauri-build`/`tauri-codegen` 经 `TAURI_CONFIG` 注入编译期（`json_patch::merge`）。本机已验证 overlay 方式 dev 可编译启动（读默认配置 + overlay 合并）。
- **陷阱：`app.windows` 是数组——json-patch 对数组整体替换**，overlay 必须**完整重复**窗口配置（title 以外的尺寸/最小尺寸照抄）。
- 启动命令（仓库根；与现有 build 命令同风格可显式双传，单传 overlay 亦可——默认配置会自动定位）：
  ```
  .\frontend\node_modules\.bin\tauri.cmd dev --config .\src-tauri\tauri.dev.conf.json
  ```
- 隔离效果：互斥锁 `com.coderproxy.desktop.dev-sim` 独立；AppData `%APPDATA%\com.coderproxy.desktop.dev`（数据指针及其默认数据目录由此派生）；WebView2 `%LOCALAPPDATA%\com.coderproxy.desktop.dev`；窗口标题带「（开发版）」防混淆。
- **release 零影响**：打包/构建命令**不加** overlay → identifier 仍 `com.coderproxy.desktop`，存量安装单实例 / 数据 / WebView2 全部不变。
- 不带 overlay 的裸 `tauri dev` **保持现状**（仍与安装版互斥）——有意保留，防止误开「共享生产数据的第二实例」。

### 2.2 dev 端口引导（用户拍板必做）

问题：dev 首启数据目录全新 → 无 `config.port` → 落默认 3601 → 与安装版撞端口；且撞死后 GUI 配置页也进不去（改端口的 API 挂在这个起不来的 relay 上）。

两处小改：

1. **壳**（`src-tauri/src/lib.rs::spawn_with_port`）：`cfg!(debug_assertions)` 时给 sidecar 传 `RELAY_PORT=13601`（常量 `DEV_RELAY_PORT`，建议值可改）。
2. **relay**（`relay/relay/cli.py::_cmd_run`）：端口装载优先级改为
   **`--port` > 磁盘（仅显式保存值）> `RELAY_PORT` env > 默认 3601**——
   即 env 在「磁盘无保存值」时才生效（顺带修复 `RELAY_PORT` env 目前被 `get_port()` 死覆盖的事实死开关）。

关键点：
- **env 派生的端口不落盘**（非用户选择）；dev GUI 改端口才落盘、此后覆盖 env（重启稳定）。
- 区分「未保存」与「默认值」：新增 `get_saved_port() -> int | None`（`config.port` 合法才返回）；`get_port()` 保留（= `saved or DEFAULT_PORT`，兼容既有调用与测试）。
- 安装版（release）不传 env，且磁盘已有 3601 → **行为零变化**；D1「磁盘持久化优先」原则不变（env 仍仅兜底）。

### 2.3 文档

`docs/开发规则.md`「常用命令」补并行启动命令与说明：

```
# dev 与安装版并行（独立身份/数据/端口；首启数据为空，需重新登录——建议小号）
.\frontend\node_modules\.bin\tauri.cmd dev --config .\src-tauri\tauri.dev.conf.json
```

并注明：`tauri dev`（不带 overlay）与安装版互斥为现状保留；dev 账号获取方式（小号登录，或安装版导出 cockpit JSON → dev 导入）。

### 2.4 被否决的备选

- **只跳过单实例插件**：否决——撞端口 + 共享生产数据并发写 + WebView2 profile 争抢，比现状更坏。
- **fork 单实例插件只改互斥键**：否决——治标不治本（数据目录 / WebView2 / 端口问题全部残留）。
- **壳侧探测 relay_state.json 再条件传 `--port`**：否决——壳耦合 relay 状态文件 schema、复制「首启」判定逻辑；relay 侧优先级修复更内聚，且顺带修死开关。
- **dev 复制/共用安装版数据目录**：否决——凭证明文复制扩大暴露面；共享目录并发写互踩（且用户明确要「分开」）。

## 3. 已知边界

- **dev 首启数据为空**：需重新登录（小号）或经「导出/导入」（安装版导出 cockpit JSON → dev 导入）。**不自动迁移** prod 数据（不替用户复制凭证）；曾共享 prod 数据的既有 dev 使用史，升级后同样从空开始（一次性成本）。
- dev 与 prod 并行时，**同一上游账号可被两边使用**（两 relay 各自出站）——属用户知情范围；上游风控纪律（硬性规则 5/6）不变：dev 冒烟只用小号、一切上游请求仍经各自 relay 伪装层。
- 只隔离「dev vs 安装版」：**同时跑两个 dev** 仍被 dev 互斥聚焦（预期）；同时跑两份安装版不变（M5 口径）。
- 窗口标题是唯一视觉区分手段（两托盘 tooltip 仍同名「CoderProxy」——可选增强，不阻塞本期）。
- `cfg!(debug_assertions)` 在 `tauri build --debug`（非标准流）也为 true；即便传入 env，磁盘已有生产端口 → 磁盘优先，无副作用。

## 4. 验收

- [ ] 安装版运行中（agent 正常在途），按 2.1 命令启动 dev：dev 正常起、两窗口并存；dev 内可正常操作，安装版 agent 请求不中断
- [ ] dev 数据落 `%APPDATA%\com.coderproxy.desktop.dev\data`；prod `%APPDATA%\com.coderproxy.desktop\data` 下文件 mtime 停更（文件级核对）；`%LOCALAPPDATA%\com.coderproxy.desktop.dev` 生成且 `...desktop` 不被 dev 触碰
- [ ] dev 首启端口 13601；dev GUI 改端口 → 重启保持；安装版保持 3601、互不影响
- [ ] dev 双开 → 第二实例聚焦第一实例（dev 内单实例仍生效）；安装版双开行为不变（`m5_single_instance_test.ps1` 口径）
- [ ] release 零回归：不加 overlay 构建后 identifier 仍 `com.coderproxy.desktop`（产物核对）；打包冒烟照跑
- [ ] relay 单测：端口装载优先级四元用例（`--port` / 磁盘 / env / 默认）+ `get_saved_port` 边界；全量 pytest 零回归

## 5. 实施步骤（commit 序列）

| # | commit | 验证 |
|---|---|---|
| 1 | `feat: dev 身份隔离——tauri.dev.conf.json overlay + 窗口标题标记`（overlay + 开发规则命令） | 手动：并行启动通过；release identifier 不变 |
| 2 | `feat: dev 端口引导——壳 debug 传 RELAY_PORT + relay 装载优先级修正`（lib.rs + cli.py + storage.py + tests） | pytest 全量；dev 首启 13601 / prod 3601 并存 |
| 3 | `chore: spec 台账收尾`（验收回填） | 台账 |
