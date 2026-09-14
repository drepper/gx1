#!/usr/bin/env python3
"""Connect to a Gira X1 using the settings from a configuration file."""

import argparse
import sys

import girax1


def main() -> int:
    """Instantiate the X1 class with the given configuration file and report the result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', help='name of the TOML configuration file')
    args = parser.parse_args()

    try:
        with girax1.X1(args.config) as x1:
            print(f'connected to {x1.config.host}:{x1.config.port}')
            for key, value in x1.info.items():
                print(f'  {key}: {value}')
            if not x1.info:
                print('  (device did not answer the availability check)')
            print(f'  token: {x1.token}')
            print(f'  uiconfig uid: {x1.uiconfig_uid()}')
    except girax1.ConfigError as e:
        print(f'configuration error: {e}', file=sys.stderr)
        return 1
    except girax1.X1Error as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
