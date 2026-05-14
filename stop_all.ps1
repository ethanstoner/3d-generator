# Stop all 3d-generator services (Ollama, ComfyUI, local backend).

$ErrorActionPreference = "Continue"

function Stop-OnPort([int]$port, [string]$label) {
    $tcp = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    if (-not $tcp) { Write-Host "  $label (port $port): not running"; return }
    $ids = $tcp | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $ids) {
        try {
            $name = (Get-Process -Id $procId -ErrorAction Stop).ProcessName
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "  $label (port $port): killed $name pid $procId"
        } catch {
            & cmd /c "taskkill /F /PID $procId" | Out-Null
            Write-Host "  $label (port $port): force-killed pid $procId"
        }
    }
}

Write-Host "stopping 3d-generator services..." -ForegroundColor Cyan
Stop-OnPort 8080  "Backend"
Stop-OnPort 8188  "ComfyUI"
Stop-OnPort 11435 "Ollama"
# Also stop any orphan ollama children
Get-Process -Name "ollama*" -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    Write-Host "  killed orphan ollama pid $($_.Id)"
}
Write-Host "done." -ForegroundColor Green
