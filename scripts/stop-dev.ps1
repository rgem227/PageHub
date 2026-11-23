$ErrorActionPreference = 'Stop'
$workspacePath = Split-Path -Parent $PSScriptRoot
$recordPath = Join-Path $workspacePath '.runtime/processes.json'
if (-not (Test-Path -LiteralPath $recordPath)) { Write-Output '没有本项目的启动记录。'; exit }
$records = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
foreach ($record in $records) {
    $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
    if ($process -and $process.StartTime.ToUniversalTime().Ticks -eq $record.start_ticks) {
        & taskkill.exe /PID $process.Id /T /F | Out-Null
        Write-Output "已停止 $($record.name)"
    }
}
Remove-Item -LiteralPath $recordPath
