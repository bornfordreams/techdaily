# Silent DuckDNS updater - runs every 5 min via Task Scheduler (TechDaily-DuckDNS)
# Updates A record (public IPv4) always; AAAA (IPv6) only when available.
# Uses curl --noproxy so the system proxy (clash etc.) can never pollute results.
# ASCII-only on purpose: PowerShell 5.1 parses no-BOM scripts as ANSI.
# Reads DUCKDNS_TOKEN / DUCKDNS_DOMAIN from ..\.env ; logs to ..\logs\duckdns.log
$ErrorActionPreference = 'SilentlyContinue'

$Root = Split-Path -Parent $PSScriptRoot
$LogFile = Join-Path $Root 'logs\duckdns.log'

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

function Fetch($url) {
    # curl.exe ignores WinINET proxy, and --noproxy kills env-var proxies too
    return (curl.exe --noproxy '*' -s -m 8 $url 2>$null | Out-String).Trim()
}

function Get-PublicV4 {
    # domestic probes first (CN international links are unreliable)
    foreach ($u in 'https://ip.3322.net', 'https://myip.ipip.net', 'https://api.ipify.org', 'https://v4.ident.me') {
        $body = Fetch $u
        if ($body -match '(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})') { return $Matches[1] }
    }
    return $null
}

function Get-PublicV6 {
    # prefer the STABLE local global address: temporary (Random-suffix) addresses
    # rotate periodically and would make the AAAA record drift.
    # The hub binds :: so every local address works for inbound.
    $cands = Get-NetIPAddress -AddressFamily IPv6 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -match '^2408:|^240e:|^2409:|^2[3-9a-f][0-9a-f]:' -and $_.IPAddress -notmatch '^fe80|^fc|^fd|^2001:0:' }
    $stable = $cands | Where-Object { $_.SuffixOrigin -ne 'Random' } | Select-Object -First 1
    if ($stable) { return $stable.IPAddress }
    foreach ($u in 'https://api6.ipify.org', 'https://v6.ident.me') {
        $ip = Fetch $u
        if ($ip -match ':') { return ($ip -split "`n")[0] }
    }
    return ($cands | Select-Object -First 1).IPAddress
}

$envMap = Read-EnvMap (Join-Path $Root '.env')
$token = $envMap['DUCKDNS_TOKEN']
$domain = $envMap['DUCKDNS_DOMAIN']
if (-not $token -or -not $domain) { exit 1 }

$v4 = Get-PublicV4
$v6 = Get-PublicV6
if (-not $v4 -and -not $v6) {
    "FAIL $(Get-Date -Format s) no-public-ip" | Add-Content $LogFile
    exit 1
}

$url = "https://www.duckdns.org/update?domains=$domain&token=$token"
if ($v4) { $url += "&ip=$v4" }
# NOTE: DuckDNS actually wants the FULL param name "ipv6" (documented as ip6,
# but ip6 is silently ignored). Raw colons are fine, no url-encoding.
if ($v6) { $url += "&ipv6=$v6" }
$resp = Fetch $url
if ($resp -like 'OK*') {
    "OK $(Get-Date -Format s) v4=$v4 v6=$v6" | Add-Content $LogFile
} else {
    "ERR $(Get-Date -Format s) v4=$v4 v6=$v6 resp=$resp" | Add-Content $LogFile
}
