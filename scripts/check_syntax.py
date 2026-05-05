#!/usr/bin/env python3
"""Check syntax of all Python files in both repos."""
import ast, sys
from pathlib import Path

def check_dir(root: Path) -> list[str]:
    errors = []
    for f in root.rglob('*.py'):
        if any(p in f.parts for p in ('__pycache__', '.git', 'build')):
            continue
        try:
            ast.parse(f.read_text(encoding='utf-8', errors='replace'))
        except SyntaxError as e:
            errors.append(f'{f}: line {e.lineno}: {e.msg}')
    return errors

def main():
    engine_dir = Path(r'H:\Github\Automatica')
    ui_dir = Path(r'H:\Github\Automatica-App')
    all_errors = []
    for d in [engine_dir, ui_dir]:
        if d.exists():
            all_errors.extend(check_dir(d))
    if all_errors:
        print(f'SYNTAX ERRORS ({len(all_errors)}):')
        for e in all_errors:
            print(f'  {e}')
        sys.exit(1)
    else:
        print(f'All Python files pass syntax check.')
        sys.exit(0)

if __name__ == '__main__':
    main()
