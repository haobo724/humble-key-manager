$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& uv sync --extra build --extra dev --frozen
if ($LASTEXITCODE) { throw 'Dependency installation failed.' }
$env:PLAYWRIGHT_BROWSERS_PATH = '0'
& uv run --no-sync playwright install chromium
if ($LASTEXITCODE) { throw 'Chromium installation failed.' }
New-Item -ItemType Directory -Force -Path build | Out-Null
& uv run --no-sync python -c "from PIL import Image; Image.open('icon.jpg').save('build/icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if ($LASTEXITCODE) { throw 'Icon conversion failed.' }
& uv run --no-sync pyinstaller --noconfirm --clean packaging/HumbleKeyManager.spec
if ($LASTEXITCODE) { throw 'Executable build failed.' }
Copy-Item -LiteralPath LICENSE -Destination dist/HumbleKeyManager/LICENSE.txt
Copy-Item -LiteralPath packaging/使用说明.txt -Destination dist/HumbleKeyManager/使用说明.txt
& uv run --no-sync python -c "import shutil,sys; from pathlib import Path; shutil.copyfile(Path(sys.base_prefix)/'LICENSE.txt', 'dist/HumbleKeyManager/PYTHON-LICENSE.txt')"
if ($LASTEXITCODE) { throw 'Python license copy failed.' }
Copy-Item -LiteralPath packaging/THIRD-PARTY-NOTICES.txt -Destination dist/HumbleKeyManager/THIRD-PARTY-NOTICES.txt
& uv run --no-sync python -c "import shutil; shutil.make_archive('dist/humble-key-manager-v0.1.0-windows-x64', 'zip', 'dist', 'HumbleKeyManager')"
if ($LASTEXITCODE) { throw 'ZIP creation failed.' }
