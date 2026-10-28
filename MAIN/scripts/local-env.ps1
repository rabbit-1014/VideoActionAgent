$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$Config = Get-Content -LiteralPath (Join-Path $ProjectRoot '.local/runtime.json') -Raw | ConvertFrom-Json
$Password = (Get-Content -LiteralPath (Join-Path $ProjectRoot '.local/pg-password.txt') -Raw).Trim()
$env:PGPASSWORD = $Password
$env:VIDEO_DATABASE_URL = "postgresql+psycopg://video_dev:${Password}@127.0.0.1:$($Config.port)/video_action_dev"
$env:VIDEO_TEST_DATABASE_URL = "postgresql+psycopg://video_dev:${Password}@127.0.0.1:$($Config.port)/video_action_test"
$env:VIDEO_API_TOKEN = (Get-Content -LiteralPath (Join-Path $ProjectRoot '.local/api-token.txt') -Raw).Trim()
$env:VIDEO_OWNER_ID = 'local-developer'

# A running Codex/PowerShell host may predate the user's Machine-level API key
# configuration. Load it into this local development process without echoing it;
# an explicitly supplied process key keeps precedence.
foreach ($Name in @('DEEPSEEK_API_KEY', 'ZHIPUAI_API_KEY')) {
    if (-not [Environment]::GetEnvironmentVariable($Name, 'Process')) {
        $MachineValue = [Environment]::GetEnvironmentVariable($Name, 'Machine')
        if ($MachineValue) { [Environment]::SetEnvironmentVariable($Name, $MachineValue, 'Process') }
    }
}

