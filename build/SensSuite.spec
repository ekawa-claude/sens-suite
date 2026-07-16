# -*- mode: python ; coding: utf-8 -*-
# One onedir bundle with two exes:
#   SensSuite.exe  — RawAccel Studio (tray app, main entry)
#   SensFinder.exe — Sens Finder (pygame, launched from Studio or directly)
import os

ROOT = os.path.dirname(SPECPATH)
ICON = os.path.join(SPECPATH, "icon.ico")

studio_a = Analysis(
    [os.path.join(ROOT, "studio", "studio.py")],
    pathex=[ROOT, os.path.join(ROOT, "studio")],
    datas=[(os.path.join(ROOT, "studio", "static"), "static")],
    hiddenimports=["pystray._win32"],
    noarchive=False,
)
finder_a = Analysis(
    [os.path.join(ROOT, "sensfinder", "sens2.py")],
    pathex=[ROOT, os.path.join(ROOT, "sensfinder")],
    noarchive=False,
)

studio_pyz = PYZ(studio_a.pure)
finder_pyz = PYZ(finder_a.pure)

studio_exe = EXE(
    studio_pyz, studio_a.scripts, [],
    exclude_binaries=True, name="SensSuite", icon=ICON,
    console=False, upx=False,
)
finder_exe = EXE(
    finder_pyz, finder_a.scripts, [],
    exclude_binaries=True, name="SensFinder", icon=ICON,
    console=False, upx=False,
)

COLLECT(
    studio_exe, studio_a.binaries, studio_a.datas,
    finder_exe, finder_a.binaries, finder_a.datas,
    name="SensSuite", upx=False,
)
