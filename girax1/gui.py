"""Graphical user interface for a Gira X1.

The entry screen shows one tile per room.  Choosing a room shows one tile per
device in it, with the current status, controls that suit the kind of device and
the timer programming in small print.

Tk is used because it is part of the standard library, which keeps the program
free of a dependency just for the interface.

Values are kept up to date from the events the device pushes, so the display
also follows changes made from a wall switch or from the Gira app.
"""

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any

import girax1
from .project import Device, Project
from .stack import BRIGHTNESS_MAX, Stack
from .timer import Timer

# Data points the interface knows how to present.
SWITCH_POINT = 'OnOff'
BRIGHTNESS_POINT = 'Brightness'
COVER_POINT = 'Up-Down'
STOP_POINT = 'Step-Up-Down'

# Data points that only ever carry a momentary command.  Their value says
# nothing about the device, so the status line leaves them out.
TRIGGER_POINTS = frozenset({STOP_POINT, 'Shift'})

DEVICE_COLUMNS = 3
ROOM_COLUMNS = 3
BRIGHTNESS_STEP = 5
POLL_INTERVAL_MS = 400
POLL_TIMEOUT = 0.02
TIMER_LINES = 3

# Devices not assigned to any room are collected under this heading.
OTHER_ROOM = 'Elsewhere'


class DeviceTile(ttk.Frame):
    """One device, with its status, its controls and its timer programming."""

    def __init__(self, master: tk.Misc, app: 'Application', device: Device,
                 timer: Timer | None) -> None:
        """Build the tile for one device."""
        super().__init__(master, padding=10, relief='ridge', borderwidth=1)
        self.app = app
        self.device = device
        self.columnconfigure(0, weight=1)

        ttk.Label(self, text=device.name, font=app.bold).grid(row=0, column=0, sticky='w')
        ttk.Label(self, text=device.kind, font=app.small,
                  foreground='grey40').grid(row=1, column=0, sticky='w')
        self.status = ttk.Label(self, text='', font=app.normal)
        self.status.grid(row=2, column=0, sticky='w', pady=(6, 0))

        controls = ttk.Frame(self)
        controls.grid(row=3, column=0, sticky='w', pady=(6, 0))
        self.scale: ttk.Scale | None = None
        self.level: ttk.Label | None = None
        # The On and Off button, keyed by the state they select.  The one
        # matching the current state is highlighted.
        self.switches: dict[bool, ttk.Button] = {}
        # Data points whose value the controls already show, so the status line
        # leaves them out.
        self.shown: set[str] = set()
        self._build_controls(controls)

        text = _timer_text(timer, app.project)
        if text:
            ttk.Label(self, text=text, font=app.small, foreground='grey30',
                      justify='left').grid(row=5, column=0, sticky='w', pady=(8, 0))

    def _build_controls(self, parent: ttk.Frame) -> None:
        """Add the buttons and sliders that suit this kind of device."""
        if (point := self.device.point(SWITCH_POINT)) is not None and point.usable:
            for on, text in ((True, 'On'), (False, 'Off')):
                button = ttk.Button(parent, text=text, width=5,
                                    command=lambda on=on: self.app.switch(self.device, on))
                button.pack(side='left', padx=(0, 4))
                self.switches[on] = button
            self.shown.add(SWITCH_POINT)
        if (point := self.device.point(COVER_POINT)) is not None and point.usable:
            ttk.Button(parent, text='Up', width=5,
                       command=lambda: self.app.move(self.device, False)).pack(side='left')
            ttk.Button(parent, text='Stop', width=5,
                       command=lambda: self.app.stop(self.device)).pack(side='left', padx=(4, 0))
            ttk.Button(parent, text='Down', width=5,
                       command=lambda: self.app.move(self.device, True)).pack(side='left',
                                                                              padx=(4, 0))
        if (point := self.device.point(BRIGHTNESS_POINT)) is not None and point.usable:
            row = ttk.Frame(self)
            row.grid(row=4, column=0, sticky='ew', pady=(8, 0))
            row.columnconfigure(0, weight=1)
            self.scale = ttk.Scale(row, from_=0, to=BRIGHTNESS_MAX, orient='horizontal')
            self.scale.grid(row=0, column=0, sticky='ew')
            # Writing on every step of a drag would flood the bus, so the value
            # is only sent once the slider is let go of.
            self.scale.bind('<ButtonRelease-1>', self._commit_brightness)
            self.scale.configure(command=self._preview_brightness)
            self.level = ttk.Label(row, text='-', font=self.app.small, width=5, anchor='e')
            self.level.grid(row=0, column=1, padx=(6, 0))
            self.shown.add(BRIGHTNESS_POINT)

    def _preview_brightness(self, value: str) -> None:
        """Show the value under the slider while it is being dragged."""
        if self.level is not None:
            self.level.configure(text=f'{round(float(value))}%')

    def _commit_brightness(self, _event: tk.Event) -> None:
        """Send the brightness the slider was left at."""
        if self.scale is not None:
            self.app.dim(self.device, int(round(self.scale.get() / BRIGHTNESS_STEP)
                                          * BRIGHTNESS_STEP))

    def refresh(self, values: dict[str, str]) -> None:
        """Update the controls and the status line from the current values."""
        parts = []
        for point in self.device.usable_points:
            value = values.get(str(point.point_id))
            if point.name == SWITCH_POINT:
                for on, button in self.switches.items():
                    selected = value == ('1' if on else '0')
                    button.configure(style='Selected.TButton' if selected else 'TButton')
            if point.name == BRIGHTNESS_POINT and self.scale is not None:
                self.app.without_commit(self.scale, _number(value))
                if self.level is not None:
                    self.level.configure(text=_readable(point.name, value))
            if point.name not in TRIGGER_POINTS and point.name not in self.shown:
                parts.append(f'{point.name} {_readable(point.name, value)}')
        if parts or not self.device.usable_points:
            self.status.configure(text=', '.join(parts) or 'no data points')
            self.status.grid()
        else:
            self.status.grid_remove()


class Application(tk.Tk):
    """The main window, switching between the room and the device view."""

    def __init__(self, stack: Stack, project: Project) -> None:
        """Set up the window and show the rooms."""
        super().__init__()
        self.stack = stack
        self.project = project
        self.values: dict[str, str] = {}
        self.timers: dict[int, Timer | None] = {}
        self.tiles: list[DeviceTile] = []

        self.title('Gira X1')
        self.geometry('960x680')
        self.minsize(640, 480)

        self.normal = tkfont.nametofont('TkDefaultFont').copy()
        self.bold = self.normal.copy()
        self.bold.configure(weight='bold', size=self.normal.cget('size') + 1)
        self.small = self.normal.copy()
        self.small.configure(size=max(self.normal.cget('size') - 2, 7))

        # The button for the state a device is in stands out from the other.
        style = ttk.Style(self)
        style.configure('Selected.TButton', font=self.bold, foreground='white',
                        background='#2e7d32')
        style.map('Selected.TButton', background=[('active', '#388e3c')],
                  foreground=[('active', 'white')])

        self.body = ttk.Frame(self, padding=12)
        self.body.pack(fill='both', expand=True)
        self.message = ttk.Label(self, text='', font=self.small, anchor='w', padding=(12, 4))
        self.message.pack(fill='x', side='bottom')

        self.rooms = self._rooms()
        self.reload_values()
        self.show_rooms()
        self.after(POLL_INTERVAL_MS, self._pump)

    # ------------------------------------------------------------------ views

    def show_rooms(self) -> None:
        """Show one tile per room."""
        self.tiles = []
        self._clear()
        ttk.Label(self.body, text='Rooms', font=self.bold).grid(row=0, column=0,
                                                                columnspan=ROOM_COLUMNS,
                                                                sticky='w', pady=(0, 10))
        for index, (name, devices) in enumerate(self.rooms):
            tile = ttk.Frame(self.body, padding=16, relief='ridge', borderwidth=1)
            tile.grid(row=1 + index // ROOM_COLUMNS, column=index % ROOM_COLUMNS,
                      sticky='nsew', padx=6, pady=6)
            ttk.Label(tile, text=name, font=self.bold).pack(anchor='w')
            ttk.Label(tile, text=f'{len(devices)} device' + ('' if len(devices) == 1 else 's'),
                      font=self.small, foreground='grey40').pack(anchor='w')
            ttk.Label(tile, text=self._summary(devices), font=self.small).pack(anchor='w',
                                                                              pady=(6, 0))
            ttk.Button(tile, text='Open',
                       command=lambda n=name: self.show_room(n)).pack(anchor='w', pady=(10, 0))
        for column in range(ROOM_COLUMNS):
            self.body.columnconfigure(column, weight=1)

    def show_room(self, name: str) -> None:
        """Show one tile per device of the named room."""
        devices = dict(self.rooms).get(name, [])
        self._load_timers(devices)
        self.tiles = []
        self._clear()

        header = ttk.Frame(self.body)
        header.grid(row=0, column=0, columnspan=DEVICE_COLUMNS, sticky='ew', pady=(0, 10))
        ttk.Button(header, text='\N{LEFTWARDS ARROW} Rooms',
                   command=self.show_rooms).pack(side='left')
        ttk.Label(header, text=name, font=self.bold).pack(side='left', padx=(12, 0))

        for index, device in enumerate(devices):
            tile = DeviceTile(self.body, self, device, self.timers.get(device.view_id))
            tile.grid(row=1 + index // DEVICE_COLUMNS, column=index % DEVICE_COLUMNS,
                      sticky='nsew', padx=6, pady=6)
            self.tiles.append(tile)
        for column in range(DEVICE_COLUMNS):
            self.body.columnconfigure(column, weight=1)
        self.refresh()

    def _clear(self) -> None:
        """Remove everything from the body of the window."""
        for child in self.body.winfo_children():
            child.destroy()
        for column in range(max(ROOM_COLUMNS, DEVICE_COLUMNS)):
            self.body.columnconfigure(column, weight=0)

    # --------------------------------------------------------------- commands

    def switch(self, device: Device, on: bool) -> None:
        """Switch a device on or off."""
        self._write(device, SWITCH_POINT, lambda i: self.stack.switch(i, on))

    def move(self, device: Device, down: bool) -> None:
        """Drive a covering."""
        self._write(device, COVER_POINT, lambda i: self.stack.move(i, down))

    def stop(self, device: Device) -> None:
        """Halt a covering."""
        self._write(device, STOP_POINT, lambda i: self.stack.stop(i))

    def dim(self, device: Device, percent: int) -> None:
        """Set the brightness of a light."""
        self._write(device, BRIGHTNESS_POINT, lambda i: self.stack.dim(i, percent))

    def _write(self, device: Device, point_name: str, action) -> None:
        """Run one write, reporting a refusal instead of letting it escape."""
        point = device.point(point_name)
        if point is None or not point.usable:
            self.note(f'{device.name} has no {point_name} data point')
            return
        try:
            reply = action(point.point_id)
        except (girax1.X1Error, TimeoutError) as e:
            self.note(f'{device.name}: {e}')
            return
        self.values[str(point.point_id)] = str(reply.get('value', ''))
        self.note(f'{device.name}: {point_name} {_readable(point_name, str(reply.get("value")))}')
        self.refresh()

    # ---------------------------------------------------------------- updates

    def reload_values(self) -> None:
        """Fetch the current value of every data point."""
        try:
            self.values = {str(d.get('id')): str(d.get('state'))
                           for d in self.stack.process_view()}
        except (girax1.X1Error, TimeoutError) as e:
            self.note(f'cannot read the current values: {e}')

    def refresh(self) -> None:
        """Update every visible tile."""
        for tile in self.tiles:
            tile.refresh(self.values)

    def note(self, text: str) -> None:
        """Show a message in the status line."""
        self.message.configure(text=text)

    def without_commit(self, scale: ttk.Scale, value: float) -> None:
        """Move a slider to follow the device.

        Only letting go of the slider writes a value, so moving it here cannot
        be mistaken for the user choosing one.
        """
        scale.set(value)

    def _pump(self) -> None:
        """Take the events the device pushed and update the display."""
        changed = False
        try:
            while (event := self.stack.poll(timeout=POLL_TIMEOUT)) is not None:
                if event.get('type') != 'Value':
                    continue
                value = event.get('value', {})
                self.values[str(value.get('id'))] = str(value.get('new'))
                changed = True
        except (girax1.X1Error, TimeoutError) as e:
            self.note(f'lost the connection to the device: {e}')
            return
        if changed:
            self.refresh()
        self.after(POLL_INTERVAL_MS, self._pump)

    # ---------------------------------------------------------------- helpers

    def _rooms(self) -> list[tuple[str, list[Device]]]:
        """Return the rooms with their devices, plus anything left over."""
        rooms = [(group.name, self.project.members(group))
                 for group in self.project.groupings().get('Location', [])]
        if rest := self.project.ungrouped('Location'):
            rooms.append((OTHER_ROOM, rest))
        return rooms

    def _summary(self, devices: list[Device]) -> str:
        """Describe how many devices of a room are currently switched on."""
        switches = [d for d in devices if (p := d.point(SWITCH_POINT)) is not None and p.usable]
        if not switches:
            return f'{len(devices)} without a switch'
        on = sum(1 for d in switches
                 if self.values.get(str(d.point(SWITCH_POINT).point_id)) == '1')
        return f'{on} of {len(switches)} switched on'

    def _load_timers(self, devices: list[Device]) -> None:
        """Fetch the timer programming of a room, remembering what was read."""
        for device in devices:
            if device.view_id in self.timers or not device.timer_channel:
                continue
            if not device.programmed_slots:
                self.timers[device.view_id] = None
                continue
            try:
                self.timers[device.view_id] = self.stack.timer(device.timer_channel)
            except (girax1.X1Error, TimeoutError):
                self.timers[device.view_id] = None


def _timer_text(timer: Timer | None, project: Project) -> str:
    """Render the timer programming as a few lines of small print."""
    if timer is None or not timer.switch_times:
        return ''
    lines = []
    for entry in timer.switch_times[:TIMER_LINES]:
        scene = timer.scene(entry)
        if scene is None:
            what = f'scene {entry.trigger_value}'
        else:
            what = ', '.join(_readable(project.point_kind(a.point_id), a.value)
                             for a in scene.actions)
        mark = '' if entry.active else '  (inactive)'
        lines.append(f'{entry.time[:5]}  {entry.days}  {what}{mark}')
    if len(timer.switch_times) > TIMER_LINES:
        lines.append(f'... and {len(timer.switch_times) - TIMER_LINES} more')
    if timer.next_trigger:
        lines.append(f'next {timer.next_trigger[:16]}')
    return '\n'.join(lines)


def _readable(name: str, value: Any) -> str:
    """Render a data point value the way a person would read it."""
    if value is None or value == '' or value == 'None':
        return '?'
    text = str(value)
    if name == SWITCH_POINT:
        return {'0': 'off', '1': 'on'}.get(text, text)
    if name == BRIGHTNESS_POINT:
        return f'{text}%'
    if name == COVER_POINT:
        return {'0': 'up', '1': 'down'}.get(text, text)
    return text


def _number(value: Any) -> int:
    """Convert a value to a number, treating anything unusable as zero."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def run(stack: Stack, project: Project) -> int:
    """Show the interface and return once the window is closed."""
    Application(stack, project).mainloop()
    return 0
