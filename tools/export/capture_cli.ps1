param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string[]]$CliArgs,
    [string]$OutputDir = (Join-Path $PSScriptRoot '../../docs/history/retrieval-selection-kit/.private'),
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
if ($Name -notmatch '^[A-Za-z0-9-]+$') { throw 'Name must contain only letters, digits and hyphens.' }
$captureDir = [IO.Path]::GetFullPath($OutputDir)
[IO.Directory]::CreateDirectory($captureDir) | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)
$started = [DateTime]::UtcNow
$timer = [Diagnostics.Stopwatch]::StartNew()
$stamp = $started.ToString('yyyyMMddTHHmmssfffffffZ')
$outputFile = Join-Path $captureDir "$stamp-$Name.log"
$env:PYTHONIOENCODING = 'utf-8'
# Windows PowerShell decodes native process output using this encoding.
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$nativePreference = $ErrorActionPreference
$previousPythonPath = $env:PYTHONPATH
$sourcePath = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) 'src'
$env:PYTHONPATH = $sourcePath
if ($previousPythonPath) { $env:PYTHONPATH += [IO.Path]::PathSeparator + $previousPythonPath }
$ErrorActionPreference = 'Continue'
try {
    $output = & $Python -m rfp_assistant.cli @CliArgs 2>&1
    $commandExit = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $previousPythonPath
    $ErrorActionPreference = $nativePreference
}
$timer.Stop()
if ($null -eq $commandExit) { $commandExit = 127 }
$lines = @($output | ForEach-Object { $_.ToString() })
[IO.File]::WriteAllText($outputFile, ($lines -join "`n") + "`n", $utf8)
$record = [ordered]@{
    name = $Name; argv = @($Python, '-m', 'rfp_assistant.cli') + $CliArgs
    exit = $commandExit; started = $started.ToString('o'); seconds = $timer.Elapsed.TotalSeconds
    output = [IO.Path]::GetFileName($outputFile)
}
[IO.File]::AppendAllText((Join-Path $captureDir 'steps.jsonl'), ($record | ConvertTo-Json -Compress) + "`n", $utf8)
$lines | ForEach-Object { Write-Output $_ }
if ($commandExit -ne 0) { throw "CLI failed with exit $commandExit. Output and step record were retained." }
