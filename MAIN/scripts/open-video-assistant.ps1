param([switch]$Open,
    [ValidatePattern('^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')][string]$ContentId)
$ErrorActionPreference='Stop'
$taskArguments=@{Open=$Open}
if ($ContentId) {$taskArguments.ContentId=$ContentId}
& 'D:/CodexWork/VideoActionAgent-purpose-output-20261015/Start-Purpose.ps1' @taskArguments
