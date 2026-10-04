from pathlib import Path

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / 'packaging' / 'launcher.py')],
    pathex=[str(root)],
    datas=[(str(root / 'humble_bundle_keys' / 'web_ui.html'), 'humble_bundle_keys')],
    hiddenimports=[],
    excludes=['pytest', 'PIL', 'tkinter'],
)
# Playwright's install bookkeeping contains paths from the build machine.
a.datas = [item for item in a.datas if '/.links/' not in item[0].replace('\\', '/')]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name='HumbleKeyManager', console=True,
    icon=str(root / 'build' / 'icon.ico'),
)
coll = COLLECT(exe, a.binaries, a.datas, name='HumbleKeyManager')
