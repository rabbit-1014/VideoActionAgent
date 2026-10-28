[CmdletBinding()]
param([switch]$Open,[ValidateSet('deepseek','codex')][string]$TextWriter='codex',
    [ValidatePattern('^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')][string]$ContentId)
$ErrorActionPreference='Stop'
$taskRoot=$PSScriptRoot
$nativeRoot='D:\CodexWork\VideoActionAgent-downstream-20261014'
$frontRoot=Join-Path $nativeRoot 'BiliNote\BillNote_frontend'
$runtimeRoot=Join-Path $taskRoot 'runtime'
$env:TEMP=Join-Path $taskRoot 'tmp'
$env:TMP=$env:TEMP
$env:PYTHONIOENCODING='utf-8'
$env:PATH='C:\Program Files\nodejs;' + $env:PATH
New-Item -ItemType Directory -Path $runtimeRoot,$env:TEMP -Force | Out-Null
foreach ($port in 8850,8851) {
    foreach ($listener in @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
        $ownedProcess=Get-CimInstance Win32_Process -Filter ('ProcessId='+$listener.OwningProcess)
        $expectedRoot=if ($port -eq 8850) {$taskRoot} else {$frontRoot}
        $normalizedCommand=$ownedProcess.CommandLine.Replace('/','\')
        if ($listener.LocalAddress -ne '127.0.0.1' -or $normalizedCommand -notlike ('*'+$expectedRoot.Replace('/','\')+'*')) {
            throw "Port $port is occupied by an unrelated process; no process was stopped."
        }
    }
}
if (-not (Get-NetTCPConnection -LocalPort 8850 -State Listen -ErrorAction SilentlyContinue)) {
    if (-not $env:DEEPSEEK_API_KEY) {$env:DEEPSEEK_API_KEY=[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','User')}
    if (-not $env:DEEPSEEK_API_KEY) {$env:DEEPSEEK_API_KEY=[Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY','Machine')}
    if (-not $env:ZHIPUAI_API_KEY) {$env:ZHIPUAI_API_KEY=[Environment]::GetEnvironmentVariable('ZHIPUAI_API_KEY','User')}
    if (-not $env:ZHIPUAI_API_KEY) {$env:ZHIPUAI_API_KEY=[Environment]::GetEnvironmentVariable('ZHIPUAI_API_KEY','Machine')}
    if ($TextWriter -eq 'deepseek' -and -not $env:DEEPSEEK_API_KEY) {Write-Warning 'Selected text-model key is unavailable. Saved content remains readable; new text generation is unavailable.'}
    Start-Process -FilePath (Join-Path $nativeRoot 'venv\Scripts\python.exe') -ArgumentList @((Join-Path $taskRoot 'purpose_app.py'),'--text-writer',$TextWriter) -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeRoot 'api-stdout.log') -RedirectStandardError (Join-Path $runtimeRoot 'api-stderr.log') | Out-Null
}
if (-not (Get-NetTCPConnection -LocalPort 8851 -State Listen -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath 'C:\Program Files\nodejs\node.exe' -ArgumentList @((Join-Path $frontRoot 'node_modules\vite\bin\vite.js'),'--config',(Join-Path $frontRoot 'vite.purpose.config.ts')) -WorkingDirectory $frontRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtimeRoot 'ui-stdout.log') -RedirectStandardError (Join-Path $runtimeRoot 'ui-stderr.log') | Out-Null
}
$ready=$false
for ($attempt=0;$attempt -lt 30;$attempt++) {
    if ((Get-NetTCPConnection -LocalPort 8850 -State Listen -ErrorAction SilentlyContinue) -and (Get-NetTCPConnection -LocalPort 8851 -State Listen -ErrorAction SilentlyContinue)) {$ready=$true;break}
    Start-Sleep -Seconds 1
}
if (-not $ready) {throw 'Local link reader not ready. No source/model request was submitted.'}
if ($Open) {
    $localSession=Get-Content -LiteralPath (Join-Path $runtimeRoot 'ui-session.json') -Raw | ConvertFrom-Json
    $contentQuery=if ($ContentId) {'?content='+[uri]::EscapeDataString($ContentId.ToLowerInvariant())} else {''}
    Start-Process -FilePath ('http://127.0.0.1:8851/purpose.html'+$contentQuery+'#token='+[uri]::EscapeDataString($localSession.token)) -WindowStyle Hidden | Out-Null
}
Write-Output 'Video outputs ready on 127.0.0.1:8851; no processing request submitted.'
