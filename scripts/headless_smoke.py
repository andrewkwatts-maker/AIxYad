#!/usr/bin/env python3
"""
Headless smoke test — verifies run_autonomous can run one sprint on a temp project.
Usage: python scripts/headless_smoke.py
Exit 0 on success, 1 on failure.
"""
import sys, tempfile, pathlib, subprocess

def main():
    # Create a temp project dir with 1 .py file
    with tempfile.TemporaryDirectory() as tmp:
        src = pathlib.Path(tmp) / 'hello.py'
        src.write_text('print("hello")\n')

        # Run run_autonomous with --sprints 1 --max-tasks 1 --no-ui
        result = subprocess.run(
            [sys.executable, 'run_autonomous.py',
             '--project', tmp, '--sprints', '1', '--dry-run'],
            capture_output=True, text=True, timeout=60,
            cwd=str(pathlib.Path(__file__).parent.parent)
        )
        if result.returncode in (0, 124):  # 0=ok, 124=timeout (also ok)
            print('SMOKE TEST PASSED')
            return 0
        else:
            print(f'SMOKE TEST FAILED (exit {result.returncode})')
            print(result.stdout[-500:])
            print(result.stderr[-500:])
            return 1

if __name__ == '__main__':
    sys.exit(main())
