"""Adding, changing and deleting switch times, at a time of day or following the sun.

A switch time is given a time either as a time of day, ``06:30``, or as
following the sunset or the sunrise, ``sunset+15`` or ``sunrise-20``; see
:mod:`girax1.sunset`.  The functions here take either and do what is needed
for it, so that the command line program and the commands the graphical
interface takes over MQTT behave alike.
"""

from __future__ import annotations

from pathlib import Path

from . import schedule, sunset
from .project import Device
from .stack import Stack
from .timer import Timer


def add(stack: Stack, source: Path, device: Device, path: str, when: str, action: str,
        days: set[int], active: bool, not_before: str | None, program: Path) -> tuple[Timer, int]:
    """Program a new switch time for a device; return the timer read back and its index.

    ``when`` is a time of day or a time following the sun, ``action`` what the
    switch time does as :func:`schedule.action` takes it, and ``not_before`` the
    earliest time of a covering going up at sunrise.  ``source`` is the
    configuration file, which records switch times following the sun, and
    ``program`` what the timer adjusting them runs.
    """
    if (sun := sunset.parse_when(when)) is None:
        return schedule.add_time(stack, device, when, action, days, active)
    event, offset = sun
    if offset is None:
        offset = sunset.default_offset(device, event)
    result = sunset.add_time(stack, source, device, path, offset, action, days, active,
                             not_before, event)
    sunset.ensure_timer(source, program)
    return result



def change(stack: Stack, source: Path, device: Device, path: str, index: int, *,
           when: str | None = None, days: set[int] | None = None, active: bool | None = None,
           action: str | None = None, scene: int | None = None,
           not_before: str | None = None, program: Path) -> Timer:
    """Change a switch time of a device, only in what is given; return the timer read back.

    A time following the sun ties the switch time to it, a time of day unties
    it.  A bare ``sunset`` or ``sunrise`` keeps the offset the switch time has,
    or gets the default one.  An earliest time alone changes that of a switch
    time following the sun, and a switch time following the sun keeps doing so
    when only its days, its activity or its action change; an action which
    follows the other event moves it there, with that event's default offset.
    """
    if all(v is None for v in (when, days, active, action, scene, not_before)):
        raise ValueError('nothing to change; give a time, days, active, an action, a scene or '
                         'an earliest time')
    link = sunset.load(source).link(path, index)
    sun = None if when is None else sunset.parse_when(when)
    if sun is not None:
        event, offset = sun
        if offset is None:
            offset = (link.offset if link is not None and link.event == event
                      else sunset.default_offset(device, event))
    elif not_before is not None:
        # The earliest time only matters to a switch time following the sun.
        if when is not None or link is None:
            raise sunset.SunsetError('an earliest time needs a time following the sun')
        event, offset = link.event, link.offset
    elif link is not None and when is None:
        # A switch time following the sun keeps doing so, even with another action.
        event, offset = link.event, link.offset
        if action and (moved := sunset.event_for(action)) != event:
            event, offset = moved, sunset.default_offset(device, moved)
    else:
        event = offset = None
    if offset is None:
        timer = schedule.change_time(stack, device, index, when=when, days=days, active=active,
                                     what=action, scene=scene)
        if when is not None:
            sunset.unlink(source, path, index)
        return timer
    if scene is not None:
        schedule.change_time(stack, device, index, scene=scene)
    timer = sunset.change_time(stack, source, device, path, index, offset, days=days,
                               active=active, what=action, earliest=not_before, event=event)
    sunset.ensure_timer(source, program)
    return timer



def delete(stack: Stack, source: Path, device: Device, path: str, index: int,
           keep_scenes: bool = False) -> Timer:
    """Delete a switch time of a device and its tie to the sun; return the timer read back."""
    timer = schedule.remove_time(stack, device, index, keep_scenes)
    sunset.unlink(source, path, index)
    return timer
