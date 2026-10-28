param([string]$CheckRoot = (Join-Path $env:PUBLIC 'VideoActionAgentChecks'))
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$Config = Get-Content -LiteralPath (Join-Path $ProjectRoot '.local/runtime.json') -Raw | ConvertFrom-Json
$Bin = Join-Path $Config.pg_root 'pgsql/bin'
$Archive = Join-Path $env:USERPROFILE 'Documents/Codex/video-action-agent-research'
New-Item -ItemType Directory -Path $CheckRoot -Force | Out-Null
$Run = Join-Path $CheckRoot ([guid]::NewGuid().ToString('N'))
$Data = Join-Path $Run 'pgdata'
$PwFile = Join-Path $Run 'password.txt'
$EnvNames = @('PGPASSWORD','VIDEO_DATABASE_URL','VIDEO_TEST_DATABASE_URL','VIDEO_API_TOKEN','VIDEO_OWNER_ID','VIDEO_RESEARCH_ROOT','PYTHONPATH','VIRTUAL_ENV')
$Saved = @{}
foreach ($Name in $EnvNames) { $Saved[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process') }
$Started = $false
function CheckExit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed ($LASTEXITCODE)" }
}
Push-Location $ProjectRoot
try {
    $env:PYTHONPATH = $null
    $env:VIRTUAL_ENV = $null
    uv sync --locked --offline
    CheckExit 'Locked offline dependency setup'
    uv run --offline python -c "import pathlib,video_action_agent; p=pathlib.Path(video_action_agent.__file__).resolve(); r=pathlib.Path.cwd().resolve(); print('Loaded code:',p); assert p.is_relative_to(r/'src')"
    CheckExit 'New-directory import check'

    # A fresh loopback-only cluster; never start or connect to the development cluster.
    New-Item -ItemType Directory -Path $Run | Out-Null
    $Password = [guid]::NewGuid().ToString('N')
    [IO.File]::WriteAllText($PwFile, $Password, [Text.Encoding]::ASCII)
    $Listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $Listener.Start()
    $Port = $Listener.LocalEndpoint.Port
    $Listener.Stop()
    if ($Port -eq $Config.port) { throw 'Refusing development database port' }
    & "$Bin/initdb.exe" -D $Data -U migration_check --pwfile=$PwFile --auth=scram-sha-256 --encoding=UTF8 --locale=C *> (Join-Path $ProjectRoot '.local/check-initdb.log')
    CheckExit 'Isolated database initialization'
    & "$Bin/pg_ctl.exe" -D $Data -l (Join-Path $Run 'postgres.log') -o "-h 127.0.0.1 -p $Port" -w start
    CheckExit 'Isolated database start'
    $Started = $true
    $env:PGPASSWORD = $Password
    & "$Bin/createdb.exe" -h 127.0.0.1 -p $Port -U migration_check migration_check_test
    CheckExit 'Isolated test database creation'
    $Url = "postgresql+psycopg://migration_check:${Password}@127.0.0.1:${Port}/migration_check_test"
    $env:VIDEO_DATABASE_URL = $Url
    $env:VIDEO_TEST_DATABASE_URL = $Url
    $env:VIDEO_API_TOKEN = 'local-check-only'
    $env:VIDEO_OWNER_ID = 'local-check'
    $env:VIDEO_RESEARCH_ROOT = $Archive
    $Temp = '.local/pytest-check-' + [guid]::NewGuid().ToString('N')
    uv run --offline pytest -q --basetemp=$Temp --junitxml=.local/local-check.xml
    CheckExit 'Local tests'
    uv run --offline ruff check .
    CheckExit 'Lint'
    Write-Output 'PASS: new-directory code, isolated local database, no live source calls.'
} finally {
    $Stopped = -not $Started
    if ($Started) {
        & "$Bin/pg_ctl.exe" -D $Data -m fast -w stop
        $Stopped = $LASTEXITCODE -eq 0
    }
    foreach ($Name in $EnvNames) { [Environment]::SetEnvironmentVariable($Name, $Saved[$Name], 'Process') }
    Pop-Location
    if ($Stopped -and (Test-Path -LiteralPath $Run)) {
        $Resolved = (Resolve-Path -LiteralPath $Run).Path
        $Allowed = [IO.Path]::GetFullPath($CheckRoot).TrimEnd('\') + '\'
        if (-not $Resolved.StartsWith($Allowed, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe cleanup target' }
        Remove-Item -LiteralPath $Resolved -Recurse -Force
    } elseif (-not $Stopped) {
        throw "Temporary database did not stop; inspect $Run"
    }
}
