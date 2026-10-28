param([switch]$Open)
$ErrorActionPreference = 'Stop'
$pilotEntry = 'D:\CodexWork\VideoActionAgent-downstream-20261014\Start-NativeNotes.ps1'
if (-not (Test-Path -LiteralPath $pilotEntry -PathType Leaf)) { throw 'The isolated native notes installation is unavailable.' }
& $pilotEntry -Open:$Open
