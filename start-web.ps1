$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is not installed or is missing from PATH. Install uv and retry.'
}
$launchArgs = @('run', '--frozen', 'python', '-m', 'humble_bundle_keys.web')
$dataConfig = Join-Path $PSScriptRoot 'data-dir.local.txt'
if ((Test-Path -LiteralPath $dataConfig) -and ($args -notcontains '--data-dir')) {
    $savedDataDir = (Get-Content -LiteralPath $dataConfig -Raw -Encoding UTF8).Trim()
    if ($savedDataDir) {
        $launchArgs += @('--data-dir', $savedDataDir)
    }
}
& uv @launchArgs @args
exit $LASTEXITCODE
