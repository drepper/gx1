"""Configuration file handling for the Gira X1 client.

The configuration is a TOML file.  It is never part of the repository since it
contains the credentials of the X1 device.  See ``gx1.toml.example``.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PORT_TLS = 443
DEFAULT_PORT_PLAIN = 80
DEFAULT_TIMEOUT = 10.0
DEFAULT_CLIENT_ID = 'org.akkadia.gx1'


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
                  source=source)
