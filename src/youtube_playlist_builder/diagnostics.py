"""Private, bounded local diagnostics for runtime failures.

The CLI configures one application logger beside its state database. The model
transport records safe request metadata and selected redacted provider errors;
raw HTTP headers, prompts and successful replies are intentionally not logged.
Do not enable upstream HTTP debug loggers through this module.
"""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re

logger = logging.getLogger('youtube_playlist_builder')
logger.addHandler(logging.NullHandler())
logger.propagate = False


class PrivateRotatingHandler(RotatingFileHandler):
    """Keep newly created log files private even after the logger rotates them."""
    def _open(self):
        """Create the replacement file with mode 0600, rather than relying on the umask."""
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)


def configure(path):
    """Install one bounded application-only log beside the state database.

    Menu commands re-enter the CLI, so repeated configuration must not duplicate
    handlers. Library loggers are deliberately not enabled: they may expose HTTP data.
    """
    path = Path(path).absolute()
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(path):
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    path.chmod(0o600)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = PrivateRotatingHandler(path, maxBytes=1_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def safe_text(value, secrets=()):
    """Redact known secrets and common credential formats, flatten controls and bound length."""
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)Bearer\s+[^\s"\',;]+', 'Bearer [REDACTED]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|access_token|refresh_token|client_secret|authorization)["\s]*[:=]["\s]*)[^\s"\',;&]+', r'\1[REDACTED]', text)
    text = re.sub(r'AIza[\w-]+', '[REDACTED]', text)
    return ''.join(c if c.isprintable() else ' ' for c in text)[:2000]


def provider_error(response, secrets=()):
    # Do not dump raw responses: they may include echoed requests or credentials.
    """Extract only diagnostic scalar fields from provider errors, never raw response bodies."""
    try:
        body = response.json()
    except (ValueError, UnicodeError):
        return 'Non-JSON error response (body omitted).'
    if isinstance(body, list):
        # Google's compatible endpoint may wrap errors in a one-element array.
        body = next((item for item in body if isinstance(item, dict) and 'error' in item), {})
    if not isinstance(body, dict):
        return 'Unrecognised error response (body omitted).'
    error = body.get('error', {})
    if not isinstance(error, dict):
        return 'Unrecognised error response (body omitted).'
    fields = {key: safe_text(error[key], secrets) for key in ('code', 'status', 'type', 'param', 'message')
              if key in error and isinstance(error[key], (str, int, float))}
    return json.dumps(fields, ensure_ascii=False)
