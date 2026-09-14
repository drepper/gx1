"""Minimal WebSocket client, sufficient for the Gira Device Stack API.

Only what the device actually uses is implemented: a client-side RFC 6455
handshake, masked client frames, reassembly of fragmented messages and automatic
answering of pings.  Compression extensions are deliberately not offered, so the
payloads stay readable.
"""

import base64
import os
import secrets
import socket
import ssl
import struct
from typing import Iterator, Self

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xa

OPCODE_NAMES = {OP_CONTINUATION: 'cont', OP_TEXT: 'text', OP_BINARY: 'binary',
                OP_CLOSE: 'close', OP_PING: 'ping', OP_PONG: 'pong'}


class WebSocketError(Exception):
    """Raised when the handshake fails or the framing is violated."""


class WebSocket:
    """A client connection to a WebSocket server."""

    def __init__(self, sock: socket.socket) -> None:
        """Wrap an already upgraded socket."""
        self.sock = sock
        self._buffer = bytearray()
        self._closed = False

    @classmethod
    def connect(cls, host: str, port: int, path: str, *, tls: bool = True,
                timeout: float = 10.0, verify: bool = False,
                headers: dict[str, str] | None = None) -> Self:
        """Open a connection and perform the upgrade handshake."""
        sock: socket.socket = socket.create_connection((host, port), timeout=timeout)
        if tls:
            context = (ssl.create_default_context() if verify
                       else ssl._create_unverified_context())
            sock = context.wrap_socket(sock, server_hostname=host)

        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = [f'GET {path} HTTP/1.1', f'Host: {host}:{port}', 'Upgrade: websocket',
                   'Connection: Upgrade', f'Sec-WebSocket-Key: {key}',
                   'Sec-WebSocket-Version: 13']
        request += [f'{n}: {v}' for n, v in (headers or {}).items()]
        sock.sendall(('\r\n'.join(request) + '\r\n\r\n').encode())

        raw = bytearray()
        while b'\r\n\r\n' not in raw:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError('connection closed during handshake')
            raw += chunk
        head, _, rest = bytes(raw).partition(b'\r\n\r\n')
        status = head.decode('latin-1').split('\r\n')[0]
        if ' 101' not in status:
            raise WebSocketError(f'upgrade refused: {status}')

        self = cls(sock)
        self._buffer += rest
        return self

    def __enter__(self) -> Self:
        """Allow use as a context manager."""
        return self

    def __exit__(self, *_: object) -> None:
        """Close the connection when leaving a ``with`` statement."""
        self.close()

    def send(self, payload: bytes | str, opcode: int | None = None) -> None:
        """Send one unfragmented, masked frame."""
        if isinstance(payload, str):
            data, opcode = payload.encode(), opcode or OP_TEXT
        else:
            data, opcode = payload, opcode or OP_BINARY
        header = bytes([0x80 | opcode])
        if len(data) < 126:
            header += bytes([0x80 | len(data)])
        elif len(data) < 65536:
            header += bytes([0x80 | 126]) + struct.pack('>H', len(data))
        else:
            header += bytes([0x80 | 127]) + struct.pack('>Q', len(data))
        mask = os.urandom(4)
        self.sock.sendall(header + mask
                          + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self, timeout: float | None = None) -> tuple[int, bytes] | None:
        """Return the next message, answering pings transparently.

        Returns ``None`` when nothing arrived before the timeout expired, and
        raises :class:`WebSocketError` once the peer has closed the connection.
        """
        self.sock.settimeout(timeout)
        pending: list[bytes] = []
        pending_opcode = OP_TEXT
        while True:
            frame = self._next_frame()
            if frame is None:
                try:
                    chunk = self.sock.recv(65536)
                except (TimeoutError, socket.timeout):
                    return None
                if not chunk:
                    self._closed = True
                    raise WebSocketError('connection closed by peer')
                self._buffer += chunk
                continue

            fin, opcode, payload = frame
            if opcode == OP_PING:
                self.send(payload, OP_PONG)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._closed = True
                return OP_CLOSE, payload
            if opcode == OP_CONTINUATION:
                pending.append(payload)
            else:
                pending = [payload]
                pending_opcode = opcode
            if fin:
                return pending_opcode, b''.join(pending)

    def messages(self, timeout: float | None = None) -> Iterator[tuple[int, bytes]]:
        """Yield messages until the peer closes the connection."""
        while not self._closed:
            try:
                message = self.recv(timeout)
            except WebSocketError:
                return
            if message is not None:
                yield message

    def close(self) -> None:
        """Send a close frame, ignoring a peer that already vanished."""
        if not self._closed:
            self._closed = True
            try:
                self.send(struct.pack('>H', 1000), OP_CLOSE)
            except OSError:
                pass
        self.sock.close()

    def _next_frame(self) -> tuple[bool, int, bytes] | None:
        """Take one complete frame out of the buffer, if there is one."""
        buf = self._buffer
        if len(buf) < 2:
            return None
        fin = bool(buf[0] & 0x80)
        opcode = buf[0] & 0x0f
        length = buf[1] & 0x7f
        offset = 2
        if length == 126:
            if len(buf) < 4:
                return None
            length = int.from_bytes(buf[2:4], 'big')
            offset = 4
        elif length == 127:
            if len(buf) < 10:
                return None
            length = int.from_bytes(buf[2:10], 'big')
            offset = 10
        mask = b''
        if buf[1] & 0x80:                # Servers must not mask, but be lenient.
            if len(buf) < offset + 4:
                return None
            mask = bytes(buf[offset:offset + 4])
            offset += 4
        if len(buf) < offset + length:
            return None
        payload = bytes(buf[offset:offset + length])
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        del buf[:offset + length]
        return fin, opcode, payload
