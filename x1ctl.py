#!/usr/bin/env python3
"""Control a Gira X1 over its Device Stack API."""

import argparse
import json
import sys

import gx1
from gx1.stack import Stack


def main() -> int:
    """Dispatch the requested sub-command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', help='name of the TOML configuration file')
    sub = parser.add_subparsers(dest='action', required=True)

    sub.add_parser('list', help='list the functions with their current values')

    get = sub.add_parser('get', help='read one data point')
    get.add_argument('point', help='numeric data point id, or a URN')

    put = sub.add_parser('set', help='write one data point')
    put.add_argument('point', help='numeric data point id, or a URN')
    put.add_argument('value', help='new value, for switches 0 or 1')

    watch = sub.add_parser('watch', help='print value changes as they happen')
    watch.add_argument('--seconds', type=float, help='stop after this many seconds')

    args = parser.parse_args()

    try:
        with Stack(args.config) as stack:
            match args.action:
                case 'list':
                    _list(stack)
                case 'get':
                    print(json.dumps(_address(stack.get_value, args.point), indent=1))
                case 'set':
                    reply = _address(stack.set_value, args.point, _value(args.value))
                    print(f"{reply.get('urn', args.point)} = {reply.get('value')}")
                case 'watch':
                    _watch(stack, args.seconds)
    except gx1.ConfigError as e:
        print(f'configuration error: {e}', file=sys.stderr)
        return 1
    except gx1.X1Error as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def _value(text: str) -> int | float | str:
    """Convert a value given on the command line to the most fitting type."""
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            continue
    return text


def _address(method, point: str, *rest):
    """Call a Stack method with the data point given either as id or as URN."""
    if point.isdigit():
        return method(*rest, point_id=int(point))
    return method(*rest, urn=point)


def _list(stack: Stack) -> None:
    """Print every function with its data points and current values."""
    values = {str(d.get('id')): d.get('state') for d in stack.process_view()}
    for entry in stack.ui_configuration():
        if not entry.get('name') or not entry.get('channelViewID'):
            continue
        print(f"\n{entry['name']}   [{str(entry.get('functionType', '')).split('.')[-1]}]")
        for point in entry.get('dataPoints', []):
            if not point.get('id'):
                continue                # Not bound to a group address in this project.
            identifier = str(point['id'])
            print(f"   {str(point.get('dataPoint')):<16} {identifier:<8} "
                  f"= {str(values.get(identifier, '-'))}")


def _watch(stack: Stack, seconds: float | None) -> None:
    """Print value change events until interrupted or until the time is up."""
    import time
    deadline = None if seconds is None else time.monotonic() + seconds
    names = {str(p['id']): f"{e['name']}/{p.get('dataPoint')}"
             for e in stack.ui_configuration() if e.get('name')
             for p in e.get('dataPoints', []) if p.get('id')}
    print('watching for value changes, press Ctrl-C to stop')
    while deadline is None or time.monotonic() < deadline:
        event = stack.poll(timeout=1.0)
        if event is not None and event.get('type') == 'Value':
            value = event.get('value', {})
            label = names.get(str(value.get('id')), value.get('urn', '?'))
            print(f"  {label}: {value.get('old')} -> {value.get('new')}"
                  f"  (source {value.get('source') or 'device'})")


if __name__ == '__main__':
    sys.exit(main())
