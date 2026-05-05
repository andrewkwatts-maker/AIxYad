"""Filter sensitive strings from log output.

C-17: No secrets in logs/stdout
C-18: Secrets scrubber before log writes

Install on the root logger or specific handlers:
    import logging
    from log_filter import SecretFilter
    logging.getLogger().addFilter(SecretFilter())
"""
import logging
import re

_SECRET_PATTERNS = [
    re.compile(r'(api[_-]?key\s*[=:]\s*)\S+', re.IGNORECASE),
    re.compile(r'(password\s*[=:]\s*)\S+', re.IGNORECASE),
    re.compile(r'(token\s*[=:]\s*)\S+', re.IGNORECASE),
    re.compile(r'Bearer\s+\S+', re.IGNORECASE),
    re.compile(r'Authorization:\s*\S+', re.IGNORECASE),
    re.compile(r'(smtp_password\s*[=:]\s*)\S+', re.IGNORECASE),
    re.compile(r'enc:[A-Za-z0-9+/=]{8,}', re.IGNORECASE),  # encrypted values
]


def _scrub(text: str) -> str:
    """Replace any secret-like patterns in text with [REDACTED]."""
    for pat in _SECRET_PATTERNS:
        # Patterns with a capture group restore the label; others fully replace
        if pat.groups:
            text = pat.sub(r'\1[REDACTED]', text)
        else:
            text = pat.sub('[REDACTED]', text)
    return text


class SecretFilter(logging.Filter):
    """Logging filter that redacts secrets from log records before they are written."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = str(record.getMessage())
            scrubbed = _scrub(msg)
            record.msg = scrubbed
            record.args = ()
        except Exception:
            pass  # Never suppress a log record due to filter failure
        return True
