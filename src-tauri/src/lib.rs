//! CoderProxy 桌面壳（Tauri 2）。
//!
//! 职责（M4 spec §6.3）：
//! 1. spawn relay sidecar（PyInstaller 打包的 `relay-sidecar`，经 `bundle.externalBin`
//!    随壳分发），解析 stdout 的 `[relay-ready]` 行拿到实际端口与 API Key；
//! 2. 向前端暴露 invoke 命令：
//!    - `relay`：通用代理，GUI → Rust → HTTP(localhost:port) 转发 `/v1/*`；
//!    - `relay_status`：当前 sidecar 就绪状态（端口 / key / 工具模式）；
//!    - `relay_stop`：停止 sidecar；
//!    - `relay_restart`：按指定端口重启 sidecar（后台线程，前端轮询就绪）。
//! 3. 应用退出时杀掉 sidecar。
//!
//! M5：sidecar 用 PyInstaller onefile exe（`binaries/relay-sidecar-<triple>.exe`），
//! 数据目录固定为 `app_data_dir`（打包态 `__file__` 指向解包目录，不可落数据）。

use std::path::{Path, PathBuf};
use std::sync::mpsc::{channel, RecvTimeoutError};
use std::sync::Mutex;
use std::time::Duration;

use serde::Serialize;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIcon, TrayIconBuilder, TrayIconEvent};
use tauri::{Emitter, Manager, State};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Windows 作业对象：把 PyInstaller onefile 的整个进程树（bootloader + 实际 relay）
/// 纳入统一回收。句柄关闭（含宿主进程被强杀时内核自动关句柄）即终止作业内全部进程，
/// 避免 `child.kill()` 只杀 bootloader 而残留 uvicorn 子进程占住端口。
#[cfg(windows)]
mod job {
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
    };
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{
        OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
    };

    pub struct JobObject(HANDLE);

    // HANDLE 在 windows-sys 里是 `*mut c_void`，非 Send/Sync；
    // 句柄由 Mutex 保护、仅持有者 Drop，跨线程共享安全。
    // SAFETY: JobObject 只包一个进程句柄，赋值/关闭操作本身线程安全。
    unsafe impl Send for JobObject {}
    unsafe impl Sync for JobObject {}

    impl JobObject {
        /// 创建带 KILL_ON_JOB_CLOSE 的匿名作业对象；失败返回 None（调用方降级为仅 kill 直子）。
        pub fn new() -> Option<JobObject> {
            // SAFETY: 匿名作业对象，无需 SECURITY_ATTRIBUTES / 名称
            let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
            if handle.is_null() {
                return None;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            // SAFETY: info 指向已初始化的有效内存
            let ok = unsafe {
                SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const _,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                )
            };
            if ok == 0 {
                // SAFETY: 关闭作业句柄
                unsafe { CloseHandle(handle) };
                return None;
            }
            Some(JobObject(handle))
        }

        /// 把 pid 进程纳入作业。注意：只影响该进程本身，不追溯已存在的子孙。
        pub fn assign(&self, pid: u32) -> bool {
            // SAFETY: 按 pid 打开进程句柄（设置配额 + 终止权限）
            let h = unsafe { OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid) };
            if h.is_null() {
                return false;
            }
            // SAFETY: 分配进程到作业（Windows 8+ 支持嵌套作业，普通双击启动无冲突）
            let ok = unsafe { AssignProcessToJobObject(self.0, h) };
            // SAFETY: 关闭进程句柄
            unsafe { CloseHandle(h) };
            ok != 0
        }
    }

    impl Drop for JobObject {
        fn drop(&mut self) {
            // 关闭作业句柄 → KILL_ON_JOB_CLOSE 终止作业内全部进程
            // SAFETY: 关闭作业句柄
            unsafe { CloseHandle(self.0) };
        }
    }

    /// 枚举 root 的整棵子孙进程（含 root 自身）。
    /// 用于把 PyInstaller onefile 的 bootloader + 实际 relay 子进程全部纳入作业——
    /// 只 assign 父进程不会追溯已存在的子进程，必须逐个 assign。
    pub fn descendants_of(root: u32) -> Vec<u32> {
        let mut all = vec![root];
        let mut grown = true;
        while grown {
            grown = false;
            // SAFETY: 创建当前进程表快照
            let snap = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0) };
            if snap == INVALID_HANDLE_VALUE {
                break;
            }
            let mut entry: PROCESSENTRY32W = unsafe { std::mem::zeroed() };
            entry.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
            // SAFETY: 取快照首项
            if unsafe { Process32FirstW(snap, &mut entry) } == 0 {
                // SAFETY: 关闭快照句柄
                unsafe { CloseHandle(snap) };
                break;
            }
            loop {
                let parent = entry.th32ParentProcessID;
                let pid = entry.th32ProcessID;
                if all.contains(&parent) && !all.contains(&pid) {
                    all.push(pid);
                    grown = true;
                }
                // SAFETY: 取快照下一项
                if unsafe { Process32NextW(snap, &mut entry) } == 0 {
                    break;
                }
            }
            // SAFETY: 关闭快照句柄
            unsafe { CloseHandle(snap) };
        }
        all
    }
}

/// 非 Windows 占位（本项目仅 Windows 分发，保编译完整）。
#[cfg(not(windows))]
mod job {
    pub struct JobObject;
    impl JobObject {
        pub fn new() -> Option<JobObject> {
            None
        }
        pub fn assign(&self, _pid: u32) -> bool {
            false
        }
    }
}

/// sidecar 就绪信息（`[relay-ready]` 行解析结果）。
#[derive(Clone, Serialize)]
struct ReadyInfo {
    port: u16,
    api_key: String,
    tool_mode: String,
}

struct AppState {
    sidecar: Mutex<Option<CommandChild>>,
    /// 回收 sidecar 整棵进程树的作业对象（PyInstaller onefile 两层结构防残留）。
    job: Mutex<Option<job::JobObject>>,
    ready: Mutex<Option<ReadyInfo>>,
    client: reqwest::Client,
    /// 保持托盘句柄存活（TrayIcon 被 Drop 会移除托盘图标）。
    tray: Mutex<Option<TrayIcon>>,
}

// ─────────────────────────── sidecar 生命周期 ───────────────────────────

/// 解析 `[relay-ready] port=... api_key=... tool_mode=...` 行。
fn parse_ready(line: &str) -> Option<ReadyInfo> {
    let rest = line.strip_prefix("[relay-ready] ")?;
    let mut port = 0u16;
    let mut api_key = String::new();
    let mut tool_mode = String::from("hybrid");
    for kv in rest.split_whitespace() {
        let (k, v) = kv.split_once('=')?;
        match k {
            "port" => port = v.parse().ok()?,
            "api_key" => api_key = v.to_string(),
            "tool_mode" => tool_mode = v.to_string(),
            _ => {}
        }
    }
    if port == 0 || api_key.is_empty() {
        return None;
    }
    Some(ReadyInfo { port, api_key, tool_mode })
}

fn spawn_sidecar(app: &tauri::AppHandle) -> Result<ReadyInfo, String> {
    let state = app.state::<AppState>();
    spawn_with_port(app, state.inner())
}

/// 解析数据目录：优先 exe 旁 `data/` 子目录（便携/绿色），无写权限时回退 AppData。
///
/// 规则（M6）：
/// - 首选 `<exe 所在目录>/data`：安装版装到用户可写目录即可持久化到安装子目录，
///   便携版解压到哪都自带数据；
/// - 装到 Program Files 等无写权限路径时，写入探测失败 → 回退 `%APPDATA%/com.coderproxy.desktop`。
fn resolve_data_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    // 候选 1：exe 旁的 data/ 子目录
    if let Ok(exe) = std::env::current_exe() {
        if let Some(base) = exe.parent() {
            let candidate = base.join("data");
            if writable_dir(&candidate) {
                return Ok(candidate);
            }
        }
    }
    // 候选 2：AppData（回退）
    let fallback = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("无法确定数据目录: {e}"))?;
    std::fs::create_dir_all(&fallback).map_err(|e| format!("创建数据目录失败: {e}"))?;
    Ok(fallback)
}

/// 探测目录是否可写：创建并尝试写入一个探针文件；成功删掉返回 true。
fn writable_dir(dir: &Path) -> bool {
    use std::io::Write as _;
    if std::fs::create_dir_all(dir).is_err() {
        return false;
    }
    let probe = dir.join(".write_probe");
    match std::fs::File::create(&probe) {
        Ok(mut f) => {
            let ok = f.write_all(b"ok").is_ok();
            let _ = std::fs::remove_file(&probe);
            ok
        }
        Err(_) => false,
    }
}

/// 首次切换数据目录时迁移旧数据：目标目录没有 relay_state.json 但 AppData 有则复制。
///
/// 仅在「选了 exe 旁 data/ 且它里面还没有状态文件」时触发，避免重复迁移与覆盖。
fn migrate_legacy_data(app: &tauri::AppHandle, target: &Path) {
    if !target.join("relay_state.json").exists() {
        let legacy = match app.path().app_data_dir() {
            Ok(d) => d.join("relay_state.json"),
            Err(_) => return,
        };
        if legacy.exists() {
            if let Err(e) = std::fs::copy(&legacy, target.join("relay_state.json")) {
                eprintln!("[shell] 迁移旧数据失败: {e}");
            } else {
                println!("[shell] 已迁移旧数据 -> {}", target.display());
            }
        }
    }
}

/// spawn 打包 sidecar 并等待 `[relay-ready]`；成功后 child/ready 写入 AppState。
///
/// 端口由 relay 侧持久化（config.port，跨启动一致），壳不再传 `--port`，
/// 只从 `[relay-ready]` 行解析实际端口供转发使用。
fn spawn_with_port(app: &tauri::AppHandle, state: &AppState) -> Result<ReadyInfo, String> {
    // 数据目录：优先 exe 旁 data/；无写权限回退 %APPDATA%（M6）。
    let data_dir = resolve_data_dir(app)?;
    // 便携版首次切换：把 AppData 旧状态复制过来，保留登录态/白名单/端口
    migrate_legacy_data(app, &data_dir);

    let (mut rx, child) = app
        .shell()
        .sidecar("relay-sidecar")
        .map_err(|e| format!("启动服务失败: {e}"))?
        .args(["run"])
        .env("RELAY_DATA_DIR", data_dir.to_string_lossy().as_ref())
        .spawn()
        .map_err(|e| format!("启动本地服务失败，请检查程序是否完整: {e}"))?;

    // 读线程：逐行扫 stdout，命中 [relay-ready] 即通知主线程
    let (tx, ready_rx) = channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        while let Some(ev) = rx.blocking_recv() {
            match ev {
                CommandEvent::Stdout(bytes) => {
                    for b in bytes {
                        if b == b'\n' {
                            let line = String::from_utf8_lossy(&buf).trim().to_string();
                            buf.clear();
                            if let Some(info) = parse_ready(&line) {
                                let _ = tx.send(info);
                                return;
                            }
                        } else {
                            buf.push(b);
                        }
                    }
                }
                CommandEvent::Terminated(_) => return,
                _ => {}
            }
        }
    });

    match ready_rx.recv_timeout(Duration::from_secs(60)) {
        Ok(info) => {
            // 把 sidecar 整棵进程树（bootloader + 实际 relay 子进程）纳入作业对象。
            // ready 是子进程打印的，说明子进程已存在；assign 父进程不会追溯已有子进程，
            // 故须枚举全部子孙逐个 assign，确保退出（含强杀）时全量回收。
            if let Some(j) = job::JobObject::new() {
                for pid in job::descendants_of(child.pid()) {
                    j.assign(pid);
                }
                *state.job.lock().unwrap() = Some(j);
            }
            *state.sidecar.lock().unwrap() = Some(child);
            *state.ready.lock().unwrap() = Some(info.clone());
            println!("[shell] sidecar 就绪 port={} tool_mode={}", info.port, info.tool_mode);
            Ok(info)
        }
        Err(RecvTimeoutError::Timeout) => {
            let _ = child.kill();
            Err("服务启动超时，请检查程序是否完整".to_string())
        }
        Err(RecvTimeoutError::Disconnected) => {
            let _ = child.kill();
            Err("服务意外退出".to_string())
        }
    }
}

/// 停掉当前 sidecar 并清空就绪状态。
fn stop_sidecar(app: &tauri::AppHandle) {
    let state = app.state::<AppState>();
    if let Some(child) = state.sidecar.lock().unwrap().take() {
        let _ = child.kill();
    }
    // 关闭作业对象句柄 → 终止作业内全部进程（兜底 onefile 子进程）
    if let Some(job) = state.job.lock().unwrap().take() {
        drop(job);
    }
    *state.ready.lock().unwrap() = None;
    println!("[shell] sidecar 已停止");
}

// ─────────────────────────── tauri 命令 ───────────────────────────

/// 通用代理：GUI → Rust → relay（仅放行 /v1/*，GET/POST）。
#[tauri::command]
async fn relay(
    state: State<'_, AppState>,
    method: String,
    path: String,
    body: Option<serde_json::Value>,
) -> Result<serde_json::Value, String> {
    if !path.starts_with("/v1/") {
        return Err(format!("不支持的请求路径: {path}"));
    }
    let ready = state
        .ready
        .lock()
        .unwrap()
        .clone()
        .ok_or_else(|| "服务尚未就绪，请稍候再试".to_string())?;

    let method = match method.to_uppercase().as_str() {
        "GET" => reqwest::Method::GET,
        "POST" => reqwest::Method::POST,
        _ => return Err("不支持的请求方式".to_string()),
    };
    let url = format!("http://127.0.0.1:{}{path}", ready.port);

    let mut req = state
        .client
        .request(method, &url)
        .header("Authorization", format!("Bearer {}", ready.api_key));
    if let Some(b) = body {
        req = req.json(&b);
    }
    let resp = req.send().await.map_err(|e| format!("服务请求失败: {e}"))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(format!("服务响应错误（{}）: {}", status.as_u16(), text));
    }
    serde_json::from_str(&text)
        .map_err(|e| format!("服务响应无法解析: {e}（{}）", &text[..text.len().min(200)]))
}

/// sidecar 就绪状态（前端轮询/初始化用）。
#[tauri::command]
fn relay_status(state: State<'_, AppState>) -> serde_json::Value {
    match state.ready.lock().unwrap().clone() {
        Some(r) => serde_json::json!({
            "running": true,
            "port": r.port,
            "api_key": r.api_key,
            "tool_mode": r.tool_mode,
        }),
        None => serde_json::json!({ "running": false }),
    }
}

/// 停止 relay sidecar（GUI「停止服务」按钮）。
#[tauri::command]
fn relay_stop(app: tauri::AppHandle) -> Result<(), String> {
    stop_sidecar(&app);
    Ok(())
}

/// 重启 relay sidecar（GUI「重启服务」按钮）。
///
/// 端口由 relay 侧持久化（config.port），重启按持久化端口立即生效，无需壳传端口。
/// 立即返回 `{"restarting": true}`，实际重启在后台线程执行，前端轮询 `relay_status` 感知就绪。
#[tauri::command]
async fn relay_restart(app: tauri::AppHandle) -> Result<serde_json::Value, String> {
    let handle = app.clone();
    std::thread::spawn(move || {
        stop_sidecar(&handle);
        if let Err(e) = spawn_sidecar(&handle) {
            eprintln!("[shell] 重启失败: {e}");
        }
    });
    Ok(serde_json::json!({ "restarting": true }))
}

/// 隐藏主窗口（最小化到托盘）——前端「最小化到托盘」按钮调用。
#[tauri::command]
fn window_hide(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.hide();
    }
    Ok(())
}

/// 真正退出应用——前端「退出应用」按钮调用，走 RunEvent::Exit 回收 sidecar。
#[tauri::command]
fn app_exit(app: tauri::AppHandle) -> Result<(), String> {
    app.exit(0);
    Ok(())
}

// ─────────────────────────── 入口 ───────────────────────────

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // 单实例：二次启动不再开新进程，聚焦已有主窗口（守护进程注册的全局互斥）
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.show();
                let _ = win.unminimize();
                let _ = win.set_focus();
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .manage(AppState {
            sidecar: Mutex::new(None),
            job: Mutex::new(None),
            ready: Mutex::new(None),
            client: reqwest::Client::new(),
            tray: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            relay,
            relay_status,
            relay_stop,
            relay_restart,
            window_hide,
            app_exit
        ])
        .setup(|app| {
            // 后台线程 spawn，窗口立即显示；就绪后 relay_status 才返回 running=true
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                if let Err(e) = spawn_sidecar(&handle) {
                    eprintln!("[shell] {e}");
                }
            });

            // ── 系统托盘：左键/菜单「显示主界面」唤回隐藏窗口；菜单「退出」真退出 ──
            let quit_item = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let show_item = MenuItem::with_id(app, "show", "显示主界面", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_item, &quit_item])?;
            let icon = app.default_window_icon().cloned().ok_or("缺少窗口图标，无法创建托盘")?;
            let tray = TrayIconBuilder::with_id("coderproxy-tray")
                .icon(icon)
                .tooltip("CoderProxy")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(win) = app.get_webview_window("main") {
                            let _ = win.show();
                            let _ = win.unminimize();
                            let _ = win.set_focus();
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(win) = app.get_webview_window("main") {
                            let _ = win.show();
                            let _ = win.unminimize();
                            let _ = win.set_focus();
                        }
                    }
                })
                .build(app)?;
            // 存入 AppState，防止被 Drop 移除托盘
            *app.state::<AppState>().tray.lock().unwrap() = Some(tray);

            // ── 拦截主窗口右上角关闭（X）：不直接关，发事件给前端弹三选对话框 ──
            let handle = app.handle().clone();
            app.get_webview_window("main")
                .expect("main 窗口应存在")
                .on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = handle.emit("close-requested", ());
                    }
                });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // 退出时回收 sidecar：kill 直子 + 关作业对象句柄杀整棵进程树。
            // 强杀（任务管理器/崩溃）场景由内核关句柄自动触发 KILL_ON_CLOSE 兜底。
            if let tauri::RunEvent::Exit = event {
                if let Some(child) = app_handle.state::<AppState>().sidecar.lock().unwrap().take() {
                    let _ = child.kill();
                }
                if let Some(job) = app_handle.state::<AppState>().job.lock().unwrap().take() {
                    drop(job);
                }
            }
        });
}
