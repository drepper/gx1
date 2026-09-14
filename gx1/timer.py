"""Switch times, the timer programming of a Gira X1.

Every function of a project owns a timer channel of type
``de.gira.schema.channels.FunctionTimer``.  Reading that channel with
``GetConfiguration`` returns two parts that belong together:

``timer.switchTimes``
    when to act, as a time of day plus a mask of weekdays, and which scene to
    apply given as ``triggerValue``.

``sceneSet.scenes``
    what to do, as a list of numbered scenes, each writing one or more data
    points to a fixed value.

A switch time is therefore only meaningful together with the scene its trigger
value names, which is why :class:`Timer` keeps both and resolves one to the
other.
"""

from dataclasses import dataclass
from typing import Any, Self

# Position of every day in the weekday mask, which starts on Monday.
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')

# The only time type seen so far; others are passed through untranslated.
POINT_IN_TIME = 'pointInTime'


@dataclass(frozen=True, slots=True)
class SceneAction:
    """One data point a scene writes."""

    point_id: int
    value: str
    urn: str = ''
    delay: int = 0


@dataclass(frozen=True, slots=True)
class Scene:
    """A numbered set of data point values a switch time can apply."""

    number: int
    name: str
    actions: tuple[SceneAction, ...] = ()


@dataclass(frozen=True, slots=True)
class SwitchTime:
    """One programmed entry of a timer."""

    index: int
    active: bool
    time: str
    weekdays: str
    trigger_value: int
    time_type: str = POINT_IN_TIME
    recurrence: str = ''
    validity: str = ''
    begin_date: str = ''

    @property
    def days(self) -> str:
        """Return the weekday mask as readable text such as ``Mon-Fri``."""
        if len(self.weekdays) != len(WEEKDAYS):
            return self.weekdays
        active = [i for i, c in enumerate(self.weekdays) if c == '1']
        if not active:
            return 'never'
        if len(active) == len(WEEKDAYS):
            return 'daily'
        runs: list[tuple[int, int]] = []
        start = previous = active[0]
        for day in active[1:]:
            if day == previous + 1:
                previous = day
                continue
            runs.append((start, previous))
            start = previous = day
        runs.append((start, previous))
        return ','.join(WEEKDAYS[a] if a == b else f'{WEEKDAYS[a]}-{WEEKDAYS[b]}'
                        for a, b in runs)

    @property
    def when(self) -> str:
        """Return the time, marked up when it is not a plain time of day."""
        return self.time if self.time_type == POINT_IN_TIME else f'{self.time} ({self.time_type})'


@dataclass(frozen=True, slots=True)
class Timer:
    """The timer channel of one function."""

    channel_id: int
    name: str = ''
    next_trigger: str = ''
    switch_times: tuple[SwitchTime, ...] = ()
    scenes: dict[int, Scene] | None = None

    @classmethod
    def parse(cls, configuration: dict[str, Any]) -> Self:
        """Build a timer from the answer to ``GetConfiguration`` for its channel."""
        timer = configuration.get('timer', {}) or {}
        scenes = {}
        for scene in (configuration.get('sceneSet', {}) or {}).get('scenes', []):
            number = _int(scene.get('number'))
            scenes[number] = Scene(
                number=number, name=str(scene.get('name', '')),
                actions=tuple(SceneAction(point_id=_int(d.get('id')), value=str(d.get('value', '')),
                                          urn=str(d.get('urn', '')), delay=_int(d.get('delay')))
                              for d in scene.get('datapoints', [])))
        return cls(channel_id=_int(configuration.get('id')),
                   name=str(configuration.get('displayName') or configuration.get('name', '')),
                   next_trigger=str(timer.get('nextTriggerTime') or ''),
                   switch_times=tuple(_switch_time(s) for s in timer.get('switchTimes', [])),
                   scenes=scenes)

    @property
    def programmed(self) -> bool:
        """Return whether any switch time is configured at all."""
        return bool(self.switch_times)

    def scene(self, switch_time: SwitchTime) -> Scene | None:
        """Return the scene a switch time applies, if it is known."""
        return (self.scenes or {}).get(switch_time.trigger_value)


def _switch_time(entry: dict[str, Any]) -> SwitchTime:
    """Convert one raw switch time into an object."""
    return SwitchTime(index=_int(entry.get('index')),
                      active=str(entry.get('active', '')).lower() == 'true',
                      time=str(entry.get('time', '')),
                      weekdays=str(entry.get('weekdays', '')),
                      trigger_value=_int(entry.get('triggerValue')),
                      time_type=str(entry.get('timeType', POINT_IN_TIME)),
                      recurrence=str(entry.get('recurrence', '')),
                      validity=str(entry.get('validity', '')),
                      begin_date=str(entry.get('beginDate', '')))


def _int(value: Any) -> int:
    """Convert a field the device sends as a string into a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
