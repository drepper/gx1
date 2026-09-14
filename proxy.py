#!/usr/bin/env python3
"""Logging HTTP and WebSocket proxy in front of a Gira X1.

The proxy listens on the local machine and forwards every connection to the same
port on the device named in the configuration file.  Each HTTP request and
response travelling through it is decoded and printed, and once a connection is
upgraded to a WebSocket every frame is printed as well.  That makes it possible
to observe how a working client talks to the device.

TLS can be terminated per port so that the traffic becomes readable; the proxy
then presents a self-signed certificate and opens its own TLS connection to the
device.
"""

import argparse
import select
import socket
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

import girax1

DEFAULT_PORTS = [80, 8080]
BODY_EXCERPT = 2000
FRAME_EXCERPT = 4000
NO_BODY_STATUS = frozenset({204, 304})
RELAY_IDLE_TIMEOUT = 600.0

# The device certificate is signed by a private Gira CA, so it can never verify.
_UPSTREAM_TLS = ssl._create_unverified_context()

WS_OPCODES = {0x0: 'cont', 0x1: 'text', 0x2: 'binary', 0x8: 'close', 0x9: 'ping', 0xa: 'pong'}
# Ping and pong carry no information and would drown out the interesting frames.
WS_QUIET = frozenset({0x9, 0xa})


class SockReader:
    """Unbuffered reader over a socket.

    Reading headers a byte at a time is deliberate: nothing may be read ahead,
    because after an upgrade the very next bytes belong to the WebSocket stream
    and have to stay in the socket for the raw relay.
    """

    def __init__(self, sock: socket.socket) -> None:
        """Remember the socket to read from."""
        self.sock = sock

    def readline(self) -> bytes:
        """Read one line, consuming nothing beyond its terminating newline."""
        out = bytearray()
        while True:
            b = self.sock.recv(1)
            if not b:
                break
            out += b
            if b == b'\n':
                break
        return bytes(out)

    def read(self, n: int | None = None) -> bytes:
        """Read exactly ``n`` bytes, or everything until the peer closes."""
        out = bytearray()
        while n is None or len(out) < n:
            chunk = self.sock.recv(65536 if n is None else n - len(out))
            if not chunk:
                break
            out += chunk
        return bytes(out)


class Message:
    """One decoded HTTP message together with the exact bytes it arrived as."""

    def __init__(self, start_line: str, headers: list[tuple[str, str]], head: bytes,
                 body_raw: bytes = b'', body: bytes = b'') -> None:
        """Store the parsed pieces and the original framing for forwarding."""
        self.start_line = start_line
        self.headers = headers
        self.head = head
        self.body_raw = body_raw
        self.body = body

    def header(self, name: str) -> str | None:
        """Return the first value of the named header, case insensitively."""
        return next((v for n, v in self.headers if n.lower() == name.lower()), None)

    def wire(self) -> bytes:
        """Return the message exactly as it must be forwarded."""
        return self.head + self.body_raw

    @property
    def status(self) -> int:
        """Return the status code of a response, or 0 for a request."""
        parts = self.start_line.split()
        return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0


def _read_headers(fp: SockReader) -> tuple[str, list[tuple[str, str]], bytes] | None:
    """Read a start line plus header block, returning them and their raw bytes."""
    start = fp.readline()
    if not start:
        return None
    raw = bytearray(start)
    headers: list[tuple[str, str]] = []
    while True:
        line = fp.readline()
        if not line:
            break
        raw += line
        if line in (b'\r\n', b'\n'):
            break
        name, sep, value = line.decode('latin-1').partition(':')
        if sep:
            headers.append((name.strip(), value.strip()))
    return start.decode('latin-1').rstrip('\r\n'), headers, bytes(raw)


def _read_chunked(fp: SockReader) -> tuple[bytes, bytes]:
    """Read a chunked body, returning the original bytes and the decoded content."""
    raw = bytearray()
    body = bytearray()
    while True:
        line = fp.readline()
        if not line:
            break
        raw += line
        size = int(line.split(b';')[0].strip() or b'0', 16)
        if size == 0:
            while True:                 # Trailer section, terminated by a blank line.
                trailer = fp.readline()
                if not trailer:
                    break
                raw += trailer
                if trailer in (b'\r\n', b'\n'):
                    break
            break
        chunk = fp.read(size)
        raw += chunk
        body += chunk
        raw += fp.read(2)               # The CRLF following every chunk.
    return bytes(raw), bytes(body)


def read_message(fp: SockReader, *, request_method: str | None = None) -> Message | None:
    """Read one complete HTTP message; ``request_method`` marks it as a response."""
    parsed = _read_headers(fp)
    if parsed is None:
        return None
    message = Message(*parsed)

    if request_method is not None:
        status = message.status
        if request_method == 'HEAD' or status in NO_BODY_STATUS or 100 <= status < 200:
            return message

    if (message.header('Transfer-Encoding') or '').lower().startswith('chunked'):
        message.body_raw, message.body = _read_chunked(fp)
    elif (length := message.header('Content-Length')) is not None:
        message.body_raw = message.body = fp.read(int(length))
    elif request_method is not None and 'close' in (message.header('Connection') or '').lower():
        message.body_raw = message.body = fp.read()
    return message


def parse_frames(buf: bytearray) -> list[tuple[int, bytes]]:
    """Pull every complete WebSocket frame out of the buffer, unmasking payloads."""
    frames: list[tuple[int, bytes]] = []
    while True:
        if len(buf) < 2:
            return frames
        opcode = buf[0] & 0x0f
        length = buf[1] & 0x7f
        offset = 2
        if length == 126:
            if len(buf) < 4:
                return frames
            length = int.from_bytes(buf[2:4], 'big')
            offset = 4
        elif length == 127:
            if len(buf) < 10:
                return frames
            length = int.from_bytes(buf[2:10], 'big')
            offset = 10
        mask = b''
        if buf[1] & 0x80:
            if len(buf) < offset + 4:
                return frames
            mask = bytes(buf[offset:offset + 4])
            offset += 4
        if len(buf) < offset + length:
            return frames
        payload = bytes(buf[offset:offset + length])
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        del buf[:offset + length]
        frames.append((opcode, payload))


class Logger:
    """Serialized writer for the observed traffic."""

    def __init__(self, stream: TextIO, logfile: TextIO | None, show_secrets: bool) -> None:
        """Remember where to write and whether credentials may be shown."""
        self.stream = stream
        self.logfile = logfile
        self.show_secrets = show_secrets
        self.lock = threading.Lock()

    @staticmethod
    def _stamp() -> str:
        """Return the current time with milliseconds."""
        return time.strftime('%H:%M:%S') + f'.{int(time.time() % 1 * 1000):03d}'

    def write(self, text: str) -> None:
        """Emit one block of text to every configured destination."""
        with self.lock:
            print(text, file=self.stream, flush=True)
            if self.logfile is not None:
                print(text, file=self.logfile, flush=True)

    def message(self, conn_id: int, port: int, arrow: str, message: Message,
                elapsed: float | None = None) -> None:
        """Print a decoded request or response."""
        timing = f'  ({elapsed * 1000:.0f} ms)' if elapsed is not None else ''
        start = message.start_line
        if not self.show_secrets:
            start = self._redact_query(start)
        lines = [f'[{self._stamp()}] #{conn_id} :{port} {arrow} {start}{timing}']
        for name, value in message.headers:
            if not self.show_secrets and name.lower() in ('authorization', 'proxy-authorization'):
                value = value.split(' ')[0] + ' <redacted>'
            lines.append(f'    {name}: {value}')
        if message.body:
            excerpt = message.body[:BODY_EXCERPT].decode('utf-8', errors='replace')
            suffix = ' ...' if len(message.body) > BODY_EXCERPT else ''
            lines.append(f'    body ({len(message.body)} bytes): {excerpt}{suffix}')
        self.write('\n'.join(lines))

    def frame(self, conn_id: int, port: int, arrow: str, opcode: int, payload: bytes) -> None:
        """Print one WebSocket frame."""
        kind = WS_OPCODES.get(opcode, f'op{opcode:x}')
        text = payload[:FRAME_EXCERPT].decode('utf-8', errors='replace')
        suffix = ' ...' if len(payload) > FRAME_EXCERPT else ''
        self.write(f'[{self._stamp()}] #{conn_id} :{port} {arrow} ws {kind} '
                   f'({len(payload)} bytes) {text}{suffix}')

    def note(self, text: str) -> None:
        """Print a line that is not part of a decoded HTTP message."""
        self.write(f'[{time.strftime("%H:%M:%S")}] {text}')

    @staticmethod
    def _redact_query(start_line: str) -> str:
        """Hide a query string, which on this device carries the credentials."""
        parts = start_line.split(' ')
        if len(parts) > 1 and '?' in parts[1]:
            parts[1] = parts[1].split('?')[0] + '?<redacted>'
        return ' '.join(parts)


class ProxyHandler(socketserver.BaseRequestHandler):
    """Relay one client connection to the device, decoding what passes through."""

    def handle(self) -> None:
        """Forward requests until either side closes the connection."""
        server = self.server
        with server.counter_lock:                                   # type: ignore[attr-defined]
            server.counter['n'] += 1                                # type: ignore[attr-defined]
            conn_id: int = server.counter['n']                      # type: ignore[attr-defined]
        logger: Logger = server.logger                              # type: ignore[attr-defined]
        port: int = server.port                                     # type: ignore[attr-defined]
        target: str = server.target                                 # type: ignore[attr-defined]
        remote: int = server.remote_port                            # type: ignore[attr-defined]

        logger.note(f'#{conn_id} :{port} open from '
                    f'{self.client_address[0]}:{self.client_address[1]}')

        tls_context: ssl.SSLContext | None = server.tls_local       # type: ignore[attr-defined]
        if tls_context is not None:
            try:
                self.request = tls_context.wrap_socket(self.request, server_side=True)
            except OSError as e:
                logger.note(f'#{conn_id} :{port} TLS handshake with client failed: {e}')
                return

        try:
            upstream = socket.create_connection((target, remote), timeout=server.timeout)  # type: ignore[attr-defined]
            if server.tls_remote:                                   # type: ignore[attr-defined]
                upstream = _UPSTREAM_TLS.wrap_socket(upstream, server_hostname=target)
        except OSError as e:
            logger.note(f'#{conn_id} :{port} cannot reach {target}:{remote}: {e}')
            return

        self.request.settimeout(server.timeout)                     # type: ignore[attr-defined]
        with upstream:
            try:
                self._pump(conn_id, port, upstream, logger, server)
            except (OSError, ValueError) as e:
                logger.note(f'#{conn_id} :{port} closed: {e}')
        logger.note(f'#{conn_id} :{port} done')

    def _pump(self, conn_id: int, port: int, upstream: socket.socket, logger: Logger,
              server: socketserver.TCPServer) -> None:
        """Relay request/response pairs, switching to frame relay after an upgrade."""
        client_in = SockReader(self.request)
        server_in = SockReader(upstream)
        while True:
            request = read_message(client_in)
            if request is None:
                return
            if request.start_line.startswith('\x16\x03'):
                logger.note(f'#{conn_id} :{port} looks like TLS, not decoding; '
                            'use --tls-port for this port')
                return
            logger.message(conn_id, port, '->', request)

            wire = (self._rewrite_host(request, server.target)       # type: ignore[attr-defined]
                    if server.rewrite_host else request.wire())      # type: ignore[attr-defined]
            started = time.monotonic()
            upstream.sendall(wire)

            method = request.start_line.split(' ')[0]
            response = read_message(server_in, request_method=method)
            if response is None:
                logger.note(f'#{conn_id} :{port} device closed without answering')
                return
            logger.message(conn_id, port, '<-', response, time.monotonic() - started)
            self.request.sendall(response.wire())

            if response.status == 101:
                logger.note(f'#{conn_id} :{port} upgraded, relaying WebSocket frames')
                self._relay_websocket(conn_id, port, upstream, logger)
                return
            if 'close' in (response.header('Connection') or '').lower() \
                    or 'close' in (request.header('Connection') or '').lower():
                return

    def _relay_websocket(self, conn_id: int, port: int, upstream: socket.socket,
                         logger: Logger) -> None:
        """Copy bytes both ways after an upgrade, decoding the frames for the log."""
        self.request.settimeout(None)
        upstream.settimeout(None)
        buffers = {'->': bytearray(), '<-': bytearray()}
        routes = {self.request: (upstream, '->'), upstream: (self.request, '<-')}
        while True:
            ready, _, _ = select.select(list(routes), [], [], RELAY_IDLE_TIMEOUT)
            if not ready:
                logger.note(f'#{conn_id} :{port} WebSocket idle, closing')
                return
            for source in ready:
                destination, arrow = routes[source]
                data = source.recv(65536)
                if not data:
                    return
                destination.sendall(data)
                buffers[arrow] += data
                for opcode, payload in parse_frames(buffers[arrow]):
                    if opcode not in WS_QUIET:
                        logger.frame(conn_id, port, arrow, opcode, payload)

    @staticmethod
    def _rewrite_host(request: Message, target: str) -> bytes:
        """Return the request with its Host header pointing at the device."""
        head = request.head.decode('latin-1').split('\r\n')
        head = [f'Host: {target}' if line.lower().startswith('host:') else line for line in head]
        return '\r\n'.join(head).encode('latin-1') + request.body_raw


def server_tls_context(cert: Path, target: str) -> ssl.SSLContext:
    """Return a TLS context for the listening side, generating a certificate if needed.

    The certificate is self-signed, so a client talking to the proxy has to skip
    verification just as it must with the certificate of the device itself.
    """
    if not cert.exists():
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(cert), '-out', str(cert), '-days', '365',
                        '-subj', f'/CN={target}',
                        '-addext', f'subjectAltName=DNS:{target},DNS:localhost'],
                       check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert))
    return context


class Server(socketserver.ThreadingTCPServer):
    """Threaded listener for one forwarded port."""

    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    """Start one listener per requested port and serve until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', help='name of the TOML configuration file')
    parser.add_argument('--port', type=int, action='append', dest='ports',
                        help='port to forward unchanged, may be repeated (default: 80 and 8080)')
    parser.add_argument('--map', action='append', dest='maps', default=[], metavar='LOCAL:REMOTE',
                        help='listen on LOCAL and forward to REMOTE on the device, which avoids '
                             'needing root for privileged device ports')
    parser.add_argument('--tls-port', type=int, action='append', dest='tls_ports', default=[],
                        help='port on which TLS is terminated and re-established towards the '
                             'device, so that the traffic can be decoded; may be repeated')
    parser.add_argument('--cert', default='proxy-cert.pem',
                        help='certificate used for terminated TLS, generated when missing')
    parser.add_argument('--bind', default='0.0.0.0', help='local address to listen on')
    parser.add_argument('--log', help='additionally append the traffic to this file')
    parser.add_argument('--show-secrets', action='store_true',
                        help='do not redact Authorization headers and query strings')
    parser.add_argument('--keep-host', action='store_true',
                        help='forward the Host header unchanged instead of rewriting it')
    args = parser.parse_args()

    try:
        config = girax1.load(args.config)
    except girax1.ConfigError as e:
        print(f'configuration error: {e}', file=sys.stderr)
        return 1

    plain = args.ports or ([] if args.maps or args.tls_ports else DEFAULT_PORTS)
    forwards: list[tuple[int, int, bool]] = [(p, p, False) for p in plain]
    forwards += [(p, p, True) for p in args.tls_ports]
    for entry in args.maps:
        local, _, remote = entry.partition(':')
        if not remote.isdigit() or not local.isdigit():
            print(f'--map expects LOCAL:REMOTE, got {entry}', file=sys.stderr)
            return 1
        forwards.append((int(local), int(remote), False))

    logfile = open(args.log, 'a', encoding='utf-8') if args.log is not None else None
    logger = Logger(sys.stdout, logfile, args.show_secrets)
    tls_context = server_tls_context(Path(args.cert), config.host) if args.tls_ports else None
    counter = {'n': 0}
    counter_lock = threading.Lock()

    servers: list[Server] = []
    for port, remote_port, tls in forwards:
        try:
            server = Server((args.bind, port), ProxyHandler)
        except PermissionError:
            print(f'cannot listen on port {port}: needs root, or lower the unprivileged port '
                  'limit with\n  sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80',
                  file=sys.stderr)
            return 1
        except OSError as e:
            print(f'cannot listen on port {port}: {e}', file=sys.stderr)
            return 1
        server.target = config.host                                 # type: ignore[attr-defined]
        server.port = port                                          # type: ignore[attr-defined]
        server.remote_port = remote_port                            # type: ignore[attr-defined]
        server.tls_local = tls_context if tls else None             # type: ignore[attr-defined]
        server.tls_remote = tls                                     # type: ignore[attr-defined]
        server.logger = logger                                      # type: ignore[attr-defined]
        server.rewrite_host = not args.keep_host                    # type: ignore[attr-defined]
        server.timeout = config.timeout                             # type: ignore[attr-defined]
        server.counter = counter                                    # type: ignore[attr-defined]
        server.counter_lock = counter_lock                          # type: ignore[attr-defined]
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        logger.note(f'listening on {args.bind}:{port} -> {config.host}:{remote_port}'
                    f'{" (TLS terminated)" if tls else ""}')

    logger.note('press Ctrl-C to stop')
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.note('shutting down')
    for server in servers:
        server.shutdown()
    if logfile is not None:
        logfile.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
