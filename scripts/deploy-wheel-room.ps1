# Deploy the static wheel room to https://itzkore.cz/oku/kolo/ (FTP /www/oku/kolo, upload-only, never deletes).
# WS goes through the Cloudflare quick tunnel (Heimdall service oku_wheel_tunnel); its host changes on every
# tunnel restart, so re-run this script after a tunnel restart. Uses C:\code\umbra\tools\_upload_umbra_resilient.js
# and the FTP credentials it already loads from umbra .env (never printed).
param([string]$WsUrl = "", [string]$Remote = "/www/oku/kolo")
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
if (-not $WsUrl) {
    $cf = Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" | ?{ $_.CommandLine -match "127\.0\.0\.1:8797" } | Select -First 1
    if (-not $cf) { throw "oku_wheel_tunnel (cloudflared -> :8797) is not running" }
    $host_ = $null
    foreach ($c in Get-NetTCPConnection -OwningProcess $cf.ProcessId -State Listen) {
        try { $host_ = (Invoke-RestMethod "http://127.0.0.1:$($c.LocalPort)/quicktunnel" -TimeoutSec 3).hostname; if ($host_) { break } } catch {}
    }
    if (-not $host_) { throw "could not read quick tunnel hostname from cloudflared metrics" }
    $WsUrl = "wss://$host_/ws"
}
$dist = Join-Path $repo "logs\wheel-dist"
Remove-Item -Recurse -Force $dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$dist\img" | Out-Null
Copy-Item "$repo\oku_slack\wheel\static\room.html" "$dist\index.html"
[IO.File]::WriteAllText("$dist\config.js", "window.OKU_WS_URL = `"$WsUrl`";`n", (New-Object Text.UTF8Encoding $false))
$py = "$repo\.venv-wheel\Scripts\python.exe"
& $py -c "from oku_slack.wheel import config,render; c=config.load(); from oku_slack.wheel import panel; import pathlib; s=[{'key':x['key'],'color':x['color'],'title':x['title'],'host_name':panel.HOST_NAMES.get(x.get('host',''),''),'avatar':(lambda p: str(p) if p.exists() else None)(config.ROOT/'assets'/'avatars'/(x.get('host','')+'.png'))} for x in c['events']]; print(len(render.render_assets(r'$dist\img', s, c['scripts']['titanic'])), 'images')"
Write-Host "WS: $WsUrl"
Push-Location C:\code\umbra
try { node tools\_upload_umbra_resilient.js $dist $Remote } finally { Pop-Location }