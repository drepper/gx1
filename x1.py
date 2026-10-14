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
import time
from pathlib import Path
from typing import Callable

import girax1
from girax1 import schedule
from girax1.project import (BRIGHTNESS_POINT, COVER_POINT, STOP_POINT, SWITCH_POINT, WILDCARDS,
                            Device, Project)
from girax1.stack import BRIGHTNESS_MAX, Stack

DEFAULT_CONFIG = Path(os.environ.get('GIRAX1_CONFIG', '~/.config/girax1.toml'))

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

    dimming = commands.add_parser('dim', help='set the brightness of the named lights')
    dimming.add_argument('level', type=_percent, metavar='PERCENT',
                         help=f'brightness from 0 to {BRIGHTNESS_MAX}')
    dimming.add_argument('paths', nargs='+', metavar='PATH',
                         help='device paths, names, rooms or patterns; at least one')
    dimming.set_defaults(run=command_dim)

    closing = commands.add_parser('close', help='close the named blinds by a percentage')
    closing.add_argument('percent', type=_percent, metavar='PERCENT',
                         help='how far to close, 0 leaves the blind open and 100 closes it')
    closing.add_argument('paths', nargs='+', metavar='PATH',
                         help='device paths, names, rooms or patterns; at least one')
    closing.set_defaults(run=command_close)

    timers = commands.add_parser('timers', help='read out the timer programming')
    timers.add_argument('paths', nargs='*', metavar='PATH',
                        help='device paths, names, rooms or patterns; all devices when omitted')
    timers.add_argument('--all', action='store_true',
                        help='also list devices that have no switch times programmed')
    timers.add_argument('--long', action='store_true',
                        help='also show validity and the scene behind every switch time')
    timers.set_defaults(run=command_timers)

    setting = commands.add_parser('settime', help='change a programmed switch time')
    setting.add_argument('path', metavar='PATH', help='the device whose timer to change')
    setting.add_argument('index', type=int, metavar='INDEX',
                         help='which switch time, as shown by the timers command')
    setting.add_argument('time', nargs='?', metavar='TIME',
                         help='new time of day as HH:MM or HH:MM:SS')
    setting.add_argument('--days', metavar='DAYS',
                         help='days to act on: daily, weekdays, weekend, or a list such as '
                              'Mon,Wed or Mon-Fri')
    setting.add_argument('--active', action=argparse.BooleanOptionalAction, default=None,
                         help='switch the entry on or off without deleting it')
    setting.add_argument('--scene', type=int, metavar='N',
                         help='number of the scene to apply, as shown by timers --long')
    setting.set_defaults(run=command_settime)

    adding = commands.add_parser('addtime', help='program a new switch time')
    adding.add_argument('path', metavar='PATH', help='the device to program')
    adding.add_argument('time', metavar='TIME', help='time of day as HH:MM or HH:MM:SS')
    adding.add_argument('action', metavar='ACTION',
                        help='what to do: on, off, up, down, or a brightness from 0 to 100')
    adding.add_argument('--days', default='daily', metavar='DAYS',
                        help='days to act on (default: daily)')
    adding.add_argument('--active', action=argparse.BooleanOptionalAction, default=False,
                        help='program the entry as active; it is added switched off by default')
    adding.set_defaults(run=command_addtime)

    deleting = commands.add_parser('deltime', help='delete a programmed switch time')
    deleting.add_argument('path', metavar='PATH', help='the device whose timer to change')
    deleting.add_argument('index', type=int, metavar='INDEX',
                          help='which switch time, as shown by the timers command')
    deleting.add_argument('--keep-scenes', action='store_true',
                          help='do not remove the scenes when the last switch time goes')
    deleting.set_defaults(run=command_deltime)

    commands.add_parser('gui', help='show the graphical user interface'
                        ).set_defaults(run=command_gui)

    args = parser.parse_args()

    try:
        with Stack(args.config) as stack:
            project = Project.parse(stack.ui_configuration())
            _record_blinds(stack, project)
            run: Callable[[Stack, Project, argparse.Namespace], int] = args.run
            return run(stack, project, args)
    except girax1.ConfigError as e:
        print(f'configuration error: {e}', file=sys.stderr)
        return 1
    except girax1.X1Error as e:
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
        except girax1.X1Error as e:
            print(f'{project.path(device)}: {e}', file=sys.stderr)
            failed = True
            continue
        print(f'{project.path(device)} {_state(SWITCH_POINT, str(reply.get("value")))}')
    return 1 if failed else 0


def command_addtime(stack: Stack, project: Project, args: argparse.Namespace) -> int:
    """Program a new switch time for one device."""
    device = _one(project, args.path)
    if device is None:
        return 1
    try:
        timer, index = schedule.add_time(stack, device, args.time, args.action,
                                         schedule.parse_days(args.days), args.active)
    except (ValueError, schedule.ScheduleError, girax1.X1Error, TimeoutError) as e:
        print(f'{project.path(device)}: {e}', file=sys.stderr)
        return 1

    entry = next(e for e in timer.switch_times if e.index == index)
    print(f'{project.path(device)}  {entry.index}  {entry.when}  {entry.days}  '
          f'{"on" if entry.active else "off"}  {_scene(project, timer, entry)}')
    return 0


def command_deltime(stack: Stack, project: Project, args: argparse.Namespace) -> int:
    """Delete one programmed switch time."""
    device = _one(project, args.path)
    if device is None:
        return 1
    try:
        timer = schedule.remove_time(stack, device, args.index, args.keep_scenes)
    except (schedule.ScheduleError, girax1.X1Error, TimeoutError) as e:
        print(f'{project.path(device)}: {e}', file=sys.stderr)
        return 1

    remaining = ', '.join(str(e.index) for e in timer.switch_times) or 'none'
    print(f'{project.path(device)}: switch time {args.index} deleted, remaining: {remaining}')
    return 0


def _one(project: Project, path: str) -> Device | None:
    """Return the single device a path names, complaining when it is not exactly one."""
    matches = project.resolve(path)
    if len(matches) != 1:
        print(f'{path!r} matches {len(matches)} devices, name exactly one', file=sys.stderr)
        return None
    return matches[0]


def command_settime(stack: Stack, project: Project, args: argparse.Namespace) -> int:
    """Change one programmed switch time of one device."""
    if args.time is None and args.days is None and args.active is None and args.scene is None:
        print('nothing to change; give a time, --days, --active or --scene', file=sys.stderr)
        return 1

    device = _one(project, args.path)
    if device is None:
        return 1
    try:
        days = None if args.days is None else schedule.parse_days(args.days)
        timer = schedule.change_time(stack, device, args.index, when=args.time, days=days,
                                     active=args.active, scene=args.scene)
    except (ValueError, schedule.ScheduleError, girax1.X1Error, TimeoutError) as e:
        print(f'{project.path(device)}: {e}', file=sys.stderr)
        return 1

    entry = next(e for e in timer.switch_times if e.index == args.index)
    print(f'{project.path(device)}  {entry.index}  {entry.when}  {entry.days}  '
          f'{"on" if entry.active else "off"}  {_scene(project, timer, entry)}')
    if timer.next_trigger:
        print(f'next {timer.next_trigger}')
    return 0


def command_gui(stack: Stack, project: Project, args: argparse.Namespace) -> int:
    """Show the graphical user interface.

    Tk is imported only here so that the command line commands keep working on
    a machine where it is not installed.
    """
    try:
        from girax1 import gui
    except ImportError as e:
        print(f'the graphical interface needs Tk: {e}', file=sys.stderr)
        return 1
    return gui.run(stack, project)


def command_timers(stack: Stack, project: Project, args: argparse.Namespace) -> int:
    """Print the switch times programmed for the selected devices."""
    devices, failed = _select(project, args.paths) if args.paths else (list(project), False)

    shown = 0
    for device in devices:
        if not device.timer_channel or (not device.programmed_slots and not args.all):
            continue
        try:
            timer = stack.timer(device.timer_channel)
        except girax1.X1Error as e:
            print(f'{project.path(device)}: {e}', file=sys.stderr)
            failed = True
            continue

        shown += 1
        following = f'next {timer.next_trigger}' if timer.next_trigger else 'nothing due'
        print(f'{project.path(device)}   [{device.kind}]   {following}')
        if not timer.switch_times:
            print('   no switch times programmed')
        for entry in timer.switch_times:
            state = 'on ' if entry.active else 'off'
            print(f'   {entry.index}  {entry.when:<9} {entry.days:<16} {state}  '
                  f'{_scene(project, timer, entry)}')
            if args.long:
                print(f'      scene {entry.trigger_value}, {entry.recurrence}, '
                      f'valid from {entry.begin_date} ({entry.validity})')
        print()

    if not shown:
        print('no timer programming found' if not args.all else 'no timer channels found')
    return 1 if failed else 0


def _scene(project: Project, timer, entry) -> str:
    """Describe what a switch time does, resolved through its scene."""
    scene = timer.scene(entry)
    if scene is None:
        return f'scene {entry.trigger_value} (unknown)'
    actions = ', '.join(
        f'{project.point_kind(a.point_id) or a.point_id} '
        f'{_state(project.point_kind(a.point_id), a.value)}'
        + (f' after {a.delay}s' if a.delay else '')
        for a in scene.actions)
    return actions or f'scene {scene.number} {scene.name!r}'


def _percent(text: str) -> int:
    """Parse and range check a percentage given on the command line."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f'{text!r} is not a whole number') from None
    if not 0 <= value <= BRIGHTNESS_MAX:
        raise argparse.ArgumentTypeError(f'has to be a percentage between 0 and {BRIGHTNESS_MAX}')
    return value


def command_dim(stack: Stack, project: Project, args: argparse.Namespace) -> int:
    """Set the brightness of every named light.

    A device named on its own is expected to be dimmable, so saying that it is
    not is useful.  One picked up by a room or a pattern is passed over in
    silence, because a room is mostly switches and coverings and complaining
    about each would bury the line that matters.
    """
    devices, failed = _select(project, args.paths)
    named = _named(project, args.paths)
    for device in devices:
        point = device.point(BRIGHTNESS_POINT)
        if point is None or not point.usable:
            if device.view_id in named:
                print(f'{project.path(device)}: no {BRIGHTNESS_POINT} data point, '
                      f'a {device.kind} cannot be dimmed', file=sys.stderr)
                failed = True
            continue
        try:
            reply = stack.dim(point.point_id, args.percent)
        except girax1.X1Error as e:
            print(f'{project.path(device)}: {e}', file=sys.stderr)
            failed = True
            continue
        print(f'{project.path(device)} {_state(BRIGHTNESS_POINT, str(reply.get("value")))}')
    return 1 if failed else 0


def _record_blinds(stack: Stack, project: Project) -> None:
    """Give every blind an entry for its travel time, so the file can be corrected.

    The default is only a guess, and a guess nobody can see is a guess nobody
    will fix; writing the entries out puts the number where it can be measured
    and corrected.
    """
    blinds = [project.path(d) for d in project
              if (p := d.point(COVER_POINT)) is not None and p.usable]
    if not blinds or stack.config.source is None:
        return
    try:
        added = girax1.ensure_travel_entries(stack.config.source, blinds,
                                             stack.config.travel_default)
    except OSError as e:
        print(f'cannot record the blinds in {stack.config.source}: {e}', file=sys.stderr)
        return
    if added:
        print(f'added {len(added)} blind{"" if len(added) == 1 else "s"} to '
              f'{stack.config.source} at {stack.config.travel_default:g}s each; '
              'correct the entries to match your blinds', file=sys.stderr)
        stack.config.travel.update(dict.fromkeys(added, stack.config.travel_default))


def _wait(stack: Stack, seconds: float) -> None:
    """Wait, while keeping the connection to the device alive.

    Sleeping outright would leave the pings the device sends every few seconds
    unanswered, and it drops a connection that stops replying.  Polling answers
    them as a side effect.
    """
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        stack.poll(timeout=min(left, 1.0))


def command_close(stack: Stack, project: Project, args: argparse.Namespace) -> int:
    """Close the named blinds by a percentage of their travel.

    Nothing reports where a blind stands, so the position is reached by timing:
    the blind is driven fully open first, which is the one position that can be
    relied upon, and then closed for the matching fraction of its travel time.
    """
    devices, failed = _select(project, args.paths)
    named = _named(project, args.paths)

    blinds = []
    for device in devices:
        move, halt = device.point(COVER_POINT), device.point(STOP_POINT)
        if move is None or not move.usable or halt is None or not halt.usable:
            if device.view_id in named:
                print(f'{project.path(device)}: a {device.kind} cannot be closed by a '
                      'percentage', file=sys.stderr)
                failed = True
            continue
        blinds.append((device, move, halt))
    if not blinds:
        return 1 if failed else 0

    travel = {d.view_id: stack.config.travel_seconds(project.path(d)) for d, _, _ in blinds}

    try:
        # Drive fully open first: that is the only position known without feedback.
        for device, move, _ in blinds:
            stack.move(move.point_id, False)
        opening = max(travel.values())
        print(f'opening {len(blinds)} blind{"" if len(blinds) == 1 else "s"}, '
              f'waiting {opening:g}s')
        _wait(stack, opening)

        if args.percent <= 0:
            for device, _, _ in blinds:
                print(f'{project.path(device)} open')
            return 1 if failed else 0

        # Close them together and halt each one after its own share of the travel.
        started = time.monotonic()
        for device, move, _ in blinds:
            stack.move(move.point_id, True)
        schedule = sorted((travel[d.view_id] * args.percent / 100, order, d, halt)
                          for order, (d, _, halt) in enumerate(blinds))
        for deadline, _, device, halt in schedule:
            _wait(stack, deadline - (time.monotonic() - started))
            if args.percent < 100:
                stack.stop(halt.point_id)
            print(f'{project.path(device)} closed {args.percent}% '
                  f'({deadline:.1f}s of {travel[device.view_id]:g}s)')
        if args.percent >= 100:
            print('left running to the end of travel')
    except girax1.X1Error as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
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
        except girax1.X1Error as e:
            print(f'{project.path(device)}: {e}', file=sys.stderr)
            failed = True
            continue
        print(f'{project.path(device)} {args.action}')
    return 1 if failed else 0


def _named(project: Project, patterns: list[str]) -> set[int]:
    """Return the devices that were named on their own rather than swept up.

    A pattern containing a wildcard, and a room written with a trailing slash,
    select whatever happens to be there.  Anything else names one device.
    """
    named: set[int] = set()
    for pattern in patterns:
        if pattern.endswith('/') or any(c in pattern for c in WILDCARDS):
            continue
        named.update(d.view_id for d in project.resolve(pattern))
    return named


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
    if name == SWITCH_POINT:
        return {'0': 'off', '1': 'on'}.get(value, value)
    if name == BRIGHTNESS_POINT:
        return f'{value}%'
    if name == COVER_POINT:
        return {'0': 'up', '1': 'down'}.get(value, value)
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
