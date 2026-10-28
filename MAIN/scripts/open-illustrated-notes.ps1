param([switch]$Open)
$ErrorActionPreference = 'Stop'
$imageEntry = 'D:\CodexWork\VideoActionAgent-illustrated-notes-20261014\Start-LinkNotes.ps1'
if (-not (Test-Path -LiteralPath $imageEntry -PathType Leaf)) { throw 'The optional-image native notes installation is unavailable.' }
& $imageEntry -Open:$Open
