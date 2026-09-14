#!/usr/bin/env python3
"""Ask a Gira X1 about itself through its device portal.

Unlike the IoT REST API this works on firmware versions older than 2.4, which is
useful to find out whether a device is new enough to offer the REST API at all.
"""

import argparse
import sys

import girax1
from girax1.portal import Portal

REST_API_FIRMWARE = (2, 4)


def main() -> int:
    """Print the device information and whether the REST API can be expected."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', help='name of the TOML configuration file')
    parser.add_argument('--port', type=int, default=80, help='port of the device portal')
    args = parser.parse_args()

    try:
        with Portal(args.config, args.port) as portal:
            info = portal.device_info()
    except girax1.ConfigError as e:
        print(f'configuration error: {e}', file=sys.stderr)
        return 1
    except girax1.X1Error as e:
        print(f'error: {e}', file=sys.stderr)
        return 1

    for key, value in info.items():
        print(f'{key}: {value}')

    version = info.get('CurrentFirmwareVersion', '')
    parts = tuple(int(p) for p in version.split('.')[:2] if p.isdigit())
    if len(parts) == 2:
        if parts < REST_API_FIRMWARE:
            print(f'\nFirmware {version} is older than '
                  f'{".".join(str(p) for p in REST_API_FIRMWARE)}, so this device does not '
                  'offer the Gira IoT REST API yet.  A firmware update is needed before '
                  'x1connect.py can work.')
        else:
            print(f'\nFirmware {version} should offer the Gira IoT REST API.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
