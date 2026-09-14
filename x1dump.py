#!/usr/bin/env python3
"""Dump everything a Gira X1 will tell us over its Device Stack API."""

import argparse
import json
import sys

import gx1
from gx1.stack import Stack, StackError

# Commands worth trying that take no parameters; unsupported ones answer with 102.
EXTRA_COMMANDS = ['GetLicenses', 'GetDeviceInfo', 'GetProjectInfo', 'GetTrades',
                  'GetLocations', 'GetFunctions', 'GetDevices', 'GetChannels',
                  'GetNextTriggerTimes', 'GetTimeProfiles', 'GetScenes', 'GetUsers']


def main() -> int:
    """Connect, register and print the configuration and current values."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', help='name of the TOML configuration file')
    parser.add_argument('--json', action='store_true', help='dump raw JSON instead of a summary')
    parser.add_argument('--probe', action='store_true',
                        help='also try commands that may not exist on this firmware')
    args = parser.parse_args()

    try:
        with Stack(args.config) as stack:
            config = stack.ui_configuration()
            values = {str(d.get('id')): d.get('state') for d in stack.process_view()}
            groups = {g: _group(stack, g) for g in ('Administrator', 'Installer', 'User')}
            probed = _probe(stack) if args.probe else {}

            if args.json:
                json.dump({'uiConfiguration': config, 'processView': values,
                           'groups': groups, 'commands': probed},
                          sys.stdout, indent=1, ensure_ascii=False)
                print()
                return 0

            _summarize(config, values)
            print('\n=== group membership ===')
            for group, member in groups.items():
                print(f'  {group:<16} {member}')
            if args.probe:
                print('\n=== command probe ===')
                for command, result in probed.items():
                    print(f'  {command:<22} {result}')
    except gx1.ConfigError as e:
        print(f'configuration error: {e}', file=sys.stderr)
        return 1
    except gx1.X1Error as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    return 0


def _group(stack: Stack, group: str) -> bool | str:
    """Return the group membership, or the error the device reported instead."""
    try:
        return stack.in_group(group)
    except StackError as e:
        return str(e)


def _probe(stack: Stack) -> dict[str, str]:
    """Try every optional command and record whether this firmware knows it."""
    results: dict[str, str] = {}
    for command in EXTRA_COMMANDS:
        try:
            reply = stack.request(command)
            keys = [k for k in reply if k not in ('request', 'version', 'error')]
            results[command] = f'OK  keys={keys}'
        except StackError as e:
            results[command] = f'{e.text} ({e.code})'
        except (TimeoutError, gx1.X1Error) as e:
            results[command] = str(e)
    return results


def _summarize(config: list[dict], values: dict[str, str]) -> None:
    """Print the functions with their data points and current values."""
    print('=== functions ===')
    for entry in config:
        if 'name' not in entry:
            print(f"project guid: {entry.get('guid')}")
            continue
        kind = str(entry.get('functionType', '')).split('.')[-1]
        print(f"\n{entry['name']}   [{kind}]")
        print(f"   channelViewID={entry.get('channelViewID')}  {entry.get('channelViewUrn', '')}")
        for point in entry.get('dataPoints', []):
            identifier = str(point.get('id'))
            current = values.get(identifier, '-')
            print(f"     {str(point.get('dataPoint')):<18} id={identifier:<8} "
                  f"value={str(current):<10} {point.get('urn', '')}")


if __name__ == '__main__':
    sys.exit(main())
