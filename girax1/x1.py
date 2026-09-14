"""Client for the Gira X1 IoT REST API."""

import http.client
import json
import ssl
from base64 import b64encode
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote, urlencode

from .config import Config, load

API_PREFIX = '/api/v2'
MAX_ERROR_DETAIL = 200


class X1Error(Exception):
    """Base class of all errors reported by the X1 client."""


class ConnectionError(X1Error):
    """The device could not be reached or answered with an unusable reply."""


class AuthError(X1Error):
    """The device rejected the credentials or the token."""


class RequestError(X1Error):
    """The device answered a request with an error status."""

    def __init__(self, method: str, path: str, status: int, detail: str) -> None:
        super().__init__(f'{method} {path}: {status} {detail}')
        self.status = status
        self.detail = detail


class X1:
    """Connection to a single Gira X1 device.

    The instance is created from the name of a configuration file.  The network
    connection is only established by :meth:`connect`, which also registers this
    program as a client at the device and obtains the access token used for all
    further requests.
    """

    def __init__(self, filename: str | Path) -> None:
        """Read the configuration file without contacting the device yet."""
        self.config: Config = load(filename)
        self.info: dict[str, Any] = {}
        self._conn: http.client.HTTPConnection | None = None
        self._token: str | None = self.config.token
        self._registered = False

    @property
    def token(self) -> str | None:
        """Return the access token, or ``None`` while not connected."""
        return self._token

    @property
    def connected(self) -> bool:
        """Return whether a token for the device is available."""
        return self._conn is not None and self._token is not None

    def __enter__(self) -> Self:
        """Connect to the device on entry of a ``with`` statement."""
        self.connect()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        """Release the connection and any token acquired by this instance."""
        self.close()

    def connect(self) -> dict[str, Any]:
        """Open the connection, register as a client and return the device information.

        When the configuration file provides a token it is used as is, otherwise
        username and password are used to register ``auth.client_id`` at the
        device and to obtain a fresh token.

        The availability check is only advisory: not every firmware version serves
        the documented base URL, so a 404 there does not prevent registration.
        """
        self._conn = self.open_connection()
        try:
            reply = self._request('GET', '/')
            self.info = reply if isinstance(reply, dict) else {}
        except X1Error:
            # Advisory only, so any failure is tolerated here: registration below
            # is the real test.  A failed request leaves the connection closed.
            self.info = {}
            self._conn = self.open_connection()

        if self._token is None:
            try:
                self._register()
            except X1Error:
                self.close()
                raise
        return self.info

    def close(self) -> None:
        """Unregister a token acquired by this instance and close the connection."""
        if self._registered and self._token is not None:
            try:
                self._request('DELETE', f'/clients/{quote(self._token)}')
            except X1Error:
                pass                    # Nothing useful left to do while shutting down.
            self._registered = False
            self._token = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---------------------------------------------------------------- API calls

    def device_info(self) -> dict[str, Any]:
        """Return name, type and API version as reported by the device."""
        return self._checked_dict(self._request('GET', '/'))

    def uiconfig(self) -> dict[str, Any]:
        """Return the complete UI configuration describing all functions and data points."""
        return self._checked_dict(self._request('GET', '/uiconfig', authenticated=True))

    def uiconfig_uid(self) -> str:
        """Return the identifier of the current UI configuration, which changes on every edit."""
        reply = self._checked_dict(self._request('GET', '/uiconfig/uid', authenticated=True))
        return str(reply.get('uid', ''))

    def get_value(self, uid: str) -> Any:
        """Return the current value of the data point with the given UID."""
        reply = self._checked_dict(self._request('GET', f'/values/{quote(uid)}',
                                                 authenticated=True))
        values = reply.get('values')
        if not isinstance(values, list) or not values:
            raise RequestError('GET', f'/values/{uid}', 0, 'no value in reply')
        return values[0].get('value')

    def set_value(self, uid: str, value: Any) -> None:
        """Set the data point with the given UID to a new value."""
        self._request('PUT', f'/values/{quote(uid)}', body={'value': value},
                      authenticated=True)

    # ----------------------------------------------------------------- internals

    def open_connection(self) -> http.client.HTTPConnection:
        """Create an unconnected HTTP(S) connection according to the configuration.

        Public because development helpers need a connection set up the same way
        this class would set it up.
        """
        if not self.config.use_tls:
            return http.client.HTTPConnection(self.config.host, self.config.port,
                                              timeout=self.config.timeout)
        return http.client.HTTPSConnection(self.config.host, self.config.port,
                                           timeout=self.config.timeout,
                                           context=self._ssl_context())

    def _ssl_context(self) -> ssl.SSLContext:
        """Build the TLS context according to the ``[tls]`` section of the configuration."""
        context = ssl.create_default_context(cafile=str(self.config.ca_cert)
                                             if self.config.ca_cert is not None else None)
        if not self.config.verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _register(self) -> None:
        """Register this program at the device and remember the returned token."""
        credentials = b64encode(f'{self.config.username}:{self.config.password}'
                                .encode()).decode('ascii')
        reply = self._checked_dict(self._request('POST', '/clients',
                                                 body={'client': self.config.client_id},
                                                 headers={'Authorization': f'Basic {credentials}'}))
        token = reply.get('token')
        if not isinstance(token, str) or not token:
            raise AuthError(f'{self.config.host}: no token in registration reply')
        self._token = token
        self._registered = True

    def _request(self, method: str, path: str, *, body: Any = None,
                 headers: dict[str, str] | None = None,
                 authenticated: bool = False) -> Any:
        """Perform one REST request and return the decoded JSON reply, if any."""
        if self._conn is None:
            raise ConnectionError('not connected')
        if authenticated and self._token is None:
            raise AuthError('no access token available')

        url = API_PREFIX + path
        if authenticated:
            url += f'?{urlencode({"token": self._token})}'
        payload = json.dumps(body).encode() if body is not None else None
        request_headers = {'Accept': 'application/json'} | (headers or {})
        if payload is not None:
            request_headers['Content-Type'] = 'application/json'

        try:
            self._conn.request(method, url, body=payload, headers=request_headers)
            with self._conn.getresponse() as response:
                status = response.status
                data = response.read()
        except (OSError, http.client.HTTPException) as e:
            self._conn.close()
            raise ConnectionError(f'{self.config.host}: {method} {path} failed: {e}') from e

        if status in (http.client.UNAUTHORIZED, http.client.FORBIDDEN):
            raise AuthError(f'{self.config.host}: {method} {path}: '
                            f'{self._error_detail(data) or "access denied"}')
        if status >= http.client.BAD_REQUEST:
            raise RequestError(method, path, status,
                               self._error_detail(data) or http.client.responses.get(status, ''))
        if not data:
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError as e:
            raise ConnectionError(f'{self.config.host}: {method} {path}: '
                                  f'malformed reply: {e}') from e

    @staticmethod
    def _error_detail(data: bytes) -> str:
        """Extract the message from an error reply of the device, if it has one."""
        try:
            reply = json.loads(data)
        except json.JSONDecodeError:
            # Not all error replies are JSON; keep only a one-line excerpt of those.
            text = ' '.join(data.decode(errors='replace').split())
            return text if len(text) <= MAX_ERROR_DETAIL else f'{text[:MAX_ERROR_DETAIL]}...'
        if isinstance(reply, dict):
            error = reply.get('error')
            if isinstance(error, dict):
                return str(error.get('message', ''))
        return ''

    @staticmethod
    def _checked_dict(reply: Any) -> dict[str, Any]:
        """Ensure a reply which must be a JSON object really is one."""
        if not isinstance(reply, dict):
            raise ConnectionError('unexpected reply from device')
        return reply
