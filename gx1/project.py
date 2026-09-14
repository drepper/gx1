"""Structured view of the project configuration of a Gira X1.

:meth:`gx1.stack.Stack.ui_configuration` returns a flat list mixing two kinds of
entry: one per configured function, and a single ``Root`` entry holding a tree
that groups those functions by trade and by location.  This module turns both
into objects that are pleasant to work with.
"""

from dataclasses import dataclass, field
from typing import Any, Iterator, Self

# Nodes of the Root tree that group functions together.
GROUPING_TYPES = ('Trade', 'Location', 'Zone', 'Profile')


@dataclass(frozen=True, slots=True)
class DataPoint:
    """One readable or writable value of a function."""

    name: str
    point_id: int
    urn: str = ''

    @property
    def usable(self) -> bool:
        """Return whether the data point is bound to a group address.

        Unbound points are reported by the device with an id of zero and cannot
        be read or written.
        """
        return self.point_id != 0


@dataclass(frozen=True, slots=True)
class Device:
    """One configured function, for example a lamp or a blind."""

    name: str
    view_id: int
    view_urn: str = ''
    function_type: str = ''
    channel_type: str = ''
    data_points: tuple[DataPoint, ...] = ()

    @property
    def kind(self) -> str:
        """Return the short function type, for example ``Switch`` or ``Covering``."""
        return self.function_type.rsplit('.', 1)[-1] or 'Unknown'

    @property
    def usable_points(self) -> tuple[DataPoint, ...]:
        """Return only the data points that are bound to a group address."""
        return tuple(p for p in self.data_points if p.usable)

    def point(self, name: str) -> DataPoint | None:
        """Return the named data point, comparing case insensitively."""
        return next((p for p in self.data_points if p.name.lower() == name.lower()), None)


@dataclass(frozen=True, slots=True)
class Group:
    """A set of devices that the project groups together."""

    kind: str
    subtype: str
    name: str
    members: tuple[int, ...] = ()

    @property
    def label(self) -> str:
        """Return a name that stays unambiguous when two groups share a name."""
        return f'{self.name} ({self.subtype})' if self.subtype != self.name else self.name


@dataclass(slots=True)
class Project:
    """Everything the device knows about its own configuration."""

    devices: dict[int, Device] = field(default_factory=dict)
    groups: list[Group] = field(default_factory=list)
    guid: str = ''

    @classmethod
    def parse(cls, configuration: list[dict[str, Any]]) -> Self:
        """Build a project from the raw answer to ``GetUIConfiguration``."""
        project = cls()
        for entry in configuration:
            if 'guid' in entry and len(entry) == 1:
                project.guid = str(entry['guid'])
            elif entry.get('mainType') == 'Root':
                project.groups = list(_groups(entry))
            elif entry.get('channelViewID') is not None and entry.get('name'):
                device = _device(entry)
                project.devices[device.view_id] = device
        return project

    def __iter__(self) -> Iterator[Device]:
        """Iterate over the devices in configuration order."""
        return iter(self.devices.values())

    def __len__(self) -> int:
        """Return the number of configured devices."""
        return len(self.devices)

    def find(self, name: str) -> Device | None:
        """Return the device with the given name, compared case insensitively."""
        return next((d for d in self.devices.values() if d.name.lower() == name.lower()), None)

    def groupings(self) -> dict[str, list[Group]]:
        """Return the non-empty groups by kind, for example ``Trade`` or ``Location``."""
        result: dict[str, list[Group]] = {}
        for group in self.groups:
            if group.members:
                result.setdefault(group.kind, []).append(group)
        return result

    def members(self, group: Group) -> list[Device]:
        """Return the devices of a group, skipping references to unknown ones."""
        return [self.devices[i] for i in group.members if i in self.devices]

    def ungrouped(self, kind: str) -> list[Device]:
        """Return the devices that no group of the given kind contains."""
        grouped = {i for g in self.groups if g.kind == kind for i in g.members}
        return [d for i, d in self.devices.items() if i not in grouped]


def _device(entry: dict[str, Any]) -> Device:
    """Convert one configuration entry into a device."""
    points = tuple(DataPoint(name=str(p.get('dataPoint', '')),
                             point_id=int(p.get('id') or 0),
                             urn=str(p.get('urn', '')))
                   for p in entry.get('dataPoints', []))
    return Device(name=str(entry['name']), view_id=int(entry['channelViewID']),
                  view_urn=str(entry.get('channelViewUrn', '')),
                  function_type=str(entry.get('functionType', '')),
                  channel_type=str(entry.get('channelType', '')), data_points=points)


def _groups(node: dict[str, Any]) -> Iterator[Group]:
    """Walk the Root tree and yield every node that collects devices."""
    children = node.get('content', []) or []
    members = tuple(int(c['channelViewID']) for c in children
                    if c.get('channelViewID') is not None and c.get('mainType') is None)
    kind = str(node.get('mainType', ''))
    if members and kind in GROUPING_TYPES:
        yield Group(kind=kind, subtype=str(node.get('subType', '')),
                    name=str(node.get('name', '')), members=members)
    for child in children:
        if child.get('mainType') is not None:
            yield from _groups(child)
