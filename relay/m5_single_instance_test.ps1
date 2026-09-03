# M5: verify single-instance - second launch exits, only one app process remains
$ErrorActionPreference = "Continue"

Get-Process coderproxy-desktop -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process relay-sidecar -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

$exe = "D:\Code\home\own-project\CoderProxy\src-tauri\target\release\coderproxy-desktop.exe"

# instance 1
$p1 = Start-Process -FilePath $exe -PassThru
Write-Host ("instance1 pid={0}" -f $p1.Id)
Start-Sleep -Seconds 5  # let first instance fully start (single-instance mutex registered)

# instance 2
$p2 = Start-Process -FilePath $exe -PassThru
Write-Host ("instance2 pid={0}" -f $p2.Id)
Start-Sleep -Seconds 5  # give it time to detect mutex and exit

# checks
$alive = @(Get-Process coderproxy-desktop -ErrorAction SilentlyContinue)
$p1Alive = [bool](Get-Process -Id $p1.Id -ErrorAction SilentlyContinue)
$p2Alive = [bool](Get-Process -Id $p2.Id -ErrorAction SilentlyContinue)
Write-Host ("app processes now: count={0}" -f $alive.Count)
Write-Host ("instance1 alive={0}  instance2 alive={1}" -f $p1Alive, $p2Alive)

if (-not $p1Alive -and $alive.Count -eq 0) {
  Write-Host "RESULT: WARN - instance1 also gone (first launch may have exited unexpectedly)"
  exit 3
}
if ($p1Alive -and -not $p2Alive -and $alive.Count -eq 1) {
  Write-Host "RESULT: PASS - second instance exited, only original remains"
  exit 0
} else {
  Write-Host "RESULT: FAIL - single-instance not effective"
  Get-Process coderproxy-desktop -ErrorAction SilentlyContinue | Stop-Process -Force
  Get-Process relay-sidecar -ErrorAction SilentlyContinue | Stop-Process -Force
  exit 2
}
