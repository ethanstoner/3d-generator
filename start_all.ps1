# Unified launcher: Ollama + ComfyUI + backend
# All-or-nothing - if any service fails to come up, stop everything this script started and exit non-zero.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$OLLAMA_EXE   = "C:\Users\ethan\AppData\Local\Programs\Ollama\ollama.exe"
$COMFYUI_BAT  = "D:\Cursor\AI\ComfyUI_windows_portable_nvidia\ComfyUI_windows_portable\START_COMFYUI_NETWORK.bat"
$BACKEND_PORT = 8080
$OLLAMA_URL   = "http://127.0.0.1:11435"
$COMFYUI_URL  = "http://127.0.0.1:8188"
$BACKEND_URL  = "http://127.0.0.1:$BACKEND_PORT"
$LOG_DIR      = Join-Path $PSScriptRoot ".launcher-logs"
New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null

$ourProcs = @()  # PIDs we started (for rollback)

function Probe-Url($url, [int]$timeoutSec = 3) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $timeoutSec -ErrorAction Stop
        return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500)
    } catch { return $false }
}

function Wait-Url($name, $url, [int]$maxSec = 120) {
    Write-Host "  waiting for $name at $url ..." -NoNewline
    $deadline = (Get-Date).AddSeconds($maxSec)
    while ((Get-Date) -lt $deadline) {
        if (Probe-Url $url 2) {
            Write-Host " up" -ForegroundColor Green
            return $true
        }
        Start-Sleep -Seconds 2
        Write-Host "." -NoNewline
    }
    Write-Host " TIMEOUT" -ForegroundColor Red
    return $false
}

function Cleanup-OurProcs() {
    if ($ourProcs.Count -eq 0) { return }
    Write-Host ""
    Write-Host "rolling back: stopping services this script started..." -ForegroundColor Yellow
    foreach ($procId in $ourProcs) {
        try { Stop-Process -Id $procId -Force -ErrorAction Stop; Write-Host "  killed pid $procId" } catch {}
    }
}

try {
    Write-Host "=== 3d-generator unified launcher ===" -ForegroundColor Cyan
    Write-Host ""

    # --- Ollama ---
    Write-Host "[1/3] Ollama" -ForegroundColor Cyan
    if (Probe-Url "$OLLAMA_URL/api/tags" 2) {
        Write-Host "  already running at $OLLAMA_URL"
    } else {
        if (-not (Test-Path $OLLAMA_EXE)) { throw "Ollama not found at $OLLAMA_EXE" }
        Write-Host "  starting..."
        $p = Start-Process -FilePath $OLLAMA_EXE -ArgumentList "serve" -PassThru `
            -RedirectStandardOutput (Join-Path $LOG_DIR "ollama-out.log") `
            -RedirectStandardError  (Join-Path $LOG_DIR "ollama-err.log") -WindowStyle Hidden
        $ourProcs += $p.Id
        if (-not (Wait-Url "Ollama" "$OLLAMA_URL/api/tags" 30)) { throw "Ollama did not come up" }
    }

    # --- ComfyUI ---
    Write-Host "[2/3] ComfyUI" -ForegroundColor Cyan
    if (Probe-Url "$COMFYUI_URL/system_stats" 2) {
        Write-Host "  already running at $COMFYUI_URL"
    } else {
        if (-not (Test-Path $COMFYUI_BAT)) { throw "ComfyUI launcher not found at $COMFYUI_BAT" }
        Write-Host "  starting (opens its own window - close that window to stop ComfyUI)..."
        $p = Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$COMFYUI_BAT`"" -PassThru
        $ourProcs += $p.Id
        if (-not (Wait-Url "ComfyUI" "$COMFYUI_URL/system_stats" 120)) { throw "ComfyUI did not come up" }
    }

    # --- Backend ---
    Write-Host "[3/3] Backend (FastAPI)" -ForegroundColor Cyan
    if (Probe-Url "$BACKEND_URL/" 2) {
        Write-Host "  already running at $BACKEND_URL"
    } else {
        $env:PYTHONIOENCODING = "utf-8"
        Write-Host "  starting..."
        $p = Start-Process -FilePath "python" `
            -ArgumentList "-m","uvicorn","backend.main:app","--host","0.0.0.0","--port",$BACKEND_PORT,"--reload" `
            -PassThru `
            -RedirectStandardOutput (Join-Path $LOG_DIR "backend-out.log") `
            -RedirectStandardError  (Join-Path $LOG_DIR "backend-err.log") -WindowStyle Hidden
        $ourProcs += $p.Id
        if (-not (Wait-Url "Backend" "$BACKEND_URL/" 30)) { throw "Backend did not come up - see $LOG_DIR\backend-err.log" }
    }

    Write-Host ""
    Write-Host "=== ALL SERVICES UP ===" -ForegroundColor Green
    Write-Host "  Ollama:   $OLLAMA_URL"
    Write-Host "  ComfyUI:  $COMFYUI_URL"
    Write-Host "  Backend:  $BACKEND_URL"
    Write-Host ""
    Write-Host "Logs: $LOG_DIR"
    Write-Host "To stop everything: run .\stop_all.ps1"
    exit 0

} catch {
    Write-Host ""
    Write-Host "FAILURE: $_" -ForegroundColor Red
    Cleanup-OurProcs
    exit 1
}
