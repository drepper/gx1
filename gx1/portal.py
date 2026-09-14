"""Client for the device portal of a Gira X1.

The device portal is the configuration web interface of the device, served over
plain HTTP, and is a different thing from the Gira IoT REST API in :mod:`gx1.x1`.
It is reachable on firmware versions that do not offer the REST API yet, which
makes it the only way to ask such a device about itself.

The protocol was derived from the JavaScript the device itself serves.  Every
request is a ``POST /api`` whose body is ``{"command": ..., "data": ...}``.  All
requests except the initial handshake are encrypted:

* the client generates an RSA-1024 key pair and sends the public key,
* the device answers with a base64 blob which, decrypted with RSAES-PKCS1-v1_5,
  contains a hex encoded AES key,
* every later body is ``hex(iv) + hex(AES-OFB(key, iv, json))`` and the answer
  uses the same framing.

The session is bound to the ``sessionId`` cookie the device sets, so the cookie
has to be sent back with every request.
"""

import base64
import http.client
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Self

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:                                    # Moved in cryptography 43, removed from the old place later.
    from cryptography.hazmat.decrepit.ciphers.modes import OFB
except ImportError:                     # pragma: no cover - depends on the installed version
    from cryptography.hazmat.primitives.ciphers.modes import OFB

from .config import Config, load
from .x1 import AuthError, ConnectionError, RequestError, X1Error

PORTAL_PATH = '/api'
PORTAL_PORT = 80
RSA_KEY_BITS = 1024
IV_LENGTH = 16

# Error identifiers used by the portal, taken from its own JavaScript.
ERROR_NOT_AUTHORIZED = '235'
ERROR_ENCRYPTION_EXPECTED = '237'


class Portal:
    """Encrypted session with the configuration portal of a device."""

    def __init__(self, filename: str | Path | Config, port: int = PORTAL_PORT) -> None:
        """Read the configuration file without contacting the device yet."""
        self.config: Config = filename if isinstance(filename, Config) else load(filename)
        self.port = port
        self._conn: http.client.HTTPConnection | None = None
        self._cookie: str | None = None
        self._key: bytes | None = None

    def __enter__(self) -> Self:
        """Open the encrypted session on entry of a ``with`` statement."""
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        """Close the connection to the portal."""
        self.close()

    def connect(self) -> None:
        """Open the connection and negotiate the symmetric session key."""
        self._conn = http.client.HTTPConnection(self.config.host, self.port,
                                                timeout=self.config.timeout)
        private = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_BITS)
        public = private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

        reply = self._post(json.dumps({
            'command': 'initSecureSession',
            'data': {'time': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                     'public_key': public}}))
        try:
            secret = json.loads(private.decrypt(base64.b64decode(reply), padding.PKCS1v15()))
            self._key = bytes.fromhex(secret['data']['secret_key'])
        except (ValueError, KeyError, TypeError) as e:
            self.close()
            raise ConnectionError(f'{self.config.host}: unusable session reply: {e}') from e

    def close(self) -> None:
        """Drop the session key and close the connection."""
        self._key = None
        self._cookie = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def command(self, name: str, data: Any = None) -> dict[str, Any]:
        """Send one portal command and return the ``data`` part of its answer."""
        if self._key is None:
            raise ConnectionError('no portal session, call connect() first')
        body: dict[str, Any] = {'command': name}
        if data is not None:
            body['data'] = data

        reply = self._decrypt(self._post(self._encrypt(body)))
        if (error := reply.get('error')) is not None:
            identifier = str(reply.get('id', ''))
            if identifier == ERROR_NOT_AUTHORIZED:
                raise AuthError(f'{name}: not authorized, the portal requires a login')
            if identifier == ERROR_ENCRYPTION_EXPECTED:
                raise RequestError('POST', name, 0, 'the device lost the encrypted session')
            raise RequestError('POST', name, 0, f'{error} ({identifier})')
        return reply.get('data', {})

    def device_info(self) -> dict[str, Any]:
        """Return firmware version and model of the device.

        This is one of the few commands the portal answers without a login.
        """
        return self.command('getDeviceInfo')

    # ----------------------------------------------------------------- internals

    def _post(self, body: str) -> str:
        """Send one request to the portal, carrying the session cookie along."""
        if self._conn is None:
            raise ConnectionError('not connected')
        headers = {'Content-type': 'application/json'}
        if self._cookie is not None:
            headers['Cookie'] = self._cookie
        try:
            self._conn.request('POST', PORTAL_PATH, body=body, headers=headers)
            with self._conn.getresponse() as response:
                if (cookie := response.getheader('Set-Cookie')) is not None:
                    self._cookie = cookie.split(';')[0]
                return response.read().decode(errors='replace')
        except (OSError, http.client.HTTPException) as e:
            self.close()
            raise ConnectionError(f'{self.config.host}: portal request failed: {e}') from e

    def _encrypt(self, body: dict[str, Any]) -> str:
        """Encode a command as ``hex(iv) + hex(ciphertext)``."""
        assert self._key is not None
        iv = secrets.token_bytes(IV_LENGTH)
        encryptor = Cipher(algorithms.AES(self._key), OFB(iv)).encryptor()
        return iv.hex() + (encryptor.update(json.dumps(body).encode())
                           + encryptor.finalize()).hex()

    def _decrypt(self, text: str) -> dict[str, Any]:
        """Decode an answer, which the device sends in the clear when it errors out."""
        assert self._key is not None
        try:
            if text.startswith('{'):
                return json.loads(text)
            iv = bytes.fromhex(text[:IV_LENGTH * 2])
            decryptor = Cipher(algorithms.AES(self._key), OFB(iv)).decryptor()
            data = bytes.fromhex(text[IV_LENGTH * 2:])
            return json.loads((decryptor.update(data) + decryptor.finalize()).decode())
        except ValueError as e:
            raise ConnectionError(f'{self.config.host}: undecodable portal answer: {e}') from e


def device_info(filename: str | Path) -> dict[str, Any]:
    """Open a portal session just long enough to read the device information."""
    with Portal(filename) as portal:
        return portal.device_info()


__all__ = ['Portal', 'X1Error', 'device_info']
