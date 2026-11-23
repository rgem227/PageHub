$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pg = Join-Path $root 'postgres\program\bin'
$data = Join-Path $root 'postgres\data'
$credDir = Join-Path $root 'credentials'
$logs = Join-Path $root 'logs\postgres'
$python = Join-Path $root 'app\.venv\Scripts\python.exe'
$serviceName = 'PageHubPostgreSQL'

if (Test-Path (Join-Path $data 'PG_VERSION')) { throw '生产数据库已初始化，拒绝重复执行。' }
if (-not (Test-Path (Join-Path $PSScriptRoot 'nginx.conf.template'))) { throw '未找到 nginx.conf.template，请将完整部署脚本复制到 D:\PageHub\scripts。' }
if (-not (Test-Path -LiteralPath $python)) { throw '未找到 app\.venv；请先创建虚拟环境并安装 backend\requirements.lock.txt。' }
if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) { throw "Windows 服务 $serviceName 已存在。" }
if (Get-NetTCPConnection -State Listen -LocalPort 55432 -ErrorAction SilentlyContinue) { throw '端口 55432 已被占用。' }

$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric,InterfaceMetric | Select-Object -First 1
$ip = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -AddressState Preferred |
    Where-Object { $_.IPAddress -notlike '127.*' } | Select-Object -First 1 -ExpandProperty IPAddress
if (-not $ip) { throw '无法取得默认网卡的 IPv4 地址。' }

$bytes = New-Object byte[] 36
[Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$pgPassword = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
$pwFile = Join-Path $credDir 'postgres-init-password.tmp'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
[IO.File]::WriteAllText($pwFile, $pgPassword, [Text.UTF8Encoding]::new($false))
icacls $credDir /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(OI)(CI)F" 'NT AUTHORITY\SYSTEM:(OI)(CI)F' 'BUILTIN\Administrators:(OI)(CI)F' | Out-Null

& (Join-Path $pg 'initdb.exe') -D $data -U postgres --auth-host=scram-sha-256 --auth-local=scram-sha-256 --pwfile=$pwFile --encoding=UTF8 --locale=C
if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL initdb 失败。' }
Remove-Item -LiteralPath $pwFile -Force

$config = @"
listen_addresses = '127.0.0.1'
port = 55432
max_connections = 150
password_encryption = 'scram-sha-256'
shared_buffers = '256MB'
logging_collector = on
log_directory = 'D:/PageHub/logs/postgres'
log_filename = 'postgresql-%Y-%m-%d.log'
log_rotation_age = '1d'
"@
Add-Content -LiteralPath (Join-Path $data 'postgresql.conf') -Value $config -Encoding utf8
icacls $data /grant 'NT AUTHORITY\NETWORK SERVICE:(OI)(CI)M' /T | Out-Null
icacls $logs /grant 'NT AUTHORITY\NETWORK SERVICE:(OI)(CI)M' /T | Out-Null
icacls $root /grant 'NT AUTHORITY\NETWORK SERVICE:(OI)(CI)RX' | Out-Null
& (Join-Path $pg 'pg_ctl.exe') register -N $serviceName -D $data -U 'NT AUTHORITY\NetworkService' -S auto
if ($LASTEXITCODE -ne 0) { throw '注册 PageHub PostgreSQL 服务失败。' }
Start-Service -Name $serviceName

$ready = $false
for ($i=0; $i -lt 40; $i++) {
    & (Join-Path $pg 'pg_isready.exe') -h 127.0.0.1 -p 55432 -U postgres *> $null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $ready) { throw 'PageHub PostgreSQL 未能在 40 秒内就绪。' }

$env:PAGEHUB_LAN_IP = $ip
$env:PAGEHUB_PG_ADMIN_PASSWORD = $pgPassword
& $python (Join-Path $PSScriptRoot 'bootstrap.py')
if ($LASTEXITCODE -ne 0) { throw '生产数据库和管理员初始化失败。' }

$nginxConfig = Join-Path $root 'nginx\conf\nginx.conf'
$nginxText = (Get-Content (Join-Path $PSScriptRoot 'nginx.conf.template') -Raw).Replace('__LAN_IP__', $ip)
[IO.File]::WriteAllText($nginxConfig, $nginxText, [Text.UTF8Encoding]::new($false))
icacls (Join-Path $root 'certs') /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(OI)(CI)F" 'NT AUTHORITY\SYSTEM:(OI)(CI)F' 'BUILTIN\Administrators:(OI)(CI)F' 'NT AUTHORITY\NETWORK SERVICE:(OI)(CI)RX' | Out-Null
icacls $credDir /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):(OI)(CI)F" 'NT AUTHORITY\SYSTEM:(OI)(CI)F' 'BUILTIN\Administrators:(OI)(CI)F' | Out-Null
icacls (Join-Path $root 'app\.env') /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):F" 'NT AUTHORITY\SYSTEM:F' 'BUILTIN\Administrators:F' | Out-Null
Import-Certificate -FilePath (Join-Path $root 'certs\pagehub-trust.cer') -CertStoreLocation 'Cert:\LocalMachine\Root' | Out-Null
$firewallRule = Get-NetFirewallRule -DisplayName 'PageHub Production HTTPS' -ErrorAction SilentlyContinue
if (-not $firewallRule) {
    $firewallRule = New-NetFirewallRule -DisplayName 'PageHub Production HTTPS' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 51730,51731 -Profile Any -RemoteAddress LocalSubnet
} else {
    Set-NetFirewallRule -InputObject $firewallRule -Profile Any
    Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $firewallRule | Set-NetFirewallAddressFilter -RemoteAddress LocalSubnet
}

Write-Output "初始化完成。访问地址: https://${ip}:51730"
Write-Output '证书信任文件（其他电脑首次访问前导入）: certs\pagehub-trust.cer'
$env:PAGEHUB_PG_ADMIN_PASSWORD = $null
