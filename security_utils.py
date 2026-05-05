"""Security utilities: settings schema validation and input sanitization.

C-20: Settings JSON schema validation on load
"""
import warnings

# C-20: Known settings keys with their expected types
_SETTINGS_SCHEMA: dict = {
    'server_url': str,
    'url': str,
    'api_key': str,
    'mode': str,
    'log_retention_days': int,
    'smtp_host': str,
    'smtp_port': int,
    'smtp_user': str,
    'smtp_password': str,
    'smtp_from': str,
    'standards_dir': str,
    'ssl_certfile': str,
    'ssl_keyfile': str,
    'allowed_origins': list,
}


def validate_settings(data: dict) -> dict:
    """Return cleaned settings dict, with type coercions and unknown keys passed through.

    Args:
        data: Raw settings dict loaded from JSON.

    Returns:
        Cleaned settings dict with type-coerced known keys.
        Unknown keys are passed through with a warning in development.
    """
    if not isinstance(data, dict):
        warnings.warn(f'Settings is not a dict: {type(data)}')
        return {}

    cleaned: dict = {}
    for key, expected_type in _SETTINGS_SCHEMA.items():
        if key in data:
            raw = data[key]
            if isinstance(raw, expected_type):
                cleaned[key] = raw
            else:
                try:
                    if expected_type is list and isinstance(raw, str):
                        # Allow comma-separated string as list
                        cleaned[key] = [v.strip() for v in raw.split(',') if v.strip()]
                    else:
                        cleaned[key] = expected_type(raw)
                except (TypeError, ValueError):
                    pass  # drop invalid value silently

    # Pass through unknown keys (not rejected, for forward compatibility)
    for key in data:
        if key not in _SETTINGS_SCHEMA:
            cleaned[key] = data[key]

    return cleaned
