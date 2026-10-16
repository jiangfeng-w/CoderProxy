# dev 与安装版并行运行（开发身份隔离）

> **状态**：**已实现（2026-10-16）**——dev overlay 身份隔离 + relay 端口「配置起点 + 自动顺延」+ 配置页「配置 vs 实际」提示全部落地；pytest **590 绿** / frontend format+build 绿 / 真机并行验收通过（旧版 relay 占 3601 + dev 自动落 3602 并存、dev 数据与 WebView2 profile 独立、dev 双开仍聚焦单实例）；实施时按本机实测修订了两处判定口径（见 §2.2.1 末注）。原定稿注记（2026-10-16；用户拍板：**单独立项小 spec** + **含端口引导**）；同日修订（用户拍板）：**端口方案改「各自配置 + 自动顺延」**——dev 默认同 3601 起扫，废弃「壳传 `RELAY_PORT=13601`」；**数据隔离保留**（共享数据影响评估见 §2.2.5）
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

**结论**：「把单实例分开」的正确形态 = **给 dev 一个独立身份（identifier）**——互斥锁 / AppData / 数据指针 / WebView2 profile **全部随 identifier 自动隔离**；唯一不在该派生链上的只剩**端口**，需单独处理（§2.2：配置各自独立 + 自动顺延）。

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

### 2.2 端口：各自配置 + 自动顺延（用户拍板；含共享数据评估附注）

**修订（2026-10-16，用户拍板）**：原方案（壳 debug 传 `RELAY_PORT=13601` + relay env 优先级修复）**废弃**——数据隔离后 dev 配置本就独立，无需壳特判；改为**两边各自配置、同一套「向上逐一找空位」规则**（dev 默认同 3601 起扫）。副作用：**壳零改动**（lib.rs 不再改）。

问题：占用即退——`cli.py::_check_port_available` 预检被占直接 `SystemExit`（「请更换端口后重启」）；且撞死后 GUI 配置页也进不去（改端口的 API 挂在这个起不来的 relay 上）。

#### 2.2.1 设计（relay 通用能力，安装版同样受益）

1. **起点**：`--port` 显式（落盘持久化，现有语义）> 磁盘 `config.port` > `RELAY_PORT` env（**不落盘**）> 默认 3601。需区分「未保存」与「默认值」：新增 `storage.get_saved_port() -> int | None`（`config.port` 合法才返回）；`get_port()` 保留（= `saved or DEFAULT_PORT`，兼容既有调用与测试）。顺带修复 `RELAY_PORT` env 死开关的事实（env 仅作通用兜底保留，dev 不再依赖）。
2. **顺延扫描**：从起点起向上最多 **20 个**端口（常量 `PORT_SCAN_WINDOW`）逐一试；窗口内全占用 → `SystemExit` 并列明范围（如「3601–3620 均被占用」）。`--port` 显式传参同样顺延（日志明示「起点 X 被占用，顺延至 Y」）。
3. **探测 = connect 活性判定**（对每个候选先 `connect` 该监听地址）：活监听必然连通 → 换下一个；连接被拒 → 视为空闲，进入预绑定。两头堵的原因：Windows 上 TIME_WAIT 残留会让**裸 bind 预检误报**占用（重启场景端口漂移）；而 SO_REUSEADDR 裸检对「同样带 REUSEADDR 的活监听」（uvicorn 默认即带，`config.py:584`）**可误判共存**——connect 判定把两种都绕开。
4. **预绑定交 uvicorn**：命中空位后 `bind`（SO_REUSEADDR，兼容 TIME_WAIT 快速重启）拿到 socket，直接 `uvicorn.Server(config).run(sockets=[sock])`——**已核实 vendored uvicorn 支持**（`server.py:126` `loop.create_server(sock=sock, backlog=...)`，内部自行 listen）。原「预检→真实 bind」之间的 TOCTOU 空窗随之归零。
5. **落盘语义**：只有「配置端口」（`--port` / GUI 保存）落盘；env 派生与顺延所得的实际端口**都不落盘**。每次启动从配置端口重新起扫 → 占用情形不变则落点稳定；`settings.relay_port` = **实际监听**（`[relay-ready]` 行 / 壳转发 / 顶栏显示自动正确，壳与协议零改动）。`settings.relay_port_configured` = 配置起点（GUI「配置 vs 实际」提示行据此判定）。

> **实施实测修订（2026-10-16，本机 socket 矩阵；原第 3/4 条按此改写）**：
> ① **connect 只作正向证据**：连接**未监听**端口在本机是 **SYN 静默超时**（不是 refused——被本机安全软件类丢包），超时与「空闲」不可区分 → 判定口径定为「**连通 = 确定被占用**（快速跳过）」，未连通**不**反推空闲；
> ② **正式绑定不加 `SO_REUSEADDR`**：实测带 REUSEADDR 的绑定对「**同样带 REUSEADDR 的活监听**」（uvicorn 默认带）**会成功 = 劫持对方端口**；不加则任何活监听都以 `WSAEADDRINUSE`/`WSAEACCES` 失败——干净。TIME_WAIT 残留实测不影响独占绑定，故无需为「快速重启」牺牲排他性；
> ③ 最终形态 = **探测与绑定同一函数**（`cli.pick_listen_socket`）：connect 快速跳过活监听 → 独占绑定落实归属，返回的已是绑定好的 socket 直接交 uvicorn（`Server.run(sockets=[...])`）——「探测→绑定」的 TOCTOU 空窗不存在（比原「预绑定」更早落实归属）。

#### 2.2.2 GUI 配套（必须，否则静默失联）

- `GET /v1/auth/config`：**新增 `listen_port`** = 实际监听；`port` 语义变为**配置起点**（磁盘 / env / 默认的有效值）；`base_url` 用**实际**监听端口。
- 配置页「服务端口」：输入框仍编辑配置端口；当 `listen_port != port` 时显示提示行——「配置端口 3601 被占用，实际监听 3602；Agent 连接地址请用上方地址」。base_url 框/复制按钮自动正确（后端字段已改）。
  - **实施补充（2026-10-16，用户实测反馈后修订）**：输入框为**本地草稿**（不写 store）——原实现输入即改共享 store，导致「只敲数字、还没保存」时提示行就误报「配置端口 X 被占用」（实测：敲 13601 未保存即弹提示）；提示行同时只在「保存 + 重启」流程结束后显示（流程中抑制），避免重启窗口内的瞬时不一致被当成结论。
- 顶栏实际端口展示已存在（`Shell.vue` 读壳 `relay_status`），无需改。

#### 2.2.3 dev 与安装版并存语义（用户拍板）

- 两者**各自独立配置**（identifier 隔离的副产品，§2.1），**默认都从 3601 起扫**：先启动者得 3601，后启动者自动 3602/3603…；两 GUI 顶栏各显示各自实际端口。
- 残余风险与缓解（知情选择，非默认）：dev 先启动且安装版未运行时，dev 会占 3601（dev 的默认配置）；此后启动安装版会被顺延到 3602 → **agent 里填的 3601 会打到 dev**。若要求 agent 永远打安装版：把 dev 配置页端口改成 13601 一次即可（各自配置独立，永久生效）。

#### 2.2.4 共享数据影响评估（2026-10-16，回答用户问；结论：隔离必须保留）

若让 dev 与安装版共享同一数据目录（即不做 §2.1 隔离），对安装版运行的影响（源码级）：

| # | 影响 | 依据 |
|---|---|---|
| 0 | **前提**：不隔离身份连启动都过不去（单实例互斥 identifier 派生，根因 1/2）——共享需先跳过插件（§2.4 已否决） | §1 |
| 1 | **账号池被 dev 测试动作直接改**（登录/删号/将来的启用停用与健康降级计数）→ 安装版选号行为跟着变 | 账号池同一份真值文件 |
| 2 | **登录 token 双份刷新**：两 relay 各自刷新同一 refresh token，若上游轮换会互相踢下线 | `ta3_auth` / WB accounts 共享一份 |
| 3 | **配置互踩**：端口 / api_key / tool_mode / 白名单 / thinking_defaults 一份；dev 改 api_key 直接让 agent 401 | `relay_state.json` 共享 |
| 4 | **JSON 写丢更新**：写是原子替换（`tmp.replace`）不会读半截，但锁只在进程内（`storage.py` / `platforms/store.py`）→ 跨进程并发「读-改-写」后写覆盖前写 | 源码已核 |
| 5 | **SQLite**：WAL + busy_timeout 能扛并发，但 dev 新代码的迁移改的是生产库，schema 演进可伤旧版 | `db.py:73` |
| 6 | 日志混流：dev 调试噪音进生产日志表 | 同上 |

**结论（用户拍板）**：**保留数据隔离**（§2.1 不动）；dev 测试账号用小号登录 / 从安装版导出 cockpit JSON 导入（§3）。

### 2.3 文档

`docs/开发规则.md`「常用命令」补并行启动命令与说明：

```
# dev 与安装版并行（独立身份/数据/端口；首启数据为空，需重新登录——建议小号）
.\frontend\node_modules\.bin\tauri.cmd dev --config .\src-tauri\tauri.dev.conf.json
```

并注明：`tauri dev`（不带 overlay）与安装版互斥为现状保留；dev 账号获取方式（小号登录，或安装版导出 cockpit JSON → dev 导入）；dev 与安装版并存时端口自动错开（§2.2），如需 agent 连接地址永远指向安装版，可在 dev 配置页把端口设为 13601（可选，知情选择）。

### 2.4 被否决的备选

- **只跳过单实例插件**：否决——撞端口 + 共享生产数据并发写 + WebView2 profile 争抢，比现状更坏。
- **fork 单实例插件只改互斥键**：否决——治标不治本（数据目录 / WebView2 / 端口问题全部残留）。
- **壳侧探测 relay_state.json 再条件传 `--port`**：否决——壳耦合 relay 状态文件 schema、复制「首启」判定逻辑；relay 侧优先级修复更内聚，且顺带修死开关。
- **dev 复制/共用安装版数据目录**：否决——凭证明文复制扩大暴露面；共享目录并发写互踩（且用户明确要「分开」，共享影响评估见 §2.2.4）。
- **（修订废止）壳 debug 传 `RELAY_PORT=13601`**：原 2.2 方案；数据隔离后 dev 配置本就独立、无需壳特判，改「各自配置 + 自动顺延」（用户拍板），壳零改动。

## 3. 已知边界

- **dev 首启数据为空**：需重新登录（小号）或经「导出/导入」（安装版导出 cockpit JSON → dev 导入）。**不自动迁移** prod 数据（不替用户复制凭证）；曾共享 prod 数据的既有 dev 使用史，升级后同样从空开始（一次性成本）。
- dev 与 prod 并行时，**同一上游账号可被两边使用**（两 relay 各自出站）——属用户知情范围；上游风控纪律（硬性规则 5/6）不变：dev 冒烟只用小号、一切上游请求仍经各自 relay 伪装层。
- 只隔离「dev vs 安装版」：**同时跑两个 dev** 仍被 dev 互斥聚焦（预期）；同时跑两份安装版不变（M5 口径）。
- 窗口标题是唯一视觉区分手段（两托盘 tooltip 仍同名「CoderProxy」——可选增强，不阻塞本期）。
- **端口落点取决于启动顺序**（§2.2.3）：dev 先启动会占 3601，安装版后启被顺延 → agent 的 3601 会打到 dev；规避 = dev 配置页端口设 13601（可选）。顺延窗口（20 个）内全被占用时启动失败，报错列明范围。

## 4. 验收

> 回填（2026-10-16 实施完成）：1–3、5、7 已实测通过；4 的 API 侧与冒烟通过、GUI 提示行由用户视觉确认；6 为 release 口径核对（identifier 未改），安装包重打与产物冒烟随用户打包时机执行。

- [x] 安装版运行中（agent 正常在途），按 2.1 命令启动 dev：dev 正常起、两窗口并存；dev 内可正常操作，安装版 agent 请求不中断 —— 实测：旧版 relay（release sidecar，隔离数据目录）占 3601，dev 起服 `[shell] sidecar 就绪 port=3602`，两者并存、3601 旧实例全程正常响应 `/v1/auth/config`（200）
- [x] dev 数据落 `%APPDATA%\com.coderproxy.desktop.dev\data`；prod `%APPDATA%\com.coderproxy.desktop\data` 下文件 mtime 停更（文件级核对）；`%LOCALAPPDATA%\com.coderproxy.desktop.dev` 生成且 `...desktop` 不被 dev 触碰 —— 实测：dev 目录生成 `data_path.txt` + `data/{coderproxy.db,relay_state.json}`；`%LOCALAPPDATA%` 两目录并存；prod `data/` mtime 保持 10-16 01:32（早于本次运行）
- [x] dev 首启从 3601 起扫：安装版在跑时 dev 落 3602（或第一个空位），两 GUI 顶栏各显示各自实际端口；dev GUI 改端口 → 重启保持；安装版保持 3601 不受影响 —— 实测：dev 落 3602（banner 打印「端口 3601 已被占用，自动顺延至 3602」），顶栏读 `[relay-ready]` 端口自动正确
- [x] 占用顺延：3601 被无关进程占用 → relay 自动顺延 3602，配置页出现「配置 3601 / 实际 3602」提示行，agent 用实际端口可正常请求；占用情形不变时重启落点稳定 —— API 实测 `GET /v1/auth/config` = `{port: 3601, listen_port: 3602, base_url: http://127.0.0.1:3602/v1}`（前端按此渲染提示行）；冒烟「A 重启落点稳定 3601」通过
- [x] dev 双开 → 第二实例聚焦第一实例（dev 内单实例仍生效）；安装版双开行为不变（`m5_single_instance_test.ps1` 口径） —— 实测：直起 `target\debug\coderproxy-desktop.exe` 第二实例后进程数仍为 1、未出现第三个 relay 端口
- [x] release 零回归：不加 overlay 构建后 identifier 仍 `com.coderproxy.desktop`（产物核对）；打包冒烟照跑 —— identifier 仅存在于 overlay（打包命令只传 `tauri.conf.json`），本次未重打安装包（随用户打包时机）
- [x] relay 单测：端口装载优先级四元用例（`--port` / 磁盘 / env / 默认；env 值不落盘）+ `get_saved_port` 边界 + 顺延用例（起点被占 → 下一空位；窗口全占 → 报错；TIME_WAIT 残留不误判） —— `relay/tests/test_port_scan.py` 14 例（含独占绑定兜底「探测漏报仍不顺延错」、窗口全占、非法起点）；全量 pytest **590 passed** 零回归

## 5. 实施步骤（commit 序列）

| # | commit | 验证 |
|---|---|---|
| 1 | `feat: dev 身份隔离——tauri.dev.conf.json overlay + 窗口标题标记`（overlay + 开发规则命令） | 手动：并行启动通过；release identifier 不变 |
| 2 | `feat: relay 端口自动顺延——配置起点向上扫 + 预绑定交 uvicorn`（cli.py + storage.py + routes.py〔`listen_port`〕+ Config.vue + tests；**壳零改动**） | pytest 全量；dev（安装版在跑）落 3602 / prod 3601 并存；配置页提示行 |
| 3 | `chore: spec 台账收尾`（验收回填） | 台账 |

> **完成回填（2026-10-16）**：① `7d5b808` dev 身份隔离（overlay + 开发规则并行启动命令）；② `de820f7` relay 端口自动顺延（storage / cli / routes / 前端 / 单测，壳零改动）；③ 本条为台账收尾。
> 支撑脚本：本目录 [`冒烟-端口顺延.py`](冒烟-端口顺延.py)——双实例并存 / 起点被占顺延 / 重启落点稳定 / `--port` 落盘，共 11 项断言全 PASS（本机回环，**无上游请求**）。
> 未随本次做：sidecar 重打（已执行，含新端口逻辑）与安装包重打（随用户打包时机）。
