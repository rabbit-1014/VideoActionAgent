param([ValidateSet('Start','Stop','Status')][string]$Action = 'Status')
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$Config = Get-Content -LiteralPath (Join-Path $ProjectRoot '.local/runtime.json') -Raw | ConvertFrom-Json
$Control = Join-Path $Config.pg_root 'pgsql/bin/pg_ctl.exe'
$Data = Join-Path $Config.pg_root 'pgdata'
if ($Action -eq 'Start') {
    & $Control -D $Data status *> $null
    if ($LASTEXITCODE -ne 0) {
        & $Control -D $Data -l (Join-Path $ProjectRoot '.local/postgres.log') -o "-h 127.0.0.1 -p $($Config.port)" -w start
        if ($LASTEXITCODE -ne 0) { throw 'Database startup failed' }
    }
} elseif ($Action -eq 'Stop') {
    & $Control -D $Data -m fast -w stop
    if ($LASTEXITCODE -ne 0) { throw 'Database stop failed' }
} else {
    & $Control -D $Data status
}

