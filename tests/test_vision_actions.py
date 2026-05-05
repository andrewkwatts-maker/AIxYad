#!/usr/bin/env python3
#====== Automatica/test_vision_actions.py ======
#!copyright (c) 2025 Andrew Keith Watts. All rights reserved.
"""
Unit and integration tests for:
  - ollama_utils.py    (model list querying)
  - ui_automation.py  (screenshot, window, input helpers)
  - vision_actions.py (agent action handlers)

Unit tests run without Ollama or a display.
Integration tests are skipped unless Ollama is running.

Run: python -m pytest test_vision_actions.py -v
Run integration only: python -m pytest test_vision_actions.py -v -k integration
"""

import base64
import io
import json
import sys
import tempfile
import time
import unittest.mock as mock
from pathlib import Path

import pytest

# =====================================================================
#  Availability helpers
# =====================================================================

def ollama_running() -> bool:
    from ollama_utils import is_ollama_running
    return is_ollama_running()


def vision_model_available() -> bool:
    from ollama_utils import get_vision_models
    return len(get_vision_models()) > 0


skip_no_ollama = pytest.mark.skipif(
    not ollama_running(), reason="Ollama not running at localhost:11434"
)
skip_no_vision_model = pytest.mark.skipif(
    not vision_model_available(),
    reason="No vision-capable Ollama model installed (need moondream, llava, or llama3.2-vision)"
)

# =====================================================================
#  TestOllamaUtils
# =====================================================================

class TestOllamaUtils:

    def test_list_installed_models_offline_returns_empty(self):
        """Unreachable endpoint should return empty list, not raise."""
        from ollama_utils import list_installed_models
        result = list_installed_models("http://127.0.0.1:19999", timeout=1)
        assert result == []

    def test_is_ollama_running_offline(self):
        from ollama_utils import is_ollama_running
        assert is_ollama_running("http://127.0.0.1:19999", timeout=1) is False

    def test_get_vision_models_offline_returns_empty(self):
        from ollama_utils import get_vision_models
        result = get_vision_models("http://127.0.0.1:19999", timeout=1)
        assert result == []

    def test_get_vision_models_filters_correctly(self):
        """get_vision_models should only return vision-capable model names."""
        from ollama_utils import get_vision_models
        with mock.patch("ollama_utils.list_installed_models") as mock_list:
            mock_list.return_value = ["llama3.2", "moondream", "codellama", "llava", "mistral"]
            result = get_vision_models()
        assert "moondream" in result
        assert "llava" in result
        assert "llama3.2" not in result
        assert "codellama" not in result

    def test_list_models_strips_tag_suffix(self):
        """Model names like 'llama3.2:latest' should be stored as 'llama3.2'."""
        from ollama_utils import list_installed_models
        fake_response = json.dumps({
            "models": [
                {"name": "llama3.2:latest"},
                {"name": "mistral:7b-instruct"},
                {"name": "moondream:latest"},
            ]
        }).encode()
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = lambda s: s
            mock_urlopen.return_value.__exit__ = mock.Mock(return_value=False)
            mock_urlopen.return_value.read.return_value = fake_response
            result = list_installed_models()
        assert "llama3.2" in result
        assert "mistral" in result
        assert "moondream" in result
        assert "llama3.2:latest" not in result

    def test_get_combined_model_list_installed_first(self):
        """Installed models appear before popular-but-uninstalled models."""
        from ollama_utils import get_combined_model_list
        with mock.patch("ollama_utils.list_installed_models") as mock_list:
            mock_list.return_value = ["zephyr", "mistral"]
            result = get_combined_model_list()
        # Installed models should be first
        assert result.index("zephyr") < result.index("llama3.2")
        assert result.index("mistral") < result.index("llama3.3")

    def test_is_vision_capable_heuristic(self):
        from ollama_utils import is_vision_capable
        assert is_vision_capable("moondream") is True
        assert is_vision_capable("llava:13b") is True
        assert is_vision_capable("llama3.2-vision") is True
        assert is_vision_capable("llama3.2") is False
        assert is_vision_capable("deepseek-coder-v2") is False

    def test_popular_models_includes_user_models(self):
        """The canonical popular list should include the models the user is downloading."""
        from ollama_utils import OLLAMA_POPULAR_MODELS
        required = [
            "deepseek-coder-v2", "mistral", "phi4", "gemma2",
            "llama3.1", "llama3.2-vision", "moondream",
        ]
        for model in required:
            # match by base name
            found = any(model in m for m in OLLAMA_POPULAR_MODELS)
            assert found, f"'{model}' not in OLLAMA_POPULAR_MODELS"

    @skip_no_ollama
    def test_list_installed_models_live(self):
        from ollama_utils import list_installed_models
        result = list_installed_models()
        assert isinstance(result, list)
        # All entries should be strings with no colon-tag
        for name in result:
            assert isinstance(name, str)
            assert len(name) > 0


# =====================================================================
#  TestUiAutomation
# =====================================================================

class TestUiAutomation:

    def test_image_to_base64_roundtrip(self, tmp_path):
        """image_to_base64 should encode and be decodable."""
        import ui_automation as ua
        # Create a tiny PNG using Pillow
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        img = Image.new("RGB", (10, 10), color=(255, 0, 0))
        img_path = str(tmp_path / "test.png")
        img.save(img_path)

        b64 = ua.image_to_base64(img_path)
        assert isinstance(b64, str)
        assert len(b64) > 0
        # Should decode back to bytes
        decoded = base64.b64decode(b64)
        assert len(decoded) > 0

    def test_pil_image_to_base64(self):
        """pil_image_to_base64 should produce valid base64."""
        import ui_automation as ua
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed")
        img = Image.new("RGB", (4, 4), color=(0, 128, 255))
        b64 = ua.pil_image_to_base64(img)
        assert isinstance(b64, str)
        decoded = base64.b64decode(b64)
        assert decoded[:8] == b'\x89PNG\r\n\x1a\n'  # PNG magic bytes

    def test_find_window_not_found_returns_error(self):
        """find_window should return success=False, not raise, when window missing."""
        import ui_automation as ua
        result = ua.find_window("__nonexistent_window_12345__")
        assert result["success"] is False
        assert "error" in result

    def test_list_windows_returns_list(self):
        """list_windows should always return a list (may be empty on CI)."""
        import ui_automation as ua
        result = ua.list_windows()
        assert isinstance(result, list)

    def test_screenshot_no_pillow_returns_error(self):
        """If Pillow is unavailable, take_screenshot should return error dict."""
        import ui_automation as ua
        original = ua._PIL_AVAILABLE
        try:
            ua._PIL_AVAILABLE = False
            result = ua.take_screenshot()
            assert result["success"] is False
            assert "error" in result
        finally:
            ua._PIL_AVAILABLE = original

    def test_mouse_click_no_pyautogui_returns_error(self):
        """If pyautogui is unavailable, mouse_click should return error dict."""
        import ui_automation as ua
        original = ua._PYAUTOGUI_AVAILABLE
        try:
            ua._PYAUTOGUI_AVAILABLE = False
            result = ua.mouse_click(100, 100)
            assert result["success"] is False
            assert "pyautogui" in result["error"].lower()
        finally:
            ua._PYAUTOGUI_AVAILABLE = original

    def test_type_text_no_pyautogui_returns_error(self):
        import ui_automation as ua
        original = ua._PYAUTOGUI_AVAILABLE
        try:
            ua._PYAUTOGUI_AVAILABLE = False
            result = ua.type_text("hello")
            assert result["success"] is False
        finally:
            ua._PYAUTOGUI_AVAILABLE = original

    def test_key_press_no_pyautogui_returns_error(self):
        import ui_automation as ua
        original = ua._PYAUTOGUI_AVAILABLE
        try:
            ua._PYAUTOGUI_AVAILABLE = False
            result = ua.key_press(["ctrl", "c"])
            assert result["success"] is False
        finally:
            ua._PYAUTOGUI_AVAILABLE = original

    def test_mouse_click_in_window_propagates_window_not_found(self):
        """mouse_click_in_window should propagate find_window error when hwnd=0."""
        import ui_automation as ua
        # We test via mocking get_window_rect to simulate failure
        with mock.patch.object(ua, 'get_window_rect', return_value={"success": False, "error": "No such window"}):
            result = ua.mouse_click_in_window(0, 50, 50)
        assert result["success"] is False
        assert "error" in result

    def test_launch_app_invalid_executable(self):
        """launch_app should return success=False for nonexistent executable."""
        import ui_automation as ua
        result = ua.launch_app("__nonexistent_executable_xyz__", capture_after=False)
        assert result["success"] is False
        assert "error" in result

    def test_screenshot_dir_created(self, tmp_path):
        """Screenshots should auto-create the logs/screenshots directory."""
        import ui_automation as ua
        original_dir = ua._SCREENSHOT_DIR
        try:
            ua._SCREENSHOT_DIR = tmp_path / "logs" / "screenshots"
            assert not ua._SCREENSHOT_DIR.exists()
            ua._ensure_screenshot_dir()
            assert ua._SCREENSHOT_DIR.exists()
        finally:
            ua._SCREENSHOT_DIR = original_dir

    def test_get_screen_size_returns_positive(self):
        """get_screen_size should return positive width/height."""
        import ui_automation as ua
        size = ua.get_screen_size()
        assert size["width"] > 0
        assert size["height"] > 0


# =====================================================================
#  TestVisionActionHandler
# =====================================================================

class TestVisionActionHandler:

    @pytest.fixture
    def handler(self):
        from vision_actions import VisionActionHandler
        return VisionActionHandler(default_vision_model="moondream")

    def test_handle_unknown_action_returns_error(self, handler):
        result = handler.handle("fly_to_moon", {})
        assert result["success"] is False
        assert "Unknown" in result["message"]

    def test_screenshot_action_calls_take_screenshot(self, handler):
        import ui_automation as ua
        with mock.patch.object(ua, 'take_screenshot', return_value={
            "success": True, "path": "/tmp/s.png", "width": 1920, "height": 1080, "base64": "abc"
        }) as mock_ss:
            result = handler.handle("screenshot", {})
        mock_ss.assert_called_once()
        assert result["success"] is True
        assert "1920" in result["message"]

    def test_screenshot_window_calls_screenshot_window(self, handler):
        import ui_automation as ua
        with mock.patch.object(ua, 'screenshot_window', return_value={
            "success": True, "path": "/tmp/w.png", "width": 800, "height": 600,
            "base64": "xyz", "window": {"x": 100, "y": 50, "width": 800, "height": 600,
                                         "title": "TestWin", "hwnd": 1}
        }) as mock_sw:
            result = handler.handle("screenshot_window", {"window_title": "TestWin"})
        mock_sw.assert_called_once_with("TestWin", None)
        assert result["success"] is True

    def test_screenshot_window_requires_title(self, handler):
        result = handler.handle("screenshot_window", {})
        assert result["success"] is False
        assert "window_title" in result["message"]

    def test_analyze_image_requires_source(self, handler):
        result = handler.handle("analyze_image", {"prompt": "what is this?"})
        assert result["success"] is False
        assert "image_path" in result["message"] or "required" in result["message"]

    def test_analyze_image_model_not_responding(self, handler, tmp_path):
        """When vision model returns None, return informative error."""
        from vision_actions import _query_vision_model
        # Create a tiny real image
        try:
            from PIL import Image as PILImage
        except ImportError:
            pytest.skip("Pillow not installed")
        img = PILImage.new("RGB", (8, 8), color=(0, 0, 0))
        img_path = str(tmp_path / "test.png")
        img.save(img_path)

        with mock.patch("vision_actions._query_vision_model", return_value=None):
            result = handler.handle("analyze_image", {
                "image_path": img_path,
                "prompt": "describe this",
                "model": "nonexistent-model",
            })
        assert result["success"] is False
        assert "nonexistent-model" in result["message"] or "not respond" in result["message"]

    def test_analyze_image_parses_structured_json(self, handler, tmp_path):
        """analyze_image should parse model JSON and return elements with bounds."""
        try:
            from PIL import Image as PILImage
        except ImportError:
            pytest.skip("Pillow not installed")
        img = PILImage.new("RGB", (100, 200), color=(255, 255, 255))
        img_path = str(tmp_path / "test.png")
        img.save(img_path)

        model_response = json.dumps({
            "description": "A white rectangle with a button",
            "elements": [{
                "label": "OK Button",
                "type": "button",
                "text": "OK",
                "bounds_pct": {"x_min": 0.3, "y_min": 0.8, "x_max": 0.7, "y_max": 0.9},
                "confidence": 0.95,
            }]
        })

        with mock.patch("vision_actions._query_vision_model", return_value=model_response):
            result = handler.handle("analyze_image", {
                "image_path": img_path,
                "prompt": "find buttons",
                "return_bounds": True,
            })

        assert result["success"] is True
        assert result["description"] == "A white rectangle with a button"
        assert len(result["elements"]) == 1
        el = result["elements"][0]
        assert el["label"] == "OK Button"
        # bounds_pct should have been converted to pixel bounds
        assert "bounds" in el
        assert "center" in el
        # Pixel bounds: x_min=0.3*100=30, y_min=0.8*200=160
        assert el["bounds"]["x_min"] == 30
        assert el["bounds"]["y_min"] == 160
        assert el["center"]["x"] == (30 + 70) // 2

    def test_analyze_image_converts_bounds_pct_to_pixels(self, handler, tmp_path):
        """bounds_pct values should be scaled by image dimensions."""
        try:
            from PIL import Image as PILImage
        except ImportError:
            pytest.skip("Pillow not installed")
        img = PILImage.new("RGB", (400, 300), color=(0, 0, 0))
        img_path = str(tmp_path / "img.png")
        img.save(img_path)

        response = json.dumps({
            "description": "test",
            "elements": [{
                "label": "Btn", "type": "button", "text": "X",
                "bounds_pct": {"x_min": 0.5, "y_min": 0.5, "x_max": 1.0, "y_max": 1.0},
                "confidence": 0.9,
            }]
        })

        with mock.patch("vision_actions._query_vision_model", return_value=response):
            result = handler.handle("analyze_image", {
                "image_path": img_path, "return_bounds": True, "prompt": "find"
            })

        el = result["elements"][0]
        assert el["bounds"]["x_min"] == 200   # 0.5 * 400
        assert el["bounds"]["y_min"] == 150   # 0.5 * 300
        assert el["bounds"]["x_max"] == 400   # 1.0 * 400
        assert el["bounds"]["y_max"] == 300   # 1.0 * 300
        assert el["center"]["x"] == 300
        assert el["center"]["y"] == 225

    def test_analyze_image_unstructured_response(self, handler, tmp_path):
        """Non-JSON model response should be returned as raw description."""
        try:
            from PIL import Image as PILImage
        except ImportError:
            pytest.skip("Pillow not installed")
        img = PILImage.new("RGB", (10, 10))
        img_path = str(tmp_path / "img.png")
        img.save(img_path)

        with mock.patch("vision_actions._query_vision_model",
                        return_value="This is a screenshot of a desktop."):
            result = handler.handle("analyze_image", {
                "image_path": img_path, "prompt": "what do you see?"
            })

        assert result["success"] is True
        assert "This is a screenshot" in result["description"]
        assert result["elements"] == []

    def test_ui_click_without_window_calls_mouse_click(self, handler):
        import ui_automation as ua
        with mock.patch.object(ua, 'mouse_click', return_value={
            "success": True, "message": "clicked left at (200, 300)"
        }) as mock_click:
            result = handler.handle("ui_click", {"x": 200, "y": 300})
        mock_click.assert_called_once_with(200, 300, button="left", double=False)
        assert result["success"] is True

    def test_ui_click_with_window_title_adds_offset(self, handler):
        """When window_title given, x/y should be offset by window position."""
        import ui_automation as ua
        with mock.patch.object(ua, 'find_window', return_value={
            "success": True, "hwnd": 1, "title": "TestApp",
            "x": 100, "y": 50, "width": 800, "height": 600,
            "center_x": 500, "center_y": 350,
        }), mock.patch.object(ua, 'mouse_click', return_value={
            "success": True, "message": "clicked"
        }) as mock_click:
            result = handler.handle("ui_click", {"x": 50, "y": 30, "window_title": "TestApp"})

        # Absolute = window_x + rel_x = 100+50=150, 50+30=80
        mock_click.assert_called_once_with(150, 80, button="left", double=False)
        assert result["success"] is True

    def test_ui_click_window_not_found_returns_error(self, handler):
        import ui_automation as ua
        with mock.patch.object(ua, 'find_window', return_value={
            "success": False, "error": "No window found matching 'Ghost'"
        }):
            result = handler.handle("ui_click", {"x": 10, "y": 10, "window_title": "Ghost"})
        assert result["success"] is False
        assert "Ghost" in result["message"]

    def test_ui_type_calls_type_text(self, handler):
        import ui_automation as ua
        with mock.patch.object(ua, 'type_text', return_value={
            "success": True, "message": "Typed 5 characters"
        }) as mock_type:
            result = handler.handle("ui_type", {"text": "hello"})
        mock_type.assert_called_once_with("hello", interval=0.02)
        assert result["success"] is True

    def test_ui_type_missing_text_returns_error(self, handler):
        result = handler.handle("ui_type", {})
        assert result["success"] is False

    def test_ui_key_press_calls_key_press(self, handler):
        import ui_automation as ua
        with mock.patch.object(ua, 'key_press', return_value={
            "success": True, "message": "Pressed ctrl+s"
        }) as mock_kp:
            result = handler.handle("ui_key_press", {"keys": ["ctrl", "s"]})
        mock_kp.assert_called_once_with(["ctrl", "s"])
        assert result["success"] is True

    def test_ui_key_press_string_converted_to_list(self, handler):
        import ui_automation as ua
        with mock.patch.object(ua, 'key_press', return_value={"success": True, "message": "ok"}) as mk:
            handler.handle("ui_key_press", {"keys": "enter"})
        mk.assert_called_once_with(["enter"])

    def test_ui_scroll_calls_scroll(self, handler):
        import ui_automation as ua
        with mock.patch.object(ua, 'scroll', return_value={
            "success": True, "message": "Scrolled down 3 clicks"
        }) as mock_scroll:
            result = handler.handle("ui_scroll", {"x": 500, "y": 400, "direction": "up", "clicks": 5})
        mock_scroll.assert_called_once_with(500, 400, clicks=5, direction="up")
        assert result["success"] is True

    def test_ui_drag_calls_drag(self, handler):
        import ui_automation as ua
        with mock.patch.object(ua, 'drag', return_value={
            "success": True, "message": "Dragged"
        }) as mock_drag:
            result = handler.handle("ui_drag", {"x1": 10, "y1": 20, "x2": 100, "y2": 200, "duration": 1.0})
        mock_drag.assert_called_once_with(10, 20, 100, 200, duration=1.0)
        assert result["success"] is True

    def test_launch_app_calls_ui_automation(self, handler):
        import ui_automation as ua
        with mock.patch.object(ua, 'launch_app', return_value={
            "success": True, "pid": 1234, "executable": "notepad.exe"
        }) as mock_launch:
            result = handler.handle("launch_app", {
                "executable": "notepad.exe", "capture_after": False
            })
        mock_launch.assert_called_once()
        assert result["success"] is True
        assert result["pid"] == 1234

    def test_launch_app_missing_executable_returns_error(self, handler):
        result = handler.handle("launch_app", {})
        assert result["success"] is False
        assert "executable" in result["message"]

    def test_get_window_info_calls_find_window(self, handler):
        import ui_automation as ua
        with mock.patch.object(ua, 'find_window', return_value={
            "success": True, "hwnd": 42, "title": "Notepad",
            "x": 0, "y": 0, "width": 800, "height": 600,
            "center_x": 400, "center_y": 300,
        }) as mock_fw:
            result = handler.handle("get_window_info", {"window_title": "Notepad"})
        mock_fw.assert_called_once_with("Notepad")
        assert result["success"] is True
        assert result["hwnd"] == 42

    def test_get_window_info_not_found_returns_error(self, handler):
        import ui_automation as ua
        with mock.patch.object(ua, 'find_window', return_value={
            "success": False, "error": "No window found"
        }):
            result = handler.handle("get_window_info", {"window_title": "Ghost"})
        assert result["success"] is False

    def test_list_windows_returns_windows(self, handler):
        import ui_automation as ua
        with mock.patch.object(ua, 'list_windows', return_value=[
            {"hwnd": 1, "title": "Taskbar", "x": 0, "y": 1040, "width": 1920, "height": 40},
            {"hwnd": 2, "title": "Desktop", "x": 0, "y": 0, "width": 1920, "height": 1080},
        ]):
            result = handler.handle("list_windows", {})
        assert result["success"] is True
        assert len(result["windows"]) == 2

    def test_get_screen_size_returns_dimensions(self, handler):
        result = handler.handle("get_screen_size", {})
        assert result["success"] is True
        assert result["width"] > 0
        assert result["height"] > 0

    def test_get_available_actions_count(self):
        from vision_actions import VisionActionHandler
        actions = VisionActionHandler.get_available_actions()
        assert len(actions) >= 15
        names = {a["name"] for a in actions}
        for expected in ["screenshot", "analyze_image", "ui_click", "ui_type",
                         "launch_app", "get_window_info", "list_windows"]:
            assert expected in names, f"'{expected}' missing from available actions"

    def test_get_available_actions_schema_format(self):
        """Every action should have name, description, and params."""
        from vision_actions import VisionActionHandler
        for action in VisionActionHandler.get_available_actions():
            assert "name" in action
            assert "description" in action
            assert "params" in action
            assert isinstance(action["params"], dict)


# =====================================================================
#  TestVisionBoundsConversion
# =====================================================================

class TestVisionBoundsConversion:

    def test_convert_bounds_pct_to_pixels_basic(self):
        from vision_actions import _convert_bounds_pct_to_pixels
        elements = [{
            "label": "Button",
            "type": "button",
            "text": "OK",
            "bounds_pct": {"x_min": 0.1, "y_min": 0.2, "x_max": 0.5, "y_max": 0.4},
            "confidence": 1.0,
        }]
        result = _convert_bounds_pct_to_pixels(elements, img_width=1000, img_height=500)
        el = result[0]
        assert el["bounds"]["x_min"] == 100    # 0.1 * 1000
        assert el["bounds"]["y_min"] == 100    # 0.2 * 500
        assert el["bounds"]["x_max"] == 500    # 0.5 * 1000
        assert el["bounds"]["y_max"] == 200    # 0.4 * 500
        assert el["bounds"]["width"] == 400
        assert el["bounds"]["height"] == 100
        assert el["center"]["x"] == 300
        assert el["center"]["y"] == 150

    def test_convert_bounds_pct_no_bounds_preserved(self):
        """Elements without bounds_pct should be returned unchanged."""
        from vision_actions import _convert_bounds_pct_to_pixels
        elements = [{"label": "Title", "type": "text", "text": "Hello", "confidence": 0.9}]
        result = _convert_bounds_pct_to_pixels(elements, 800, 600)
        assert result[0]["label"] == "Title"
        assert "bounds" not in result[0]
        assert "center" not in result[0]

    def test_convert_bounds_pct_removes_bounds_pct_key(self):
        from vision_actions import _convert_bounds_pct_to_pixels
        elements = [{"label": "X", "bounds_pct": {"x_min": 0, "y_min": 0, "x_max": 1, "y_max": 1}}]
        result = _convert_bounds_pct_to_pixels(elements, 100, 100)
        assert "bounds_pct" not in result[0]

    def test_convert_bounds_pct_list_format(self):
        """bounds_pct as [x_min, y_min, x_max, y_max] list — format moondream sometimes emits."""
        from vision_actions import _convert_bounds_pct_to_pixels
        elements = [{"label": "Btn", "type": "button", "text": "OK",
                     "bounds_pct": [0.1, 0.2, 0.5, 0.4], "confidence": 0.9}]
        result = _convert_bounds_pct_to_pixels(elements, 1000, 500)
        assert len(result) == 1
        assert "bounds_pct" not in result[0]
        assert result[0]["bounds"]["x_min"] == 100  # 0.1 * 1000
        assert result[0]["bounds"]["y_min"] == 100  # 0.2 * 500
        assert result[0]["bounds"]["x_max"] == 500  # 0.5 * 1000
        assert result[0]["bounds"]["y_max"] == 200  # 0.4 * 500
        assert result[0]["center"] == {"x": 300, "y": 150}

    def test_convert_bounds_pct_non_dict_element_skipped(self):
        """Non-dict items in elements list are skipped gracefully."""
        from vision_actions import _convert_bounds_pct_to_pixels
        elements = [{"label": "OK", "bounds_pct": [0.0, 0.0, 0.5, 0.5]},
                    "not a dict",
                    42]
        result = _convert_bounds_pct_to_pixels(elements, 100, 100)
        assert len(result) == 1
        assert result[0]["label"] == "OK"

    def test_extract_json_from_response_direct(self):
        from vision_actions import _extract_json_from_response
        text = '{"description": "hi", "elements": []}'
        result = _extract_json_from_response(text)
        assert result == {"description": "hi", "elements": []}

    def test_extract_json_from_response_code_fence(self):
        from vision_actions import _extract_json_from_response
        text = 'Here is the result:\n```json\n{"description": "test", "elements": []}\n```'
        result = _extract_json_from_response(text)
        assert result["description"] == "test"

    def test_extract_json_from_response_embedded(self):
        from vision_actions import _extract_json_from_response
        text = 'I see a button. {"description": "button", "elements": []} That is all.'
        result = _extract_json_from_response(text)
        assert result["description"] == "button"

    def test_extract_json_from_response_invalid_returns_none(self):
        from vision_actions import _extract_json_from_response
        assert _extract_json_from_response("not json at all") is None
        assert _extract_json_from_response("") is None
        assert _extract_json_from_response(None) is None

    def test_extract_json_from_response_list_is_wrapped(self):
        """A JSON array of dicts is wrapped into {"description":"","elements":[...]}."""
        from vision_actions import _extract_json_from_response
        result = _extract_json_from_response('[{"label":"btn","type":"button"}]')
        assert result is not None
        assert "elements" in result
        assert result["elements"][0]["label"] == "btn"

    def test_extract_json_from_response_scalar_returns_none(self):
        """Scalar JSON (string, number) returns None."""
        from vision_actions import _extract_json_from_response
        assert _extract_json_from_response('"just a string"') is None
        assert _extract_json_from_response('42') is None

    def test_extract_json_from_response_rescues_flat_objects_from_broken_json(self):
        """Strategy 5: rescue flat {…} objects when outer array/nesting is broken."""
        from vision_actions import _extract_json_from_response
        # moondream sometimes outputs correct element objects inside a broken array
        broken = (
            '[{"label":"Submit Order", "type": "button", "text": "SUBMIT ORDER", "confidence": 0.9},'
            '{"elements":[{"label":"<name>", "type": "button"}]}]'
        )
        result = _extract_json_from_response(broken)
        assert result is not None
        # Should rescue the flat Submit Order object
        labels = [e.get("label", "") for e in result.get("elements", [])]
        assert "Submit Order" in labels

    def test_extract_json_from_response_rescue_ignores_nested_only(self):
        """Strategy 5 skips deeply nested objects that have inner braces."""
        from vision_actions import _extract_json_from_response
        # If only nested objects exist, strategy 5 should find nothing
        nested_only = '[{"outer": {"inner": "value"}}]'
        # The outer object has inner braces, so strategy 5 won't match it
        # (strategies 1-4 also fail for malformed JSON)
        result = _extract_json_from_response(nested_only)
        # The inner {"inner":"value"} has no label/type so it's not rescued as an element
        if result is not None:
            # If something was parsed, it shouldn't have label/type from the nested object
            assert result.get("elements", []) == [] or \
                   all("label" not in e and "type" not in e for e in result.get("elements", []))


# =====================================================================
#  TestAgentExecutorVisionIntegration
# =====================================================================

class TestAgentExecutorVisionIntegration:
    """Verify vision actions are routed correctly through AgentExecutor."""

    @pytest.fixture
    def executor(self, tmp_path):
        from agent_executor import AgentExecutor
        perms = {
            "can_screenshot": True,
            "can_ui_input": True,
            "can_run_command": True,
        }
        return AgentExecutor(tmp_path, perms)

    def test_screenshot_action_routed(self, executor):
        import ui_automation as ua
        with mock.patch.object(ua, 'take_screenshot', return_value={
            "success": True, "path": "/tmp/s.png", "width": 100, "height": 100, "base64": "abc"
        }):
            result = executor.execute({"action": "screenshot", "params": {}})
        assert result["success"] is True

    def test_ui_click_denied_when_no_permission(self, tmp_path):
        from agent_executor import AgentExecutor
        perms = {"can_screenshot": True, "can_ui_input": False}
        ex = AgentExecutor(tmp_path, perms)
        result = ex.execute({"action": "ui_click", "params": {"x": 0, "y": 0}})
        assert result["success"] is False
        assert "DENIED" in result["message"]

    def test_screenshot_denied_when_no_permission(self, tmp_path):
        from agent_executor import AgentExecutor
        perms = {"can_screenshot": False}
        ex = AgentExecutor(tmp_path, perms)
        result = ex.execute({"action": "screenshot", "params": {}})
        assert result["success"] is False
        assert "DENIED" in result["message"]

    def test_list_windows_action_routed(self, executor):
        import ui_automation as ua
        with mock.patch.object(ua, 'list_windows', return_value=[]):
            result = executor.execute({"action": "list_windows", "params": {}})
        assert result["success"] is True

    def test_get_screen_size_routed(self, executor):
        result = executor.execute({"action": "get_screen_size", "params": {}})
        assert result["success"] is True
        assert "width" in result


# =====================================================================
#  Integration tests (require Ollama + vision model)
# =====================================================================

class TestVisionActionsIntegration:

    @skip_no_ollama
    def test_screenshot_full_screen_integration(self):
        """Take a real full-screen screenshot and verify it's a valid PNG."""
        import ui_automation as ua
        result = ua.take_screenshot()
        if not result.get("success"):
            pytest.skip(f"Screenshot failed: {result.get('error')}")
        assert result["width"] > 0
        assert result["height"] > 0
        b64 = result["base64"]
        decoded = base64.b64decode(b64)
        assert decoded[:8] == b'\x89PNG\r\n\x1a\n', "Expected PNG magic bytes"
        # Clean up
        Path(result["path"]).unlink(missing_ok=True)

    @skip_no_vision_model
    def test_analyze_screenshot_with_vision_model_integration(self):
        """Take screenshot and analyze with a real vision model."""
        from vision_actions import VisionActionHandler
        from ollama_utils import get_vision_models
        models = get_vision_models()
        model = models[0]

        handler = VisionActionHandler(default_vision_model=model)
        result = handler.handle("analyze_screenshot", {
            "prompt": "Briefly describe what you see on the screen.",
            "model": model,
            "return_bounds": False,
        })

        assert result["success"] is True
        assert len(result.get("description", "")) > 0
        # Clean up screenshot
        if "screenshot" in result:
            Path(result["screenshot"].get("path", "")).unlink(missing_ok=True)

    @skip_no_vision_model
    def test_analyze_image_with_bounds_integration(self, tmp_path):
        """Analyze a synthetic image and request bounding boxes."""
        from vision_actions import VisionActionHandler
        from ollama_utils import get_vision_models
        try:
            from PIL import Image as PILImage, ImageDraw
        except ImportError:
            pytest.skip("Pillow not installed")

        models = get_vision_models()
        model = models[0]

        # Create a simple test image with a labeled rectangle
        img = PILImage.new("RGB", (400, 300), color=(200, 200, 200))
        draw = ImageDraw.Draw(img)
        draw.rectangle([150, 200, 250, 240], fill=(0, 120, 255))
        draw.text((175, 212), "OK", fill=(255, 255, 255))
        img_path = str(tmp_path / "test_ui.png")
        img.save(img_path)

        handler = VisionActionHandler(default_vision_model=model)
        result = handler.handle("analyze_image", {
            "image_path": img_path,
            "prompt": "Describe the UI elements in this image.",
            "model": model,
            "return_bounds": True,
        })

        assert result["success"] is True
        assert "description" in result
        assert result["image_size"]["width"] == 400
        assert result["image_size"]["height"] == 300
