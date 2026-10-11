#!/usr/bin/env python3
#====== AIxYad/vision_actions.py ======
#!copyright (c) 2025 Andrew Keith Watts. All rights reserved.
"""
High-level agent action handlers for vision analysis and UI automation.

Every public action follows the same contract as db_actions.py / agent_executor.py:
    handler(params: dict) -> {"success": bool, "message": str, ...extra fields}

Agents interact via structured JSON:
    {"action": "screenshot", "params": {"window_title": "Notepad"}}
    {"action": "analyze_image", "params": {"image_path": "...", "model": "moondream",
                                            "return_bounds": true}}
    {"action": "ui_click", "params": {"x": 512, "y": 300}}

Coordinate system:
    - All x/y values are ABSOLUTE screen pixels by default.
    - When window_title is supplied, x/y are WINDOW-RELATIVE and automatically
      converted to absolute using the window's screen position.
"""

import json
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

import ui_automation as ua
from ollama_utils import OLLAMA_ENDPOINT_DEFAULT, is_vision_capable


# ---------------------------------------------------------------------------
# Vision model query
# ---------------------------------------------------------------------------

_BOUNDS_INSTRUCTION = (
    'Look at the image and identify every button, tab, link, and input field. '
    'Output ONLY valid JSON — no markdown, no explanation. '
    'Use the ACTUAL names and text you observe in the image, not example values. '
    'Format: {"description":"one sentence about what you see",'
    '"elements":[{"label":"actual name","type":"button","text":"actual text",'
    '"bounds_pct":[0.1,0.2,0.5,0.3],"confidence":0.9}]} '
    'bounds_pct is [left,top,right,bottom] as 0.0-1.0 fractions of image size.'
)

_DESCRIPTION_INSTRUCTION = (
    'Carefully look at the image. Output ONLY a JSON object with one field: '
    '{"description":"<describe what you see>"} '
    'Replace <describe what you see> with a real sentence about the actual image content.'
)


def _query_vision_model(
    image_base64: str,
    prompt: str,
    model: str,
    endpoint: str = OLLAMA_ENDPOINT_DEFAULT,
    timeout: int = 120,
) -> Optional[str]:
    """Send an image + prompt to an Ollama vision model.

    Returns the raw text response, or None on failure.
    """
    body = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": prompt,
            "images": [image_base64],
        }],
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        f"{endpoint}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data.get("message", {}).get("content", "")
    except Exception:
        return None


def _coerce_to_response_dict(obj) -> Optional[dict]:
    """Given a parsed JSON object, return a valid response dict or None.

    Handles three cases:
    - Already a dict with 'description' or 'elements': return as-is.
    - A list of dicts (model wrapped elements in an array): wrap into
      {"description": "", "elements": [...]}.
    - Anything else: return None.
    """
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list) and all(isinstance(i, dict) for i in obj) and obj:
        return {"description": "", "elements": obj}
    return None


def _extract_json_from_response(text: str) -> Optional[dict]:
    """Try to parse a JSON response dict from a vision model's text output.

    Accepts:
    - A JSON object  {"description":..., "elements":[...]}
    - A JSON array   [{...}, ...] — wrapped into {"description":"","elements":[...]}
    - JSON embedded in prose or code fences

    Returns None for plain text, scalars, or unparseable responses.
    """
    if not text:
        return None

    import re

    def _try_parse(raw: str) -> Optional[dict]:
        try:
            obj = json.loads(raw.strip())
            return _coerce_to_response_dict(obj)
        except Exception:
            return None

    # 1. Direct parse (dict or array)
    result = _try_parse(text)
    if result is not None:
        return result

    # 2. Extract from code fence ```json ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if m:
        result = _try_parse(m.group(1))
        if result is not None:
            return result

    # 3. Greedy { ... } extraction from prose — only when '{' comes before any '['
    #    so we don't pull a dict out of a surrounding JSON array.
    first_brace   = text.find("{")
    first_bracket = text.find("[")
    if first_brace != -1 and (first_bracket == -1 or first_brace < first_bracket):
        end = text.rfind("}")
        if end > first_brace:
            result = _try_parse(text[first_brace:end + 1])
            if result is not None:
                return result

    # 4. Greedy [ ... ] extraction from prose
    first_bracket = text.find("[")
    if first_bracket != -1:
        end = text.rfind("]")
        if end > first_bracket:
            result = _try_parse(text[first_bracket:end + 1])
            if result is not None:
                return result

    # 5. Rescue flat element objects from malformed / deeply-nested JSON.
    #    Matches only non-nested {...} — no inner braces — so each match is a
    #    self-contained flat object.  Useful when moondream returns correct
    #    element labels but wraps them in broken outer structure like
    #    [{"label":"Submit Order",...},{"elements":[{"label":"<name>",...},...]}]
    element_candidates = re.findall(r'\{[^{}]+\}', text)
    rescued = []
    for candidate in element_candidates:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict) and ("label" in obj or "type" in obj):
                rescued.append(obj)
        except Exception:
            pass
    if rescued:
        return {"description": "", "elements": rescued}

    return None


def _convert_bounds_pct_to_pixels(elements: list, img_width: int, img_height: int) -> list:
    """Convert bounds_pct (0.0-1.0) to pixel bounds and add center point.

    bounds_pct may be a dict {"x_min":..., "y_min":..., "x_max":..., "y_max":...}
    or a list [x_min, y_min, x_max, y_max] (both formats emitted by Ollama models).
    """
    result = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        el = dict(el)
        bp = el.pop("bounds_pct", None)
        if bp and img_width and img_height:
            if isinstance(bp, (list, tuple)) and len(bp) >= 4:
                x_min_pct, y_min_pct, x_max_pct, y_max_pct = (
                    float(bp[0]), float(bp[1]), float(bp[2]), float(bp[3])
                )
            elif isinstance(bp, dict):
                x_min_pct = float(bp.get("x_min", 0))
                y_min_pct = float(bp.get("y_min", 0))
                x_max_pct = float(bp.get("x_max", 1))
                y_max_pct = float(bp.get("y_max", 1))
            else:
                result.append(el)
                continue
            x_min = int(x_min_pct * img_width)
            y_min = int(y_min_pct * img_height)
            x_max = int(x_max_pct * img_width)
            y_max = int(y_max_pct * img_height)
            el["bounds"] = {
                "x_min": x_min, "y_min": y_min,
                "x_max": x_max, "y_max": y_max,
                "width": x_max - x_min,
                "height": y_max - y_min,
            }
            el["center"] = {
                "x": (x_min + x_max) // 2,
                "y": (y_min + y_max) // 2,
            }
        result.append(el)
    return result


def _repair_response(
    raw: str,
    existing: Optional[dict],
    image_base64: str,
    prompt: str,
    model: str,
    endpoint: str,
    return_bounds: bool = False,
    max_retries: int = 2,
) -> dict:
    """Iteratively repair a vision model response.

    Re-queries the model with simplified targeted prompts when the initial
    response is missing required fields. Merges all partial results.
    Always returns a dict (may still be partial — callers must handle gaps).

    When return_bounds=False, only 'description' is required.
    When return_bounds=True, both 'description' and 'elements' are repaired.
    """
    merged: dict = dict(existing) if existing else {}

    try:
        for _attempt in range(max_retries):
            missing = []
            if not merged.get("description"):
                missing.append("description")
            if return_bounds and not merged.get("elements"):
                missing.append("elements")

            if not missing:
                break  # All required keys present

            # Build a minimal targeted fallback prompt
            if "elements" in missing and "description" in missing:
                fallback = (
                    'Reply with ONLY valid JSON: '
                    '{"description":"brief description",'
                    '"elements":[{"label":"element name","type":"button","text":"visible text",'
                    '"bounds_pct":[0.1,0.1,0.4,0.2],"confidence":0.85}]}'
                )
            elif "elements" in missing:
                fallback = (
                    'List every button, tab, link, and menu visible in the image. '
                    'Reply with ONLY: {"elements":[{"label":"name","type":"button","text":"text",'
                    '"bounds_pct":[0.0,0.0,0.5,0.1],"confidence":0.9}]}'
                )
            else:
                fallback = 'Reply with ONLY: {"description":"brief description of what you see"}'

            retry_raw = _query_vision_model(image_base64, fallback, model, endpoint)
            if not retry_raw:
                break

            retry_parsed = _extract_json_from_response(retry_raw)
            if retry_parsed:
                for key in ("description", "elements"):
                    if not merged.get(key) and retry_parsed.get(key):
                        merged[key] = retry_parsed[key]
    except Exception:
        pass  # Best-effort — never raise from repair

    # Ensure keys always exist
    merged.setdefault("description", "")
    merged.setdefault("elements", [])
    return merged


# ---------------------------------------------------------------------------
# VisionActionHandler
# ---------------------------------------------------------------------------

class VisionActionHandler:
    """Handles all vision and UI automation agent actions."""

    ACTIONS: List[str] = [
        "screenshot",
        "screenshot_window",
        "screenshot_region",
        "analyze_image",
        "analyze_screenshot",
        "ui_click",
        "ui_type",
        "ui_key_press",
        "ui_scroll",
        "ui_drag",
        "ui_type_in_field",
        "launch_app",
        "get_window_info",
        "list_windows",
        "bring_window_to_front",
        "get_screen_size",
        "get_mouse_position",
    ]

    def __init__(self, default_endpoint: str = OLLAMA_ENDPOINT_DEFAULT,
                 default_vision_model: str = "moondream"):
        self.default_endpoint = default_endpoint
        self.default_vision_model = default_vision_model

    def handle(self, action: str, params: dict) -> dict:
        """Dispatch an action by name. Returns result dict."""
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return {"success": False, "message": f"Unknown vision action: '{action}'"}
        try:
            return handler(params or {})
        except Exception as exc:
            return {"success": False, "message": f"Action '{action}' raised: {exc}"}

    @staticmethod
    def get_available_actions() -> List[dict]:
        """Return the action schema list for registration in agent_protocol."""
        return [
            {
                "name": "screenshot",
                "description": "Capture the full screen or a specific window as an image. Returns base64-encoded PNG and the file path.",
                "params": {
                    "window_title": "(optional) Capture only the window whose title contains this string",
                    "save_path": "(optional) File path to save the screenshot",
                },
            },
            {
                "name": "screenshot_window",
                "description": "Capture only the area of a named window. Returns base64 PNG, dimensions, and window screen position.",
                "params": {
                    "window_title": "Substring of the window title to capture (case-insensitive)",
                    "save_path": "(optional) Output file path",
                },
            },
            {
                "name": "screenshot_region",
                "description": "Capture a specific rectangular region of the screen.",
                "params": {
                    "x": "Left edge (absolute screen pixels)",
                    "y": "Top edge (absolute screen pixels)",
                    "width": "Region width in pixels",
                    "height": "Region height in pixels",
                    "save_path": "(optional) Output file path",
                },
            },
            {
                "name": "analyze_image",
                "description": (
                    "Send an image to a local Ollama vision model for analysis. "
                    "Returns a description and, when return_bounds=true, a list of detected elements "
                    "with pixel bounding boxes and center coordinates for clicking."
                ),
                "params": {
                    "image_path": "Path to the image file (or omit if base64 provided)",
                    "image_base64": "(optional) Base64-encoded image (use instead of image_path)",
                    "prompt": "Question or instruction for the vision model",
                    "model": f"(optional) Ollama vision model name, default: moondream",
                    "endpoint": "(optional) Ollama endpoint, default: http://localhost:11434",
                    "return_bounds": "(optional, bool) If true, request structured element bounding boxes",
                    "elements_of_interest": "(optional) Comma-separated list of element types to focus on, e.g. 'buttons,text fields'",
                },
            },
            {
                "name": "analyze_screenshot",
                "description": (
                    "Take a screenshot and immediately analyze it with a vision model. "
                    "Combines screenshot + analyze_image in one step."
                ),
                "params": {
                    "window_title": "(optional) Window to screenshot; full screen if omitted",
                    "prompt": "Question or instruction for the vision model",
                    "model": "(optional) Vision model name",
                    "endpoint": "(optional) Ollama endpoint",
                    "return_bounds": "(optional, bool) Request bounding boxes",
                },
            },
            {
                "name": "ui_click",
                "description": (
                    "Click at a screen position. If window_title is given, x/y are "
                    "window-relative and automatically offset to absolute screen coordinates."
                ),
                "params": {
                    "x": "X coordinate",
                    "y": "Y coordinate",
                    "button": "(optional) 'left', 'right', or 'middle' — default: left",
                    "double": "(optional, bool) Double-click if true",
                    "window_title": "(optional) If set, x/y are relative to this window's top-left",
                },
            },
            {
                "name": "ui_type",
                "description": "Type text at the current keyboard focus.",
                "params": {
                    "text": "Text to type",
                    "interval": "(optional) Delay between keystrokes in seconds, default: 0.02",
                },
            },
            {
                "name": "ui_key_press",
                "description": "Press a key or key combination, e.g. ['ctrl','s'] or ['enter'].",
                "params": {
                    "keys": "List of key names (pyautogui format), e.g. ['ctrl', 's']",
                },
            },
            {
                "name": "ui_scroll",
                "description": "Scroll the mouse wheel at the given screen coordinates.",
                "params": {
                    "x": "Screen X coordinate",
                    "y": "Screen Y coordinate",
                    "direction": "(optional) 'up' or 'down' — default: down",
                    "clicks": "(optional) Number of scroll steps — default: 3",
                },
            },
            {
                "name": "ui_drag",
                "description": "Click-drag from one position to another.",
                "params": {
                    "x1": "Start X", "y1": "Start Y",
                    "x2": "End X", "y2": "End Y",
                    "duration": "(optional) Duration in seconds — default: 0.5",
                },
            },
            {
                "name": "ui_type_in_field",
                "description": "Click a field then type text into it (optionally clearing it first).",
                "params": {
                    "x": "Field X coordinate",
                    "y": "Field Y coordinate",
                    "text": "Text to type",
                    "clear_first": "(optional, bool) Select-all and delete before typing — default: true",
                },
            },
            {
                "name": "launch_app",
                "description": "Launch an executable, wait, and optionally capture a screenshot.",
                "params": {
                    "executable": "Path to executable or command name",
                    "args": "(optional) List of arguments",
                    "cwd": "(optional) Working directory",
                    "wait_ms": "(optional) Milliseconds to wait after launch — default: 2000",
                    "capture_after": "(optional, bool) Take screenshot after launch — default: true",
                    "window_title_hint": "(optional) Window title to screenshot after launch",
                },
            },
            {
                "name": "get_window_info",
                "description": "Find a window by title and return its screen position and dimensions.",
                "params": {
                    "window_title": "Substring of window title to search for (case-insensitive)",
                },
            },
            {
                "name": "list_windows",
                "description": "List all visible top-level windows with their positions.",
                "params": {},
            },
            {
                "name": "bring_window_to_front",
                "description": "Focus and raise a window by title.",
                "params": {
                    "window_title": "Substring of window title",
                },
            },
            {
                "name": "get_screen_size",
                "description": "Return the primary screen resolution.",
                "params": {},
            },
            {
                "name": "get_mouse_position",
                "description": "Return the current mouse cursor position.",
                "params": {},
            },
        ]

    # -----------------------------------------------------------------------
    # Screenshot actions
    # -----------------------------------------------------------------------

    def _do_screenshot(self, params: dict) -> dict:
        window_title = params.get("window_title", "").strip()
        save_path = params.get("save_path")
        if window_title:
            result = ua.screenshot_window(window_title, save_path)
        else:
            result = ua.take_screenshot(save_path)
        if result.get("success"):
            result["message"] = f"Screenshot saved: {result.get('path')} ({result.get('width')}x{result.get('height')})"
        else:
            result["message"] = result.get("error", "Screenshot failed")
        return result

    def _do_screenshot_window(self, params: dict) -> dict:
        window_title = params.get("window_title", "")
        if not window_title:
            return {"success": False, "message": "window_title is required"}
        result = ua.screenshot_window(window_title, params.get("save_path"))
        result["message"] = (
            f"Window screenshot saved: {result.get('path')}" if result.get("success")
            else result.get("error", "Failed")
        )
        return result

    def _do_screenshot_region(self, params: dict) -> dict:
        try:
            x, y = int(params["x"]), int(params["y"])
            w, h = int(params["width"]), int(params["height"])
        except (KeyError, ValueError, TypeError) as exc:
            return {"success": False, "message": f"Invalid region params: {exc}"}
        result = ua.screenshot_region(x, y, w, h, params.get("save_path"))
        result["message"] = (
            f"Region screenshot saved: {result.get('path')}" if result.get("success")
            else result.get("error", "Failed")
        )
        return result

    # -----------------------------------------------------------------------
    # Vision analysis actions
    # -----------------------------------------------------------------------

    def _do_analyze_image(self, params: dict) -> dict:
        # Resolve image source
        b64 = params.get("image_base64")
        if not b64:
            image_path = params.get("image_path")
            if not image_path:
                return {"success": False, "message": "image_path or image_base64 is required"}
            try:
                b64 = ua.image_to_base64(image_path)
            except Exception as exc:
                return {"success": False, "message": f"Cannot read image: {exc}"}

        # Get image dimensions for bounds conversion.
        # Accept pre-computed dims (e.g. passed by _do_analyze_screenshot) to
        # avoid re-opening the file when the caller already has the dimensions.
        img_width  = int(params.get("_image_width")  or 0)
        img_height = int(params.get("_image_height") or 0)
        image_path = params.get("image_path")
        if not (img_width and img_height) and image_path:
            try:
                from PIL import Image as PILImage
                with PILImage.open(image_path) as im:
                    img_width, img_height = im.size
            except Exception:
                pass

        # Resize very large images before sending to the vision model.
        # Small vision models (moondream) process a fixed-resolution internal
        # representation; sending a 1920×1080 screenshot wastes bandwidth and
        # can produce empty responses.  We cap at 1024px on the longest side
        # while keeping the original dimensions for bounds conversion.
        _max_vision_dim = int(params.get("_max_vision_dim") or 1024)
        if b64 and (img_width > _max_vision_dim or img_height > _max_vision_dim):
            try:
                import base64 as _b64mod
                import io
                from PIL import Image as PILImage
                raw_bytes = _b64mod.b64decode(b64)
                with PILImage.open(io.BytesIO(raw_bytes)) as im:
                    scale = _max_vision_dim / max(im.width, im.height)
                    new_w = int(im.width * scale)
                    new_h = int(im.height * scale)
                    resized = im.resize((new_w, new_h), PILImage.LANCZOS)
                    buf = io.BytesIO()
                    resized.save(buf, format="PNG")
                    b64 = _b64mod.b64encode(buf.getvalue()).decode()
            except Exception:
                pass  # keep original b64 on error

        model = params.get("model") or self.default_vision_model
        endpoint = params.get("endpoint") or self.default_endpoint
        return_bounds = bool(params.get("return_bounds", False))
        elements_of_interest = params.get("elements_of_interest", "")

        # Build prompt — format instruction comes FIRST so the model reads it
        # before any free-text question (small models follow the first instruction).
        user_prompt = params.get("prompt", "").strip()
        if return_bounds:
            prefix = _BOUNDS_INSTRUCTION
            if elements_of_interest:
                prefix += f" Focus especially on: {elements_of_interest}."
            full_prompt = f"{prefix}\nContext: {user_prompt}" if user_prompt else prefix
        else:
            # Description-only mode: ask naturally without JSON format constraint.
            # Small models (moondream) answer natural language questions much more
            # reliably than structured JSON format instructions.
            full_prompt = user_prompt or "Describe what you see in this image."

        # Query model
        raw = _query_vision_model(b64, full_prompt, model, endpoint)
        if raw is None:
            return {
                "success": False,
                "message": f"Vision model '{model}' did not respond. Is Ollama running with that model?",
                "model_used": model,
            }

        if not return_bounds:
            # For description-only: accept the raw text response directly.
            # Try to extract JSON if the model happened to return it; fall back to raw text.
            parsed = _extract_json_from_response(raw)
            description = (parsed.get("description", "") if parsed else "") or raw
            return {
                "success": True,
                "message": f"Image analyzed by {model}",
                "description": description,
                "elements": [],
                "image_size": {"width": img_width, "height": img_height},
                "model_used": model,
                "raw_response": raw,
            }

        # Parse structured response (bounds mode) — with iterative repair
        parsed = _extract_json_from_response(raw)

        # Repair pass: if parsed is None or missing required fields, retry with targeted prompts
        needs_repair = (
            parsed is None
            or not parsed.get("description")
            or not parsed.get("elements")
        )
        if needs_repair:
            parsed = _repair_response(
                raw=raw,
                existing=parsed,
                image_base64=b64,
                prompt=full_prompt,
                model=model,
                endpoint=endpoint,
                return_bounds=True,
                max_retries=2,
            )

        description = parsed.get("description", "") or raw  # fall back to raw if still empty
        elements    = parsed.get("elements", [])
        if return_bounds and elements and img_width and img_height:
            elements = _convert_bounds_pct_to_pixels(elements, img_width, img_height)

        return {
            "success": True,
            "message": f"Image analyzed by {model}",
            "description": description,
            "elements": elements,
            "image_size": {"width": img_width, "height": img_height},
            "model_used": model,
            "raw_response": raw,
        }

    def _do_analyze_screenshot(self, params: dict) -> dict:
        """Take a screenshot then immediately analyze it."""
        window_title = params.get("window_title", "").strip()

        # Screenshot
        if window_title:
            shot = ua.screenshot_window(window_title)
        else:
            shot = ua.take_screenshot()

        if not shot.get("success"):
            return {"success": False, "message": shot.get("error", "Screenshot failed")}

        # Build analyze params — pass dimensions so _do_analyze_image
        # doesn't need to re-open the file to get width/height.
        analyze_params = dict(params)
        analyze_params["image_path"]    = shot["path"]
        analyze_params["image_base64"]  = shot["base64"]
        analyze_params["_image_width"]  = shot["width"]
        analyze_params["_image_height"] = shot["height"]

        result = self._do_analyze_image(analyze_params)
        result["screenshot"] = {
            "path": shot["path"],
            "width": shot["width"],
            "height": shot["height"],
        }
        if "window" in shot:
            result["window"] = shot["window"]
        return result

    # -----------------------------------------------------------------------
    # UI input actions
    # -----------------------------------------------------------------------

    def _resolve_coordinates(self, params: dict) -> tuple:
        """Resolve (x, y) from params, applying window offset if window_title given.

        Returns (abs_x, abs_y, error_dict_or_None)
        """
        try:
            x, y = int(params["x"]), int(params["y"])
        except (KeyError, ValueError, TypeError) as exc:
            return 0, 0, {"success": False, "message": f"x/y coordinates required: {exc}"}

        window_title = params.get("window_title", "").strip()
        if window_title:
            win = ua.find_window(window_title)
            if not win.get("success"):
                return 0, 0, {"success": False, "message": win.get("error", "Window not found")}
            x = win["x"] + x
            y = win["y"] + y

        return x, y, None

    def _do_ui_click(self, params: dict) -> dict:
        x, y, err = self._resolve_coordinates(params)
        if err:
            return err
        button = params.get("button", "left")
        double = bool(params.get("double", False))
        result = ua.mouse_click(x, y, button=button, double=double)
        result.setdefault("message", result.get("error", "Click failed"))
        return result

    def _do_ui_type(self, params: dict) -> dict:
        text = params.get("text")
        if text is None:
            return {"success": False, "message": "text is required"}
        interval = float(params.get("interval", 0.02))
        result = ua.type_text(str(text), interval=interval)
        result.setdefault("message", result.get("error", "Type failed"))
        return result

    def _do_ui_key_press(self, params: dict) -> dict:
        keys = params.get("keys")
        if not keys:
            return {"success": False, "message": "keys list is required, e.g. ['ctrl', 's']"}
        if isinstance(keys, str):
            keys = [keys]
        result = ua.key_press(keys)
        result.setdefault("message", result.get("error", "Key press failed"))
        return result

    def _do_ui_scroll(self, params: dict) -> dict:
        try:
            x, y = int(params["x"]), int(params["y"])
        except (KeyError, ValueError, TypeError) as exc:
            return {"success": False, "message": f"x/y required: {exc}"}
        direction = params.get("direction", "down")
        clicks = int(params.get("clicks", 3))
        result = ua.scroll(x, y, clicks=clicks, direction=direction)
        result.setdefault("message", result.get("error", "Scroll failed"))
        return result

    def _do_ui_drag(self, params: dict) -> dict:
        try:
            x1, y1 = int(params["x1"]), int(params["y1"])
            x2, y2 = int(params["x2"]), int(params["y2"])
        except (KeyError, ValueError, TypeError) as exc:
            return {"success": False, "message": f"x1/y1/x2/y2 required: {exc}"}
        duration = float(params.get("duration", 0.5))
        result = ua.drag(x1, y1, x2, y2, duration=duration)
        result.setdefault("message", result.get("error", "Drag failed"))
        return result

    def _do_ui_type_in_field(self, params: dict) -> dict:
        try:
            x, y = int(params["x"]), int(params["y"])
        except (KeyError, ValueError, TypeError) as exc:
            return {"success": False, "message": f"x/y required: {exc}"}
        text = params.get("text")
        if text is None:
            return {"success": False, "message": "text is required"}
        clear_first = bool(params.get("clear_first", True))
        result = ua.type_in_field(x, y, str(text), clear_first=clear_first)
        result.setdefault("message", result.get("error", "Type in field failed"))
        return result

    # -----------------------------------------------------------------------
    # App / window management actions
    # -----------------------------------------------------------------------

    def _do_launch_app(self, params: dict) -> dict:
        executable = params.get("executable")
        if not executable:
            return {"success": False, "message": "executable is required"}
        result = ua.launch_app(
            executable=executable,
            args=params.get("args"),
            cwd=params.get("cwd"),
            wait_ms=int(params.get("wait_ms", 2000)),
            capture_after=bool(params.get("capture_after", True)),
            window_title_hint=params.get("window_title_hint"),
        )
        result.setdefault(
            "message",
            f"Launched {executable} (pid={result.get('pid')})" if result.get("success")
            else result.get("error", "Launch failed")
        )
        return result

    def _do_get_window_info(self, params: dict) -> dict:
        window_title = params.get("window_title", "").strip()
        if not window_title:
            return {"success": False, "message": "window_title is required"}
        result = ua.find_window(window_title)
        result.setdefault(
            "message",
            f"Found window: '{result.get('title')}' at ({result.get('x')},{result.get('y')}) "
            f"{result.get('width')}x{result.get('height')}" if result.get("success")
            else result.get("error", "Window not found")
        )
        return result

    def _do_list_windows(self, params: dict) -> dict:
        windows = ua.list_windows()
        return {
            "success": True,
            "message": f"Found {len(windows)} visible windows",
            "windows": windows,
        }

    def _do_bring_window_to_front(self, params: dict) -> dict:
        window_title = params.get("window_title", "").strip()
        if not window_title:
            return {"success": False, "message": "window_title is required"}
        win = ua.find_window(window_title)
        if not win.get("success"):
            return {"success": False, "message": win.get("error", "Window not found")}
        return ua.bring_window_to_front(win["hwnd"])

    def _do_get_screen_size(self, params: dict) -> dict:
        size = ua.get_screen_size()
        return {
            "success": True,
            "message": f"Screen: {size['width']}x{size['height']}",
            **size,
        }

    def _do_get_mouse_position(self, params: dict) -> dict:
        pos = ua.get_mouse_position()
        return {
            "success": True,
            "message": f"Mouse at ({pos['x']}, {pos['y']})",
            **pos,
        }
