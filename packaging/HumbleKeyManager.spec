from pathlib import Path
import os

root = Path(SPECPATH).parent
edge = os.environ.get('HUMBLE_BUILD_EDGE') == '1'
data = [(str(root / 'humble_bundle_keys' / 'web_ui.html'), 'humble_bundle_keys')]
if edge:
    data.append((str(root / 'packaging' / 'edge-mode'), 'humble_bundle_keys'))
a = Analysis(
    [str(root / 'packaging' / 'launcher.py')],
    pathex=[str(root)],
    datas=data,
    hiddenimports=[],
    excludes=['pytest', 'PIL', 'tkinter'],
)
# Playwright's install bookkeeping contains paths from the build machine.
a.datas = [item for item in a.datas if '/.links/' not in item[0].replace('\\', '/')]
if edge:
    a.datas = [item for item in a.datas if '/.local-browsers/' not in item[0].replace('\\', '/')]
    a.binaries = [item for item in a.binaries if '/.local-browsers/' not in item[0].replace('\\', '/')]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name='HumbleKeyManager', console=True,
    icon=str(root / 'build' / 'icon.ico'),
)
coll = COLLECT(exe, a.binaries, a.datas, name='HumbleKeyManager-Edge' if edge else 'HumbleKeyManager')
