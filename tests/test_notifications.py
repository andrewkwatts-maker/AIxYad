"""
Tests for email, webhook, and Slack notification senders (F-23).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure engine root is importable
_ENGINE = Path(__file__).resolve().parent.parent
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))


class TestWebhook:
    """Tests for send_webhook()."""

    def test_webhook_sends_post(self):
        cfg = {'webhook_url': 'http://example.com/hook'}
        with patch('notifications._load_email_config', return_value=cfg):
            with patch('requests.post') as mock_post:
                mock_post.return_value = MagicMock(status_code=200)
                from notifications import send_webhook
                send_webhook('sprint_complete', {'sprint_number': 1})
                mock_post.assert_called_once()
                call_args = mock_post.call_args
                assert 'http://example.com/hook' in str(call_args)

    def test_webhook_skipped_when_no_url(self):
        cfg = {}  # no webhook_url
        with patch('notifications._load_email_config', return_value=cfg):
            with patch('requests.post') as mock_post:
                from notifications import send_webhook
                send_webhook('sprint_complete', {'sprint_number': 1})
                mock_post.assert_not_called()

    def test_webhook_skipped_when_config_none(self):
        with patch('notifications._load_email_config', return_value=None):
            with patch('requests.post') as mock_post:
                from notifications import send_webhook
                send_webhook('sprint_complete', {'sprint_number': 1})
                mock_post.assert_not_called()

    def test_webhook_includes_event_and_timestamp(self):
        cfg = {'webhook_url': 'http://example.com/hook'}
        with patch('notifications._load_email_config', return_value=cfg):
            with patch('requests.post') as mock_post:
                mock_post.return_value = MagicMock(status_code=200)
                from notifications import send_webhook
                send_webhook('task_complete', {'task_id': 'T-001'})
                _, kwargs = mock_post.call_args
                payload = kwargs.get('json', {})
                assert payload.get('event') == 'task_complete'
                assert 'timestamp' in payload
                assert 'task_id' in payload


class TestSlack:
    """Tests for send_slack()."""

    def test_slack_sends_to_webhook(self):
        cfg = {'slack_webhook_url': 'http://hooks.slack.com/test'}
        with patch('notifications._load_email_config', return_value=cfg):
            with patch('requests.post') as mock_post:
                mock_post.return_value = MagicMock(status_code=200)
                from notifications import send_slack
                send_slack('Sprint 1 complete')
                mock_post.assert_called_once()
                _, kwargs = mock_post.call_args
                assert kwargs['json']['text'] == 'Sprint 1 complete'

    def test_slack_skipped_when_no_url(self):
        cfg = {}
        with patch('notifications._load_email_config', return_value=cfg):
            with patch('requests.post') as mock_post:
                from notifications import send_slack
                send_slack('Sprint 1 complete')
                mock_post.assert_not_called()

    def test_slack_sends_blocks_when_provided(self):
        cfg = {'slack_webhook_url': 'http://hooks.slack.com/test'}
        blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'hello'}}]
        with patch('notifications._load_email_config', return_value=cfg):
            with patch('requests.post') as mock_post:
                mock_post.return_value = MagicMock(status_code=200)
                from notifications import send_slack
                send_slack('Sprint 1 complete', blocks=blocks)
                _, kwargs = mock_post.call_args
                assert 'blocks' in kwargs['json']


class TestEmail:
    """Tests for send_sprint_complete() email sender."""

    def test_email_skipped_when_disabled(self):
        cfg = {'email_enabled': False}
        with patch('notifications._load_email_config', return_value=cfg):
            with patch('smtplib.SMTP') as mock_smtp:
                from notifications import send_sprint_complete
                send_sprint_complete({'sprint_number': 1, 'velocity': 5})
                mock_smtp.assert_not_called()

    def test_email_skipped_when_no_credentials(self):
        cfg = {'email_enabled': True, 'smtp_user': '', 'smtp_password': '', 'notify_email': ''}
        with patch('notifications._load_email_config', return_value=cfg):
            with patch('smtplib.SMTP') as mock_smtp:
                from notifications import send_sprint_complete
                send_sprint_complete({'sprint_number': 1, 'velocity': 5})
                mock_smtp.assert_not_called()
