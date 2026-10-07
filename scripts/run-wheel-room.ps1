# Run the OKU wheel room locally: http://127.0.0.1:8787/?p=kore&k=kore-local
# -Slack connects the dedicated "OKU Kolo" app (needs SLACK_OKU_WHEEL_BOT_TOKEN / SLACK_OKU_WHEEL_APP_TOKEN in env).
# -Test runs the wheel tests instead. Does NOT touch the Heimdall oku_slack service or its .venv.
param([switch]$Slack, [switch]$Test, [int]$Port = 8787, [string]$BindHost = "127.0.0.1")
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$py = Join-Path $repo ".venv-wheel\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating .venv-wheel ..."
    python -m venv .venv-wheel
    & $py -m pip install -q -r requirements-wheel.txt
}
if ($Test) { & $py -m pytest -q -p no:cacheprovider --basetemp logs\pytest-tmp tests\test_wheel.py; exit $LASTEXITCODE }
$env:OKU_WHEEL_PORT = "$Port"; $env:OKU_WHEEL_HOST = $BindHost
if ($Slack) { $env:OKU_WHEEL_SLACK = "1" } else { Remove-Item Env:OKU_WHEEL_SLACK -ErrorAction SilentlyContinue }
Write-Host "Room: http://${BindHost}:$Port/?p=kore&k=kore-local   (ICIK: ?p=icik&k=icik-local, spectator: no params)"
$url = "http://${BindHost}:$Port/?p=kore&k=kore-local"; Start-Job -ScriptBlock { param($u) Start-Sleep 2; Start-Process $u } -ArgumentList $url | Out-Null
& $py -m oku_slack.wheel.server
