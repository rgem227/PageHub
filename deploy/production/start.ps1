$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$app = Join-Path $root 'app'
$python = Join-Path $app '.venv\Scripts\python.exe'
$nginx = Join-Path $root 'nginx\nginx.exe'
$rustfs = Join-Path $root 's3\rustfs.exe'
$runtime = Join-Path $root 'logs'
$recordPath = Join-Path $runtime 'processes.json'
$envPath = Join-Path $app '.env'
$serviceName = 'PageHubPostgreSQL'

if (-not (Test-Path $envPath) -or -not (Test-Path $python) -or -not (Test-Path $nginx) -or -not (Test-Path $rustfs)) {
    throw '生产文件不完整，请先按 部署说明.md 完成初始化。'
}
if (Test-Path $recordPath) { throw '发现已有启动记录，请先执行 stop.ps1。' }
foreach ($port in @(51730,51731,58000,58011,59000)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "端口 $port 已被占用。" }
}
if ((Get-Service -Name $serviceName).Status -ne 'Running') { Start-Service -Name $serviceName }

$values = @{}
foreach ($line in Get-Content $envPath) {
    if ($line -match '^([A-Z0-9_]+)=(.*)$') { $values[$matches[1]] = $matches[2].Trim('"') }
}
$env:RUSTFS_ACCESS_KEY = $values['S3_ACCESS_KEY']
$env:RUSTFS_SECRET_KEY = $values['S3_SECRET_KEY']
$env:RUSTFS_ADDRESS = '127.0.0.1:59000'

$started = [Collections.Generic.List[object]]::new()
try {
    $store = Start-Process -FilePath $rustfs -ArgumentList @('server',(Join-Path $root 's3\data'),'--address','127.0.0.1:59000') -WorkingDirectory (Join-Path $root 's3') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 's3.log') -RedirectStandardError (Join-Path $runtime 's3.err.log')
    $started.Add(@{name='s3';id=$store.Id;start_ticks=$store.StartTime.ToUniversalTime().Ticks})

    $ready = $false
    for ($i=0; $i -lt 60; $i++) {
        if (Get-NetTCPConnection -State Listen -LocalPort 59000 -ErrorAction SilentlyContinue) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'S3 服务未能在 60 秒内就绪，请查看 logs\s3.err.log。' }

    $env:PYTHONPATH = Join-Path $app 'backend'
    & $python (Join-Path $PSScriptRoot 'ensure_bucket.py')
    if ($LASTEXITCODE -ne 0) { throw '初始化 S3 bucket 失败。' }

    $api = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','58000','--workers','4') -WorkingDirectory (Join-Path $app 'backend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'api.log') -RedirectStandardError (Join-Path $runtime 'api.err.log')
    $started.Add(@{name='api';id=$api.Id;start_ticks=$api.StartTime.ToUniversalTime().Ticks})
    $content = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','app.content:app','--host','127.0.0.1','--port','58011','--workers','2','--no-access-log') -WorkingDirectory (Join-Path $app 'backend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'content.log') -RedirectStandardError (Join-Path $runtime 'content.err.log')
    $started.Add(@{name='content';id=$content.Id;start_ticks=$content.StartTime.ToUniversalTime().Ticks})
    $worker = Start-Process -FilePath $python -ArgumentList @('-m','app.worker') -WorkingDirectory (Join-Path $app 'backend') -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtime 'worker.log') -RedirectStandardError (Join-Path $runtime 'worker.err.log')
    $started.Add(@{name='worker';id=$worker.Id;start_ticks=$worker.StartTime.ToUniversalTime().Ticks})

    $apiReady = $false
    for ($i=0; $i -lt 40; $i++) {
        try { $null = Invoke-RestMethod -Uri 'http://127.0.0.1:58000/api/health' -TimeoutSec 2; $apiReady = $true; break } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $apiReady) { throw 'PageHub API 未能在 40 秒内就绪，请查看 logs\api.err.log。' }
    $null = Start-Process -FilePath $nginx -ArgumentList @('-p',"$root\nginx",'-c','conf/nginx.conf') -WorkingDirectory (Join-Path $root 'nginx') -WindowStyle Hidden -PassThru
    $started | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding utf8
    Write-Output "PageHub 已启动: $($values['WEB_ORIGIN'])"
    Write-Output '内容服务、API 和 PostgreSQL 仅监听本机；外部访问经 Nginx HTTPS 入口。'
} catch {
    foreach ($item in $started) { & taskkill.exe /PID $item.id /T /F *> $null }
    throw
}
