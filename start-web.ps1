$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
uv run --frozen humble-bundle-keys-web
