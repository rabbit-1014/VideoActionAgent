param([switch]$Open)
$ErrorActionPreference = 'Stop'
$linkEntry = 'D:\CodexWork\VideoActionAgent-link-native-20261014\Start-LinkNotes.ps1'
if (-not (Test-Path -LiteralPath $linkEntry -PathType Leaf)) { throw 'The isolated link-to-native-notes installation is unavailable.' }
& $linkEntry -Open:$Open
