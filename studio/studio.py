#!/usr/bin/env python3
"""RawAccel Studio — native desktop app.

Wraps the local server (server.py) in a WebView2 window with a tray icon:
  - closing the window hides it to the tray, the server keeps running
  - tray menu: open window, switch profiles, toggle autostart, quit
  - `--minimized` starts hidden (used by the login autostart shortcut)
  - second launch just pops the window of the running instance and exits
"""
import json
import sys
import threading
import urllib.request

import server

APP_NAME = "RawAccel Studio"
URL = f"http://127.0.0.1:{server.PORT}/"
STARTUP_LINK = "RawAccel Studio.lnk"

window = None
tray = None


# ---------- single instance ----------

def ping_running_instance() -> bool:
    """If another instance owns the port, ask it to show its window."""
    try:
        req = urllib.request.Request(URL + "api/show", data=b"{}",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


# ---------- autostart (shortcut in shell:startup) ----------

def startup_dir():
    import os
    return os.path.join(os.environ["APPDATA"],
                        r"Microsoft\Windows\Start Menu\Programs\Startup")


def startup_link_path():
    import os
    return os.path.join(startup_dir(), STARTUP_LINK)


def autostart_enabled() -> bool:
    import os
    return os.path.exists(startup_link_path())


def pythonw_exe() -> str:
    import os
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return w if os.path.exists(w) else exe


def set_autostart(enable: bool):
    import os
    link = startup_link_path()
    if not enable:
        try:
            os.remove(link)
        except FileNotFoundError:
            pass
        return
    if getattr(sys, "frozen", False):
        target, args, wd = sys.executable, "--minimized", os.path.dirname(sys.executable)
    else:
        target = pythonw_exe()
        args = '"{}" --minimized'.format(server.APP_DIR / "studio.py")
        wd = str(server.APP_DIR)
    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{link}'); "
        "$s.TargetPath = '{target}'; "
        "$s.Arguments = '{args}'; "
        "$s.WorkingDirectory = '{wd}'; "
        "$s.Description = 'RawAccel Studio (tray)'; "
        "$s.Save()"
    ).format(link=link.replace("'", "''"), target=target, args=args, wd=wd)
    import subprocess
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True, timeout=30)


# ---------- tray ----------

def make_icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # dark rounded "mouse" body with a mint accel curve
    d.rounded_rectangle([8, 4, 56, 60], radius=22, fill=(24, 27, 34, 255),
                        outline=(90, 226, 187, 255), width=3)
    d.line([12, 52, 24, 48, 34, 38, 44, 22, 52, 12], fill=(90, 226, 187, 255),
           width=4, joint="curve")
    return img


def show_window():
    if window:
        window.show()
        window.restore()


def pick_folder():
    """Native folder dialog for the RawAccel path (used by /api/radir/pick)."""
    if not window:
        return None
    import webview
    dialog = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
    if dialog is None:
        dialog = webview.FOLDER_DIALOG
    res = window.create_file_dialog(dialog)
    if not res:
        return None
    return res[0] if isinstance(res, (list, tuple)) else res


def quit_app(icon=None, item=None):
    if tray:
        tray.stop()
    if window:
        window.destroy()


def apply_profile_from_tray(name: str):
    try:
        settings = server.read_json(server.PROFILES_DIR / (name + ".json"))
        r = server.apply_to_driver(settings)
        st = server.load_state()
        st["activeProfile"] = name
        server.save_state(st)
        if tray:
            ok = r["code"] == 0
            tray.notify(f"Профиль «{name}» применён" if ok
                        else f"Ошибка применения: {r['err'] or r['out']}", APP_NAME)
    except Exception as e:
        if tray:
            tray.notify(f"Ошибка: {e}", APP_NAME)


def build_menu():
    import pystray
    active = server.load_state().get("activeProfile")

    def profile_items():
        for p in server.list_profiles():
            name = p["name"]
            yield pystray.MenuItem(
                name,
                (lambda n: lambda icon, item: apply_profile_from_tray(n))(name),
                checked=(lambda n: lambda item: server.load_state().get("activeProfile") == n)(name),
            )

    return pystray.Menu(
        pystray.MenuItem("Открыть RawAccel Studio",
                         lambda icon, item: show_window(), default=True),
        pystray.MenuItem("Профили", pystray.Menu(profile_items)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Автозапуск при входе",
                         lambda icon, item: set_autostart(not autostart_enabled()),
                         checked=lambda item: autostart_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", quit_app),
    )


def start_tray():
    global tray
    import pystray
    tray = pystray.Icon("rawaccel-studio", make_icon_image(), APP_NAME, build_menu())
    tray.run_detached()


# ---------- main ----------

def main():
    global window
    minimized = "--minimized" in sys.argv

    if ping_running_instance():
        return  # already running; it just showed its window

    server.SHOW_WINDOW_CALLBACK = show_window
    server.PICK_FOLDER_CALLBACK = pick_folder
    threading.Thread(target=server.serve, daemon=True).start()

    import webview
    window = webview.create_window(
        APP_NAME, URL, width=1280, height=820, min_size=(980, 640),
        background_color="#14161c", hidden=minimized,
    )

    def on_closing():
        # hide to tray instead of quitting
        window.hide()
        return False

    window.events.closing += on_closing
    start_tray()
    try:
        webview.start()
    finally:
        if tray:
            tray.stop()


if __name__ == "__main__":
    main()
