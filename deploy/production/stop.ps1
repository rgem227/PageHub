$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$nginx = Join-Path $root 'nginx\nginx.exe'
$recordPath = Join-Path $root 'logs\processes.json'

if (Test-Path (Join-Path $root 'nginx\logs\nginx.pid')) {
    & $nginx -p "$root\nginx" -c conf/nginx.conf -s stop
}
if (Test-Path $recordPath) {
    foreach ($record in (Get-Content $recordPath -Raw | ConvertFrom-Json)) {
        $process = Get-Process -Id $record.id -ErrorAction SilentlyContinue
        if ($process -and $process.StartTime.ToUniversalTime().Ticks -eq $record.start_ticks) {
            & taskkill.exe /PID $process.Id /T /F *> $null
            Write-Output "已停止 $($record.name)"
        }
    }
    Remove-Item -LiteralPath $recordPath -Force
}
Write-Output 'PageHub 应用进程已停止；PostgreSQL 服务保持运行。'
