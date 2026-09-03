# M5: verify Job Object fix - force-kill the desktop app, check sidecar tree is cleaned up
$ErrorActionPreference = "Continue"

# 1. clean up any leftover sidecar
Get-Process relay-sidecar -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

# 2. launch release GUI exe
$exe = "D:\Code\home\own-project\CoderProxy\src-tauri\target\release\coderproxy-desktop.exe"
$app = Start-Process -FilePath $exe -PassThru
Write-Host ("launched app pid={0}" -f $app.Id)

# 3. wait for sidecar process tree to appear
$deadline = (Get-Date).AddSeconds(45)
$sc = @()
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 500
  $sc = @(Get-Process relay-sidecar -ErrorAction SilentlyContinue)
  if ($sc.Count -ge 1) { break }
}
if ($sc.Count -eq 0) {
  Write-Host "FAIL: sidecar did not start"
  Stop-Process -Id $app.Id -Force -ErrorAction SilentlyContinue
  exit 1
}
$pids = ($sc | ForEach-Object { $_.Id }) -join ","
Write-Host ("sidecar running: pids={0} count={1}" -f $pids, $sc.Count)

# 4. force-kill the desktop app (simulates task manager / crash)
Stop-Process -Id $app.Id -Force
Start-Sleep -Seconds 3

# 5. check sidecar tree cleaned up
$remaining = @(Get-Process relay-sidecar -ErrorAction SilentlyContinue)
$still = ($remaining | ForEach-Object { $_.Id }) -join ","
if ($remaining.Count -eq 0) {
  Write-Host "RESULT: PASS - force-kill cleaned up whole sidecar tree"
  exit 0
} else {
  Write-Host ("RESULT: FAIL - orphan sidecar remains: {0}" -f $still)
  $remaining | Stop-Process -Force -ErrorAction SilentlyContinue
  exit 2
}
