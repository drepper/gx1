"""Graphical user interface for a Gira X1.

The entry screen shows one tile per room.  Choosing a room shows one tile per
device in it, with the current status, controls that suit the kind of device and
all its switch times.  Clicking on those opens a window in which they can be
added, changed and removed.

Tk is used because it is part of the standard library, which keeps the program
free of a dependency just for the interface.

Values are kept up to date from the events the device pushes, so the display
also follows changes made from a wall switch or from the Gira app.
"""

import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Any

import girax1
from . import schedule
from .project import (BRIGHTNESS_POINT, COVER_POINT, STOP_POINT, SWITCH_POINT, Device,
                      Project)
from .stack import BRIGHTNESS_MAX, Stack
from .timer import WEEKDAYS, SwitchTime, Timer

# Data points that only ever carry a momentary command.  Their value says
# nothing about the device, so the status line leaves them out.
TRIGGER_POINTS = frozenset({STOP_POINT, 'Shift'})

DEVICE_COLUMNS = 3
ROOM_COLUMNS = 3
BRIGHTNESS_STEP = 5
POLL_INTERVAL_MS = 400
POLL_TIMEOUT = 0.02

# Background of the switch times of a device, normally and under the pointer.
TIMES_BACKGROUND = '#f4f4f4'
TIMES_HOVER = '#e2e8f0'
TIMES_SHOWN = 10

# What can go wrong while programming a switch time.
SCHEDULE_ERRORS = (ValueError, schedule.ScheduleError, girax1.X1Error, TimeoutError)

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

        # The switch times, all of them, in an area that opens the window to
        # change them when clicked.  The next time due is shown below it.
        self.times: tk.Frame | None = None
        self.next = ttk.Label(self, text='', font=app.small, foreground='grey30')
        if device.timer_channel:
            self.times = tk.Frame(self, background=TIMES_BACKGROUND, relief='groove',
                                  borderwidth=2, padx=10, pady=10, cursor='hand2',
                                  takefocus=True, highlightthickness=1)
            self.times.grid(row=5, column=0, sticky='ew', pady=(8, 0))
            self.times.bind('<Return>', self._open_times)
            self.times.bind('<space>', self._open_times)
            self.next.grid(row=6, column=0, sticky='w', pady=(4, 0))
        self.show_timer(timer)

    def show_timer(self, timer: Timer | None) -> None:
        """Lay out the switch times and the next time due."""
        if self.times is None:
            return
        for child in self.times.winfo_children():
            child.destroy()
        rows = [] if timer is None else [
            (entry.when[:5], entry.days, _what(timer, entry, self.app.project, self.device),
             '' if entry.active else 'inactive')
            for entry in sorted(timer.switch_times, key=lambda e: e.time)]
        if not rows:
            tk.Label(self.times, text='No switch times', font=self.app.normal,
                     foreground='grey30', background=TIMES_BACKGROUND).grid(row=0, column=0,
                                                                            sticky='w')
        for row, cells in enumerate(rows):
            for column, text in enumerate(cells):
                tk.Label(self.times, text=text, font=self.app.small,
                         foreground='grey50' if cells[-1] else 'black',
                         background=TIMES_BACKGROUND).grid(row=row, column=column, sticky='w',
                                                           padx=(0, 10))
        # A click anywhere in the area counts, including on the text in it.
        for widget in (self.times, *self.times.winfo_children()):
            widget.bind('<Button-1>', self._open_times)
            widget.bind('<Enter>', lambda _: self._hover(True))
            widget.bind('<Leave>', lambda _: self._hover(False))

        if timer is not None and timer.next_trigger:
            self.next.configure(text=f'next {timer.next_trigger[:16]}')
            self.next.grid()
        else:
            self.next.grid_remove()

    def _hover(self, inside: bool) -> None:
        """Shade the switch times while the pointer is over them."""
        if self.times is None:
            return
        colour = TIMES_HOVER if inside else TIMES_BACKGROUND
        for widget in (self.times, *self.times.winfo_children()):
            widget.configure(background=colour)

    def _open_times(self, _event: tk.Event) -> None:
        """Open the window to change the switch times."""
        TimesDialog(self.app, self.device)

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
                    button.configure(style=('On.TButton' if on else 'Off.TButton')
                                     if selected else 'TButton')
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

        # The button for the state a device is in stands out from the other,
        # green when it is on and red when it is off.
        style = ttk.Style(self)
        for name, normal, active in (('On', '#2e7d32', '#388e3c'),
                                     ('Off', '#c62828', '#d32f2f')):
            style.configure(f'{name}.TButton', font=self.bold, foreground='white',
                            background=normal)
            style.map(f'{name}.TButton', background=[('active', active)],
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

    def update_timer(self, device: Device, timer: Timer) -> None:
        """Remember timer programming read back after a change and show it."""
        self.timers[device.view_id] = timer
        for tile in self.tiles:
            if tile.device.view_id == device.view_id:
                tile.show_timer(timer)

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


class TimesDialog(tk.Toplevel):
    """The switch times of one device, with the means to change them."""

    def __init__(self, app: 'Application', device: Device) -> None:
        """Open the window and read the current programming."""
        super().__init__(app)
        self.app = app
        self.device = device
        self.timer: Timer | None = None
        self.title(f'{device.name} \N{EN DASH} switch times')
        self.transient(app)

        body = ttk.Frame(self, padding=12)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        ttk.Label(body, text=device.name, font=app.bold).grid(row=0, column=0, sticky='w')

        self.table = ttk.Treeview(body, columns=('time', 'days', 'action', 'active'),
                                  show='headings', selectmode='browse', height=TIMES_SHOWN)
        for column, heading, width in (('time', 'Time', 80), ('days', 'Days', 160),
                                       ('action', 'Action', 120), ('active', 'Active', 70)):
            self.table.heading(column, text=heading, anchor='w')
            self.table.column(column, width=width, anchor='w')
        self.table.grid(row=1, column=0, sticky='nsew', pady=(8, 0))
        self.table.bind('<Double-1>', lambda _: self._edit())
        self.table.bind('<<TreeviewSelect>>', lambda _: self._enable())

        self.next = ttk.Label(body, text='', font=app.small, foreground='grey30')
        self.next.grid(row=2, column=0, sticky='w', pady=(4, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, sticky='ew', pady=(10, 0))
        ttk.Button(buttons, text='Add\N{HORIZONTAL ELLIPSIS}', command=self._add).pack(side='left')
        self.edit = ttk.Button(buttons, text='Edit\N{HORIZONTAL ELLIPSIS}', command=self._edit)
        self.edit.pack(side='left', padx=(4, 0))
        self.remove = ttk.Button(buttons, text='Remove', command=self._remove)
        self.remove.pack(side='left', padx=(4, 0))
        ttk.Button(buttons, text='Close', command=self.destroy).pack(side='right')
        self.bind('<Escape>', lambda _: self.destroy())

        self._load()

    def _load(self) -> None:
        """Read the programming from the device and show it."""
        try:
            self.show(self.app.stack.timer(self.device.timer_channel))
        except (girax1.X1Error, TimeoutError) as e:
            messagebox.showerror('Switch times', f'cannot read the switch times: {e}',
                                 parent=self)

    def show(self, timer: Timer, select: int | None = None) -> None:
        """Fill the table from timer programming, keeping a row selected."""
        self.timer = timer
        self.app.update_timer(self.device, timer)
        if select is None and (chosen := self.table.selection()):
            select = int(chosen[0])
        self.table.delete(*self.table.get_children())
        for entry in sorted(timer.switch_times, key=lambda e: e.time):
            self.table.insert('', 'end', iid=str(entry.index),
                              values=(entry.when[:5], entry.days,
                                      _what(timer, entry, self.app.project, self.device),
                                      'yes' if entry.active else 'no'))
        if select is not None and self.table.exists(str(select)):
            self.table.selection_set(str(select))
            self.table.see(str(select))
        self.next.configure(text=f'next {timer.next_trigger[:16]}' if timer.next_trigger
                            else 'nothing due')
        self._enable()

    def _enable(self) -> None:
        """Allow editing and removing only while a switch time is selected."""
        state = ['!disabled'] if self.table.selection() else ['disabled']
        self.edit.state(state)
        self.remove.state(state)

    def _selected(self) -> SwitchTime | None:
        """Return the selected switch time."""
        if self.timer is None or not (chosen := self.table.selection()):
            return None
        return next((e for e in self.timer.switch_times if e.index == int(chosen[0])), None)

    def _add(self) -> None:
        """Ask for a new switch time and program it."""
        if self.timer is not None:
            TimeEditor(self, self.device, self.timer, None)

    def _edit(self) -> None:
        """Ask for changes to the selected switch time and program them."""
        if self.timer is not None and (entry := self._selected()) is not None:
            TimeEditor(self, self.device, self.timer, entry)

    def _remove(self) -> None:
        """Delete the selected switch time after asking whether that is meant."""
        if (entry := self._selected()) is None:
            return
        if not messagebox.askyesno('Remove switch time',
                                   f'Remove the switch time at {entry.when[:5]} '
                                   f'on {entry.days}?', parent=self):
            return
        try:
            self.show(schedule.remove_time(self.app.stack, self.device, entry.index))
        except SCHEDULE_ERRORS as e:
            messagebox.showerror('Remove switch time', str(e), parent=self)


class TimeEditor(tk.Toplevel):
    """Asks for the details of a new switch time or for changes to one."""

    def __init__(self, owner: TimesDialog, device: Device, timer: Timer,
                 entry: SwitchTime | None) -> None:
        """Open the window, filled in from the switch time to change if there is one."""
        super().__init__(owner)
        self.owner = owner
        self.device = device
        self.entry = entry
        self.title('Change switch time' if entry is not None else 'New switch time')
        self.transient(owner)
        self.resizable(False, False)

        body = ttk.Frame(self, padding=12)
        body.pack(fill='both', expand=True)

        hour, minute = (entry.time.split(':')[:2] if entry is not None and ':' in entry.time
                        else ('07', '00'))
        ttk.Label(body, text='Time').grid(row=0, column=0, sticky='w', pady=4)
        clock = ttk.Frame(body)
        clock.grid(row=0, column=1, sticky='w', pady=4)
        self.hour = ttk.Spinbox(clock, from_=0, to=23, width=3, format='%02.0f', wrap=True)
        self.hour.set(hour)
        self.hour.pack(side='left')
        ttk.Label(clock, text=':').pack(side='left', padx=2)
        self.minute = ttk.Spinbox(clock, from_=0, to=59, width=3, format='%02.0f', wrap=True)
        self.minute.set(minute)
        self.minute.pack(side='left')
        self.original_time = (hour, minute)

        ttk.Label(body, text='Days').grid(row=1, column=0, sticky='nw', pady=4)
        days = ttk.Frame(body)
        days.grid(row=1, column=1, sticky='w', pady=4)
        chosen = entry.day_numbers if entry is not None else set(range(len(WEEKDAYS)))
        self.days = [tk.BooleanVar(self, value=i in chosen) for i in range(len(WEEKDAYS))]
        for i, (name, var) in enumerate(zip(WEEKDAYS, self.days)):
            ttk.Checkbutton(days, text=name, variable=var).grid(row=0, column=i, padx=(0, 4))
        shortcuts = ttk.Frame(days)
        shortcuts.grid(row=1, column=0, columnspan=len(WEEKDAYS), sticky='w', pady=(4, 0))
        for text, group in (('Daily', 'daily'), ('Weekdays', 'weekdays'),
                            ('Weekend', 'weekend')):
            ttk.Button(shortcuts, text=text,
                       command=lambda g=group: self._choose_days(schedule.DAY_GROUPS[g])
                       ).pack(side='left', padx=(0, 4))

        self.original_what = (schedule.describe(device, timer, entry.index)
                              if entry is not None else None)
        ttk.Label(body, text='Action').grid(row=2, column=0, sticky='w', pady=4)
        offered = schedule.actions(device, timer)
        self.what = ttk.Combobox(body, values=offered, width=12)
        self.what.grid(row=2, column=1, sticky='w', pady=4)
        if entry is None:
            self.what.set(offered[0] if offered else '')
        elif self.original_what is not None:
            self.what.set(self.original_what)
        else:
            # A scene no single action describes stays as it is unless one is chosen.
            self.what.set('')
            ttk.Label(body, text=f'now: {_what(timer, entry, owner.app.project, device)}',
                      font=owner.app.small, foreground='grey30').grid(row=3, column=1,
                                                                      sticky='w')

        self.active = tk.BooleanVar(self, value=entry.active if entry is not None else True)
        ttk.Checkbutton(body, text='Active', variable=self.active).grid(row=4, column=1,
                                                                        sticky='w', pady=4)

        buttons = ttk.Frame(body)
        buttons.grid(row=5, column=0, columnspan=2, sticky='e', pady=(10, 0))
        ttk.Button(buttons, text='Cancel', command=self.destroy).pack(side='right')
        ttk.Button(buttons, text='OK', command=self._apply).pack(side='right', padx=(0, 4))
        self.bind('<Return>', lambda _: self._apply())
        self.bind('<Escape>', lambda _: self.destroy())

        self.wait_visibility()
        self.grab_set()
        self.hour.focus_set()

    def _choose_days(self, days: set[int]) -> None:
        """Tick exactly the given days."""
        for i, var in enumerate(self.days):
            var.set(i in days)

    def _apply(self) -> None:
        """Program what was entered and close, or explain why it cannot be done."""
        days = {i for i, var in enumerate(self.days) if var.get()}
        what = self.what.get().strip()
        try:
            if not days:
                raise ValueError('choose at least one day')
            when = schedule.parse_time(f'{self.hour.get()}:{self.minute.get()}')
            app = self.owner.app
            if self.entry is None:
                if not what:
                    raise ValueError('choose what the switch time does')
                timer, index = schedule.add_time(app.stack, self.device, when, what, days,
                                                 self.active.get())
            else:
                index = self.entry.index
                timer = schedule.change_time(
                    app.stack, self.device, index,
                    when=None if (self.hour.get(), self.minute.get()) == self.original_time
                    else when,
                    days=None if days == self.entry.day_numbers else days,
                    active=None if self.active.get() == self.entry.active else self.active.get(),
                    what=None if not what or what == self.original_what else what)
        except SCHEDULE_ERRORS as e:
            messagebox.showerror(self.title(), str(e), parent=self)
            return
        self.owner.show(timer, index)
        self.destroy()


def _what(timer: Timer, entry: SwitchTime, project: Project, device: Device) -> str:
    """Describe what a switch time does."""
    if (what := schedule.describe(device, timer, entry.index)) is not None:
        return what
    if (scene := timer.scene(entry)) is None:
        return f'scene {entry.trigger_value}'
    return ', '.join(_readable(project.point_kind(a.point_id), a.value)
                     for a in scene.actions) or f'scene {scene.number}'


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
