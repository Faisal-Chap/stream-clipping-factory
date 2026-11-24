param(
    [int]$Port = 8765,
    [string]$Data = '',
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$projectPython = @('.local/tools/python', '.local/tools/semantic') | ForEach-Object {
    Join-Path $PSScriptRoot $_
} | Where-Object { Test-Path -LiteralPath $_ }
if ($projectPython) {
    $env:PYTHONPATH = (@($projectPython) + @($env:PYTHONPATH) | Where-Object { $_ }) -join [IO.Path]::PathSeparator
}

$factoryArgs = @('-m', 'factory', '--port', $Port)
if ($Data) { $factoryArgs += @('--data', [IO.Path]::GetFullPath($Data)) }
if (!$NoBrowser) { $factoryArgs += '--open' }

Push-Location -LiteralPath $PSScriptRoot
try {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py @factoryArgs
    } else {
        & python @factoryArgs
    }
} finally {
    Pop-Location
}
