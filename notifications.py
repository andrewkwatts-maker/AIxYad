#!/usr/bin/env python3
"""
Send email, Slack, and webhook notifications on sprint events.

Configuration is read from ~/.aixyad/settings.json under the
'notifications' key:
  {
    "notifications": {
      "email_enabled": true,
      "smtp_host": "smtp.gmail.com",
      "smtp_port": 587,
      "smtp_user": "you@gmail.com",
      "smtp_password": "app-password",
      "notify_email": "you@gmail.com",
      "webhook_url": "https://example.com/hook",
      "slack_webhook_url": "https://hooks.slack.com/services/..."
    }
  }
"""
import json
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import aixyad_paths


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def _load_email_config() -> dict:
    """Load notification config from ~/.aixyad/settings.json."""
    cfg_path = aixyad_paths.home() / 'settings.json'
    if not cfg_path.exists():
        return None
    try:
        cfg = json.loads(cfg_path.read_text())
        return cfg.get('notifications', {})
    except Exception:
        return None


# ---------------------------------------------------------------------------
# F-14: Email notification
# ---------------------------------------------------------------------------

def send_sprint_complete(sprint_result: dict):
    """Send an email on sprint completion if email notifications are enabled."""
    cfg = _load_email_config()
    if not cfg or not cfg.get('email_enabled'):
        return

    smtp_host = cfg.get('smtp_host', 'smtp.gmail.com')
    smtp_port = cfg.get('smtp_port', 587)
    smtp_user = cfg.get('smtp_user', '')
    smtp_pass = cfg.get('smtp_password', '')  # decrypted by caller
    to_email  = cfg.get('notify_email', smtp_user)

    if not smtp_user or not smtp_pass or not to_email:
        return

    msg = MIMEMultipart()
    sprint_num = sprint_result.get('sprint_number', '?')
    velocity   = sprint_result.get('velocity', 0)
    msg['Subject'] = f"Sprint {sprint_num} complete — Velocity {velocity}"
    msg['From']    = smtp_user
    msg['To']      = to_email

    body = (
        f"Sprint Review\n\n"
        f"Completed: {sprint_result.get('tasks_completed', 0)}\n"
        f"Failed:    {sprint_result.get('tasks_failed', 0)}\n"
        f"Velocity:  {velocity}\n"
        f"Goal:      {sprint_result.get('goal', '')}\n"
    )
    msg.attach(MIMEText(body, 'plain'))

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls(context=ctx)
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f'[Notifications] Email sent to {to_email}')
    except Exception as e:
        print(f'[Notifications] Email failed: {e}')


# ---------------------------------------------------------------------------
# F-15: Webhook dispatch
# ---------------------------------------------------------------------------

def send_webhook(event: str, data: dict):
    """POST an event payload to the configured webhook URL."""
    cfg = _load_email_config()
    if not cfg:
        return
    webhook_url = cfg.get('webhook_url', '')
    if not webhook_url:
        return
    try:
        import requests
        payload = {'event': event, 'timestamp': datetime.now().isoformat(), **data}
        requests.post(webhook_url, json=payload, timeout=10)
        print(f'[Notifications] Webhook sent: {event}')
    except Exception as e:
        print(f'[Notifications] Webhook failed: {e}')


# ---------------------------------------------------------------------------
# F-16: Slack integration
# ---------------------------------------------------------------------------

def send_slack(message: str, blocks=None):
    """POST a message to the configured Slack incoming webhook."""
    cfg = _load_email_config()
    if not cfg:
        return
    slack_url = cfg.get('slack_webhook_url', '')
    if not slack_url:
        return
    try:
        import requests
        payload = {'text': message}
        if blocks:
            payload['blocks'] = blocks
        requests.post(slack_url, json=payload, timeout=10)
        print(f'[Notifications] Slack message sent')
    except Exception as e:
        print(f'[Notifications] Slack failed: {e}')
