Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
& 'C:\Python315\python.exe' "$(Split-Path -Parent $PSScriptRoot)\run_daily.py" *>> "$(Split-Path -Parent $PSScriptRoot)\logs\task_out.log"
"[$(Get-Date -Format s)] run_daily exit=$LASTEXITCODE" | Add-Content -LiteralPath "$(Split-Path -Parent $PSScriptRoot)\logs\task_out.log"
exit $LASTEXITCODE
