"""Validate all screen modules can be parsed/imported without Kivy running."""
import ast, sys
from pathlib import Path
import pytest

UI_DIR = Path(r'H:\Github\Automatica-App')

def get_screen_files():
    screens_dir = UI_DIR / 'screens'
    if screens_dir.exists():
        return list(screens_dir.glob('*.py'))
    return []

def get_widget_files():
    widgets_dir = UI_DIR / 'widgets'
    if widgets_dir.exists():
        return list(widgets_dir.glob('*.py'))
    return []

@pytest.mark.parametrize('screen_file', get_screen_files(), ids=lambda f: f.name)
def test_screen_syntax_valid(screen_file):
    """All screen files must be syntactically valid Python."""
    try:
        ast.parse(screen_file.read_text(encoding='utf-8', errors='replace'))
    except SyntaxError as e:
        pytest.fail(f'SyntaxError in {screen_file.name}: {e}')

@pytest.mark.parametrize('widget_file', get_widget_files(), ids=lambda f: f.name)
def test_widget_syntax_valid(widget_file):
    """All widget files must be syntactically valid Python."""
    try:
        ast.parse(widget_file.read_text(encoding='utf-8', errors='replace'))
    except SyntaxError as e:
        pytest.fail(f'SyntaxError in {widget_file.name}: {e}')
