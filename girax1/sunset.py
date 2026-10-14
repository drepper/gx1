"""Switch times which follow the sunset or the sunrise.

The times of sunset and sunrise move through the year, while a switch time of
the X1 stays where it was programmed.  A switch time can therefore be tied to
the sunset or the sunrise, with an offset in minutes, and is then moved after
it from time to time: on the days and at the time of day chosen, the sunset
and the sunrise of the day are computed for the location of the house, and
every such switch time is set to its event plus its offset.  Everything else
about it, the days it acts on, whether it is active and what it does, stays
as programmed.  Any device with a timer can have such switch times: lights and
switches go on at sunset and off at sunrise, coverings go down at sunset and
up at sunrise.

The settings live in the ``[sunset]`` table of the configuration file.  The
switch times tied to the sunset are listed in ``[sunset.times]``, by device,
each with its index, its offset and what it does; the latter only serves to
program it anew should it have been deleted on the X1.

    [sunset]
    location = "Berlin, Deutschland"
    latitude = 52.5174
    longitude = 13.3951
    days = "Mon,Thu"
    time = "12:00"

    [sunset.times]
    "Flur/Deckenlampen Flur" = [{index = 3, offset = 15, action = "on"}]
    "Küche/Rollo links" = [{index = 1, offset = 20, action = "down"},
                           {index = 2, event = "sunrise", offset = -20, action = "up",
                            earliest = "06:00"}]

A switch time follows the sunset unless its event says sunrise.  A covering
going up has an earliest time as well, 06:00 unless another is chosen: it is
not raised before then, whatever the sunrise plus the offset comes to.

A location given as a description is looked up once with the geocoder of
OpenStreetMap, which needs a connection to the internet, and its coordinates
are kept next to it.  The adjustments are run by a timer of the systemd
service manager of the user, see :func:`install_timer`, but only on the
computer whose configuration says it is the control node:

    control-node = true

Several computers can share a configuration that way while only one of them
reprograms the X1.
"""

import json
import math
import os
import re
import shutil
import subprocess
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import girax1
from . import config, schedule
from .project import COVER_POINT, Device, Project
from .stack import Stack
from .timer import Timer

SECTION = 'sunset'
TIMES = 'sunset.times'

# The key saying whether this computer adjusts the switch times.
CONTROL_NODE = 'control-node'

DEFAULT_DAYS = 'daily'
DEFAULT_TIME = '12:00'

# An offset of more than this many minutes would no longer be about the sunset.
MAX_OFFSET = 720

# The events a switch time can follow.
SUNSET = 'sunset'
SUNRISE = 'sunrise'
EVENTS = (SUNSET, SUNRISE)

# The offset a switch time gets unless another is chosen: lights are switched
# on a little after the sunset, coverings closed once it is darker.  At sunrise
# it is the same the other way round, at the same light, before the sunrise.
LIGHT_OFFSET = 10
COVERING_OFFSET = 20

# What switches off or opens follows the sunrise, anything else the sunset.
MORNING_ACTIONS = frozenset({'off', 'up', '0%', '0'})

# Coverings are not raised before this time of day, whatever the sunset plus
# the offset comes to, unless another time is chosen for the switch time.
EARLIEST_UP = '06:00'
UP = 'up'

# The adjustments leave a switch time alone unless the sunset plus its offset
# has moved away from it by more than this many minutes, so that the X1 is not
# reprogrammed for every minute the sunset moves.
MIN_CHANGE = 10

# The sun counts as set once its upper edge is below the horizon, which with
# refraction is the case at this angle from the zenith.
SUNSET_ZENITH = 90.833

# Where descriptions of locations are looked up, and how long that may take.
GEOCODER = 'https://nominatim.openstreetmap.org/search'
GEOCODER_TIMEOUT = 15

# The units of the service manager which run the adjustments.
TIMER_NAME = 'girax1-sunset'
UNIT_DIRECTORY = (Path(os.environ.get('XDG_CONFIG_HOME', '~/.config')).expanduser()
                  / 'systemd' / 'user')

# A switch time programmed anew, because it was deleted, acts every day.
EVERY_DAY = set(range(7))

# How a time is written to follow the sun: "sunset", "sunset+15", "sunrise-10".
SUN_TIME = re.compile(rf'\s*({"|".join(EVENTS)})\s*(?:([+-])\s*(\d+))?\s*', re.IGNORECASE)

COORDINATES = re.compile(r'\s*([-+]?\d+(?:\.\d+)?)\s*[,;\s]\s*([-+]?\d+(?:\.\d+)?)\s*')


class SunsetError(Exception):
    """The sunset settings are incomplete or invalid, or cannot be acted on."""


class NotControlNode(SunsetError):
    """This computer is not the one which adjusts the switch times."""


@dataclass(frozen=True, slots=True)
class Link:
    """A switch time which follows the sunset or the sunrise."""

    index: int
    offset: int = 0
    # What the switch time does, as schedule.action takes it; empty if unknown.
    action: str = ''
    # The time of day as HH:MM before which the switch time never acts, or
    # empty; only coverings going up have one.
    earliest: str = ''
    # What the switch time follows, the sunset or the sunrise.
    event: str = SUNSET

    @property
    def text(self) -> str:
        """Return when the switch time acts, as in ``sunrise-20, not before 06:00``."""
        return when_text(self.event, self.offset) + (
            f', not before {self.earliest}' if self.earliest else '')


@dataclass(frozen=True, slots=True)
class Settings:
    """The sunset settings of the configuration file."""

    location: str = ''
    latitude: float | None = None
    longitude: float | None = None
    days: frozenset[int] = frozenset(EVERY_DAY)
    time: str = schedule.parse_time(DEFAULT_TIME)
    # The switch times following the sunset, by device path.
    times: dict[str, tuple[Link, ...]] = field(default_factory=dict)
    # Whether this computer adjusts the switch times following the sun.
    control_node: bool = False

    @property
    def located(self) -> bool:
        """Return whether the coordinates of the location are known."""
        return self.latitude is not None and self.longitude is not None

    @property
    def calendar(self) -> str:
        """Return when the adjustments run, in the notation of systemd timers."""
        days = '' if self.days == EVERY_DAY else ','.join(
            schedule.WEEKDAYS[d] for d in sorted(self.days)) + ' '
        return f'{days}*-*-* {self.time}'

    def link(self, path: str, index: int) -> Link | None:
        """Return how a switch time follows the sunset, if it does."""
        return next((link for link in self.times.get(path, ()) if link.index == index), None)


@dataclass(frozen=True, slots=True)
class Outcome:
    """What moving one switch time came to."""

    path: str
    link: Link
    time: str = ''
    # Whether the switch time was reprogrammed; it is not if it was close enough.
    moved: bool = False
    error: str = ''


def parse_when(text: str) -> tuple[str, int | None] | None:
    """Return the event and the offset if a time is given as following the sun, else None.

    ``sunset+15`` is a quarter of an hour after the sunset, ``sunrise-10`` ten
    minutes before the sunrise and ``sunset+0`` the sunset itself.  Without an
    offset, as in ``sunset``, the offset is None; see :func:`default_offset`.
    """
    if (match := SUN_TIME.fullmatch(text)) is None:
        return None
    event = match[1].lower()
    if match[3] is None:
        return event, None
    offset = int(match[3]) * (-1 if match[2] == '-' else 1)
    if abs(offset) > MAX_OFFSET:
        raise SunsetError(f'an offset of {offset} minutes is more than {MAX_OFFSET}')
    return event, offset


def event_for(action: str) -> str:
    """Return what a switch time doing an action follows: switching off or opening
    follows the sunrise, anything else the sunset."""
    return SUNRISE if action.strip().lower() in MORNING_ACTIONS else SUNSET


def default_offset(device: Device, event: str = SUNSET) -> int:
    """Return the offset a switch time of a device gets unless another is chosen."""
    point = device.point(COVER_POINT)
    offset = COVERING_OFFSET if point is not None and point.usable else LIGHT_OFFSET
    return offset if event == SUNSET else -offset


def when_text(event: str, offset: int) -> str:
    """Return how a time following the sun is written, as in ``sunset+15``."""
    return event if offset == 0 else f'{event}{offset:+d}'


def load(source: Path) -> Settings:
    """Read the sunset settings from the configuration file."""
    try:
        with source.open('rb') as fp:
            data = tomllib.load(fp)
    except OSError as e:
        raise SunsetError(f'cannot read {source}: {e.strerror}') from e
    except tomllib.TOMLDecodeError as e:
        raise SunsetError(f'{source}: {e}') from e
    table = data.get(SECTION, {})
    if not isinstance(table, dict):
        raise SunsetError(f'{source}: [{SECTION}] must be a table')

    def number(key: str, low: float, high: float) -> float | None:
        value = table.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not low <= value <= high:
            raise SunsetError(f'{source}: {SECTION}.{key} must be a number from {low} to {high}')
        return float(value)

    try:
        days = schedule.parse_days(str(table.get('days', DEFAULT_DAYS)))
        time = schedule.parse_time(str(table.get('time', DEFAULT_TIME)))
    except ValueError as e:
        raise SunsetError(f'{source}: {SECTION}: {e}') from None
    times = table.get('times', {})
    if not isinstance(times, dict):
        raise SunsetError(f'{source}: [{TIMES}] must be a table')
    links = {}
    for path, entries in times.items():
        try:
            links[path] = tuple(_link(entry) for entry in entries)
        except (KeyError, TypeError, ValueError):
            raise SunsetError(f'{source}: {TIMES}."{path}" must be a list of tables with an '
                              f'index, an offset in minutes and an action') from None
    if not isinstance(control_node := table.get(CONTROL_NODE, False), bool):
        raise SunsetError(f'{source}: {SECTION}.{CONTROL_NODE} must be true or false')
    return Settings(location=str(table.get('location', '')),
                    latitude=number('latitude', -90, 90),
                    longitude=number('longitude', -180, 180),
                    days=frozenset(days), time=time, times=links, control_node=control_node)


def configure(source: Path, *, location: str | None = None,
              place: tuple[float, float, str] | None = None,
              days: set[int] | None = None, time: str | None = None,
              control_node: bool | None = None) -> Settings:
    """Change the general sunset settings and return them as they are then.

    A location is either coordinates, as in ``52.52, 13.40``, or a description,
    which is looked up with :func:`locate`.  A place is what :func:`locate`
    returned for one already.  What is not given stays as it is.  Changing
    whether this computer is the control node does not install or remove the
    timer; see :func:`sync_timer`.
    """
    entries: list[tuple[str, str, Any]] = []
    if place is None and location is not None:
        place = locate(location)
    if place is not None:
        latitude, longitude, description = place
        entries += [(SECTION, 'location', description), (SECTION, 'latitude', latitude),
                    (SECTION, 'longitude', longitude)]
    if days is not None:
        if not days:
            raise SunsetError('choose at least one day for the adjustments')
        entries.append((SECTION, 'days', schedule.days_text(days)))
    if time is not None:
        try:
            entries.append((SECTION, 'time', schedule.parse_time(time)[:5]))
        except ValueError as e:
            raise SunsetError(str(e)) from None
    if control_node is not None:
        entries.append((SECTION, CONTROL_NODE, control_node))
    if entries:
        _write(source, entries)
    return load(source)


def locate(text: str) -> tuple[float, float, str]:
    """Return the latitude, the longitude and a description of a location.

    Coordinates are taken as they are.  Anything else is looked up with the
    geocoder of OpenStreetMap, which sends the text there.
    """
    if (match := COORDINATES.fullmatch(text)) is not None:
        latitude, longitude = float(match[1]), float(match[2])
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise SunsetError(f'{text!r} are no valid coordinates')
        return latitude, longitude, f'{latitude:.4f}, {longitude:.4f}'
    if not text.strip():
        raise SunsetError('the location is empty')
    query = urllib.parse.urlencode({'q': text, 'format': 'json', 'limit': '1'})
    request = urllib.request.Request(
        f'{GEOCODER}?{query}', headers={'User-Agent': f'girax1/{girax1.__version__}'})
    try:
        with urllib.request.urlopen(request, timeout=GEOCODER_TIMEOUT) as response:
            places = json.load(response)
    except (OSError, ValueError) as e:
        raise SunsetError(f'cannot look up {text!r}: {e}') from e
    if not places:
        raise SunsetError(f'no place called {text!r} is known')
    place = places[0]
    return (round(float(place['lat']), 4), round(float(place['lon']), 4),
            str(place.get('display_name', text)))


def sunset(day: date, latitude: float, longitude: float) -> datetime | None:
    """Return the time of sunset on a day, in UTC, or None if the sun does not set."""
    return _sun(day, latitude, longitude, rising=False)


def sunrise(day: date, latitude: float, longitude: float) -> datetime | None:
    """Return the time of sunrise on a day, in UTC, or None if the sun does not rise."""
    return _sun(day, latitude, longitude, rising=True)


def _sun(day: date, latitude: float, longitude: float, rising: bool) -> datetime | None:
    """Return the time of sunrise or sunset on a day, in UTC, or None if there is none.

    The equations are those of the solar calculator of the NOAA Global
    Monitoring Laboratory, accurate to about a minute away from the polar
    regions.  They are evaluated for the moment of the previous result until
    that settles.  Longitudes count positive to the east.
    """
    julian_day = day.toordinal() + 1721424.5
    phi = math.radians(latitude)
    minutes = 720.0
    for _ in range(3):
        t = (julian_day + minutes / 1440 - 2451545.0) / 36525
        mean_longitude = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
        anomaly = math.radians(357.52911 + t * (35999.05029 - 0.0001537 * t))
        eccentricity = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
        centre = (math.sin(anomaly) * (1.914602 - t * (0.004817 + 0.000014 * t))
                  + math.sin(2 * anomaly) * (0.019993 - 0.000101 * t)
                  + math.sin(3 * anomaly) * 0.000289)
        omega = math.radians(125.04 - 1934.136 * t)
        apparent = math.radians(mean_longitude + centre - 0.00569 - 0.00478 * math.sin(omega))
        mean_obliquity = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813)))
                               / 60) / 60
        obliquity = math.radians(mean_obliquity + 0.00256 * math.cos(omega))
        declination = math.asin(math.sin(obliquity) * math.sin(apparent))
        y = math.tan(obliquity / 2) ** 2
        l0 = math.radians(mean_longitude)
        equation = 4 * math.degrees(
            y * math.sin(2 * l0) - 2 * eccentricity * math.sin(anomaly)
            + 4 * eccentricity * y * math.sin(anomaly) * math.cos(2 * l0)
            - 0.5 * y * y * math.sin(4 * l0) - 1.25 * eccentricity ** 2 * math.sin(2 * anomaly))
        cos_angle = (math.cos(math.radians(SUNSET_ZENITH)) / (math.cos(phi) * math.cos(declination))
                     - math.tan(phi) * math.tan(declination))
        if not -1 <= cos_angle <= 1:
            return None
        # The sun is as far before noon at sunrise as after it at sunset.
        angle = math.degrees(math.acos(cos_angle))
        minutes = 720 - 4 * (longitude + (angle if rising else -angle)) - equation
    return datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(minutes=minutes)


def time_at(settings: Settings, offset: int, day: date | None = None,
            event: str = SUNSET) -> tuple[datetime, datetime]:
    """Return the sunset or sunrise of a day and it plus an offset, both in local time.

    Both are rounded to the minute, as switch times are kept.  The local time
    is the one of this computer, which is assumed to be the one of the X1.
    Without a day it is today.
    """
    if settings.latitude is None or settings.longitude is None:
        raise SunsetError('the location of the house is not set')
    day = day or date.today()
    when = _sun(day, settings.latitude, settings.longitude, rising=event == SUNRISE)
    if when is None:
        raise SunsetError(f'there is no {event} on {day} at this location')
    local = (when.astimezone() + timedelta(seconds=30)).replace(second=0, microsecond=0)
    return local, local + timedelta(minutes=offset)


def switch_time(settings: Settings, offset: int, earliest: str = '',
                day: date | None = None, event: str = SUNSET) -> str:
    """Return the time of day as HH:MM a switch time following the sun acts at.

    That is the sunset or sunrise plus the offset, but not before the earliest
    time if there is one.
    """
    _, when = time_at(settings, offset, day, event)
    time = when.strftime('%H:%M')
    return max(time, earliest) if earliest else time


def earliest_for(action: str, earliest: str | None = None) -> str:
    """Return the earliest time a switch time doing an action may act at, as HH:MM.

    Only coverings going up have one: the one given, or :data:`EARLIEST_UP`.
    """
    if action.strip().lower() != UP:
        return ''
    try:
        return schedule.parse_time(earliest or EARLIEST_UP)[:5]
    except ValueError as e:
        raise SunsetError(str(e)) from None


def add_time(stack: Stack, source: Path, device: Device, path: str, offset: int, what: str,
             days: set[int], active: bool = False, earliest: str | None = None,
             event: str | None = None) -> tuple[Timer, int]:
    """Program a new switch time following the sun; return the timer and its index.

    Without an event it is the one which suits the action, see :func:`event_for`.
    A covering going up gets an earliest time, the one given or the default.
    """
    event = event or event_for(what)
    earliest = earliest_for(what, earliest)
    time = switch_time(load(source), offset, earliest, event=event)
    timer, index = schedule.add_time(stack, device, time, what, days, active)
    link(source, path, Link(index, offset, what, earliest, event))
    return timer, index


def change_time(stack: Stack, source: Path, device: Device, path: str, index: int,
                offset: int, *, days: set[int] | None = None, active: bool | None = None,
                what: str | None = None, earliest: str | None = None,
                event: str | None = None) -> Timer:
    """Make a switch time follow the sun, or change how, and return the timer.

    Without an event given it is the one which suits the action, and without
    an earliest time given, one the switch time had is kept.
    """
    settings = load(source)
    previous = settings.link(path, index)
    timer = stack.timer(device.timer_channel)
    action = (what or schedule.describe(device, timer, index)
              or (previous.action if previous else ''))
    event = event or event_for(action)
    earliest = earliest_for(action, earliest or (previous.earliest if previous else None))
    timer = schedule.change_time(stack, device, index,
                                 when=switch_time(settings, offset, earliest, event=event),
                                 days=days, active=active, what=what)
    link(source, path, Link(index, offset, action, earliest, event))
    return timer


def link(source: Path, path: str, new: Link) -> None:
    """Record that a switch time follows the sunset, replacing what was recorded for it."""
    links = [old for old in load(source).times.get(path, ()) if old.index != new.index]
    _store(source, path, sorted([*links, new], key=lambda entry: entry.index))


def unlink(source: Path, path: str, index: int) -> None:
    """Record that a switch time no longer follows the sunset."""
    links = load(source).times.get(path, ())
    if any(old.index == index for old in links):
        _store(source, path, [old for old in links if old.index != index])


def adjust(stack: Stack, project: Project, source: Path) -> list[Outcome]:
    """Move every switch time following the sun to today's sunset or sunrise plus its offset.

    A switch time is only reprogrammed when it is more than :data:`MIN_CHANGE`
    minutes away from where it belongs.  One deleted on the X1 is programmed
    anew, acting every day, and the index it then has is recorded.  Only the
    control node adjusts; anywhere else :class:`NotControlNode` is raised.
    """
    settings = load(source)
    if not settings.control_node:
        raise NotControlNode(f'this computer is not the control node; set '
                             f'{SECTION}.{CONTROL_NODE} to true in {source} to adjust here')
    devices = project.paths()
    outcomes = []
    for path, links in settings.times.items():
        device = devices.get(path)
        kept = []
        for old in links:
            if device is None:
                outcomes.append(Outcome(path, old, error='no such device in the project'))
                kept.append(old)
                continue
            try:
                time = switch_time(settings, old.offset, old.earliest, event=old.event)
                new, moved = _move(stack, device, old, time)
            except (ValueError, SunsetError, schedule.ScheduleError, girax1.X1Error,
                    TimeoutError) as e:
                outcomes.append(Outcome(path, old, error=str(e)))
                kept.append(old)
                continue
            outcomes.append(Outcome(path, new, time, moved))
            kept.append(new)
        if kept != list(links):
            _store(source, path, kept)
    return outcomes


def install_timer(source: Path, program: Path, settings: Settings) -> None:
    """Have the service manager of the user run the adjustments when they are due.

    A run missed while the computer was off is made up for once it is on
    again.  Without lingering enabled for the user (``loginctl enable-linger``)
    the timer only runs while the user is logged in.
    """
    if shutil.which('systemctl') is None:
        raise SunsetError('there is no systemd to run the adjustments')
    UNIT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    (UNIT_DIRECTORY / f'{TIMER_NAME}.service').write_text(
        '[Unit]\n'
        'Description=Move the Gira X1 switch times of lights after the sunset\n'
        '\n'
        '[Service]\n'
        'Type=oneshot\n'
        f'ExecStart="{program}" -c "{source.resolve()}" sunset run\n', encoding='utf-8')
    (UNIT_DIRECTORY / f'{TIMER_NAME}.timer').write_text(
        '[Unit]\n'
        'Description=Move the Gira X1 switch times of lights after the sunset\n'
        '\n'
        '[Timer]\n'
        f'OnCalendar={settings.calendar}\n'
        'Persistent=true\n'
        '\n'
        '[Install]\n'
        'WantedBy=timers.target\n', encoding='utf-8')
    _systemctl('daemon-reload')
    _systemctl('enable', '--now', f'{TIMER_NAME}.timer')
    # A timer already running keeps its old schedule until it is restarted.
    _systemctl('restart', f'{TIMER_NAME}.timer')


def uninstall_timer() -> None:
    """Stop running the adjustments and remove the units."""
    if timer_installed():
        _systemctl('disable', '--now', f'{TIMER_NAME}.timer')
    for suffix in ('timer', 'service'):
        (UNIT_DIRECTORY / f'{TIMER_NAME}.{suffix}').unlink(missing_ok=True)
    if shutil.which('systemctl') is not None:
        _systemctl('daemon-reload')


def timer_installed() -> bool:
    """Return whether the units running the adjustments are in place."""
    return (UNIT_DIRECTORY / f'{TIMER_NAME}.timer').is_file()


def ensure_timer(source: Path, program: Path) -> bool:
    """Install the timer running the adjustments on the control node unless it is there.

    Returns whether it was installed now.
    """
    if timer_installed() or not (settings := load(source)).control_node:
        return False
    install_timer(source, program, settings)
    return True


def sync_timer(source: Path, program: Path) -> str:
    """Install or update the timer on the control node, and remove it anywhere else.

    Returns what was done: ``installed``, ``removed`` or an empty string.
    """
    if load(source).control_node:
        install_timer(source, program, load(source))
        return 'installed'
    if timer_installed():
        uninstall_timer()
        return 'removed'
    return ''


def _move(stack: Stack, device: Device, old: Link, time: str) -> tuple[Link, bool]:
    """Set a switch time to a time unless it is close to it already.

    Returns how the switch time follows the sunset afterwards, which only
    differs when it had to be programmed anew because it was deleted, and
    whether it was reprogrammed.
    """
    timer = stack.timer(device.timer_channel)
    if (entry := next((e for e in timer.switch_times if e.index == old.index), None)) is not None:
        if (apart := _minutes_apart(entry.time, time)) is not None and apart <= MIN_CHANGE:
            return old, False
        schedule.change_time(stack, device, old.index, when=time)
        return old, True
    if not old.action:
        raise schedule.ScheduleError(f'switch time {old.index} was deleted, and what it did '
                                     f'is not known')
    _, index = schedule.add_time(stack, device, time, old.action, EVERY_DAY, active=True)
    return Link(index, old.offset, old.action, old.earliest, old.event), True


def _minutes_apart(one: str, other: str) -> int | None:
    """Return how many minutes two times of day are apart, None if one is no time.

    Times on either side of midnight count as close.
    """
    try:
        minutes = [int(t.split(':')[0]) * 60 + int(t.split(':')[1]) for t in (one, other)]
    except (IndexError, ValueError):
        return None
    apart = abs(minutes[0] - minutes[1])
    return min(apart, 24 * 60 - apart)


def _link(entry: Any) -> Link:
    """Convert one recorded switch time from the configuration file."""
    index, offset, action = entry['index'], entry.get('offset', 0), entry.get('action', '')
    earliest, event = entry.get('earliest', ''), entry.get('event', SUNSET)
    if not (isinstance(index, int) and isinstance(offset, int) and isinstance(action, str)
            and isinstance(earliest, str)) or event not in EVENTS \
            or isinstance(index, bool) or isinstance(offset, bool) or index <= 0 \
            or abs(offset) > MAX_OFFSET:
        raise ValueError(entry)
    # Raises ValueError for anything but a time of day.
    return Link(index, offset, action, schedule.parse_time(earliest)[:5] if earliest else '',
                event)


def _store(source: Path, path: str, links: list[Link]) -> None:
    """Write the switch times of a device which follow the sunset."""
    value = [{'index': entry.index}
             | ({'event': entry.event} if entry.event != SUNSET else {})
             | {'offset': entry.offset, 'action': entry.action}
             | ({'earliest': entry.earliest} if entry.earliest else {})
             for entry in links] or None
    _write(source, [(TIMES, config.toml_key(path), value)])


def _write(source: Path, entries: list[tuple[str, str, Any]]) -> None:
    """Write entries to the configuration file, reporting failures as sunset errors."""
    try:
        config.write_entries(source, entries)
    except config.ConfigError as e:
        raise SunsetError(str(e)) from None


def _systemctl(*arguments: str) -> None:
    """Run systemctl for the service manager of the user."""
    try:
        subprocess.run(('systemctl', '--user', *arguments), capture_output=True, text=True,
                       check=True)
    except subprocess.CalledProcessError as e:
        raise SunsetError(f'systemctl {" ".join(arguments)} failed: '
                          f'{e.stderr.strip() or e.returncode}') from e
