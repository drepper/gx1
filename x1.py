#!/usr/bin/env python3
"""Text based program to control a Gira X1.

The first argument is the command to carry out.  The project configuration,
which holds the names of all devices, is downloaded from the X1 at start-up so
that every command can address devices by name.

Devices are addressed by a path made of the room they sit in, a slash and the
device name, for example ``Flur/Deckenlampen Flur``.

    x1.py list                      list all devices and the available groupings
    x1.py list --by location        list the devices grouped by location
    x1.py status                    show the status of every device
    x1.py status "Küche/"           show the status of one room
    x1.py status "*/Rollo*"         show the status of everything matching
    x1.py on "Flur/Deckenlampen Flur"   switch one lamp on
    x1.py down "Küche/"             lower every covering in the kitchen
    x1.py stop "*/Rollo*"           halt every covering whose name matches
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Callable

import gx1
from gx1.project import Device, Project
from gx1.stack import Stack

DEFAULT_CONFIG = Path(os.environ.get('GX1_CONFIG', '~/.config/gx1.toml'))

# The data point that switching a device on and off writes to.
SWITCH_POINT = 'OnOff'

# The data points that drive and halt a covering.
COVER_POINT = 'Up-Down'
STOP_POINT = 'Step-Up-Down'

# German names of the grouping kinds, as the device itself presents them.
GROUPING_LABELS = {'Trade': 'trade (Gewerk)', 'Location': 'location (Standort)',
                   'Zone': 'zone (Zone)', 'Profile': 'profile (Profil)'}


def main() -> int:
    """Parse the command line, download the configuration and run the command."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('-c', '--config', type=Path, default=DEFAULT_CONFIG,
                        help=f'configuration file to use (default: {DEFAULT_CONFIG})')
    commands = parser.add_subparsers(dest='command', required=True, metavar='COMMAND')

    listing = commands.add_parser('list', help='list the devices and the possible groupings')
    listing.add_argument('--by', dest='grouping', metavar='KIND',
                         help='group the output by this kind, for example trade or location')
    listing.add_argument('--long', action='store_true',
                         help='also show the data points of every device')
    listing.set_defaults(run=command_list)

    status = commands.add_parser('status', help='print the current status of devices')
    status.add_argument('paths', nargs='*', metavar='PATH',
                        help='device paths such as "Flur/Deckenlampen Flur"; a bare device '
                             'name, a room with a trailing slash such as "Küche/", or a '
                             'pattern such as "*/Rollo*" also work.  All devices when omitted')
    status.set_defaults(run=command_status)

    for name, state in (('on', True), ('off', False)):
        switching = commands.add_parser(name, help=f'switch the named devices {name}')
        switching.add_argument('paths', nargs='+', metavar='PATH',
                               help='device paths, names, rooms or patterns; at least one')
        switching.set_defaults(run=command_switch, state=state)

    for name, description in (('up', 'raise the named coverings'),
                              ('down', 'lower the named coverings'),
                              ('stop', 'halt the named coverings')):
        moving = commands.add_parser(name, help=description)
        moving.add_argument('paths', nargs='+', metavar='PATH',
                            help='device paths, names, rooms or patterns; at least one')
        moving.set_defaults(run=command_cover, action=name)

    args = parser.parse_args()

    try:
        with Stack(args.config) as stack:
            project = Project.parse(stack.ui_configuration())
            run: Callable[[Stack, Project, argparse.Namespace], int] = args.run
            return run(stack, project, args)
    except gx1.ConfigError as e:
        print(f'configuration error: {e}', file=sys.stderr)
        return 1
    except gx1.X1Error as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    except TimeoutError:
        print('error: the device did not answer in time', file=sys.stderr)
        return 1


def command_switch(stack: Stack, project: Project, args: argparse.Namespace) -> int:
    """Switch every named device on or off."""
    devices, failed = _select(project, args.paths)
    for device in devices:
        point = device.point(SWITCH_POINT)
        if point is None or not point.usable:
            print(f'{project.path(device)}: no {SWITCH_POINT} data point, '
                  f'a {device.kind} cannot be switched this way', file=sys.stderr)
            failed = True
            continue
        try:
            reply = stack.switch(point.point_id, args.state)
        except gx1.X1Error as e:
            print(f'{project.path(device)}: {e}', file=sys.stderr)
            failed = True
            continue
        print(f'{project.path(device)} {_state(SWITCH_POINT, str(reply.get("value")))}')
    return 1 if failed else 0


def command_cover(stack: Stack, project: Project, args: argparse.Namespace) -> int:
    """Drive or halt every named covering.

    Halting writes to a different data point than driving, which is why the
    two cases pick a different one here.
    """
    devices, failed = _select(project, args.paths)
    wanted = STOP_POINT if args.action == 'stop' else COVER_POINT
    for device in devices:
        point = device.point(wanted)
        if point is None or not point.usable:
            print(f'{project.path(device)}: no {wanted} data point, '
                  f'a {device.kind} cannot be moved', file=sys.stderr)
            failed = True
            continue
        try:
            if args.action == 'stop':
                stack.stop(point.point_id)
            else:
                stack.move(point.point_id, args.action == 'down')
        except gx1.X1Error as e:
            print(f'{project.path(device)}: {e}', file=sys.stderr)
            failed = True
            continue
        print(f'{project.path(device)} {args.action}')
    return 1 if failed else 0


def _select(project: Project, patterns: list[str]) -> tuple[list[Device], bool]:
    """Resolve the given patterns, reporting the ones that match nothing."""
    devices: list[Device] = []
    failed = False
    for pattern in patterns:
        found = project.resolve(pattern)
        if not found:
            print(f'no device matches {pattern!r}', file=sys.stderr)
            failed = True
        devices += (d for d in found if d not in devices)
    return devices, failed


def command_status(stack: Stack, project: Project, args: argparse.Namespace) -> int:
    """Print the current value of every data point of the selected devices."""
    devices, missing = _select(project, args.paths) if args.paths else (list(project), False)

    values = {str(d.get('id')): d.get('state') for d in stack.process_view()}
    width = max((len(project.path(d)) for d in devices), default=0)
    for device in devices:
        states = ', '.join(f'{p.name} {_state(p.name, values.get(str(p.point_id)))}'
                           for p in device.usable_points)
        print(f'{project.path(device):<{width}}  {device.kind:<9} {states or "no data points"}')
    return 1 if missing else 0


def _state(name: str, value: str | None) -> str:
    """Render one data point value the way a person would read it."""
    if value is None or value == '':
        return '?'
    if name == 'OnOff':
        return {'0': 'off', '1': 'on'}.get(value, value)
    if name == 'Brightness':
        return f'{value}%'
    return value


def command_list(stack: Stack, project: Project, args: argparse.Namespace) -> int:
    """List the devices, either flat or grouped as requested."""
    groupings = project.groupings()
    if args.grouping is None:
        _list_flat(project, args.long)
        _list_groupings(groupings)
        return 0

    kind = _match_grouping(args.grouping, groupings)
    if kind is None:
        available = ', '.join(sorted(k.lower() for k in groupings)) or 'none'
        print(f'unknown grouping {args.grouping!r}; available: {available}', file=sys.stderr)
        return 1

    for group in groupings[kind]:
        members = project.members(group)
        print(f'{group.label}  ({_count(len(members))})')
        for device in members:
            _print_device(device, args.long, indent='   ')
        print()
    if rest := project.ungrouped(kind):
        print(f'not assigned to any {kind.lower()}  ({_count(len(rest))})')
        for device in rest:
            _print_device(device, args.long, indent='   ')
    return 0


def _list_flat(project: Project, long: bool) -> None:
    """Print every device once, in the order the device reports them."""
    print(_count(len(project)))
    for device in project:
        _print_device(device, long, indent='   ')
    print()


def _list_groupings(groupings: dict[str, list]) -> None:
    """Print which groupings exist and what they contain."""
    if not groupings:
        print('no groupings configured')
        return
    print('possible groupings')
    for kind, groups in groupings.items():
        label = GROUPING_LABELS.get(kind, kind.lower())
        print(f'   --by {kind.lower()}   {label}')
        for group in groups:
            print(f'      {group.label:<28} {_count(len(group.members))}')


def _count(n: int) -> str:
    """Return a device count with a correctly spelled noun."""
    return f'{n} device' + ('' if n == 1 else 's')


def _print_device(device: Device, long: bool, indent: str = '') -> None:
    """Print one device, optionally with its data points."""
    print(f'{indent}{device.name:<28} {device.kind:<10} id={device.view_id}')
    if not long:
        return
    for point in device.data_points:
        if point.usable:
            print(f'{indent}   {point.name:<16} {point.point_id}')
        else:
            print(f'{indent}   {point.name:<16} -  not bound to a group address')


def _match_grouping(wanted: str, groupings: dict[str, list]) -> str | None:
    """Return the grouping kind matching the user's spelling, if there is one."""
    return next((k for k in groupings if k.lower() == wanted.lower()), None)


if __name__ == '__main__':
    sys.exit(main())
