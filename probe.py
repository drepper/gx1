#!/usr/bin/env python3
"""Probe a Gira X1 for the paths under which it serves its REST API.

This is a development helper used to pin down protocol details that are not
documented.  It only issues GET requests and prints the status of each.
"""

import argparse
import http.client
import sys
from base64 import b64encode
from dataclasses import replace

import girax1
from girax1.x1 import X1

CANDIDATES = [
    '/',
    '/api',
    '/api/',
    '/api/v2',
    '/api/v2/',
    '/api/version',
    '/api/v2/version',
    '/api/uiconfig',
    '/api/v2/uiconfig',
    '/api/clients',
    '/api/v2/clients',
    '/api/values',
    '/api/v2/values',
    '/endpoints',
    '/endpoints/',
    '/gds',
    '/gds/',
]


def main() -> int:
    """Request every candidate path, with and without credentials, and report the replies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', help='name of the TOML configuration file')
    parser.add_argument('--extra', action='append', default=[],
                        help='additional path to probe, may be repeated')
    parser.add_argument('--tls', action=argparse.BooleanOptionalAction, default=None,
                        help='override whether TLS is used')
    parser.add_argument('--port', type=int, help='override the port from the configuration')
    args = parser.parse_args()

    try:
        config = girax1.load(args.config)
    except girax1.ConfigError as e:
        print(f'configuration error: {e}', file=sys.stderr)
        return 1
    if args.tls is not None:
        config = replace(config, use_tls=args.tls,
                         port=args.port or (443 if args.tls else 80))
    if args.port is not None:
        config = replace(config, port=args.port)

    client = X1.__new__(X1)             # Reuse the connection setup without a second config read.
    client.config = config
    auth = b64encode(f'{config.username}:{config.password}'.encode()).decode('ascii')

    print(f'probing {config.base_url.removesuffix("/api")}')
    print(f'{"path":26s} {"anon":>6s} {"auth":>6s}  server            excerpt')
    for path in CANDIDATES + args.extra:
        results: list[tuple[int | str, str, str]] = []
        for headers in ({}, {'Authorization': f'Basic {auth}'}):
            # A fresh connection per request: a 502 page or an unread body would
            # otherwise poison connection reuse and hide the real status.
            conn = client.open_connection()
            try:
                conn.request('GET', path, headers={'Accept': 'application/json'} | headers)
                with conn.getresponse() as response:
                    results.append((response.status, response.headers.get('Server', ''),
                                    ' '.join(response.read().decode(errors='replace').split())))
            except (OSError, http.client.HTTPException) as e:
                results.append(('ERR', '', str(e)))
            finally:
                conn.close()
        status, server, excerpt = next((r for r in results if r[0] not in (404, 'ERR') and r[2]),
                                       results[0])
        print(f'{path:26s} {results[0][0]!s:>6s} {results[1][0]!s:>6s}  {server[:16]:16s}  '
              f'{excerpt[:80]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
