"""Configuration file handling for the Gira X1 client.

The configuration is a TOML file.  It is never part of the repository since it
contains the credentials of the X1 device.  See ``girax1.toml.example``.
"""

import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

DEFAULT_PORT_TLS = 443
DEFAULT_PORT_PLAIN = 80
DEFAULT_TIMEOUT = 10.0
DEFAULT_CLIENT_ID = 'org.akkadia.girax1'

# How long a blind is assumed to need for the journey from open to closed when
# the configuration says nothing about it.
DEFAULT_TRAVEL_SECONDS = 30.0

# Where the travel times live in the configuration file.
BLIND_SECTION = 'blinds'
TRAVEL_TABLE = 'blinds.seconds'

# The settings that can be edited, named as section.key, with the type of
# their values.  The travel times of the blinds are kept up separately.
SETTINGS: dict[str, type] = {
    'device.host': str, 'device.port': int, 'device.timeout': float,
    'auth.username': str, 'auth.password': str, 'auth.token': str, 'auth.client_id': str,
    'tls.enabled': bool, 'tls.verify': bool, 'tls.ca_cert': str,
}


class ConfigError(Exception):
    """Raised when a configuration file is missing, malformed or incomplete."""


@dataclass(frozen=True, slots=True)
class Config:
    """All information needed to reach and authenticate against an X1 device."""

    host: str
    port: int = DEFAULT_PORT_TLS
    username: str = ''
    password: str = ''
    client_id: str = DEFAULT_CLIENT_ID
    token: str | None = None
    use_tls: bool = True
    verify_tls: bool = True
    ca_cert: Path | None = None
    timeout: float = DEFAULT_TIMEOUT
    source: Path | None = None
    travel_default: float = DEFAULT_TRAVEL_SECONDS
    travel: dict[str, float] = field(default_factory=dict)

    def travel_seconds(self, path: str) -> float:
        """Return how long the named blind needs from fully open to fully closed."""
        return self.travel.get(path, self.travel_default)

    @property
    def scheme(self) -> str:
        """Return the URL scheme used to talk to the device."""
        return 'https' if self.use_tls else 'http'

    @property
    def base_url(self) -> str:
        """Return the URL prefix of the REST API endpoints of the device."""
        return f'{self.scheme}://{self.host}:{self.port}/api'


def _section(data: dict[str, Any], name: str, source: Path) -> dict[str, Any]:
    """Return the named top-level table of a parsed config file, empty if absent."""
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f'{source}: [{name}] must be a table')
    return value


def _typed[T](table: dict[str, Any], section: str, key: str, kind: type[T],
              default: T | None, source: Path) -> T | None:
    """Return the value of ``section.key`` checked against the expected type."""
    if key not in table:
        return default
    value = table[key]
    # bool is a subclass of int, so an explicit test is needed for numbers.
    if kind is float and isinstance(value, int) and not isinstance(value, bool):
        return kind(value)
    if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
        raise ConfigError(f'{source}: {section}.{key} must be of type {kind.__name__}')
    return value


def load(filename: str | Path) -> Config:
    """Parse the TOML configuration file and return the validated settings."""
    source = Path(filename).expanduser()
    try:
        with source.open('rb') as fp:
            data = tomllib.load(fp)
    except OSError as e:
        raise ConfigError(f'cannot read {source}: {e.strerror}') from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f'{source}: {e}') from e
    return _parse(data, source)


def _parse(data: dict[str, Any], source: Path) -> Config:
    """Validate the parsed contents of a configuration file."""
    device = _section(data, 'device', source)
    auth = _section(data, 'auth', source)
    tls = _section(data, 'tls', source)

    host = _typed(device, 'device', 'host', str, None, source)
    if host is None:
        raise ConfigError(f'{source}: device.host is required')

    token = _typed(auth, 'auth', 'token', str, None, source)
    username = _typed(auth, 'auth', 'username', str, '', source) or ''
    password = _typed(auth, 'auth', 'password', str, '', source) or ''
    if token is None and not (username and password):
        raise ConfigError(f'{source}: either auth.token or auth.username and '
                          'auth.password are required')

    ca_cert = _typed(tls, 'tls', 'ca_cert', str, None, source)

    blinds = _section(data, BLIND_SECTION, source)
    travel_default = _typed(blinds, BLIND_SECTION, 'default_seconds', float,
                            DEFAULT_TRAVEL_SECONDS, source) or DEFAULT_TRAVEL_SECONDS
    travel: dict[str, float] = {}
    for name, seconds in (blinds.get('seconds') or {}).items():
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) \
                or seconds <= 0:
            raise ConfigError(f'{source}: {TRAVEL_TABLE}."{name}" has to be a positive '
                              'number of seconds')
        travel[name] = float(seconds)

    # Without an explicit port the default follows the choice of protocol.
    use_tls = _typed(tls, 'tls', 'enabled', bool, True, source) is not False
    port = _typed(device, 'device', 'port', int, None, source)
    if port is None:
        port = DEFAULT_PORT_TLS if use_tls else DEFAULT_PORT_PLAIN

    return Config(host=host,
                  port=port,
                  username=username,
                  password=password,
                  client_id=_typed(auth, 'auth', 'client_id', str, DEFAULT_CLIENT_ID, source)
                            or DEFAULT_CLIENT_ID,
                  token=token,
                  use_tls=use_tls,
                  verify_tls=_typed(tls, 'tls', 'verify', bool, True, source) is not False,
                  ca_cert=Path(ca_cert).expanduser() if ca_cert is not None else None,
                  timeout=_typed(device, 'device', 'timeout', float, DEFAULT_TIMEOUT, source)
                          or DEFAULT_TIMEOUT,
                  source=source,
                  travel_default=travel_default,
                  travel=travel)


def read_settings(source: Path) -> dict[str, Any]:
    """Return the editable settings as the file has them, keyed as in :data:`SETTINGS`.

    Settings the file leaves out are missing from the result, so that the
    defaults stay apparent.
    """
    try:
        with source.open('rb') as fp:
            data = tomllib.load(fp)
    except OSError as e:
        raise ConfigError(f'cannot read {source}: {e.strerror}') from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f'{source}: {e}') from e
    result = {}
    for name in SETTINGS:
        section, key = name.split('.')
        if isinstance(table := data.get(section), dict) and key in table:
            result[name] = table[key]
    return result


def write_settings(source: Path, changes: dict[str, Any]) -> Config:
    """Change settings in the file and return the configuration as it is then.

    ``changes`` is keyed as :data:`SETTINGS`; a value of None removes the
    setting so that its default applies.  The file is edited as text rather
    than rewritten from the parsed data, so that comments and layout survive.
    A setting the file only has commented out is filled in where that comment
    is.  The result is checked before it replaces the file, which is left
    alone when the settings would not be valid, and keeps its permissions.
    """
    text = source.read_text(encoding='utf-8')
    for name, value in changes.items():
        if name not in SETTINGS:
            raise ConfigError(f'{name} is not a setting that can be changed')
        section, key = name.split('.')
        text = _set_line(text, section, key, None if value is None else _toml(value))
    try:
        config = _parse(tomllib.loads(text), source)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f'{source}: {e}') from e

    temporary = source.with_name(f'.{source.name}.new')
    try:
        temporary.write_text(text, encoding='utf-8')
        os.chmod(temporary, stat.S_IMODE(source.stat().st_mode))
        temporary.replace(source)
    except OSError as e:
        temporary.unlink(missing_ok=True)
        raise ConfigError(f'cannot write {source}: {e.strerror}') from e
    return config


def _set_line(text: str, section: str, key: str, value: str | None) -> str:
    """Set or remove one key of one table in the text of a TOML file."""
    lines = text.splitlines(keepends=True)
    header = re.compile(rf'\s*\[\s*{re.escape(section)}\s*\]\s*(#.*)?$')
    start = next((i for i, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        if value is None:
            return text
        if text and not text.endswith('\n'):
            text += '\n'
        return f'{text}\n[{section}]\n{key} = {value}\n'

    end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith('[')),
               len(lines))
    active = re.compile(rf'\s*{re.escape(key)}\s*=')
    commented = re.compile(rf'\s*#\s*{re.escape(key)}\s*=')
    entry = f'{key} = {value}\n'
    if (at := next((i for i in range(start + 1, end) if active.match(lines[i])), None)) is not None:
        # Removing deletes the line rather than commenting it out, so that no
        # password is left behind in a comment.
        lines[at:at + 1] = [] if value is None else [entry]
    elif value is None:
        return text
    elif (at := next((i for i in reversed(range(start + 1, end))
                      if commented.match(lines[i])), None)) is not None:
        lines[at] = entry
    else:
        last = max((i for i in range(start, end) if lines[i].strip()), default=start)
        if not lines[last].endswith('\n'):
            lines[last] += '\n'
        lines.insert(last + 1, entry)
    return ''.join(lines)


def _toml(value: Any) -> str:
    """Render a value the way TOML writes it."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    # A JSON string is also a valid basic string in TOML.
    return json.dumps(str(value), ensure_ascii=False)


def ensure_travel_entries(source: Path, paths: Iterable[str],
                          default: float = DEFAULT_TRAVEL_SECONDS) -> list[str]:
    """Add a travel time for every named blind the file does not mention yet.

    The file is edited as text rather than rewritten from the parsed data, so
    that comments and layout survive.  Returns the names that were added.
    """
    text = source.read_text(encoding='utf-8')
    known = set((tomllib.loads(text).get(BLIND_SECTION, {}).get('seconds') or {}))
    missing = [p for p in paths if p not in known]
    if not missing:
        return []

    added = ''.join(f'"{p}" = {default:g}\n' for p in missing)
    marker = f'[{TRAVEL_TABLE}]'
    if (at := text.find(marker)) >= 0:
        # Keep the new entries inside the table that is already there.
        line_end = text.find('\n', at) + 1
        text = text[:line_end] + added + text[line_end:]
    else:
        if not text.endswith('\n'):
            text += '\n'
        text += ('\n# How long each blind needs for the journey from fully open to fully\n'
                 '# closed, in seconds.  Measure a blind and correct its entry to make\n'
                 '# closing by a percentage accurate.\n'
                 f'{marker}\n{added}')
    source.write_text(text, encoding='utf-8')
    return missing
