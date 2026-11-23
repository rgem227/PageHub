$ErrorActionPreference = 'Stop'
$workspacePath = Split-Path -Parent $PSScriptRoot
$runtimePath = Join-Path $workspacePath '.runtime'
$pythonPath = Join-Path $workspacePath '.venv/Scripts/python.exe'
$backendPath = Join-Path $workspacePath 'backend'
if (-not (Test-Path -LiteralPath (Join-Path $workspacePath '.env'))) { throw '请先执行 backend/bootstrap.py 初始化数据库。' }
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
$udp = [System.Net.Sockets.UdpClient]::new()
try {
    $udp.Connect('192.0.2.1', 80)
    $lanAddress = $udp.Client.LocalEndPoint.Address.ToString()
} finally { $udp.Dispose() }
if (-not $lanAddress -or $lanAddress.StartsWith('127.') -or $lanAddress -eq '0.0.0.0') { throw '无法探测局域网 IPv4 地址。' }
foreach ($port in @(8000,8011,5173)) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "端口 $port 已被占用；请先检查已有服务。" }
}
$jobs = @(
    @{Name='api'; Args=@('-m','uvicorn','app.main:app','--host','0.0.0.0','--port','8000'); Dir=$backendPath; Exe=$pythonPath},
    @{Name='content'; Args=@('-m','uvicorn','app.content:app','--host','0.0.0.0','--port','8011','--no-access-log'); Dir=$backendPath; Exe=$pythonPath},
    @{Name='worker'; Args=@('-m','app.worker'); Dir=$backendPath; Exe=$pythonPath},
    @{Name='frontend'; Args=@('node_modules/vite/bin/vite.js','--host','0.0.0.0','--strictPort'); Dir=(Join-Path $workspacePath 'frontend'); Exe=(Get-Command node).Source}
)
$started = @()
$environment = @{
    WEB_ORIGIN = [Environment]::GetEnvironmentVariable('WEB_ORIGIN', 'Process')
    CONTENT_ORIGIN = [Environment]::GetEnvironmentVariable('CONTENT_ORIGIN', 'Process')
    MCP_PUBLIC_URL = [Environment]::GetEnvironmentVariable('MCP_PUBLIC_URL', 'Process')
}
$env:WEB_ORIGIN = "http://${lanAddress}:5173"
$env:CONTENT_ORIGIN = "http://${lanAddress}:8011"
$env:MCP_PUBLIC_URL = "http://${lanAddress}:8000/mcp/"
try {
    foreach ($job in $jobs) {
        $process = Start-Process -FilePath $job.Exe -ArgumentList $job.Args -WorkingDirectory $job.Dir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath ($job.Name+'.log')) -RedirectStandardError (Join-Path $runtimePath ($job.Name+'.err.log'))
        $started += @{name=$job.Name; id=$process.Id; start_ticks=$process.StartTime.ToUniversalTime().Ticks}
    }
    $started | ConvertTo-Json | Set-Content (Join-Path $runtimePath 'processes.json') -Encoding utf8
    Write-Output "工作空间：http://${lanAddress}:5173"
    Write-Output "API 文档：http://${lanAddress}:8000/docs"
    Write-Output '日志位于 .runtime；停止服务请执行 scripts/stop-dev.ps1。'
} catch {
    foreach ($item in $started) { & taskkill.exe /PID $item.id /T /F 2>$null | Out-Null }
    throw
} finally {
    foreach ($name in $environment.Keys) {
        if ($null -eq $environment[$name]) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
        else { Set-Item "Env:$name" $environment[$name] }
    }
}
