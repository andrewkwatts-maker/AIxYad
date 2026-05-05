"""Security utilities for path validation and input sanitization.

C-07: Path traversal prevention
C-08: Agent slug sanitization
"""
from pathlib import Path
import re


def safe_path(base: Path, user_path: str) -> Path:
    """Resolve user_path relative to base, raising ValueError if it escapes base.

    Args:
        base: The allowed base directory.
        user_path: User-supplied relative path string.

    Returns:
        Resolved absolute Path guaranteed to be under base.

    Raises:
        ValueError: If the resolved path would escape the base directory.
    """
    base = base.resolve()
    # Reject absolute paths from user input directly
    candidate = Path(user_path)
    if candidate.is_absolute():
        raise ValueError(f'Path traversal detected: absolute path not allowed: {user_path!r}')
    resolved = (base / user_path).resolve()
    if not str(resolved).startswith(str(base)):
        raise ValueError(f'Path traversal detected: {user_path!r}')
    return resolved


def safe_project_path(path_str: str) -> Path:
    """Validate a project path (must be absolute, must exist as directory).

    Args:
        path_str: User-supplied path string.

    Returns:
        Resolved absolute Path to an existing directory.

    Raises:
        ValueError: If the path doesn't exist, isn't a directory, or is suspicious.
    """
    p = Path(path_str).resolve()
    if not p.exists():
        raise ValueError(f'Project path does not exist: {path_str}')
    if not p.is_dir():
        raise ValueError(f'Project path is not a directory: {path_str}')
    return p


def _sanitize_slug(slug: str) -> str:
    """C-08: Sanitize an agent slug to safe lowercase alphanumeric-and-dash form.

    Args:
        slug: Raw slug string from user input.

    Returns:
        Sanitized slug string, max 63 characters.

    Raises:
        ValueError: If the slug cannot be made valid after sanitization.
    """
    slug = slug.lower().strip()
    slug = re.sub(r'[^a-z0-9-]', '-', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    if not slug:
        raise ValueError('Invalid agent slug: result is empty after sanitization')
    return slug[:63]
