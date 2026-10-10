"""End-to-end check: run one real plan-and-run job through a running gateway.

    python -m local_ai.gateway            # in one terminal, with the big model available
    python -m local_ai.e2e                # in another

Creates a small sample repository in a temporary folder, asks the gateway to plan and
carry out a task on it (Claude Code headless on the `planner` and `worker` routes),
approves the plan, prints every agent event with timings, then runs the sample's tests
inside the job's worktree to judge the result.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from local_ai.gateway_config import load_gateway

SAMPLE = {
    "inventory.py": '''"""A tiny stock inventory used to exercise the local agent pipeline."""

from dataclasses import dataclass, field


@dataclass
class Item:
    sku: str
    name: str
    quantity: int = 0
    unit_price: float = 0.0


@dataclass
class Inventory:
    items: dict[str, Item] = field(default_factory=dict)

    def add(self, item: Item) -> None:
        self.items[item.sku] = item

    def remove(self, sku: str, quantity: int) -> None:
        item = self.items[sku]
        item.quantity -= quantity

    def value(self) -> float:
        return sum(i.quantity * i.unit_price for i in self.items.values())
''',
    "test_inventory.py": '''from inventory import Inventory, Item


def test_value():
    inv = Inventory()
    inv.add(Item("a", "bolt", 10, 0.5))
    inv.add(Item("b", "nut", 4, 0.25))
    assert inv.value() == 6.0
''',
}

TASK = (
    "In inventory.py: make Inventory.remove raise ValueError when the quantity is not positive or is more "
    "than is in stock, and raise KeyError naming the SKU when the SKU is unknown. Add a method "
    "Inventory.low_stock(threshold) that returns the SKUs whose quantity is below threshold, sorted. "
    "Add pytest tests for each of these behaviours to test_inventory.py."
)


def make_sample(root: Path) -> Path:
    repo = root / "inventory"
    repo.mkdir(parents=True)
    for name, text in SAMPLE.items():
        (repo / name).write_text(text, encoding="utf-8")
    ident = ["-c", "user.email=e2e@example.com", "-c", "user.name=e2e"]
    for args in (["init", "-q", "-b", "main"], [*ident, "add", "."], [*ident, "commit", "-qm", "baseline"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    return repo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", default="plan_and_run", choices=("plan", "run", "plan_and_run"))
    parser.add_argument("--repo", type=Path, help="use this repository instead of a fresh sample")
    args = parser.parse_args(argv)

    spec = load_gateway()
    host, port = spec.listen[0]
    base = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    token = spec.token_file.read_text(encoding="utf-8").strip()

    def call(path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=60) as reply:
            return json.load(reply)

    repo = args.repo or make_sample(Path(tempfile.mkdtemp(prefix="local-ai-e2e-")))
    started = time.time()
    job_id = call("/gateway/jobs", {"task": TASK, "cwd": str(repo), "mode": args.mode, "review_plan": True,
                                    "max_parallel": 2, "worktrees": True})["id"]
    print(f"job {job_id} on {repo}", flush=True)
    after, approved = 0, False
    while True:
        page = call(f"/gateway/jobs/{job_id}/events?after={after}")
        for event in page["events"]:
            text = (event["text"] or "").replace("\n", " ")[:300]
            print(f"{time.time() - started:7.1f}s {event['step'] or 'job':4} {event['kind']:11} {text}", flush=True)
        after = page["next"]
        if page["status"] == "awaiting_approval" and not approved:
            print(json.dumps(call(f"/gateway/jobs/{job_id}")["plan"], indent=1), flush=True)
            call(f"/gateway/jobs/{job_id}/approve", {})
            approved = True
        if page["status"] in ("done", "failed", "cancelled"):
            break

    job = call(f"/gateway/jobs/{job_id}")
    print(f"\n{job['status']} in {time.time() - started:.0f}s; steps "
          f"{[(s['id'], s['status']) for s in job['plan']]}; error={job['error']}")
    passed = False
    if job.get("worktree"):
        print("\n--- diff ---\n" + subprocess.run(["git", "-C", job["worktree"], "diff"],
                                                  capture_output=True, text=True).stdout[:6000])
        tests = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=job["worktree"], capture_output=True, text=True)
        print("--- pytest in the worktree ---\n" + tests.stdout[-2500:])
        passed = tests.returncode == 0
    print(json.dumps(call("/gateway/status")["backends"], indent=1))
    return 0 if job["status"] == "done" and passed else 1


if __name__ == "__main__":
    sys.exit(main())
