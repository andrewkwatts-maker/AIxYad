#!/usr/bin/env python3
#====== Automatica/scripts/generate-sprint-report.py ======
#!copyright (c) 2025 Andrew Keith Watts. All rights reserved.
#!
#!This is the intellectual property of Andrew Keith Watts. Unauthorized
#!reproduction, distribution, or modification of this code, in whole or in part,
#!without the express written permission of Andrew Keith Watts is strictly prohibited.
#!
#!For inquiries, please contact AndrewKWatts@Gmail.com

"""
Sprint Report Generator & Email Sender

Generates a sprint completion report (HTML email) with:
  - Page 1: Executive summary (1-page skimmable overview)
  - Pages 2-3: Detailed breakdown (tasks, agents, metrics, next sprint)

Usage:
  python generate-sprint-report.py [--send] [--output report.html] [--dry-run]
"""

import json
import os
import re
import smtplib
import sys
from collections import Counter
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = PROJECT_ROOT / "settings.json"
SPRINT_FILE = PROJECT_ROOT / "CURRENT_SPRINT.json"
COMPLETED_FILE = PROJECT_ROOT / "Completed.md"
TODO_FILE = PROJECT_ROOT / "TODO.md"
LOG_DIR = PROJECT_ROOT / "logs"


def load_settings():
    with open(SETTINGS_FILE) as f:
        return json.load(f)


def load_sprint():
    if not SPRINT_FILE.exists():
        print("ERROR: No CURRENT_SPRINT.json found. Start a sprint first.")
        print("  Run: ./scripts/start-sprint.sh")
        sys.exit(1)
    with open(SPRINT_FILE) as f:
        return json.load(f)


def parse_completed_tasks(sprint_start, sprint_end):
    """Parse Completed.md for tasks completed during this sprint window."""
    if not COMPLETED_FILE.exists():
        return []

    content = COMPLETED_FILE.read_text(encoding="utf-8")
    tasks = []
    current_task = None
    current_timestamp = None

    for line in content.splitlines():
        # Match completion headers: ## [2026-04-03 10:46:00] Completed
        ts_match = re.match(r"^## \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] Completed", line)
        if ts_match:
            current_timestamp = ts_match.group(1)
            current_task = {"timestamp": current_timestamp, "raw_lines": [], "title": "", "priority": "MEDIUM", "agent": "Unknown", "value": "", "criteria": []}
            continue

        if current_task is not None:
            if line.strip() == "---":
                # End of task block — check if it falls within sprint window
                try:
                    task_dt = datetime.strptime(current_task["timestamp"], "%Y-%m-%d %H:%M:%S")
                    start_dt = datetime.strptime(sprint_start, "%Y-%m-%d")
                    end_dt = datetime.strptime(sprint_end, "%Y-%m-%d %H:%M:%S") if " " in sprint_end else datetime.strptime(sprint_end, "%Y-%m-%d")
                    if start_dt <= task_dt <= end_dt:
                        tasks.append(current_task)
                except ValueError:
                    pass
                current_task = None
                continue

            current_task["raw_lines"].append(line)

            # Parse metadata
            priority_match = re.match(r"\[PRIORITY:\s*(\w+)\]", line.strip())
            if priority_match:
                current_task["priority"] = priority_match.group(1)

            agent_match = re.match(r"\[AGENT:\s*([^\]]+)\]", line.strip())
            if agent_match:
                current_task["agent"] = agent_match.group(1).strip()

            value_match = re.match(r"\[VALUE:\s*([^\]]+)\]", line.strip())
            if value_match:
                current_task["value"] = value_match.group(1).strip()

            criteria_match = re.match(r"^- \[[ xX]\]\s+(.*)", line.strip())
            if criteria_match:
                current_task["criteria"].append(criteria_match.group(1))

            # Title: first non-metadata, non-empty line
            if not current_task["title"] and line.strip() and not line.strip().startswith("[") and not line.strip().startswith("---") and not line.strip().startswith("- ["):
                current_task["title"] = line.strip()

    return tasks


def count_backlog_tasks():
    """Count remaining tasks in TODO.md."""
    if not TODO_FILE.exists():
        return {"total": 0, "high": 0, "medium": 0, "low": 0}

    content = TODO_FILE.read_text(encoding="utf-8")
    return {
        "total": content.count("---TASK_START---"),
        "high": content.count("[PRIORITY: HIGH]"),
        "medium": content.count("[PRIORITY: MEDIUM]"),
        "low": content.count("[PRIORITY: LOW]"),
    }


def get_next_sprint_tasks(limit=5):
    """Preview the next sprint's likely tasks from TODO.md."""
    if not TODO_FILE.exists():
        return []

    content = TODO_FILE.read_text(encoding="utf-8")
    tasks = []
    in_task = False
    current = {}

    for line in content.splitlines():
        if "---TASK_START---" in line:
            in_task = True
            current = {"title": "", "priority": "MEDIUM", "agent": "Unknown"}
            continue
        if "---TASK_END---" in line:
            in_task = False
            if current.get("title"):
                tasks.append(current)
            if len(tasks) >= limit:
                break
            continue
        if in_task:
            pm = re.match(r"\[PRIORITY:\s*(\w+)\]", line.strip())
            if pm:
                current["priority"] = pm.group(1)
            am = re.match(r"\[AGENT:\s*([^\]]+)\]", line.strip())
            if am:
                current["agent"] = am.group(1).strip()
            if not current["title"] and line.strip() and not line.strip().startswith("[") and not line.strip().startswith("---"):
                current["title"] = line.strip()

    return tasks


def build_html_report(sprint, tasks, backlog, next_tasks, settings):
    """Build the full HTML email report."""
    project_name = settings.get("project", {}).get("name", "Project")
    sprint_name = sprint.get("name", "Sprint")
    sprint_number = sprint.get("number", "?")
    sprint_start = sprint.get("start_date", "")
    sprint_end = sprint.get("end_date", datetime.now().strftime("%Y-%m-%d"))
    sprint_goal = sprint.get("goal", "")

    total_tasks = len(tasks)
    agents_used = Counter(t["agent"] for t in tasks)
    priorities = Counter(t["priority"] for t in tasks)

    # Calculate velocity
    try:
        start_dt = datetime.strptime(sprint_start, "%Y-%m-%d")
        end_dt = datetime.strptime(sprint_end, "%Y-%m-%d %H:%M:%S") if " " in sprint_end else datetime.strptime(sprint_end, "%Y-%m-%d")
        sprint_days = max((end_dt - start_dt).days, 1)
    except ValueError:
        sprint_days = 14

    velocity = round(total_tasks / sprint_days, 1)

    # Agent summary rows
    agent_rows = ""
    for agent, count in agents_used.most_common():
        pct = round(count / total_tasks * 100) if total_tasks else 0
        agent_rows += f"""
        <tr>
          <td style="padding: 6px 12px; border-bottom: 1px solid #eee;">{agent}</td>
          <td style="padding: 6px 12px; border-bottom: 1px solid #eee; text-align: center;">{count}</td>
          <td style="padding: 6px 12px; border-bottom: 1px solid #eee; text-align: center;">{pct}%</td>
        </tr>"""

    # Task detail rows
    task_detail_rows = ""
    for i, t in enumerate(tasks, 1):
        pri_color = {"HIGH": "#e74c3c", "MEDIUM": "#f39c12", "LOW": "#27ae60"}.get(t["priority"], "#95a5a6")
        task_detail_rows += f"""
        <tr>
          <td style="padding: 6px 12px; border-bottom: 1px solid #eee;">{i}</td>
          <td style="padding: 6px 12px; border-bottom: 1px solid #eee;">{t['title']}</td>
          <td style="padding: 6px 12px; border-bottom: 1px solid #eee;">
            <span style="background: {pri_color}; color: white; padding: 2px 8px; border-radius: 3px; font-size: 11px;">{t['priority']}</span>
          </td>
          <td style="padding: 6px 12px; border-bottom: 1px solid #eee;">{t['agent']}</td>
          <td style="padding: 6px 12px; border-bottom: 1px solid #eee; font-size: 12px; color: #888;">{t['timestamp']}</td>
        </tr>"""

    # Next sprint preview rows
    next_sprint_rows = ""
    for t in next_tasks:
        pri_color = {"HIGH": "#e74c3c", "MEDIUM": "#f39c12", "LOW": "#27ae60"}.get(t["priority"], "#95a5a6")
        next_sprint_rows += f"""
        <tr>
          <td style="padding: 6px 12px; border-bottom: 1px solid #eee;">{t['title']}</td>
          <td style="padding: 6px 12px; border-bottom: 1px solid #eee;">
            <span style="background: {pri_color}; color: white; padding: 2px 8px; border-radius: 3px; font-size: 11px;">{t['priority']}</span>
          </td>
          <td style="padding: 6px 12px; border-bottom: 1px solid #eee;">{t['agent']}</td>
        </tr>"""

    if not next_sprint_rows:
        next_sprint_rows = '<tr><td colspan="3" style="padding: 12px; color: #888; text-align: center;">No tasks in backlog</td></tr>'

    # Priority breakdown bar
    h_pct = round(priorities.get("HIGH", 0) / total_tasks * 100) if total_tasks else 0
    m_pct = round(priorities.get("MEDIUM", 0) / total_tasks * 100) if total_tasks else 0
    l_pct = 100 - h_pct - m_pct if total_tasks else 0

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #333;">

<!-- ============================================================ -->
<!-- PAGE 1: EXECUTIVE SUMMARY (1-page skimmable report)          -->
<!-- ============================================================ -->
<table width="100%" cellpadding="0" cellspacing="0" style="max-width: 700px; margin: 20px auto; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">

  <!-- Header -->
  <tr>
    <td style="background: linear-gradient(135deg, #2c3e50, #3498db); padding: 30px 40px; color: white;">
      <h1 style="margin: 0 0 4px 0; font-size: 22px; font-weight: 600;">{project_name} — Sprint {sprint_number} Complete</h1>
      <p style="margin: 0; opacity: 0.85; font-size: 14px;">{sprint_name} &nbsp;|&nbsp; {sprint_start} → {sprint_end}</p>
    </td>
  </tr>

  <!-- Sprint Goal -->
  {"" if not sprint_goal else f'''
  <tr>
    <td style="padding: 20px 40px 0 40px;">
      <div style="background: #f0f7ff; border-left: 4px solid #3498db; padding: 12px 16px; border-radius: 0 4px 4px 0;">
        <strong style="font-size: 12px; text-transform: uppercase; color: #3498db; letter-spacing: 0.5px;">Sprint Goal</strong>
        <p style="margin: 4px 0 0 0; font-size: 14px;">{sprint_goal}</p>
      </div>
    </td>
  </tr>'''}

  <!-- Key Metrics -->
  <tr>
    <td style="padding: 24px 40px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="25%" style="text-align: center; padding: 16px 8px; background: #f8f9fa; border-radius: 6px;">
            <div style="font-size: 32px; font-weight: 700; color: #2c3e50;">{total_tasks}</div>
            <div style="font-size: 12px; color: #888; text-transform: uppercase; letter-spacing: 0.5px;">Tasks Done</div>
          </td>
          <td width="5%"></td>
          <td width="25%" style="text-align: center; padding: 16px 8px; background: #f8f9fa; border-radius: 6px;">
            <div style="font-size: 32px; font-weight: 700; color: #2c3e50;">{len(agents_used)}</div>
            <div style="font-size: 12px; color: #888; text-transform: uppercase; letter-spacing: 0.5px;">Agents Used</div>
          </td>
          <td width="5%"></td>
          <td width="25%" style="text-align: center; padding: 16px 8px; background: #f8f9fa; border-radius: 6px;">
            <div style="font-size: 32px; font-weight: 700; color: #2c3e50;">{sprint_days}</div>
            <div style="font-size: 12px; color: #888; text-transform: uppercase; letter-spacing: 0.5px;">Days</div>
          </td>
          <td width="5%"></td>
          <td width="25%" style="text-align: center; padding: 16px 8px; background: #f8f9fa; border-radius: 6px;">
            <div style="font-size: 32px; font-weight: 700; color: #2c3e50;">{velocity}</div>
            <div style="font-size: 12px; color: #888; text-transform: uppercase; letter-spacing: 0.5px;">Tasks/Day</div>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Priority Breakdown Bar -->
  <tr>
    <td style="padding: 0 40px 20px 40px;">
      <div style="font-size: 12px; text-transform: uppercase; color: #888; letter-spacing: 0.5px; margin-bottom: 8px;">Priority Breakdown</div>
      <div style="display: flex; height: 24px; border-radius: 4px; overflow: hidden; background: #eee;">
        {"" if not h_pct else f'<div style="width: {h_pct}%; background: #e74c3c; display: inline-block; height: 24px; float: left;"></div>'}
        {"" if not m_pct else f'<div style="width: {m_pct}%; background: #f39c12; display: inline-block; height: 24px; float: left;"></div>'}
        {"" if not l_pct else f'<div style="width: {l_pct}%; background: #27ae60; display: inline-block; height: 24px; float: left;"></div>'}
      </div>
      <div style="font-size: 11px; color: #888; margin-top: 4px;">
        <span style="color: #e74c3c;">&#9632;</span> High: {priorities.get('HIGH', 0)}
        &nbsp;&nbsp;
        <span style="color: #f39c12;">&#9632;</span> Medium: {priorities.get('MEDIUM', 0)}
        &nbsp;&nbsp;
        <span style="color: #27ae60;">&#9632;</span> Low: {priorities.get('LOW', 0)}
      </div>
    </td>
  </tr>

  <!-- Agent Summary (compact) -->
  <tr>
    <td style="padding: 0 40px 20px 40px;">
      <div style="font-size: 12px; text-transform: uppercase; color: #888; letter-spacing: 0.5px; margin-bottom: 8px;">Agent Workload</div>
      <table width="100%" cellpadding="0" cellspacing="0" style="border: 1px solid #eee; border-radius: 4px;">
        <tr style="background: #f8f9fa;">
          <th style="padding: 8px 12px; text-align: left; font-size: 12px; font-weight: 600;">Agent</th>
          <th style="padding: 8px 12px; text-align: center; font-size: 12px; font-weight: 600;">Tasks</th>
          <th style="padding: 8px 12px; text-align: center; font-size: 12px; font-weight: 600;">Share</th>
        </tr>
        {agent_rows}
      </table>
    </td>
  </tr>

  <!-- Quick Glance: Backlog & Next Sprint -->
  <tr>
    <td style="padding: 0 40px 20px 40px;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="48%" style="vertical-align: top;">
            <div style="background: #fff8e1; border-radius: 6px; padding: 16px;">
              <div style="font-size: 12px; text-transform: uppercase; color: #f39c12; font-weight: 600; letter-spacing: 0.5px;">Remaining Backlog</div>
              <div style="font-size: 28px; font-weight: 700; margin: 4px 0;">{backlog['total']}</div>
              <div style="font-size: 11px; color: #888;">H:{backlog['high']} M:{backlog['medium']} L:{backlog['low']}</div>
            </div>
          </td>
          <td width="4%"></td>
          <td width="48%" style="vertical-align: top;">
            <div style="background: #e8f5e9; border-radius: 6px; padding: 16px;">
              <div style="font-size: 12px; text-transform: uppercase; color: #27ae60; font-weight: 600; letter-spacing: 0.5px;">Next Sprint</div>
              <div style="font-size: 28px; font-weight: 700; margin: 4px 0;">Sprint {sprint_number + 1 if isinstance(sprint_number, int) else "Next"}</div>
              <div style="font-size: 11px; color: #888;">{len(next_tasks)} tasks queued</div>
            </div>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- Page 1 Footer -->
  <tr>
    <td style="padding: 16px 40px; border-top: 1px solid #eee; font-size: 11px; color: #aaa; text-align: center;">
      Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} by Automatica Sprint Reporter &nbsp;|&nbsp; Detailed breakdown below &#8595;
    </td>
  </tr>
</table>

<!-- ============================================================ -->
<!-- PAGE 2-3: DETAILED BREAKDOWN                                 -->
<!-- ============================================================ -->
<table width="100%" cellpadding="0" cellspacing="0" style="max-width: 700px; margin: 20px auto; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">

  <!-- Section: Completed Tasks Detail -->
  <tr>
    <td style="padding: 30px 40px 10px 40px;">
      <h2 style="margin: 0; font-size: 18px; color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 8px;">Completed Tasks — Full Detail</h2>
    </td>
  </tr>
  <tr>
    <td style="padding: 10px 40px 30px 40px;">
      <table width="100%" cellpadding="0" cellspacing="0" style="border: 1px solid #eee; border-radius: 4px; font-size: 13px;">
        <tr style="background: #f8f9fa;">
          <th style="padding: 8px 12px; text-align: left; font-weight: 600;">#</th>
          <th style="padding: 8px 12px; text-align: left; font-weight: 600;">Task</th>
          <th style="padding: 8px 12px; text-align: left; font-weight: 600;">Priority</th>
          <th style="padding: 8px 12px; text-align: left; font-weight: 600;">Agent</th>
          <th style="padding: 8px 12px; text-align: left; font-weight: 600;">Completed</th>
        </tr>
        {task_detail_rows if task_detail_rows else '<tr><td colspan="5" style="padding: 12px; color: #888; text-align: center;">No tasks recorded for this sprint</td></tr>'}
      </table>
    </td>
  </tr>

  <!-- Section: Agent Performance -->
  <tr>
    <td style="padding: 10px 40px 10px 40px;">
      <h2 style="margin: 0; font-size: 18px; color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 8px;">Agent Performance</h2>
    </td>
  </tr>
  <tr>
    <td style="padding: 10px 40px 30px 40px;">
      <table width="100%" cellpadding="0" cellspacing="0" style="font-size: 13px;">
        {"".join(f'''
        <tr>
          <td style="padding: 8px 0;">
            <strong>{agent}</strong>
            <div style="background: #eee; height: 18px; border-radius: 3px; margin-top: 4px; overflow: hidden;">
              <div style="background: #3498db; height: 18px; width: {round(count / total_tasks * 100) if total_tasks else 0}%; border-radius: 3px; text-align: center; color: white; font-size: 11px; line-height: 18px;">{count}</div>
            </div>
          </td>
        </tr>''' for agent, count in agents_used.most_common())}
      </table>
    </td>
  </tr>

  <!-- Section: Next Sprint Preview -->
  <tr>
    <td style="padding: 10px 40px 10px 40px;">
      <h2 style="margin: 0; font-size: 18px; color: #2c3e50; border-bottom: 2px solid #27ae60; padding-bottom: 8px;">Next Sprint — Upcoming Tasks</h2>
    </td>
  </tr>
  <tr>
    <td style="padding: 10px 40px 30px 40px;">
      <table width="100%" cellpadding="0" cellspacing="0" style="border: 1px solid #eee; border-radius: 4px; font-size: 13px;">
        <tr style="background: #f8f9fa;">
          <th style="padding: 8px 12px; text-align: left; font-weight: 600;">Task</th>
          <th style="padding: 8px 12px; text-align: left; font-weight: 600;">Priority</th>
          <th style="padding: 8px 12px; text-align: left; font-weight: 600;">Agent</th>
        </tr>
        {next_sprint_rows}
      </table>
      <p style="font-size: 12px; color: #888; margin-top: 8px;">Showing top {len(next_tasks)} of {backlog['total']} backlog tasks</p>
    </td>
  </tr>

  <!-- Section: Sprint Metadata -->
  <tr>
    <td style="padding: 10px 40px 10px 40px;">
      <h2 style="margin: 0; font-size: 18px; color: #2c3e50; border-bottom: 2px solid #95a5a6; padding-bottom: 8px;">Sprint Metadata</h2>
    </td>
  </tr>
  <tr>
    <td style="padding: 10px 40px 30px 40px; font-size: 13px;">
      <table cellpadding="0" cellspacing="0">
        <tr><td style="padding: 4px 20px 4px 0; color: #888;">Sprint</td><td>{sprint_name} (#{sprint_number})</td></tr>
        <tr><td style="padding: 4px 20px 4px 0; color: #888;">Start Date</td><td>{sprint_start}</td></tr>
        <tr><td style="padding: 4px 20px 4px 0; color: #888;">End Date</td><td>{sprint_end}</td></tr>
        <tr><td style="padding: 4px 20px 4px 0; color: #888;">Duration</td><td>{sprint_days} days</td></tr>
        <tr><td style="padding: 4px 20px 4px 0; color: #888;">Tasks Completed</td><td>{total_tasks}</td></tr>
        <tr><td style="padding: 4px 20px 4px 0; color: #888;">Velocity</td><td>{velocity} tasks/day</td></tr>
        <tr><td style="padding: 4px 20px 4px 0; color: #888;">Unique Agents</td><td>{len(agents_used)}</td></tr>
        <tr><td style="padding: 4px 20px 4px 0; color: #888;">Backlog Remaining</td><td>{backlog['total']}</td></tr>
      </table>
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="padding: 20px 40px; border-top: 1px solid #eee; background: #f8f9fa; text-align: center;">
      <p style="margin: 0; font-size: 12px; color: #aaa;">
        {project_name} — Automatica Sprint Report<br>
        Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
      </p>
    </td>
  </tr>
</table>

</body>
</html>"""

    return html


def send_email(html, sprint, settings):
    """Send the report via SMTP."""
    email_cfg = settings.get("email", {})

    smtp_host = email_cfg.get("smtp_host", "")
    smtp_port = email_cfg.get("smtp_port", 587)
    smtp_user = email_cfg.get("smtp_user", "")
    smtp_password = email_cfg.get("smtp_password", "")
    use_tls = email_cfg.get("use_tls", True)
    from_addr = email_cfg.get("from_address", smtp_user)
    recipients = email_cfg.get("recipients", [])

    if not smtp_host or not recipients:
        print("ERROR: Email not configured. Set smtp_host and recipients in settings.json")
        print('  "email": { "smtp_host": "smtp.gmail.com", "recipients": ["you@example.com"] }')
        return False

    project_name = settings.get("project", {}).get("name", "Project")
    sprint_name = sprint.get("name", "Sprint")
    sprint_number = sprint.get("number", "?")
    subject = f"[{project_name}] Sprint {sprint_number} Complete — {sprint_name}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)

    # Plain-text fallback
    plain = f"{project_name} Sprint {sprint_number} ({sprint_name}) has been completed.\nView the HTML version of this email for the full report."
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        if use_tls:
            server = smtplib.SMTP(smtp_host, smtp_port)
            server.starttls()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port)

        if smtp_user and smtp_password:
            server.login(smtp_user, smtp_password)

        server.sendmail(from_addr, recipients, msg.as_string())
        server.quit()
        print(f"Email sent to: {', '.join(recipients)}")
        return True
    except Exception as e:
        print(f"ERROR sending email: {e}")
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Generate and send sprint completion report")
    parser.add_argument("--send", action="store_true", help="Send email (requires SMTP config)")
    parser.add_argument("--output", type=str, default="", help="Save HTML report to file")
    parser.add_argument("--dry-run", action="store_true", help="Generate report without sending")
    args = parser.parse_args()

    settings = load_settings()
    sprint = load_sprint()

    sprint_start = sprint.get("start_date", "")
    sprint_end = sprint.get("end_date", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    print(f"Generating sprint report: {sprint.get('name', 'Sprint')} #{sprint.get('number', '?')}")
    print(f"  Period: {sprint_start} -> {sprint_end}")

    tasks = parse_completed_tasks(sprint_start, sprint_end)
    backlog = count_backlog_tasks()
    next_tasks = get_next_sprint_tasks(limit=5)

    print(f"  Tasks completed: {len(tasks)}")
    print(f"  Backlog remaining: {backlog['total']}")

    html = build_html_report(sprint, tasks, backlog, next_tasks, settings)

    # Save to file
    output_path = args.output
    if not output_path:
        LOG_DIR.mkdir(exist_ok=True)
        output_path = str(LOG_DIR / f"sprint_{sprint.get('number', 0)}_report.html")

    Path(output_path).write_text(html, encoding="utf-8")
    print(f"  Report saved: {output_path}")

    # Send email
    if args.send and not args.dry_run:
        send_email(html, sprint, settings)
    elif args.dry_run:
        print("  Dry run — email not sent")
    else:
        print("  Use --send to email the report")

    return 0


if __name__ == "__main__":
    sys.exit(main())
