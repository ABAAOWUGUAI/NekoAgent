[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot "bridge.env.local"),
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "bridge.env.example") -Destination $ConfigPath
    throw "Created $ConfigPath. Create its distinct admin/channel token files, review the paths, then run this command again."
}

$values = @{}
foreach ($raw in Get-Content -LiteralPath $ConfigPath -Encoding utf8) {
    $line = $raw.Trim()
    if (-not $line -or $line.StartsWith("#")) { continue }
    $separator = $line.IndexOf("=")
    if ($separator -lt 1) { throw "Invalid configuration line: $line" }
    $name = $line.Substring(0, $separator).Trim()
    $value = $line.Substring($separator + 1).Trim()
    $values[$name] = $value
    Set-Item -Path "Env:$name" -Value $value
}

# Resolve a file, never an executable discovered from PATH. Environment
# imports above do not alter the interpreter selected for this repository.
if (-not $PythonPath) {
    $PythonPath = Join-Path $root ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Virtual environment interpreter not found: $PythonPath. Create .venv and install requirements.lock using that interpreter, or pass -PythonPath explicitly."
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).ProviderPath

foreach ($tokenSetting in @("TOKEN_PATH", "CHANNEL_TOKEN_PATH")) {
    $tokenPath = $values[$tokenSetting]
    if (-not $tokenPath -or -not (Test-Path -LiteralPath (Join-Path $root $tokenPath) -PathType Leaf)) {
        throw "$tokenSetting must point to an existing local token file."
    }
}

Push-Location -LiteralPath $root
try {
    & $PythonPath .\codex_qq_bridge.py
    if ($LASTEXITCODE -ne 0) {
        throw "Bridge exited with status $LASTEXITCODE."
    }
} finally {
    Pop-Location
}
