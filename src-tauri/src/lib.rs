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

use std::sync::atomic::{AtomicU16, Ordering};
use std::sync::mpsc::{channel, RecvTimeoutError};
use std::sync::Mutex;
use std::time::Duration;

use serde::Serialize;
use tauri::{Manager, State};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// sidecar 就绪信息（`[relay-ready]` 行解析结果）。
#[derive(Clone, Serialize)]
struct ReadyInfo {
    port: u16,
    api_key: String,
    tool_mode: String,
}

struct AppState {
    sidecar: Mutex<Option<CommandChild>>,
    ready: Mutex<Option<ReadyInfo>>,
    /// GUI 配置页期望的端口；0 = 随机端口。重启时按此值 spawn。
    requested_port: AtomicU16,
    client: reqwest::Client,
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
    let port = state.requested_port.load(Ordering::SeqCst);
    spawn_with_port(app, &state, port)
}

/// 按指定端口 spawn 打包 sidecar 并等待 `[relay-ready]`；成功后将 child/ready 写入 AppState。
fn spawn_with_port(app: &tauri::AppHandle, state: &AppState, port: u16) -> Result<ReadyInfo, String> {
    // 数据目录：%APPDATA%/com.coderproxy.desktop（打包态不可用 exe 解包目录）
    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("解析 app_data_dir 失败: {e}"))?;
    std::fs::create_dir_all(&data_dir).map_err(|e| format!("创建数据目录失败: {e}"))?;

    let (mut rx, child) = app
        .shell()
        .sidecar("relay-sidecar")
        .map_err(|e| format!("解析 sidecar 失败: {e}"))?
        .args(["run", "--port", &port.to_string()])
        .env("RELAY_DATA_DIR", data_dir.to_string_lossy().as_ref())
        .spawn()
        .map_err(|e| format!("spawn sidecar 失败（打包 exe 与依赖就绪？）: {e}"))?;

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
            *state.sidecar.lock().unwrap() = Some(child);
            *state.ready.lock().unwrap() = Some(info.clone());
            println!("[shell] sidecar 就绪 port={} tool_mode={}", info.port, info.tool_mode);
            Ok(info)
        }
        Err(RecvTimeoutError::Timeout) => {
            let _ = child.kill();
            Err("sidecar 60s 内未就绪（检查打包 exe）".to_string())
        }
        Err(RecvTimeoutError::Disconnected) => {
            let _ = child.kill();
            Err("sidecar 提前退出（看上方 stderr）".to_string())
        }
    }
}

/// 停掉当前 sidecar 并清空就绪状态。
fn stop_sidecar(app: &tauri::AppHandle) {
    let state = app.state::<AppState>();
    if let Some(child) = state.sidecar.lock().unwrap().take() {
        let _ = child.kill();
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
        return Err(format!("路径受限（仅 /v1/*）: {path}"));
    }
    let ready = state
        .ready
        .lock()
        .unwrap()
        .clone()
        .ok_or_else(|| "relay 尚未就绪".to_string())?;

    let method = match method.to_uppercase().as_str() {
        "GET" => reqwest::Method::GET,
        "POST" => reqwest::Method::POST,
        _ => return Err("仅支持 GET/POST".to_string()),
    };
    let url = format!("http://127.0.0.1:{}{path}", ready.port);

    let mut req = state
        .client
        .request(method, &url)
        .header("Authorization", format!("Bearer {}", ready.api_key));
    if let Some(b) = body {
        req = req.json(&b);
    }
    let resp = req.send().await.map_err(|e| format!("relay 请求失败: {e}"))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(format!("relay HTTP {}: {}", status.as_u16(), text));
    }
    serde_json::from_str(&text)
        .map_err(|e| format!("relay 响应解析失败: {e}（{}）", &text[..text.len().min(200)]))
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
/// `port`: 新的期望端口（None = 保持当前），0 = 随机端口。
/// 立即返回 `{"restarting": true}`，实际重启在后台线程执行，前端轮询 `relay_status` 感知就绪。
#[tauri::command]
async fn relay_restart(app: tauri::AppHandle, port: Option<u16>) -> Result<serde_json::Value, String> {
    if let Some(p) = port {
        app.state::<AppState>().requested_port.store(p, Ordering::SeqCst);
        println!("[shell] 请求端口 -> {p}");
    }
    let handle = app.clone();
    std::thread::spawn(move || {
        stop_sidecar(&handle);
        if let Err(e) = spawn_sidecar(&handle) {
            eprintln!("[shell] 重启失败: {e}");
        }
    });
    Ok(serde_json::json!({ "restarting": true }))
}

// ─────────────────────────── 入口 ───────────────────────────

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .manage(AppState {
            sidecar: Mutex::new(None),
            ready: Mutex::new(None),
            requested_port: AtomicU16::new(0),
            client: reqwest::Client::new(),
        })
        .invoke_handler(tauri::generate_handler![relay, relay_status, relay_stop, relay_restart])
        .setup(|app| {
            // 后台线程 spawn，窗口立即显示；就绪后 relay_status 才返回 running=true
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                if let Err(e) = spawn_sidecar(&handle) {
                    eprintln!("[shell] {e}");
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // 退出时杀掉 sidecar
            if let tauri::RunEvent::Exit = event {
                if let Some(child) = app_handle.state::<AppState>().sidecar.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}
