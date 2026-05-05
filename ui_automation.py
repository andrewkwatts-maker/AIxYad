#!/usr/bin/env python3
#====== Automatica/ui_automation.py ======
#!copyright (c) 2025 Andrew Keith Watts. All rights reserved.
"""
Low-level Windows UI automation: screenshots, window management, mouse/keyboard input.

All public functions return structured dicts so callers can check {success, error} without
catching exceptions. Coordinate system is always absolute screen pixels unless documented
as window-relative.

Dependencies:
  - pyautogui  (pip install pyautogui)  — screenshots, mouse, keyboard
  - win32gui, win32con, win32api        — window discovery & positioning (pywin32)
  - Pillow (PIL)                        — image encoding for vision models
  - base64, io, subprocess, time        — stdlib
"""

import base64
import io
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------------------
# Optional dependency guards
# ---------------------------------------------------------------------------

try:
    import pyautogui
    pyautogui.FAILSAFE = False  # disable corner-exit safety for headless/agent use
    _PYAUTOGUI_AVAILABLE = True
except ImportError:
    pyautogui = None  # type: ignore
    _PYAUTOGUI_AVAILABLE = False

try:
    import win32gui
    import win32con
    import win32api
    _WIN32_AVAILABLE = True
except ImportError:
    win32gui = win32con = win32api = None  # type: ignore
    _WIN32_AVAILABLE = False

try:
    from PIL import Image, ImageGrab
    _PIL_AVAILABLE = True
except ImportError:
    Image = ImageGrab = None  # type: ignore
    _PIL_AVAILABLE = False

# ---------------------------------------------------------------------------
# Screenshot directory
# ---------------------------------------------------------------------------

_SCREENSHOT_DIR = Path(__file__).parent / "logs" / "screenshots"


def _ensure_screenshot_dir() -> Path:
    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    return _SCREENSHOT_DIR


def _temp_screenshot_path(suffix: str = ".png") -> str:
    _ensure_screenshot_dir()
    ts = int(time.time() * 1000)
    return str(_SCREENSHOT_DIR / f"screenshot_{ts}{suffix}")


# ---------------------------------------------------------------------------
# Image encoding helpers
# ---------------------------------------------------------------------------

def image_to_base64(image_path: str) -> str:
    """Encode an image file to a base64 string (no data URI prefix)."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def pil_image_to_base64(img) -> str:
    """Convert a PIL Image object to base64 PNG string."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------

def take_screenshot(save_path: Optional[str] = None) -> dict:
    """Capture the full screen.

    Returns:
        {success, path, width, height, base64}
    """
    if not _PIL_AVAILABLE:
        return {"success": False, "error": "Pillow not installed — pip install Pillow"}

    path = save_path or _temp_screenshot_path()
    try:
        img = ImageGrab.grab()
        img.save(path)
        w, h = img.size
        b64 = pil_image_to_base64(img)
        return {"success": True, "path": path, "width": w, "height": h, "base64": b64}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def screenshot_window(window_title: str, save_path: Optional[str] = None) -> dict:
    """Capture only the area occupied by a named window.

    Args:
        window_title: Substring to match against window titles (case-insensitive).
        save_path: Optional output path; auto-generated if None.

    Returns:
        {success, path, width, height, base64, window: {x, y, width, height, title, hwnd}}
    """
    if not _PIL_AVAILABLE:
        return {"success": False, "error": "Pillow not installed — pip install Pillow"}

    win = find_window(window_title)
    if not win.get("success"):
        return win  # propagate the error

    x, y = win["x"], win["y"]
    w, h = win["width"], win["height"]

    path = save_path or _temp_screenshot_path()
    try:
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
        img.save(path)
        b64 = pil_image_to_base64(img)
        return {
            "success": True,
            "path": path,
            "width": w,
            "height": h,
            "base64": b64,
            "window": {
                "x": x, "y": y, "width": w, "height": h,
                "title": win["title"], "hwnd": win["hwnd"],
            },
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def screenshot_region(x: int, y: int, width: int, height: int,
                      save_path: Optional[str] = None) -> dict:
    """Capture a rectangular region of the screen.

    Returns:
        {success, path, width, height, base64}
    """
    if not _PIL_AVAILABLE:
        return {"success": False, "error": "Pillow not installed — pip install Pillow"}

    path = save_path or _temp_screenshot_path()
    try:
        img = ImageGrab.grab(bbox=(x, y, x + width, y + height))
        img.save(path)
        b64 = pil_image_to_base64(img)
        return {"success": True, "path": path, "width": width, "height": height, "base64": b64}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Window management
# ---------------------------------------------------------------------------

def _enum_windows_callback(hwnd, result_list):
    if win32gui.IsWindowVisible(hwnd):
        title = win32gui.GetWindowText(hwnd)
        if title:
            try:
                rect = win32gui.GetWindowRect(hwnd)
                x, y, x2, y2 = rect
                w, h = x2 - x, y2 - y
                if w > 0 and h > 0:
                    result_list.append({
                        "hwnd": hwnd,
                        "title": title,
                        "x": x, "y": y,
                        "width": w, "height": h,
                    })
            except Exception:
                pass


def list_windows() -> List[dict]:
    """Return all visible top-level windows with their screen coordinates.

    Returns:
        [{hwnd, title, x, y, width, height}, ...]
    """
    if not _WIN32_AVAILABLE:
        return []
    windows = []
    win32gui.EnumWindows(_enum_windows_callback, windows)
    return sorted(windows, key=lambda w: w["title"].lower())


def find_window(title_pattern: str) -> dict:
    """Find the first visible window whose title contains title_pattern (case-insensitive).

    Returns:
        {success, hwnd, title, x, y, width, height, center_x, center_y}
        or {success: False, error: message}
    """
    if not _WIN32_AVAILABLE:
        return {"success": False, "error": "pywin32 not available"}

    pattern = title_pattern.lower()
    for win in list_windows():
        if pattern in win["title"].lower():
            cx = win["x"] + win["width"] // 2
            cy = win["y"] + win["height"] // 2
            return {
                "success": True,
                "hwnd": win["hwnd"],
                "title": win["title"],
                "x": win["x"], "y": win["y"],
                "width": win["width"], "height": win["height"],
                "center_x": cx, "center_y": cy,
            }
    return {"success": False, "error": f"No window found matching '{title_pattern}'"}


def get_window_rect(hwnd: int) -> dict:
    """Get the screen rectangle for a window handle.

    Returns:
        {success, x, y, width, height, center_x, center_y}
    """
    if not _WIN32_AVAILABLE:
        return {"success": False, "error": "pywin32 not available"}
    try:
        x, y, x2, y2 = win32gui.GetWindowRect(hwnd)
        w, h = x2 - x, y2 - y
        return {
            "success": True,
            "x": x, "y": y,
            "width": w, "height": h,
            "center_x": x + w // 2,
            "center_y": y + h // 2,
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def bring_window_to_front(hwnd: int) -> dict:
    """Focus and raise a window by handle.

    Returns:
        {success, message}
    """
    if not _WIN32_AVAILABLE:
        return {"success": False, "error": "pywin32 not available"}
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.2)
        return {"success": True, "message": f"Window {hwnd} brought to front"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Mouse input
# ---------------------------------------------------------------------------

def mouse_click(x: int, y: int, button: str = "left", double: bool = False) -> dict:
    """Click at absolute screen coordinates.

    Args:
        x, y: Absolute screen coordinates.
        button: "left", "right", or "middle".
        double: If True, double-click.

    Returns:
        {success, message}
    """
    if not _PYAUTOGUI_AVAILABLE:
        return {"success": False, "error": "pyautogui not installed — pip install pyautogui"}
    try:
        clicks = 2 if double else 1
        pyautogui.click(x, y, button=button, clicks=clicks, interval=0.1)
        action = "double-clicked" if double else "clicked"
        return {"success": True, "message": f"{action} ({button}) at ({x}, {y})"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def mouse_click_in_window(hwnd: int, rel_x: int, rel_y: int,
                           button: str = "left", double: bool = False) -> dict:
    """Click at window-relative coordinates.

    Converts (rel_x, rel_y) relative to the window's top-left corner into
    absolute screen coordinates, then clicks.

    Returns:
        {success, message, absolute_x, absolute_y}
    """
    rect = get_window_rect(hwnd)
    if not rect.get("success"):
        return rect
    abs_x = rect["x"] + rel_x
    abs_y = rect["y"] + rel_y
    result = mouse_click(abs_x, abs_y, button=button, double=double)
    result["absolute_x"] = abs_x
    result["absolute_y"] = abs_y
    return result


def mouse_move(x: int, y: int, duration: float = 0.2) -> dict:
    """Move mouse to absolute coordinates without clicking.

    Returns:
        {success, message}
    """
    if not _PYAUTOGUI_AVAILABLE:
        return {"success": False, "error": "pyautogui not installed"}
    try:
        pyautogui.moveTo(x, y, duration=duration)
        return {"success": True, "message": f"Mouse moved to ({x}, {y})"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def scroll(x: int, y: int, clicks: int = 3, direction: str = "down") -> dict:
    """Scroll the mouse wheel at the given coordinates.

    Args:
        x, y: Screen coordinates to scroll at.
        clicks: Number of scroll steps (positive).
        direction: "up" or "down".

    Returns:
        {success, message}
    """
    if not _PYAUTOGUI_AVAILABLE:
        return {"success": False, "error": "pyautogui not installed"}
    try:
        amount = clicks if direction == "up" else -clicks
        pyautogui.scroll(amount, x=x, y=y)
        return {"success": True, "message": f"Scrolled {direction} {clicks} clicks at ({x}, {y})"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5,
         button: str = "left") -> dict:
    """Click-drag from (x1, y1) to (x2, y2).

    Returns:
        {success, message}
    """
    if not _PYAUTOGUI_AVAILABLE:
        return {"success": False, "error": "pyautogui not installed"}
    try:
        pyautogui.moveTo(x1, y1, duration=0.1)
        pyautogui.dragTo(x2, y2, duration=duration, button=button)
        return {"success": True, "message": f"Dragged from ({x1},{y1}) to ({x2},{y2})"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Keyboard input
# ---------------------------------------------------------------------------

def type_text(text: str, interval: float = 0.02) -> dict:
    """Type a string of text at the current cursor focus.

    Args:
        text: Text to type.
        interval: Delay between keystrokes in seconds.

    Returns:
        {success, message}
    """
    if not _PYAUTOGUI_AVAILABLE:
        return {"success": False, "error": "pyautogui not installed"}
    try:
        pyautogui.write(text, interval=interval)
        return {"success": True, "message": f"Typed {len(text)} characters"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def key_press(keys: List[str]) -> dict:
    """Press a key combination.

    Args:
        keys: List of key names, e.g. ["ctrl", "s"] or ["enter"].
              pyautogui key names: https://pyautogui.readthedocs.io/en/latest/keyboard.html

    Returns:
        {success, message}
    """
    if not _PYAUTOGUI_AVAILABLE:
        return {"success": False, "error": "pyautogui not installed"}
    try:
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
        return {"success": True, "message": f"Pressed keys: {'+'.join(keys)}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def type_in_field(x: int, y: int, text: str, clear_first: bool = True) -> dict:
    """Click a field at (x, y) then type text into it.

    Args:
        x, y: Absolute screen coordinates of the field.
        text: Text to type.
        clear_first: If True, select-all + delete before typing.

    Returns:
        {success, message}
    """
    click_result = mouse_click(x, y)
    if not click_result.get("success"):
        return click_result
    time.sleep(0.1)
    if clear_first:
        key_press(["ctrl", "a"])
        time.sleep(0.05)
        key_press(["delete"])
        time.sleep(0.05)
    return type_text(text)


# ---------------------------------------------------------------------------
# App launching
# ---------------------------------------------------------------------------

def launch_app(
    executable: str,
    args: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    wait_ms: int = 2000,
    capture_after: bool = True,
    window_title_hint: Optional[str] = None,
) -> dict:
    """Launch an executable and optionally capture a screenshot after launch.

    Args:
        executable: Path to executable or command name on PATH.
        args: Optional list of command-line arguments.
        cwd: Working directory (defaults to executable's directory).
        wait_ms: Milliseconds to wait after launch before screenshot.
        capture_after: If True, take a full-screen screenshot after wait_ms.
        window_title_hint: If set, try to screenshot this window instead of full screen.

    Returns:
        {success, pid, executable, _proc (subprocess handle), screenshot?: {...}}
    """
    cmd = [executable] + (args or [])
    work_dir = cwd or str(Path(executable).parent) if Path(executable).exists() else None

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=work_dir,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        result: dict = {"success": True, "pid": proc.pid, "executable": executable, "_proc": proc}

        time.sleep(wait_ms / 1000.0)  # always wait so window can appear

        if capture_after:
            if window_title_hint:
                shot = screenshot_window(window_title_hint)
                if not shot.get("success"):
                    shot = take_screenshot()  # fallback to full screen
            else:
                shot = take_screenshot()
            result["screenshot"] = shot

        return result
    except FileNotFoundError:
        return {"success": False, "error": f"Executable not found: {executable}"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Application tracking registry
# ---------------------------------------------------------------------------

_APP_REGISTRY: dict = {}  # app_id → {pid, window_title_hint, proc, executable}


def launch_and_track(
    app_id: str,
    executable: str,
    args: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    wait_ms: int = 2000,
    window_title_hint: Optional[str] = None,
) -> dict:
    """Launch an app and register it by app_id for future screenshot/input actions.

    Args:
        app_id: Identifier used to reference this app in later calls.
        executable / args / cwd / wait_ms / window_title_hint: same as launch_app().

    Returns:
        launch_app() result, enriched with app_id.
    """
    result = launch_app(
        executable=executable,
        args=args,
        cwd=cwd,
        wait_ms=wait_ms,
        capture_after=False,          # caller can screenshot via screenshot_tracked
        window_title_hint=window_title_hint,
    )
    if result.get("success"):
        _APP_REGISTRY[app_id] = {
            "pid":               result["pid"],
            "window_title_hint": window_title_hint or executable,
            "executable":        executable,
            "proc":              result.get("_proc"),
        }
        result["app_id"] = app_id
    return result


def screenshot_tracked(app_id: str, save_path: Optional[str] = None) -> dict:
    """Screenshot the window of a tracked application.

    Returns screenshot dict, same shape as screenshot_window().
    """
    app = _APP_REGISTRY.get(app_id)
    if not app:
        return {"success": False, "error": f"No tracked app with id '{app_id}'. "
                                            f"Available: {list(_APP_REGISTRY.keys())}"}
    return screenshot_window(app["window_title_hint"], save_path=save_path)


def click_tracked(app_id: str, rel_x: int, rel_y: int,
                  button: str = "left", double: bool = False) -> dict:
    """Click within a tracked application window (window-relative coordinates).

    Automatically finds the window position and converts to absolute coords.
    """
    app = _APP_REGISTRY.get(app_id)
    if not app:
        return {"success": False, "error": f"No tracked app: '{app_id}'"}
    win = find_window(app["window_title_hint"])
    if not win.get("success"):
        return {"success": False, "error": f"Window not found for '{app_id}': {win.get('error')}"}
    return mouse_click_in_window(win["hwnd"], rel_x, rel_y, button=button, double=double)


def type_in_tracked(app_id: str, text: str) -> dict:
    """Type text into a tracked application (window must have focus)."""
    app = _APP_REGISTRY.get(app_id)
    if not app:
        return {"success": False, "error": f"No tracked app: '{app_id}'"}
    # Bring to front first
    win = find_window(app["window_title_hint"])
    if win.get("success"):
        bring_window_to_front(win["hwnd"])
    return type_text(text)


def close_tracked(app_id: str) -> dict:
    """Terminate a tracked application and remove it from the registry.

    Returns:
        {success, message}
    """
    app = _APP_REGISTRY.pop(app_id, None)
    if not app:
        return {"success": False, "error": f"No tracked app: '{app_id}'"}
    proc = app.get("proc")
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return {"success": True, "message": f"Closed app '{app_id}' (pid {app.get('pid')})"}


def get_tracked_app(app_id: str) -> Optional[dict]:
    """Return tracking info for app_id, or None if not tracked."""
    entry = _APP_REGISTRY.get(app_id)
    if not entry:
        return None
    return {k: v for k, v in entry.items() if k != "proc"}


def list_tracked_apps() -> List[dict]:
    """Return a list of all currently tracked applications."""
    return [{"app_id": k, **{kk: vv for kk, vv in v.items() if kk != "proc"}}
            for k, v in _APP_REGISTRY.items()]


def get_screen_size() -> dict:
    """Return the primary monitor's resolution.

    Returns:
        {width, height}
    """
    if _PYAUTOGUI_AVAILABLE:
        w, h = pyautogui.size()
        return {"width": w, "height": h}
    if _PIL_AVAILABLE:
        img = ImageGrab.grab()
        w, h = img.size
        return {"width": w, "height": h}
    return {"width": 1920, "height": 1080}  # safe fallback


def get_mouse_position() -> dict:
    """Return the current mouse cursor position.

    Returns:
        {x, y}
    """
    if _PYAUTOGUI_AVAILABLE:
        pos = pyautogui.position()
        return {"x": pos.x, "y": pos.y}
    if _WIN32_AVAILABLE:
        x, y = win32api.GetCursorPos()
        return {"x": x, "y": y}
    return {"x": 0, "y": 0}
