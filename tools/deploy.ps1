# TechDaily one-shot deployer: IPv6 dual-stack hub + DuckDNS + firewall + scheduled tasks
# Usage:
#   powershell -ExecutionPolicy Bypass -File deploy.ps1            # install everything
#   powershell -ExecutionPolicy Bypass -File deploy.ps1 -Uninstall # remove tasks + firewall rule
# NOTE: all Chinese lives in config/.env (UTF-8); this script is ASCII-only on purpose
#       because Windows PowerShell 5.1 reads no-BOM scripts as ANSI.
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
# Python 自动探测（不写死路径；conda/官方安装/venv 均可）
$Pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $Pyw) { $Pyw = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $Pyw) { throw '未找到 Python：请先安装并加入 PATH（python.org 或 conda 均可）' }
$Py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = $Pyw }
$TaskDaily = 'TechDaily-Daily'
$TaskDuck = 'TechDaily-DuckDNS'
$FwRule = 'TechDaily Hub'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunName = 'TechDaily-Hub'

# 生成适配本机的任务包装脚本（定时任务用绝对 Python 路径，避免任务环境找不到 conda）
$RunTask = @"
Set-Location -LiteralPath '$Root'
& '$Py' '$Root\run_daily.py' *>> '$Root\logs\task_out.log'
"[`$(Get-Date -Format s)] run_daily exit=`$LASTEXITCODE" | Add-Content -LiteralPath '$Root\logs\task_out.log'
exit `$LASTEXITCODE
"@
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'run_task.ps1'), $RunTask, (New-Object Text.UTF8Encoding($true)))

if ($Uninstall) {
    schtasks /Delete /TN $TaskDaily /F 2>$null
    schtasks /Delete /TN $TaskDuck /F 2>$null
    netsh advfirewall firewall delete rule name="$FwRule" | Out-Null
    Remove-ItemProperty -Path $RunKey -Name $RunName -ErrorAction SilentlyContinue
    Write-Host 'Uninstalled: scheduled tasks + firewall rule + autostart entry. (files kept)'
    exit 0
}

# ---------- [1/6] config: hub binds :: (IPv6 + IPv4 dual stack) ----------
# IMPORTANT: always read/write UTF-8 explicitly; PS 5.1 defaults to ANSI and
# would silently corrupt every Chinese comment/keyword in the file.
$cfg = Join-Path $Root 'config.toml'
if (-not (Test-Path $cfg)) { throw "config.toml not found: $cfg" }
$raw = [IO.File]::ReadAllText($cfg, [Text.Encoding]::UTF8)
$raw = $raw -replace '(?m)^bind\s*=.*$', 'bind = "::"'
[IO.File]::WriteAllText($cfg, $raw, (New-Object Text.UTF8Encoding($false)))
$port = 8300
foreach ($line in ($raw -split "`n")) {
    if ($line -match '^\s*port\s*=\s*(\d+)') { $port = $Matches[1]; break }
}
Write-Host "[1/6] config.toml -> bind = ::  (hub port $port)"

# ---------- [2/6] firewall ----------
netsh advfirewall firewall delete rule name="$FwRule" 2>$null | Out-Null
$null = netsh advfirewall firewall add rule name="$FwRule" dir=in action=allow protocol=TCP localport=$port 2>&1
if ($LASTEXITCODE -eq 0) { Write-Host "[2/6] Windows firewall: inbound TCP $port allowed" }
else { Write-Warning "[2/6] firewall rule FAILED (need admin once): run -> netsh advfirewall firewall add rule name=`"$FwRule`" dir=in action=allow protocol=TCP localport=$port" }

# ---------- [3/6] DuckDNS ----------
function Read-EnvMap($path) {
    $map = @{}
    if (Test-Path $path) {
        foreach ($line in ([IO.File]::ReadAllLines($path, [Text.Encoding]::UTF8))) {
            $t = $line.Trim()
            if ($t -and -not $t.StartsWith('#') -and $t.Contains('=')) {
                $k, $v = $t.Split('=', 2)
                $map[$k.Trim()] = $v.Trim()
            }
        }
    }
    return $map
}
$envMap = Read-EnvMap (Join-Path $Root '.env')
$token = $envMap['DUCKDNS_TOKEN']
$domain = $envMap['DUCKDNS_DOMAIN']
if ($token -and $domain) {
    & (Join-Path $PSScriptRoot 'update_duckdns.ps1')
    $log = Get-Content (Join-Path $Root 'logs\duckdns.log') -Tail 1 -ErrorAction SilentlyContinue
    Write-Host "[3/6] DuckDNS: $domain.duckdns.org -> $log"
} else {
    Write-Warning '[3/6] .env missing DUCKDNS_TOKEN / DUCKDNS_DOMAIN - DuckDNS SKIPPED (fill .env then rerun deploy.ps1)'
    Write-Host '       get a free token at https://www.duckdns.org (login, create subdomain)'
}

# ---------- [4/6] scheduled tasks + autostart ----------
schtasks /Create /TN $TaskDaily /TR "`"$Pyw`" `"$Root\run_daily.py`"" /SC DAILY /ST 06:00 /F 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Set-ScheduledTask -TaskName $TaskDaily -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable) | Out-Null
    Write-Host '[4/6] task Daily@06:00 OK (catch-up if missed)'
} else { Write-Warning '[4/6] task Daily@06:00 FAILED' }
schtasks /Create /TN $TaskDuck /TR "powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\update_duckdns.ps1`"" /SC MINUTE /MO 5 /F 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) { Write-Host '[4/6] task DuckDNS@5min OK' } else { Write-Warning '[4/6] task DuckDNS@5min FAILED' }
# hub autostart: HKCU Run key (works without admin, unlike ONLOGON tasks)
Set-ItemProperty -Path $RunKey -Name $RunName -Value "`"$Pyw`" `"$Root\run_hub.py`""
Write-Host '[4/6] hub autostart: HKCU Run OK'

# ---------- [5/6] start hub now + local self-test ----------
$alive = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if (-not $alive) {
    Start-Process $Pyw -ArgumentList "`"$Root\run_hub.py`""
    Start-Sleep -Seconds 3
}
$ok4 = $false; $ok6 = $false
try { $r = Invoke-RestMethod "http://127.0.0.1:$port/api/ping" -TimeoutSec 5; $ok4 = ($r.ok -eq $true) } catch { }
try { $r = Invoke-RestMethod "http://[::1]:$port/api/ping" -TimeoutSec 5; $ok6 = ($r.ok -eq $true) } catch { }
Write-Host "[5/6] hub self-test: IPv4=$ok4 IPv6=$ok6  (expect True True)"

# ---------- [6/6] summary ----------
Write-Host '================ DONE ================'
if ($domain) { Write-Host "APK server URL : http://$domain.duckdns.org:$port" }
else { Write-Host "APK server URL : http://<your-subdomain>.duckdns.org:$port  (set .env then rerun deploy)" }
Write-Host 'Team code      : see config.toml [hub].team_code'
Write-Host "NOTE: if phone (cellular data) cannot connect, check ROUTER IPv6 firewall allows inbound TCP port $port"
