"""Programming the switch times of a Gira X1.

A switch time does not name a value to write but a scene of its timer channel,
see :mod:`girax1.timer`.  Adding or changing one therefore starts from an
action such as ``on`` or a brightness, looks for the scene that carries it out
and, on a channel that has no scenes yet, creates the set the Gira app would
create.  The command line program and the graphical interface both program
switch times through the functions here.
"""

from .project import BRIGHTNESS_POINT, COVER_POINT, SWITCH_POINT, DataPoint, Device
from .stack import BRIGHTNESS_MAX, Stack
from .timer import WEEKDAYS, Timer, write_days

# Day names and groups accepted in a days specification.
DAY_NAMES = {name.lower(): index for index, name in enumerate(WEEKDAYS)}
DAY_GROUPS = {'daily': set(range(7)), 'everyday': set(range(7)),
              'weekdays': {0, 1, 2, 3, 4}, 'weekend': {5, 6}}

# What each action writes, and the scenes to create when a channel has none.
# The names follow the ones the Gira app itself uses.
ACTIONS = {'on': (SWITCH_POINT, '1', 'switch'), 'off': (SWITCH_POINT, '0', 'switch'),
           'up': (COVER_POINT, '0', 'move'), 'down': (COVER_POINT, '1', 'move')}
DEFAULT_SCENES = {'switch': [(1, 'switch-1', '1'), (2, 'switch-0', '0')],
                  'move': [(1, 'move-0', '0'), (2, 'move-1', '1')],
                  'dim': [(i + 1, f'dim-{v}', str(v))
                          for i, v in enumerate(range(0, BRIGHTNESS_MAX + 1, 10))]}

# The steps of brightness a channel without scenes gets scenes for.
BRIGHTNESS_STEPS = tuple(int(value) for _, _, value in DEFAULT_SCENES['dim'])


class ScheduleError(Exception):
    """The device did not program a switch time the way it was asked to."""


def parse_days(text: str) -> set[int]:
    """Parse a days specification such as ``Mon-Fri``, ``Mon,Wed`` or ``weekend``.

    Days are counted from Monday as zero.  A malformed specification raises
    :class:`ValueError`.
    """
    if (group := DAY_GROUPS.get(text.lower())) is not None:
        return set(group)
    days: set[int] = set()
    for part in text.split(','):
        first, sep, last = part.strip().partition('-')
        try:
            start = DAY_NAMES[first.strip().lower()]
            stop = DAY_NAMES[last.strip().lower()] if sep else start
        except KeyError:
            raise ValueError(f'{part.strip()!r} is not a day; '
                             f'use names such as Mon or a range Mon-Fri') from None
        days.update(range(start, stop + 1) if start <= stop
                    else list(range(start, 7)) + list(range(0, stop + 1)))
    return days


def parse_time(text: str) -> str:
    """Normalise a time of day to the HH:MM:SS the device stores.

    A malformed time raises :class:`ValueError`.
    """
    parts = text.strip().split(':')
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f'{text!r} is not a time of day such as 06:30')
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise ValueError(f'{text!r} is not a valid time of day')
    return f'{hour:02d}:{minute:02d}:{second:02d}'


def action(device: Device, text: str) -> tuple[DataPoint, str, str]:
    """Return the data point, the value and the scene family an action names.

    An action is ``on``, ``off``, ``up``, ``down`` or a brightness, which may
    carry a trailing percent sign.  One the device cannot carry out raises
    :class:`ValueError`.
    """
    word = text.strip().lower().removesuffix('%').strip()
    if (known := ACTIONS.get(word)) is not None:
        name, value, family = known
    elif word.isdigit() and 0 <= int(word) <= BRIGHTNESS_MAX:
        name, value, family = BRIGHTNESS_POINT, str(int(word)), 'dim'
    else:
        raise ValueError(f'{text!r} is not an action; use on, off, up, down '
                         f'or a brightness from 0 to {BRIGHTNESS_MAX}')
    point = device.point(name)
    if point is None or not point.usable:
        raise ValueError(f'a {device.kind} has no {name} data point, so it cannot do {text!r}')
    return point, value, family


def actions(device: Device, timer: Timer | None = None) -> list[str]:
    """Return the actions a device can carry out, as offered for a switch time.

    Given the timer of the device, a channel that already has scenes only
    offers what those scenes carry out, since no others are created for it.
    """
    result = [word for word, (name, _, _) in ACTIONS.items()
              if (point := device.point(name)) is not None and point.usable]
    if (point := device.point(BRIGHTNESS_POINT)) is not None and point.usable:
        result.extend(f'{step}%' for step in BRIGHTNESS_STEPS)
    if timer is None or not timer.scenes:
        return result
    usable = []
    for word in result:
        point, value, _ = action(device, word)
        if scene_for(timer, point.point_id, value) is not None:
            usable.append(word)
    return usable


def describe(device: Device, timer: Timer, index: int) -> str | None:
    """Return the action a switch time carries out, if a single one describes it.

    The result is in the form :func:`actions` offers.  A scene writing several
    data points, or one not known for the device, gives ``None``.
    """
    entry = next((e for e in timer.switch_times if e.index == index), None)
    if entry is None or (scene := timer.scene(entry)) is None or len(scene.actions) != 1:
        return None
    step = scene.actions[0]
    point = next((p for p in device.usable_points if p.point_id == step.point_id), None)
    if point is None:
        return None
    if point.name.lower() == BRIGHTNESS_POINT.lower():
        return f'{_number(step.value)}%'
    return next((word for word, (name, value, _) in ACTIONS.items()
                 if name.lower() == point.name.lower() and _same_value(value, step.value)),
                None)


def scene_for(timer: Timer, point_id: int, value: str) -> int | None:
    """Return the number of the scene writing this value, if one exists."""
    for number, scene in sorted((timer.scenes or {}).items()):
        if any(a.point_id == point_id and _same_value(a.value, value) for a in scene.actions):
            return number
    return None


def add_time(stack: Stack, device: Device, when: str, what: str, days: set[int],
             active: bool = False) -> tuple[Timer, int]:
    """Program a new switch time and return the timer read back with its index.

    ``when`` and ``what`` are a time of day and an action as :func:`parse_time`
    and :func:`action` accept them.
    """
    channel = _channel(device)
    point, value, family = action(device, what)
    time = parse_time(when)
    timer, scene = _scene(stack, channel, point, value, family, what)
    index = max((e.index for e in timer.switch_times), default=0) + 1
    stack.add_switch_time(channel, index, time=time, weekdays=write_days(days),
                          trigger_value=scene, active=active)
    timer = stack.timer(channel)
    if not any(e.index == index for e in timer.switch_times):
        raise ScheduleError('the device did not keep the new switch time')
    return timer, index


def change_time(stack: Stack, device: Device, index: int, *, when: str | None = None,
                days: set[int] | None = None, active: bool | None = None,
                what: str | None = None, scene: int | None = None) -> Timer:
    """Change one switch time and return the timer read back.

    Only what is given is changed.  The action can be given as ``what``, in
    the form :func:`action` accepts, or directly as the number of a scene.
    """
    channel = _channel(device)
    timer = stack.timer(channel)
    _check_index(timer, index)
    fields: dict[str, object] = {}
    if when is not None:
        fields['time'] = parse_time(when)
        fields['timeType'] = 'pointInTime'
    if days is not None:
        fields['weekdays'] = write_days(days)
        fields['recurrence'] = 'daily'
    if active is not None:
        fields['active'] = 'true' if active else 'false'
    if what is not None:
        point, value, family = action(device, what)
        _, scene = _scene(stack, channel, point, value, family, what)
    if scene is not None:
        fields['triggerValue'] = str(scene)
    if not fields:
        return timer
    stack.set_switch_time(channel, index, **fields)
    return stack.timer(channel)


def remove_time(stack: Stack, device: Device, index: int, keep_scenes: bool = False) -> Timer:
    """Delete one switch time and return the timer read back.

    The scenes go along with the last switch time, as the app does it, unless
    ``keep_scenes`` is set.
    """
    channel = _channel(device)
    _check_index(stack.timer(channel), index)
    stack.remove_switch_time(channel, index)
    timer = stack.timer(channel)
    if not timer.switch_times and not keep_scenes:
        stack.clear_scenes(channel)
        timer = stack.timer(channel)
    return timer


def _channel(device: Device) -> int:
    """Return the timer channel of a device, complaining when it has none."""
    if not device.timer_channel:
        raise ScheduleError(f'{device.name} has no timer channel')
    return device.timer_channel


def _check_index(timer: Timer, index: int) -> None:
    """Complain unless the timer has a switch time with this index."""
    if not any(e.index == index for e in timer.switch_times):
        available = ', '.join(str(e.index) for e in timer.switch_times) or 'none'
        raise ScheduleError(f'there is no switch time {index}; programmed: {available}')


def _scene(stack: Stack, channel: int, point: DataPoint, value: str, family: str,
           what: str) -> tuple[Timer, int]:
    """Return the timer and the number of the scene writing a value.

    A channel without scenes first gets the set the app would create for it.
    """
    timer = stack.timer(channel)
    if (scene := scene_for(timer, point.point_id, value)) is not None:
        return timer, scene
    if timer.scenes:
        raise ScheduleError(f'no scene writes {what!r}; '
                            f'the channel already has scenes, so none are created')
    stack.set_scene_set(channel, point.point_id, DEFAULT_SCENES[family])
    timer = stack.timer(channel)
    if (scene := scene_for(timer, point.point_id, value)) is None:
        raise ScheduleError('the device did not take the scenes')
    return timer, scene


def _same_value(one: str, other: str) -> bool:
    """Compare two data point values, which the device may spell differently."""
    try:
        return int(float(one)) == int(float(other))
    except (TypeError, ValueError):
        return str(one) == str(other)


def _number(value: str) -> int:
    """Convert a value to a number, treating anything unusable as zero."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
