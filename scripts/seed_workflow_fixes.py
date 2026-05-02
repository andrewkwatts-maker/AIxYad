"""Queue workflow-improvement tasks into the Automatica project backlog."""
import sqlite3
import os
from datetime import datetime, timezone

WORKFLOW_FIXES = [
    ("WF-1",
     "Sprint planner: blocked dep should not gridlock dependents forever",
     "sprint_runner._get_blocked_task_ids treats any non-done dep as blocking. "
     "When a parent task hits status=blocked (retry-cap), every descendant becomes "
     "permanently ineligible. Fix: when computing dependency satisfaction, treat "
     "blocked AND done as resolved IF the parent has been blocked > N hours OR has "
     "a blocked_reason indicating a recoverable error (CLI timeout, network). "
     "Alternatively, expose a planner mode skip_blocked_deps=true that admin can "
     "toggle. Add a unit test that gridlocked descendants get retried after parent "
     "unblock."),

    ("WF-2",
     "Retry cap: add cool-down then auto-retry instead of permanent block",
     "Once failure_count hits MAX_TASK_RETRIES (5), the task is blocked forever with "
     "no path back without manual reset. Fix: add blocked_until column. On hitting "
     "cap, set blocked_until = now + 24h. Eligibility check ignores blocked tasks "
     "where now >= blocked_until and instead resets failure_count and re-queues. "
     "Migration: ALTER TABLE tasks ADD COLUMN blocked_until TEXT."),

    ("WF-3",
     "Race protection: manual_claim_until field for human-queued tasks",
     "When user calls --AddTask, the new task immediately enters the autonomous loop "
     "pool. If the user is also doing the work manually (or via parallel agents), "
     "both race and the loop wastes retry budget. Fix: --AddTask sets a default "
     "manual_claim_until = now + 1h. Sprint planner skips tasks where "
     "manual_claim_until > now. Provide --no-claim flag to override."),

    ("WF-4",
     "Classify failure reason: transient vs refusal vs build-break vs timeout",
     "Currently every failure type increments failure_count uniformly. A transient "
     "network blip should not consume the same retry budget as a Claude refusal. "
     "Fix: add failure_class column (transient|refusal|timeout|build|test|unknown). "
     "Classify in sprint_runner._handle_task_failure based on loop_result.reason: "
     "timeout/network -> transient (no fc++, retry with backoff); refusal patterns "
     "-> refusal (mark needs-human, no retry); build/test exit code -> build|test "
     "(count toward cap); other -> unknown."),

    ("WF-5",
     "Logging: write full stderr/stdout to per-task log files",
     "subprocess_cli.py truncates stderr to 4K and stdout to 1K in error responses. "
     "Real debugging needs full output. Fix: in subprocess_cli when mode=agentic and "
     "exit != 0, write full stderr+stdout to "
     "logs/cli_failures/<task_id>_<timestamp>.log and include the path in the error "
     "response. Cap directory size to 100 MB with FIFO eviction."),

    ("WF-6",
     "Pipeline_config: warn (do not silently fall through) when provider is None",
     "Automatica-App pipeline_config.provider was None and the CLI --provider arg "
     "saved the day. A misconfigured automatic run with no CLI override would "
     "silently use a wrong/missing provider. Fix: validate pipeline_config on load. "
     "If provider is None or unset, log a WARNING with the project name and refuse "
     "to run unless an explicit override is in place. Add a one-line CLI command: "
     "python run_autonomous.py --SetPipelineProvider Claude-Opus --Project <dir>."),
]


def main():
    db_path = r"H:\Github\Automatica\.automatica\project.db"
    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}")
        return
    c = sqlite3.connect(db_path)
    cols = [r[1] for r in c.execute("PRAGMA table_info(tasks)")]
    have_complexity = "complexity" in cols
    have_priority = "priority" in cols
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    inserted = 0
    for did, title, desc in WORKFLOW_FIXES:
        if c.execute("SELECT 1 FROM tasks WHERE display_id=?", (did,)).fetchone():
            continue
        fields = ["display_id", "title", "description", "status", "created_at", "updated_at"]
        values = [did, title, desc, "backlog", now, now]
        if have_complexity:
            fields.append("complexity"); values.append(5)
        if have_priority:
            fields.append("priority"); values.append("HIGH")
        placeholders = ",".join(["?"] * len(values))
        c.execute(f"INSERT INTO tasks ({','.join(fields)}) VALUES ({placeholders})", values)
        inserted += 1

    c.commit()
    print(f"Inserted {inserted}/{len(WORKFLOW_FIXES)} workflow-fix tasks")
    for row in c.execute("SELECT display_id, status FROM tasks WHERE display_id LIKE 'WF-%' ORDER BY display_id"):
        print(f"  {row[0]:6s} {row[1]}")
    c.close()


if __name__ == "__main__":
    main()
