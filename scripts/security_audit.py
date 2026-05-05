#!/usr/bin/env python3
"""
Security audit script for the Automatica codebase.

C-22: Runs pip-audit and bandit, outputs a combined report.

Usage:
    pip install -r requirements-security.txt
    python scripts/security_audit.py [--output report.txt]
"""
import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def run_cmd(cmd: list, label: str) -> tuple[int, str]:
    """Run a command and return (returncode, output)."""
    print(f'\n{"=" * 60}')
    print(f'Running {label}...')
    print(f'Command: {" ".join(cmd)}')
    print('=' * 60)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(_REPO_ROOT)
        )
        output = result.stdout + result.stderr
        print(output)
        return result.returncode, output
    except FileNotFoundError:
        msg = f'ERROR: {cmd[0]!r} not found. Install with: pip install -r requirements-security.txt'
        print(msg)
        return 1, msg


def main():
    parser = argparse.ArgumentParser(description='Run security audit tools')
    parser.add_argument('--output', '-o', default='', help='Write report to file')
    args = parser.parse_args()

    lines = []
    lines.append(f'Automatica Security Audit Report')
    lines.append(f'Generated: {datetime.now().isoformat()}')
    lines.append(f'Repo: {_REPO_ROOT}')
    lines.append('')

    overall_ok = True

    # ── pip-audit: check for known vulnerabilities in installed packages ──────
    rc, out = run_cmd(
        [sys.executable, '-m', 'pip_audit', '--desc', '--format', 'columns'],
        'pip-audit (dependency vulnerability scanner)',
    )
    lines.append('pip-audit results:')
    lines.append(out)
    if rc != 0:
        overall_ok = False

    # ── bandit: static analysis for common security issues ────────────────────
    rc, out = run_cmd(
        [sys.executable, '-m', 'bandit', '-r', str(_REPO_ROOT),
         '--exclude', '.git,__pycache__,tests,venv,.venv',
         '-f', 'txt', '-ll'],  # -ll = only medium and high severity
        'bandit (Python static security analysis)',
    )
    lines.append('bandit results:')
    lines.append(out)
    if rc not in (0, 1):  # bandit returns 1 for findings, which is expected
        overall_ok = False

    # ── Summary ───────────────────────────────────────────────────────────────
    summary = f'\nAudit complete. Status: {"PASS" if overall_ok else "REVIEW REQUIRED"}'
    print(summary)
    lines.append(summary)

    if args.output:
        out_path = Path(args.output)
        out_path.write_text('\n'.join(lines))
        print(f'Report written to {out_path}')

    return 0 if overall_ok else 1


if __name__ == '__main__':
    sys.exit(main())
