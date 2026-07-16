"""Shared path resolution for Sens Suite (Sens Finder + RawAccel Studio).

User data (configs, history, profiles) lives outside the app folder so the
app can be replaced on update without losing anything:
  * portable mode — a `data/` folder next to the app root, if it exists;
  * otherwise %LOCALAPPDATA%\\SensSuite.
"""
import os
import sys

APP_NAME = "SensSuite"


def app_root() -> str:
    """Folder that holds the app itself (exe dir when frozen, repo root in dev)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    portable = os.path.join(app_root(), "data")
    if os.path.isdir(portable):
        return portable
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def _sub(name: str) -> str:
    d = os.path.join(data_dir(), name)
    os.makedirs(d, exist_ok=True)
    return d


def sensfinder_dir() -> str:
    """config2.json / history2.json of Sens Finder."""
    return _sub("sensfinder")


def studio_dir() -> str:
    """state.json + profiles/ of RawAccel Studio."""
    return _sub("studio")


def sensfinder_exe():
    """Path to the bundled Sens Finder launcher, or None in dev."""
    if getattr(sys, "frozen", False):
        exe = os.path.join(app_root(), "SensFinder.exe")
        return exe if os.path.exists(exe) else None
    return None
