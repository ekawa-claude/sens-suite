#!/usr/bin/env python3
"""RawAccel Studio — local web UI over the RawAccel driver.

Serves the static frontend and a tiny JSON API:
  GET  /api/state            current driver settings + profile list
  POST /api/apply            {settings} -> write settings.json + run writer.exe
  POST /api/profiles/save    {name, settings}
  POST /api/profiles/apply   {name}
  POST /api/profiles/delete  {name}
"""
import json
import os
import re
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if not getattr(sys, "frozen", False):
    _root = str(APP_DIR.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
import suitepaths

PORT = 8898
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(suitepaths.studio_dir())
PROFILES_DIR = DATA_DIR / "profiles"
STATE_FILE = DATA_DIR / "state.json"
SENS_FINDER_DIR = Path(suitepaths.sensfinder_dir())

RA_CANDIDATES = [
    Path("F:/Games/RawAccel"),
    Path("C:/RawAccel"),
    Path(os.path.expanduser("~/Desktop/RawAccel")),
    Path(os.path.expanduser("~/Downloads/RawAccel")),
]

# set by studio.py so POST /api/show can pop the native window
SHOW_WINDOW_CALLBACK = None
# set by studio.py: native folder dialog, returns a path string or None
PICK_FOLDER_CALLBACK = None


def find_ra_dir():
    """RawAccel folder: remembered path first, then common candidates."""
    saved = load_state().get("raDir")
    for p in ([Path(saved)] if saved else []) + RA_CANDIDATES:
        if (p / "writer.exe").exists():
            return p
    return None


def ra_dir():
    return find_ra_dir()


def settings_file():
    d = find_ra_dir()
    return d / "settings.json" if d else None

MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def load_state():
    if STATE_FILE.exists():
        try:
            return read_json(STATE_FILE)
        except Exception:
            pass
    return {"activeProfile": None}


def save_state(state):
    write_json(STATE_FILE, state)


def safe_name(name: str) -> str:
    name = re.sub(r"[^\w \-\.()Ѐ-ӿ]", "", name).strip()
    if not name or name.startswith("."):
        raise ValueError("bad profile name")
    return name


def list_profiles():
    out = []
    for f in sorted(PROFILES_DIR.glob("*.json")):
        try:
            data = read_json(f)
            prof = data["profiles"][0]
            params = prof.get("Whole or horizontal accel parameters", {})
            out.append({
                "name": f.stem,
                "mode": params.get("mode", "?"),
                "sens": prof.get("Sensitivity multiplier", 1.0),
            })
        except Exception:
            out.append({"name": f.stem, "mode": "?", "sens": None})
    return out


def apply_to_driver(settings_obj):
    """Write settings.json into the RawAccel dir and push it to the driver."""
    d = find_ra_dir()
    if not d:
        return {"code": 1, "out": "", "err": "RawAccel folder is not set"}
    backup = d / "settings.backup-original.json"
    if not backup.exists():
        shutil.copy2(d / "settings.json", backup)
    write_json(d / "settings.json", settings_obj)
    exe = str(d / "writer.exe")
    r = subprocess.run([exe, "settings.json"], cwd=str(d),
                       capture_output=True, text=True, timeout=20)
    return {"code": r.returncode, "out": (r.stdout or "").strip(), "err": (r.stderr or "").strip()}


def sensfinder_summary():
    """Latest results from Sens Finder: config + newest entry per mode."""
    cfg_file = SENS_FINDER_DIR / "config2.json"
    hist_file = SENS_FINDER_DIR / "history2.json"
    if not hist_file.exists():
        return {"available": False}
    cfg = read_json(cfg_file) if cfg_file.exists() else {}
    hist = read_json(hist_file)
    latest = {}
    for entry in hist:  # chronological; keep the newest of each mode
        m = entry.get("mode")
        if m:
            latest[m] = entry
    return {
        "available": True,
        "config": cfg,
        "find": latest.get("find"),
        "find_track": latest.get("find_track"),
        "bench": latest.get("bench"),
    }


def launch_sensfinder():
    exe = suitepaths.sensfinder_exe()
    if exe:
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
        return
    # dev mode: run sens2.py from the repo with this interpreter
    script = Path(suitepaths.app_root()) / "sensfinder" / "sens2.py"
    if not script.exists():
        raise FileNotFoundError(f"{script} not found")
    subprocess.Popen([sys.executable, str(script)], cwd=str(script.parent))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text") or ctype == "application/json" else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            state = load_state()
            d = find_ra_dir()
            settings = None
            if d:
                try:
                    settings = read_json(d / "settings.json")
                except Exception as e:
                    return self._send(500, {"error": f"cannot read settings.json: {e}"})
            return self._send(200, {
                "settings": settings,
                "profiles": list_profiles(),
                "activeProfile": state.get("activeProfile"),
                "raDir": str(d) if d else None,
            })
        if path == "/api/sensfinder":
            try:
                return self._send(200, sensfinder_summary())
            except Exception as e:
                return self._send(500, {"error": str(e)})
        if path == "/api/profile":
            # /api/profile?name=... -> full stored settings of a profile
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            try:
                f = PROFILES_DIR / (safe_name(q["name"][0]) + ".json")
                return self._send(200, read_json(f))
            except Exception as e:
                return self._send(404, {"error": str(e)})
        # static
        if path == "/":
            path = "/index.html"
        f = (STATIC_DIR / path.lstrip("/")).resolve()
        if STATIC_DIR.resolve() in f.parents and f.is_file():
            return self._send(200, f.read_bytes(), MIME.get(f.suffix, "application/octet-stream"))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = self._body()
            if self.path == "/api/apply":
                result = apply_to_driver(body["settings"])
                state = load_state()
                state["activeProfile"] = body.get("profileName")
                save_state(state)
                return self._send(200 if result["code"] == 0 else 500, result)
            if self.path == "/api/profiles/save":
                name = safe_name(body["name"])
                write_json(PROFILES_DIR / (name + ".json"), body["settings"])
                return self._send(200, {"ok": True, "profiles": list_profiles()})
            if self.path == "/api/profiles/delete":
                name = safe_name(body["name"])
                (PROFILES_DIR / (name + ".json")).unlink(missing_ok=True)
                state = load_state()
                if state.get("activeProfile") == name:
                    state["activeProfile"] = None
                    save_state(state)
                return self._send(200, {"ok": True, "profiles": list_profiles()})
            if self.path == "/api/sensfinder/launch":
                launch_sensfinder()
                return self._send(200, {"ok": True})
            if self.path == "/api/radir":
                # {path} -> validate + remember the RawAccel folder
                p = Path(body.get("path", "").strip().strip('"'))
                if not (p / "writer.exe").exists():
                    return self._send(400, {"error": "writer.exe не найден в этой папке"})
                state = load_state()
                state["raDir"] = str(p)
                save_state(state)
                return self._send(200, {"ok": True, "raDir": str(p)})
            if self.path == "/api/radir/pick":
                # native folder dialog (only available in the desktop app)
                if not PICK_FOLDER_CALLBACK:
                    return self._send(400, {"error": "нет нативного диалога — введи путь вручную"})
                chosen = PICK_FOLDER_CALLBACK()
                if not chosen:
                    return self._send(200, {"ok": False})
                if not (Path(chosen) / "writer.exe").exists():
                    return self._send(400, {"error": "writer.exe не найден в этой папке"})
                state = load_state()
                state["raDir"] = str(chosen)
                save_state(state)
                return self._send(200, {"ok": True, "raDir": str(chosen)})
            if self.path == "/api/show":
                if SHOW_WINDOW_CALLBACK:
                    SHOW_WINDOW_CALLBACK()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(400, {"error": str(e)})


def serve():
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"RawAccel Studio  ->  http://localhost:{PORT}")
    print(f"RawAccel dir: {find_ra_dir()}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    serve()
