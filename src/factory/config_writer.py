"""Writes a single ``KEY=value`` line into a real ``.env`` file on disk.

Used only by the dashboard's credential-write endpoint
(``api/dashboard.py::write_credential``). A write here changes nothing for
the *running* process -- ``Settings`` is an ``lru_cache``'d singleton read
once at boot (``settings.py::get_settings``) -- it only changes what the next
``docker compose up -d factory`` picks up via docker-compose's own
``env_file: .env`` directive. The caller is responsible for saying so; this
module only writes the file.
"""

from __future__ import annotations

from pathlib import Path

#: A submitted value must never be able to inject a second line -- a newline
#: would let a "credential" value redefine an unrelated key or truncate the
#: rest of the file's meaning, entirely outside what the write endpoint's
#: (key, value) contract is supposed to allow.
_FORBIDDEN_CHARS = ("\n", "\r")


class InvalidEnvValue(ValueError):
    pass


def write_env_value(path: Path, key: str, value: str) -> None:
    """Replace ``KEY=...``'s line if present (first match, top of file wins),
    else append a new line at the end. Every other line -- comments, blank
    lines, unrelated keys -- is preserved verbatim and in order.
    """
    if any(char in value for char in _FORBIDDEN_CHARS):
        raise InvalidEnvValue("value must not contain a newline")

    prefix = f"{key}="
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{prefix}{value}"
            break
    else:
        lines.append(f"{prefix}{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_env_value(path: Path, key: str) -> str | None:
    """The read counterpart to `write_env_value` -- same line-scan, first
    match wins. Needed for a credential that isn't a typed `Settings` field
    (a dashboard-added custom one, see `observability/integrations.py`),
    where `getattr(settings, key)` isn't an option for previewing the
    current value. Returns `None` if the file or the key doesn't exist.
    """
    if not path.exists():
        return None
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None
