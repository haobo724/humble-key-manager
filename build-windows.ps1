param([switch]$Edge)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& uv sync --extra build --extra dev --frozen
if ($LASTEXITCODE) { throw 'Dependency installation failed.' }
$env:PLAYWRIGHT_BROWSERS_PATH = '0'
$env:HUMBLE_BUILD_EDGE = if ($Edge) { '1' } else { '0' }
$folderName = if ($Edge) { 'HumbleKeyManager-Edge' } else { 'HumbleKeyManager' }
$packageName = 'humble-key-manager-v0.1.0-windows-x64' + $(if ($Edge) { '-edge' } else { '' })
$outputDir = "dist/$folderName"
if (-not $Edge) {
    & uv run --no-sync playwright install chromium
    if ($LASTEXITCODE) { throw 'Chromium installation failed.' }
}
New-Item -ItemType Directory -Force -Path build | Out-Null
& uv run --no-sync python -c "from PIL import Image; Image.open('icon.jpg').save('build/icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if ($LASTEXITCODE) { throw 'Icon conversion failed.' }
& uv run --no-sync pyinstaller --noconfirm --clean packaging/HumbleKeyManager.spec
if ($LASTEXITCODE) { throw 'Executable build failed.' }
Copy-Item -LiteralPath LICENSE -Destination "$outputDir/LICENSE.txt"
$instructions = Get-Content -LiteralPath packaging/使用说明.txt -Raw -Encoding UTF8
if ($Edge) {
    $instructions = $instructions.Replace('无需安装 Python、uv 或浏览器依赖。登录窗口使用附带的 Chromium。', '无需安装 Python 或 uv；需要本机已安装 Microsoft Edge。登录和扫描使用 Edge 的独立会话。')
}
[IO.File]::WriteAllText((Join-Path $PWD "$outputDir/使用说明.txt"), $instructions, (New-Object Text.UTF8Encoding($false)))
& uv run --no-sync python -c "import shutil,sys; from pathlib import Path; shutil.copyfile(Path(sys.base_prefix)/'LICENSE.txt', '$outputDir/PYTHON-LICENSE.txt')"
if ($LASTEXITCODE) { throw 'Python license copy failed.' }
$notices = Get-Content -LiteralPath packaging/THIRD-PARTY-NOTICES.txt -Raw -Encoding UTF8
if ($Edge) {
    $notices = $notices -replace 'Chromium: BSD and third-party licenses; see the LICENSE files in\r?\n_internal/playwright/driver/package/\.local-browsers/\.', 'The Edge edition uses the installed Microsoft Edge and does not redistribute Chromium.'
}
[IO.File]::WriteAllText((Join-Path $PWD "$outputDir/THIRD-PARTY-NOTICES.txt"), $notices, (New-Object Text.UTF8Encoding($false)))
& uv run --no-sync python -c "import shutil; shutil.make_archive('dist/$packageName', 'zip', 'dist', '$folderName')"
if ($LASTEXITCODE) { throw 'ZIP creation failed.' }
