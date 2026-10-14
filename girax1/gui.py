"""Graphical user interface for a Gira X1.

The entry screen shows one tile per room.  Choosing a room shows one tile per
device in it, with the current status, controls that suit the kind of device and
all its switch times.  Clicking on those opens a window in which they can be
added, changed and removed.

Tk is used because it is part of the standard library, which keeps the program
free of a dependency just for the interface.

Values are kept up to date from the events the device pushes, so the display
also follows changes made from a wall switch or from the Gira app.

The interface is shown light or dark, or follows the desktop, as chosen from the
menu on the room selection screen.  The same menu leads to a setup screen for
the settings of the configuration file.
"""

import math
import platform
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import filedialog, messagebox, ttk
from pathlib import Path
from typing import Any, Callable

import girax1
from . import appearance, config, schedule, sunset, x11
from .appearance import Palette
from .project import (BRIGHTNESS_POINT, COVER_POINT, STOP_POINT, SWITCH_POINT, Device,
                      Project)
from .stack import BRIGHTNESS_MAX, Stack
from .timer import WEEKDAYS, SwitchTime, Timer

# Data points that only ever carry a momentary command.  Their value says
# nothing about the device, so the status line leaves them out.
TRIGGER_POINTS = frozenset({STOP_POINT, 'Shift'})

DEVICE_COLUMNS = 3
BODY_PADDING = 12
TILE_GAP = 6
PAGE_HEADER_GAP = 10
ROOM_COLUMNS = 3
BRIGHTNESS_STEP = 5
POLL_INTERVAL_MS = 400
POLL_TIMEOUT = 0.02

# Geometry of the round light switch: the size of the canvas, the radius of the
# button and the radius and width of the brightness ring around it.
SWITCH_SIZE = 96
SWITCH_RADIUS = 28
RING_RADIUS = 40
RING_WIDTH = 8

# The brightness ring covers three quarters of a circle, starting at the lower
# left and running clockwise over the top.  Angles count counterclockwise from
# the right, as Tk does.
RING_START = 225
RING_SPAN = 270

# Colours of the round light switch.
SWITCH_ON = '#2e7d32'
SWITCH_ON_PRESSED = '#1b5e20'
SWITCH_OFF = '#c62828'
SWITCH_OFF_PRESSED = '#8e0000'
SWITCH_UNKNOWN = '#9e9e9e'
SWITCH_UNKNOWN_PRESSED = '#707070'
RING_LEVEL = '#f9a825'
TIMES_SHOWN = 10

# The length of the slider choosing what a switch time does to a light.
SLIDER_LENGTH = 200

# How often to check whether looking up a location is done.
LOOKUP_POLL_MS = 100

# How often to check whether the desktop changed between light and dark while
# the interface follows it.
APPEARANCE_POLL_MS = 5000

# The appearances offered in the menu, with the label of each.
APPEARANCE_LABELS = ((appearance.LIGHT, 'Light'), (appearance.DARK, 'Dark'),
                     (appearance.AUTO, 'Auto (follow the system)'))

# What can go wrong while programming a switch time.
SCHEDULE_ERRORS = (ValueError, schedule.ScheduleError, sunset.SunsetError, config.ConfigError,
                   girax1.X1Error, TimeoutError)

# The window class, which the desktop file names so that the desktop matches
# the window to it, and the name of the icon.  Tk capitalises the class.
WINDOW_CLASS = 'girax1'
ICON_NAME = 'org.akkadia.girax1'
ICON_FILES = (Path(__file__).resolve().parent.parent / 'data' / f'{ICON_NAME}.svg',
              Path(sys.prefix) / 'share' / 'icons' / 'hicolor' / 'scalable' / 'apps'
              / f'{ICON_NAME}.svg')
ICON_SIZE = 64

# The license, its address and where its text is found, in the sources or as
# the package installs it.
LICENSE_SHORT = 'CC BY-NC-ND 4.0'
LICENSE_NAME = 'Creative Commons Attribution-NonCommercial-NoDerivatives 4.0 International'
LICENSE_SUMMARY = ('It may be copied and passed on for non-commercial purposes, giving credit '
                   'to the author, but not in changed form.')
LICENSE_URL = 'https://creativecommons.org/licenses/by-nc-nd/4.0/'
LICENSE_FILES = (Path(__file__).resolve().parent.parent / 'LICENSE',
                 Path(sys.prefix) / 'share' / 'licenses' / 'girax1' / 'LICENSE')
ABOUT_WIDTH = 440

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
        # Position in the grid of the room, counting rows over all pages.
        self.row = 0
        self.column = 0
        self.columnconfigure(0, weight=1)

        # Everything above the switch times, so that its height can be made the
        # same in all tiles of a row and the switch times line up.
        self.top = ttk.Frame(self)
        self.top.grid(row=0, column=0, sticky='new')
        self.top.columnconfigure(0, weight=1)
        ttk.Label(self.top, text=device.name, font=app.bold).grid(row=0, column=0, sticky='w')
        ttk.Label(self.top, text=device.kind, font=app.small,
                  style='Muted.TLabel').grid(row=1, column=0, sticky='w')
        self.status = ttk.Label(self.top, text='', font=app.normal)
        self.status.grid(row=2, column=0, sticky='w', pady=(6, 0))

        controls = ttk.Frame(self.top)
        controls.grid(row=3, column=0, pady=(6, 0))
        self.light: LightSwitch | None = None
        # Data points whose value the controls already show, so the status line
        # leaves them out.
        self.shown: set[str] = set()
        self._build_controls(controls)

        # The switch times, all of them, in an area that opens the window to
        # change them when clicked.  The next time due is shown below it.
        self.times: tk.Frame | None = None
        self.next = ttk.Label(self, text='', font=app.small, style='Subtle.TLabel')
        if device.timer_channel:
            self.times = tk.Frame(self, background=app.palette.times, relief='groove',
                                  borderwidth=2, padx=10, pady=10, cursor='hand2',
                                  takefocus=True, highlightthickness=1)
            self.times.grid(row=1, column=0, sticky='ew', pady=(8, 0))
            self.next.grid(row=2, column=0, sticky='w', pady=(4, 0))
        self.show_timer(timer)

    def align(self, height: int) -> None:
        """Start the switch times at this height, as all tiles of a row do."""
        self.rowconfigure(0, minsize=height)

    def show_timer(self, timer: Timer | None) -> None:
        """Lay out the switch times and the next time due."""
        if self.times is None:
            return
        for child in self.times.winfo_children():
            child.destroy()
        # Switch times following the sun, and inactive ones, are marked as such
        # on a line of their own below them, which keeps the lines short enough
        # for the tile.
        path = self.app.project.path(self.device)
        entries = [] if timer is None else sorted(timer.switch_times, key=lambda e: e.time)
        colours = self.app.palette
        if not entries:
            tk.Label(self.times, text='No switch times', font=self.app.normal,
                     foreground=colours.subtle, background=colours.times).grid(row=0, column=0,
                                                                               sticky='w')
        # A note spans an empty last column which takes all spare width, so that
        # its length does not widen the columns of the switch times.
        self.times.columnconfigure(3, weight=1)
        row = 0
        for entry in entries:
            cells = (entry.when[:5], entry.days,
                     _what(timer, entry, self.app.project, self.device))
            for column, text in enumerate(cells):
                tk.Label(self.times, text=text, font=self.app.small,
                         foreground=colours.text if entry.active else colours.inactive,
                         background=colours.times).grid(row=row, column=column, sticky='w',
                                                        padx=(0, 10))
            row += 1
            note = ', '.join(note for note in (
                _sunset_mark(self.app.sunset_settings.link(path, entry.index)),
                '' if entry.active else 'inactive') if note)
            if note:
                tk.Label(self.times, text=note, font=self.app.small, foreground=colours.subtle,
                         background=colours.times).grid(row=row, column=1, columnspan=3,
                                                        sticky='w', pady=(0, 2))
                row += 1
        _clickable(self.times, lambda: TimesDialog(self.app, self.device), colours.times,
                   colours.hover)

        if timer is not None and timer.next_trigger:
            self.next.configure(text=f'next {timer.next_trigger[:16]}')
            self.next.grid()
        else:
            self.next.grid_remove()


    def _build_controls(self, parent: ttk.Frame) -> None:
        """Add the buttons and sliders that suit this kind of device."""
        switchable = (point := self.device.point(SWITCH_POINT)) is not None and point.usable
        dimmable = (point := self.device.point(BRIGHTNESS_POINT)) is not None and point.usable
        if switchable or dimmable:
            self.light = LightSwitch(parent, self.app, switchable, dimmable,
                                     lambda on: self.app.switch(self.device, on),
                                     lambda percent: self.app.dim(self.device, percent))
            self.light.pack(side='left')
            self.shown.update(name for name, usable in ((SWITCH_POINT, switchable),
                                                        (BRIGHTNESS_POINT, dimmable))
                              if usable)
        if (point := self.device.point(COVER_POINT)) is not None and point.usable:
            for icon, command in (
                    ('\N{BLACK UP-POINTING TRIANGLE}', lambda: self.app.move(self.device, False)),
                    ('\N{BLACK SQUARE}', lambda: self.app.stop(self.device)),
                    ('\N{BLACK DOWN-POINTING TRIANGLE}', lambda: self.app.move(self.device, True))):
                ttk.Button(parent, text=icon, width=3, command=command).pack(side='left', padx=2)
            self.shown.add(COVER_POINT)

    def refresh(self, values: dict[str, str]) -> None:
        """Update the controls and the status line from the current values."""
        parts = []
        for point in self.device.usable_points:
            value = values.get(str(point.point_id))
            if point.name == SWITCH_POINT and self.light is not None:
                self.light.show_state({'1': True, '0': False}.get(str(value)))
            if point.name == BRIGHTNESS_POINT and self.light is not None:
                self.light.show_level(None if value in (None, '', 'None') else _number(value))
            if point.name not in TRIGGER_POINTS and point.name not in self.shown:
                parts.append(f'{point.name} {_readable(point.name, value)}')
        if parts or not self.device.usable_points:
            self.status.configure(text=', '.join(parts) or 'no data points')
            self.status.grid()
        else:
            self.status.grid_remove()


class LightSwitch(tk.Canvas):
    """A round button switching a light, ringed by its brightness if it is dimmable.

    Clicking the button switches the light.  Pressing on the ring, or dragging
    along it, chooses a brightness, which is only sent once the mouse button is
    let go of: writing on every step of a drag would flood the bus.
    """

    def __init__(self, master: tk.Misc, app: 'Application', switchable: bool, dimmable: bool,
                 on_switch: Callable[[bool], None], on_dim: Callable[[int], None]) -> None:
        """Draw the switch, with the brightness ring if the light is dimmable."""
        colours = app.palette
        super().__init__(master, width=SWITCH_SIZE, height=SWITCH_SIZE,
                         background=colours.background, highlightthickness=0, cursor='hand2')
        self.switchable = switchable
        self.dimmable = dimmable
        self.on_switch = on_switch
        self.on_dim = on_dim
        self.lit: bool | None = None
        self.level: int | None = None
        # What the mouse button went down on: the button, the ring or nothing.
        self.grabbed: str | None = None

        middle = SWITCH_SIZE / 2
        if dimmable:
            ring = (middle - RING_RADIUS, middle - RING_RADIUS,
                    middle + RING_RADIUS, middle + RING_RADIUS)
            self.create_arc(*ring, start=RING_START - RING_SPAN, extent=RING_SPAN,
                            style='arc', width=RING_WIDTH, outline=colours.ring_track)
            self.ring = self.create_arc(*ring, start=RING_START, extent=0, style='arc',
                                        width=RING_WIDTH, outline=RING_LEVEL, state='hidden')
            # The percentage sits in the gap the ring leaves at the bottom.
            self.percent = self.create_text(middle, middle + RING_RADIUS - 2, text='?',
                                            font=app.small, fill=colours.subtle)
        self.button = self.create_oval(middle - SWITCH_RADIUS, middle - SWITCH_RADIUS,
                                       middle + SWITCH_RADIUS, middle + SWITCH_RADIUS,
                                       outline='', fill=SWITCH_UNKNOWN)
        self.label = self.create_text(middle, middle, text='?', font=app.bold, fill='white')

        self.bind('<ButtonPress-1>', self._press)
        self.bind('<B1-Motion>', self._drag)
        self.bind('<ButtonRelease-1>', self._release)

    def show_state(self, on: bool | None) -> None:
        """Show whether the light is on, off or in an unknown state."""
        self.lit = on
        self._paint()

    def show_level(self, percent: int | None) -> None:
        """Show the brightness, unless the user is choosing one right now."""
        if self.grabbed != 'ring':
            self._show_level(percent)

    def _show_level(self, percent: int | None) -> None:
        """Draw the ring and the percentage for a brightness."""
        if not self.dimmable:
            return
        self.level = percent
        if percent is None or percent <= 0:
            self.itemconfigure(self.ring, state='hidden')
        else:
            self.itemconfigure(self.ring, state='normal',
                               extent=-RING_SPAN * min(percent, BRIGHTNESS_MAX) / BRIGHTNESS_MAX)
        self.itemconfigure(self.percent, text='?' if percent is None else f'{percent}%')
        if not self.switchable:
            self._paint()

    def _paint(self, pressed: bool = False) -> None:
        """Colour the button after the state of the light."""
        state = self.lit
        if not self.switchable and self.level is not None:
            # Without a switch the brightness alone says whether the light is on.
            state = self.level > 0
        colours = {True: (SWITCH_ON, SWITCH_ON_PRESSED, 'On'),
                   False: (SWITCH_OFF, SWITCH_OFF_PRESSED, 'Off'),
                   None: (SWITCH_UNKNOWN, SWITCH_UNKNOWN_PRESSED, '?')}
        normal, darker, text = colours[state]
        self.itemconfigure(self.button, fill=darker if pressed else normal)
        self.itemconfigure(self.label, text=text)

    def _where(self, event: tk.Event) -> tuple[float, float]:
        """Return the distance of the pointer from the middle and its angle."""
        dx = event.x - SWITCH_SIZE / 2
        dy = SWITCH_SIZE / 2 - event.y
        return math.hypot(dx, dy), math.degrees(math.atan2(dy, dx))

    def _percent(self, angle: float) -> int:
        """Convert an angle on the ring to a brightness in whole steps.

        A point in the gap at the bottom counts as the end of the ring nearer to it.
        """
        along = (RING_START - angle) % 360
        if along > RING_SPAN:
            along = RING_SPAN if along < (RING_SPAN + 360) / 2 else 0
        percent = along / RING_SPAN * BRIGHTNESS_MAX
        return int(round(percent / BRIGHTNESS_STEP) * BRIGHTNESS_STEP)

    def _press(self, event: tk.Event) -> None:
        """Note whether the button or the ring was pressed."""
        distance, angle = self._where(event)
        if distance <= SWITCH_RADIUS and self.switchable:
            self.grabbed = 'button'
            self._paint(pressed=True)
        elif self.dimmable and abs(distance - RING_RADIUS) <= RING_WIDTH:
            self.grabbed = 'ring'
            self._show_level(self._percent(angle))
        else:
            self.grabbed = None

    def _drag(self, event: tk.Event) -> None:
        """Follow the pointer along the ring."""
        if self.grabbed == 'ring':
            self._show_level(self._percent(self._where(event)[1]))
        elif self.grabbed == 'button':
            self._paint(pressed=self._where(event)[0] <= SWITCH_RADIUS)

    def _release(self, event: tk.Event) -> None:
        """Switch the light, or send the brightness chosen on the ring."""
        grabbed, self.grabbed = self.grabbed, None
        if grabbed == 'button':
            self._paint()
            if self._where(event)[0] <= SWITCH_RADIUS:
                self.on_switch(not self.lit)
        elif grabbed == 'ring' and self.level is not None:
            self.on_dim(self.level)


class Application(tk.Tk):
    """The main window, switching between the room and the device view."""

    def __init__(self, stack: Stack, project: Project) -> None:
        """Set up the window and show the rooms."""
        super().__init__(className=WINDOW_CLASS)
        self.stack = stack
        self.project = project
        self.values: dict[str, str] = {}
        self.timers: dict[int, Timer | None] = {}
        self.tiles: list[DeviceTile] = []
        # The rows of tiles on every page of a room, the page shown and the
        # widgets to turn pages.  Without a room on display there is no pager.
        self.pages: list[list[int]] = []
        self.page = 0
        self.header: ttk.Frame | None = None
        self.pager: ttk.Frame | None = None
        self.page_label: ttk.Label | None = None
        self.previous_page: ttk.Button | None = None
        self.next_page: ttk.Button | None = None
        self.paging = False
        # The room on display, or None while the rooms or the setup are shown.
        self.room: str | None = None
        self.setup: SetupForm | None = None

        # The appearance chosen and the colours in use for it.
        self.appearance = tk.StringVar(self, value=appearance.load())
        self.palette = appearance.palette(self.appearance.get())
        self.style = ttk.Style(self)

        self.title('Gira X1')
        self._set_icon()
        self.geometry('960x680')
        self.minsize(640, 480)

        self.normal = tkfont.nametofont('TkDefaultFont').copy()
        self.bold = self.normal.copy()
        self.bold.configure(weight='bold', size=self.normal.cget('size') + 1)
        self.small = self.normal.copy()
        self.small.configure(size=max(self.normal.cget('size') - 2, 7))
        self._apply_palette()

        # The status line is packed first so that the body cannot push it out
        # of the window; the body gets what is left, which paging relies on.
        self.message = ttk.Label(self, text='', font=self.small, anchor='w', padding=(12, 4))
        self.message.pack(fill='x', side='bottom')
        self.body = ttk.Frame(self, padding=BODY_PADDING)
        self.body.pack(fill='both', expand=True)
        self.body.bind('<Configure>', lambda _: self._repaginate_soon())
        self.bind('<Control-q>', lambda _: self.destroy())
        self.bind('<Prior>', lambda _: self.turn(-1))
        self.bind('<Next>', lambda _: self.turn(1))
        self.bind('<MouseWheel>', lambda e: self.turn(-1 if e.delta > 0 else 1))
        self.bind('<Button-4>', lambda _: self.turn(-1))
        self.bind('<Button-5>', lambda _: self.turn(1))

        self.rooms = self._rooms()
        self.sunset_settings = self.load_sunset()
        self.reload_values()
        self.show_rooms()
        self.after(POLL_INTERVAL_MS, self._pump)
        self.after(APPEARANCE_POLL_MS, self._follow_system)

    # ------------------------------------------------------------------ views

    def show_rooms(self) -> None:
        """Show one tile per room."""
        self.tiles = []
        self._clear()
        self.room = self.setup = None
        self.header = self.pager = None
        self.pages = []

        heading = ttk.Frame(self.body)
        heading.grid(row=0, column=0, columnspan=ROOM_COLUMNS, sticky='ew', pady=(0, 10))
        ttk.Label(heading, text='Rooms', font=self.bold).pack(side='left')
        menu_button = ttk.Button(heading, text='\N{TRIGRAM FOR HEAVEN}', width=3)
        menu_button.configure(command=lambda: self._show_menu(menu_button))
        menu_button.pack(side='right')

        colours = self.palette
        for index, (name, devices) in enumerate(self.rooms):
            # Clicking anywhere on the tile opens the room.
            tile = tk.Frame(self.body, background=colours.background, relief='ridge',
                            borderwidth=1, padx=16, pady=16, cursor='hand2', takefocus=True,
                            highlightthickness=1, highlightbackground=colours.background,
                            highlightcolor=colours.select)
            tile.grid(row=1 + index // ROOM_COLUMNS, column=index % ROOM_COLUMNS,
                      sticky='nsew', padx=TILE_GAP, pady=TILE_GAP)
            tk.Label(tile, text=name, font=self.bold, foreground=colours.text,
                     background=colours.background).pack(anchor='w')
            tk.Label(tile, text=f'{len(devices)} device' + ('' if len(devices) == 1 else 's'),
                     font=self.small, foreground=colours.muted,
                     background=colours.background).pack(anchor='w')
            tk.Label(tile, text=self._summary(devices), font=self.small, foreground=colours.text,
                     background=colours.background).pack(anchor='w', pady=(6, 0))
            _clickable(tile, lambda n=name: self.show_room(n), colours.background, colours.hover)
        for column in range(ROOM_COLUMNS):
            self.body.columnconfigure(column, weight=1, uniform='tiles')

    def show_room(self, name: str, top: int = 0) -> None:
        """Show one tile per device of the named room.

        The page shown is the one holding the row of tiles numbered ``top``.
        """
        devices = dict(self.rooms).get(name, [])
        self._load_timers(devices)
        self.tiles = []
        self._clear()
        self.room = name
        self.setup = None

        self.header = ttk.Frame(self.body)
        self.header.grid(row=0, column=0, columnspan=DEVICE_COLUMNS, sticky='ew',
                         pady=(0, PAGE_HEADER_GAP))
        ttk.Button(self.header, text='\N{LEFTWARDS ARROW} Rooms',
                   command=self.show_rooms).pack(side='left')
        ttk.Label(self.header, text=name, font=self.bold).pack(side='left', padx=(12, 0))

        # Shown only when the tiles need more than one page.
        self.pager = ttk.Frame(self.header)
        self.previous_page = ttk.Button(self.pager, text='\N{BLACK LEFT-POINTING TRIANGLE}',
                                        width=3, command=lambda: self.turn(-1))
        self.previous_page.pack(side='left')
        self.page_label = ttk.Label(self.pager, text='', width=7, anchor='center')
        self.page_label.pack(side='left', padx=4)
        self.next_page = ttk.Button(self.pager, text='\N{BLACK RIGHT-POINTING TRIANGLE}',
                                    width=3, command=lambda: self.turn(1))
        self.next_page.pack(side='left')
        # Paging starts out from the row to keep in view.
        self.pages = [[top]]
        self.page = 0

        for index, device in enumerate(devices):
            tile = DeviceTile(self.body, self, device, self.timers.get(device.view_id))
            tile.row, tile.column = divmod(index, DEVICE_COLUMNS)
            self.tiles.append(tile)
        for column in range(DEVICE_COLUMNS):
            self.body.columnconfigure(column, weight=1, uniform='tiles')
        self.refresh()

    def show_setup(self) -> None:
        """Show the settings of the configuration file for editing."""
        source = self.stack.config.source
        if source is None:
            self.note('the settings did not come from a file, so there is nothing to edit')
            return
        try:
            settings = config.read_settings(source)
        except config.ConfigError as e:
            messagebox.showerror('Setup', str(e), parent=self)
            return
        self.tiles = []
        self._clear()
        self.room = None
        self.header = self.pager = None
        self.pages = []

        heading = ttk.Frame(self.body)
        heading.grid(row=0, column=0, sticky='ew', pady=(0, PAGE_HEADER_GAP))
        ttk.Button(heading, text='\N{LEFTWARDS ARROW} Rooms',
                   command=self.show_rooms).pack(side='left')
        ttk.Label(heading, text='Setup', font=self.bold).pack(side='left', padx=(12, 0))
        self.setup = SetupForm(self.body, self, source, settings)
        self.setup.grid(row=1, column=0, sticky='nw')

    def reconnect(self, settings: config.Config) -> None:
        """Connect with changed settings, keeping the old connection if that fails."""
        stack = Stack(settings)
        try:
            stack.connect()
            project = Project.parse(stack.ui_configuration())
        except (girax1.X1Error, TimeoutError) as e:
            stack.close()
            messagebox.showerror('Setup', f'The settings are saved, but connecting with them '
                                 f'failed: {e}\n\nThe connection made with the previous '
                                 f'settings stays in use.', parent=self)
            return
        self.stack.close()
        self.stack = stack
        self.project = project
        self.rooms = self._rooms()
        self.timers = {}
        self.reload_values()
        self.note(f'settings saved, connected to {settings.host}')

    def _clear(self) -> None:
        """Remove everything from the body of the window."""
        for child in self.body.winfo_children():
            child.destroy()
        for column in range(max(ROOM_COLUMNS, DEVICE_COLUMNS)):
            self.body.columnconfigure(column, weight=0, uniform='')

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

    def load_sunset(self) -> sunset.Settings:
        """Read the sunset settings, falling back to none if they cannot be read."""
        if self.stack.config.source is None:
            return sunset.Settings()
        try:
            return sunset.load(self.stack.config.source)
        except sunset.SunsetError as e:
            self.note(str(e))
            return sunset.Settings()

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
        self._align()

    def _align(self) -> None:
        """Let the switch times of all tiles in a row start at the same height."""
        self.update_idletasks()
        for tiles in self._rows().values():
            height = max(tile.top.winfo_reqheight() for tile in tiles)
            for tile in tiles:
                tile.align(height)
        self._repaginate()

    def _rows(self) -> dict[int, list['DeviceTile']]:
        """Return the tiles of the room shown, by row."""
        rows: dict[int, list[DeviceTile]] = {}
        for tile in self.tiles:
            rows.setdefault(tile.row, []).append(tile)
        return rows

    def _set_icon(self) -> None:
        """Give the window the icon of the program, from the sources or as installed.

        Reading SVG needs Tk 8.7 or later; with an older one the window keeps
        the icon the desktop gives it.
        """
        for path in ICON_FILES:
            if not path.is_file():
                continue
            try:
                # The image has to be kept, or Tk drops it.
                self.icon = tk.PhotoImage(file=str(path),
                                          format=f'svg -scaletoheight {ICON_SIZE}')
            except tk.TclError:
                return
            self.iconphoto(True, self.icon)
            return

    # ------------------------------------------------------------- appearance

    def _show_menu(self, button: ttk.Button) -> None:
        """Pop up the menu of settings below its button."""
        colours = self.palette
        options = {'background': colours.field, 'foreground': colours.text,
                   'activebackground': colours.select, 'activeforeground': colours.select_text,
                   'selectcolor': colours.text, 'disabledforeground': colours.disabled,
                   'tearoff': False}
        menu = tk.Menu(self, **options)
        looks = tk.Menu(menu, **options)
        for choice, label in APPEARANCE_LABELS:
            looks.add_radiobutton(label=label, value=choice, variable=self.appearance,
                                  command=self._choose_appearance)
        menu.add_cascade(label='Appearance', menu=looks)
        menu.add_command(label='Setup\N{HORIZONTAL ELLIPSIS}', command=self.show_setup)
        menu.add_separator()
        menu.add_command(label='About\N{HORIZONTAL ELLIPSIS}', command=lambda: AboutDialog(self))
        menu.add_separator()
        menu.add_command(label='Quit', accelerator='Ctrl+Q', command=self.destroy)
        menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())

    def _choose_appearance(self) -> None:
        """Switch to the appearance chosen in the menu and remember it."""
        appearance.save(self.appearance.get())
        self._use_palette(appearance.palette(self.appearance.get()))

    def _follow_system(self) -> None:
        """Change between light and dark along with the desktop, if it is followed."""
        if self.appearance.get() == appearance.AUTO:
            self._use_palette(appearance.palette(appearance.AUTO))
        self.after(APPEARANCE_POLL_MS, self._follow_system)

    def _use_palette(self, colours: Palette) -> None:
        """Show the interface in other colours, drawing the current view anew."""
        if colours == self.palette:
            return
        self.palette = colours
        self._apply_palette()
        for window in self.winfo_children():
            if isinstance(window, tk.Toplevel):
                window.configure(background=colours.background)
        # The setup screen is made of themed widgets only, which follow by
        # themselves; drawing it anew would lose what is being typed.
        if self.setup is not None:
            return
        if self.room is None:
            self.show_rooms()
        else:
            self.show_room(self.room, self.pages[self.page][0] if self.pages else 0)

    def _apply_palette(self) -> None:
        """Set the colours of all themed widgets and the defaults of the plain ones."""
        c = self.palette
        style = self.style
        # Only the clam theme lets every colour be set, on every platform.
        style.theme_use('clam')
        style.configure('.', background=c.background, foreground=c.text,
                        fieldbackground=c.field, bordercolor=c.border, lightcolor=c.background,
                        darkcolor=c.background, troughcolor=c.field, focuscolor=c.select,
                        selectbackground=c.select, selectforeground=c.select_text,
                        insertcolor=c.text, arrowcolor=c.text)
        style.map('.', foreground=[('disabled', c.disabled)],
                  background=[('disabled', c.background)])
        style.configure('TButton', background=c.button, lightcolor=c.button, darkcolor=c.button)
        style.map('TButton', background=[('disabled', c.background), ('pressed', c.button_active),
                                         ('active', c.button_active)],
                  lightcolor=[('active', c.button_active)],
                  darkcolor=[('active', c.button_active)])
        style.configure('TCheckbutton', indicatorbackground=c.field, indicatorforeground=c.text)
        style.map('TCheckbutton', background=[('active', c.background)],
                  indicatorbackground=[('pressed', c.button_active), ('disabled', c.background)])
        style.configure('Treeview', background=c.field, fieldbackground=c.field,
                        foreground=c.text)
        style.map('Treeview', background=[('selected', c.select)],
                  foreground=[('selected', c.select_text)])
        style.configure('Treeview.Heading', background=c.button, foreground=c.text)
        style.map('Treeview.Heading', background=[('active', c.button_active)])
        for widget in ('TCombobox', 'TSpinbox', 'TEntry'):
            style.configure(widget, fieldbackground=c.field, foreground=c.text,
                            background=c.button)
            style.map(widget, fieldbackground=[('readonly', c.field)],
                      background=[('active', c.button_active)])
        style.configure('Muted.TLabel', foreground=c.muted)
        style.configure('Subtle.TLabel', foreground=c.subtle)

        # Plain Tk widgets, the list of a combobox and the message boxes take
        # their colours from the option database when they are created.
        for pattern, value in (('*Background', c.background), ('*Foreground', c.text),
                               ('*activeBackground', c.button_active),
                               ('*activeForeground', c.text),
                               ('*highlightBackground', c.background),
                               ('*TCombobox*Listbox.background', c.field),
                               ('*TCombobox*Listbox.foreground', c.text),
                               ('*TCombobox*Listbox.selectBackground', c.select),
                               ('*TCombobox*Listbox.selectForeground', c.select_text)):
            self.option_add(pattern, value)
        self.configure(background=c.background)

    # ----------------------------------------------------------------- paging

    def turn(self, step: int) -> None:
        """Show the next or the previous page of the room."""
        if self.pager is None or not 0 <= self.page + step < len(self.pages):
            return
        self.page += step
        self._show_page()

    def _repaginate_soon(self) -> None:
        """Repaginate once the window has settled after a change of size."""
        if not self.paging:
            self.paging = True
            self.after_idle(self._repaginate)

    def _repaginate(self) -> None:
        """Split the rows of tiles into pages that fit the body of the window.

        The page shown afterwards is the one holding the row that was on top
        before, so that changing the size of the window does not lose the place.
        """
        self.paging = False
        if self.header is None:
            return
        self.update_idletasks()
        available = (self.body.winfo_height() - 2 * BODY_PADDING
                     - self.header.winfo_reqheight() - PAGE_HEADER_GAP)
        pages: list[list[int]] = []
        used = 0
        for row, tiles in sorted(self._rows().items()):
            height = max(tile.winfo_reqheight() for tile in tiles) + 2 * TILE_GAP
            # A row too tall for any page still gets one of its own.
            if pages and used + height <= available:
                pages[-1].append(row)
                used += height
            else:
                pages.append([row])
                used = height
        top = self.pages[self.page][0] if self.pages else 0
        self.pages = pages or [[]]
        self.page = next((i for i, rows in enumerate(self.pages) if top in rows), 0)
        self._show_page()

    def _show_page(self) -> None:
        """Lay out the tiles of the current page and update the pager."""
        rows = self.pages[self.page]
        for tile in self.tiles:
            if tile.row in rows:
                tile.grid(row=1 + tile.row - rows[0], column=tile.column, sticky='nsew',
                          padx=TILE_GAP, pady=TILE_GAP)
            else:
                tile.grid_remove()
        if self.pager is None or self.page_label is None:
            return
        if len(self.pages) > 1:
            self.page_label.configure(text=f'{self.page + 1} / {len(self.pages)}')
            if self.previous_page is not None and self.next_page is not None:
                self.previous_page.state(['!disabled'] if self.page > 0 else ['disabled'])
                self.next_page.state(['!disabled'] if self.page < len(self.pages) - 1
                                     else ['disabled'])
            self.pager.pack(side='right')
        else:
            self.pager.pack_forget()

    def note(self, text: str) -> None:
        """Show a message in the status line."""
        self.message.configure(text=text)

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


class AboutDialog(tk.Toplevel):
    """Names the program, its version, its license and what it is connected to."""

    def __init__(self, app: 'Application') -> None:
        """Open the window."""
        super().__init__(app)
        self.title('About')
        self.resizable(False, False)

        body = ttk.Frame(self, padding=20)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Gira X1', font=app.bold).pack(anchor='w')
        ttk.Label(body, text=f'Version {girax1.__version__}').pack(anchor='w', pady=(2, 10))
        ttk.Label(body, text='Controls the lights, blinds and switch times of a Gira X1.'
                  ).pack(anchor='w')
        ttk.Label(body, text='Written by Ulrich Drepper.').pack(anchor='w', pady=(2, 10))
        settings = app.stack.config
        for line in (f'Connected to {settings.host}',
                     f'Settings in {settings.source}' if settings.source is not None else '',
                     f'Python {platform.python_version()}, Tk {self.tk.call("info", "patchlevel")}'):
            if line:
                ttk.Label(body, text=line, font=app.small, style='Muted.TLabel').pack(anchor='w')

        ttk.Label(body, text='License', font=app.bold).pack(anchor='w', pady=(14, 2))
        ttk.Label(body, text=f'Licensed under {LICENSE_SHORT}, the').pack(anchor='w')
        ttk.Label(body, text=f'{LICENSE_NAME}.', wraplength=ABOUT_WIDTH,
                  justify='left').pack(anchor='w')
        ttk.Label(body, text=LICENSE_SUMMARY, wraplength=ABOUT_WIDTH,
                  justify='left').pack(anchor='w', pady=(4, 0))
        ttk.Label(body, text=LICENSE_URL, font=app.small,
                  style='Muted.TLabel').pack(anchor='w', pady=(4, 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill='x', pady=(16, 0))
        self.license_text = _license_text()
        self.toggle: ttk.Button | None = None
        if self.license_text is not None:
            self.toggle = ttk.Button(buttons, command=self._toggle_license)
            self.toggle.pack(side='left')
        close = ttk.Button(buttons, text='Close', command=self.destroy)
        close.pack(side='right')

        # The full text, shown on request below everything else.
        self.license = ttk.Frame(body)
        self.license_shown = False
        if self.license_text is not None:
            colours = app.palette
            text = tk.Text(self.license, width=76, height=20, wrap='none',
                           font=tkfont.nametofont('TkFixedFont'), background=colours.field,
                           foreground=colours.text, highlightthickness=0, borderwidth=1,
                           relief='solid')
            scroll = ttk.Scrollbar(self.license, orient='vertical', command=text.yview)
            text.configure(yscrollcommand=scroll.set)
            text.insert('1.0', self.license_text)
            text.configure(state='disabled')
            text.pack(side='left', fill='both', expand=True)
            scroll.pack(side='right', fill='y')
            self._label_toggle()
        self.bind('<Escape>', lambda _: self.destroy())
        self.bind('<Return>', lambda _: self.destroy())

        _make_modal(self, app)
        close.focus_set()

    def _toggle_license(self) -> None:
        """Show or hide the full text of the license."""
        self.license_shown = not self.license_shown
        if self.license_shown:
            self.license.pack(fill='both', expand=True, pady=(12, 0))
        else:
            self.license.pack_forget()
        self._label_toggle()

    def _label_toggle(self) -> None:
        """Label the button for the license text with an arrow showing its state."""
        if self.toggle is not None:
            arrow = '\N{BLACK DOWN-POINTING SMALL TRIANGLE}' if self.license_shown \
                else '\N{BLACK RIGHT-POINTING SMALL TRIANGLE}'
            self.toggle.configure(text=f'{arrow} Show license')


class SetupForm(ttk.Frame):
    """The settings of the configuration file, apart from the travel times of the blinds.

    The settings needed to reach the device are shown at once; those which
    rarely need changing are behind a button.
    """

    def __init__(self, master: tk.Misc, app: 'Application', source: Path,
                 settings: dict[str, Any]) -> None:
        """Lay out the settings as the file has them."""
        super().__init__(master)
        self.app = app
        self.source = source
        self.settings = settings
        self.fields: dict[str, tk.Variable] = {}
        self.secrets: list[ttk.Entry] = []

        # All settings share one grid, so that the advanced ones line up with
        # the others when they are shown.
        row = self._heading(0, 'Device')
        row = self._entry(row, 'device.host', 'Host name or address')
        row = self._entry(row, 'device.port', 'Port', 'left empty: 443 with TLS, 80 without',
                          width=8)
        row = self._entry(row, 'device.timeout', 'Timeout', 'seconds', width=8)
        row = self._heading(row, 'Login')
        row = self._entry(row, 'auth.username', 'User name')
        row = self._entry(row, 'auth.password', 'Password', secret=True)
        row = self._entry(row, 'auth.token', 'Token',
                          'used instead of user name and password if given', secret=True)
        self.reveal = tk.BooleanVar(self, value=False)
        ttk.Checkbutton(self, text='Show password and token', variable=self.reveal,
                        command=self._reveal).grid(row=row, column=1, sticky='w', pady=(2, 0))
        row += 1
        row = self._sunset_section(row)

        self.toggle = ttk.Button(self, command=self._toggle_advanced)
        self.toggle.grid(row=row, column=0, columnspan=2, sticky='w', pady=(16, 6))
        row += 1
        before = set(self.winfo_children())
        row = self._entry(row, 'auth.client_id', 'Client ID',
                          f'left empty: {config.DEFAULT_CLIENT_ID}')
        row = self._check(row, 'tls.enabled', 'Use TLS for the REST API')
        row = self._check(row, 'tls.verify', 'Verify the certificate of the device')
        row = self._entry(row, 'tls.ca_cert', 'CA certificate',
                          'left empty: the certificates of the system')
        ttk.Button(self, text='Choose\N{HORIZONTAL ELLIPSIS}',
                   command=self._choose_certificate).grid(row=row - 1, column=3, sticky='w',
                                                         padx=(6, 0))
        self.advanced = [w for w in self.winfo_children() if w not in before]
        self.advanced_shown = False
        for widget in self.advanced:
            widget.grid_remove()
        self._show_toggle()

        buttons = ttk.Frame(self)
        buttons.grid(row=row, column=0, columnspan=3, sticky='w', pady=(20, 0))
        ttk.Button(buttons, text='Save', command=self._save).pack(side='left')
        ttk.Button(buttons, text='Revert', command=self._revert).pack(side='left', padx=(6, 0))
        ttk.Label(self, text=f'Stored in {source}', font=app.small,
                  style='Muted.TLabel').grid(row=row + 1, column=0, columnspan=4, sticky='w',
                                             pady=(10, 0))

    def _sunset_section(self, row: int) -> int:
        """Add the settings for switching lights on at sunset and return the next free row."""
        row = self._heading(row, 'Switch times following the sun')
        self.sun = self.app.sunset_settings
        self.sun_location = tk.StringVar(self)
        self.sun_days = [tk.BooleanVar(self) for _ in WEEKDAYS]
        self.sun_time = tk.StringVar(self)

        ttk.Label(self, text='Location').grid(row=row, column=0, sticky='w', padx=(0, 12),
                                              pady=2)
        # Leaving the field looks the location up, so that what was found can
        # be checked before it is saved.
        location = ttk.Entry(self, textvariable=self.sun_location, width=32)
        location.bind('<FocusOut>', lambda _: self._locate())
        location.bind('<Return>', lambda _: self._locate())
        location.grid(row=row, column=1,
                                                                      sticky='w', pady=2)
        ttk.Label(self, text='a place, or latitude, longitude', font=self.app.small,
                  style='Muted.TLabel').grid(row=row, column=2, sticky='w', padx=(8, 0))
        self.sun_place = ttk.Label(self, text='', font=self.app.small, style='Muted.TLabel')
        self.sun_place.grid(row=row + 1, column=1, columnspan=3, sticky='w')
        row += 2

        ttk.Label(self, text='Adjusted on').grid(row=row, column=0, sticky='w', padx=(0, 12),
                                                 pady=2)
        days = ttk.Frame(self)
        days.grid(row=row, column=1, columnspan=3, sticky='w', pady=2)
        for name, chosen in zip(WEEKDAYS, self.sun_days):
            ttk.Checkbutton(days, text=name, variable=chosen).pack(side='left', padx=(0, 4))
        row += 1

        ttk.Label(self, text='Adjusted at').grid(row=row, column=0, sticky='w', padx=(0, 12),
                                                 pady=2)
        ttk.Entry(self, textvariable=self.sun_time, width=8).grid(row=row, column=1, sticky='w',
                                                                  pady=2)
        ttk.Label(self, text='time of day the switch times are moved after the sun',
                  font=self.app.small, style='Muted.TLabel').grid(row=row, column=2, sticky='w',
                                                                  padx=(8, 0))
        self.sun_today = ttk.Label(self, text='', font=self.app.small, style='Muted.TLabel')
        self.sun_today.grid(row=row + 1, column=1, columnspan=3, sticky='w', pady=(2, 0))
        self._fill_sunset()
        return row + 2

    def _locate(self) -> None:
        """Look up the location typed, in the background, and fill in what is found.

        The lookup may take a while over the internet, so it runs in a thread
        of its own while the interface stays responsive; its result is picked
        up from Tk's event loop.
        """
        text = self.sun_location.get().strip()
        if not text or text in (self.sun.location, self.sun_found[2] if self.sun_found else None):
            return
        self.sun_place.configure(text=f'looking up {text}\N{HORIZONTAL ELLIPSIS}')
        result: list[tuple[float, float, str] | Exception] = []

        def look_up() -> None:
            try:
                result.append(sunset.locate(text))
            except sunset.SunsetError as e:
                result.append(e)

        threading.Thread(target=look_up, daemon=True).start()
        self.after(LOOKUP_POLL_MS, self._located, text, result)

    def _located(self, text: str, result: list[tuple[float, float, str] | Exception]) -> None:
        """Fill in the location found, once the lookup is done."""
        if not self.winfo_exists():
            return
        if not result:
            self.after(LOOKUP_POLL_MS, self._located, text, result)
            return
        # What was typed meanwhile is not overwritten with an older answer.
        if self.sun_location.get().strip() != text:
            return
        if isinstance(found := result[0], Exception):
            self.sun_found = None
            self.sun_place.configure(text=str(found))
            return
        self.sun_found = found
        latitude, longitude, description = found
        self.sun_location.set(description)
        # The field is often too narrow for the whole name.
        self.sun_place.configure(text=f'at {latitude:.4f}, {longitude:.4f}: {description}')

    def _fill_sunset(self) -> None:
        """Show the sunset settings as they are, with today's times."""
        sun = self.sun
        # The place looked up last, as latitude, longitude and description.
        self.sun_found: tuple[float, float, str] | None = None
        self.sun_location.set(sun.location)
        for day, chosen in enumerate(self.sun_days):
            chosen.set(day in sun.days)
        self.sun_time.set(sun.time[:5])
        if sun.latitude is None or sun.longitude is None:
            self.sun_place.configure(text='not set yet')
            self.sun_today.configure(text='')
            return
        self.sun_place.configure(text=f'at {sun.latitude:.4f}, {sun.longitude:.4f}: '
                                      f'{sun.location}')
        try:
            up, _ = sunset.time_at(sun, 0, event=sunset.SUNRISE)
            down, _ = sunset.time_at(sun, 0)
            today = f'Today the sun rises at {up:%H:%M} and sets at {down:%H:%M}.'
        except sunset.SunsetError as e:
            today = f'{e}.'
        if not sunset.timer_installed():
            today += '  No timer adjusts the switch times yet; saving installs it.'
        self.sun_today.configure(text=today)

    def _sunset_changes(self) -> dict[str, Any]:
        """Return the sunset settings which differ from the file, for :func:`sunset.configure`."""
        sun = self.sun
        changes: dict[str, Any] = {}
        if (location := self.sun_location.get().strip()) and location != sun.location:
            # What was looked up already is not looked up again.
            if self.sun_found is not None and location == self.sun_found[2]:
                changes['place'] = self.sun_found
            else:
                changes['location'] = location
        if (days := {d for d, chosen in enumerate(self.sun_days) if chosen.get()}) != sun.days:
            changes['days'] = days
        if (time := schedule.parse_time(self.sun_time.get())) != sun.time:
            changes['time'] = time
        return changes

    def _heading(self, row: int, text: str) -> int:
        """Add the heading of a group of settings and return the next free row."""
        ttk.Label(self, text=text, font=self.app.bold).grid(
            row=row, column=0, columnspan=3, sticky='w', pady=(0 if row == 0 else 14, 4))
        return row + 1

    def _entry(self, row: int, name: str, label: str, hint: str = '', width: int = 32,
               secret: bool = False) -> int:
        """Add a text field for a setting and return the next free row."""
        value = self.settings.get(name)
        variable = tk.StringVar(self, value='' if value is None else str(value))
        self.fields[name] = variable
        ttk.Label(self, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=2)
        entry = ttk.Entry(self, textvariable=variable, width=width,
                          show='\N{BULLET}' if secret else '')
        entry.grid(row=row, column=1, sticky='w', pady=2)
        if secret:
            self.secrets.append(entry)
        if hint:
            ttk.Label(self, text=hint, font=self.app.small, style='Muted.TLabel').grid(
                row=row, column=2, sticky='w', padx=(8, 0))
        return row + 1

    def _check(self, row: int, name: str, label: str) -> int:
        """Add a check box for a setting which is true unless the file says otherwise."""
        variable = tk.BooleanVar(self, value=self.settings.get(name, True) is not False)
        self.fields[name] = variable
        ttk.Checkbutton(self, text=label, variable=variable).grid(row=row, column=1,
                                                                  columnspan=2, sticky='w',
                                                                  pady=2)
        return row + 1

    def _reveal(self) -> None:
        """Show or hide the password and the token."""
        for entry in self.secrets:
            entry.configure(show='' if self.reveal.get() else '\N{BULLET}')

    def _toggle_advanced(self) -> None:
        """Show or hide the settings which rarely need changing."""
        self.advanced_shown = not self.advanced_shown
        for widget in self.advanced:
            if self.advanced_shown:
                widget.grid()
            else:
                widget.grid_remove()
        self._show_toggle()

    def _show_toggle(self) -> None:
        """Label the button for the advanced settings with an arrow showing its state."""
        arrow = '\N{BLACK DOWN-POINTING SMALL TRIANGLE}' if self.advanced_shown \
            else '\N{BLACK RIGHT-POINTING SMALL TRIANGLE}'
        self.toggle.configure(text=f'{arrow} Advanced settings')

    def _choose_certificate(self) -> None:
        """Pick the file of the CA certificate."""
        chosen = filedialog.askopenfilename(
            parent=self, title='CA certificate',
            filetypes=[('Certificates', '*.pem *.crt *.cer'), ('All files', '*')])
        if chosen:
            self.fields['tls.ca_cert'].set(chosen)

    def _changes(self) -> dict[str, Any]:
        """Return the settings which differ from the file, converted to their types.

        An emptied text field removes its setting, so that the default applies.
        """
        changes: dict[str, Any] = {}
        for name, variable in self.fields.items():
            kind = config.SETTINGS[name]
            if kind is bool:
                value = bool(variable.get())
                if value != (self.settings.get(name, True) is not False):
                    changes[name] = value
                continue
            text = str(variable.get()).strip()
            if not text:
                if name in self.settings:
                    changes[name] = None
                continue
            try:
                value = kind(text)
            except ValueError:
                raise ValueError(f'{name} has to be a {"whole " if kind is int else ""}'
                                 f'number, not {text!r}') from None
            if value != self.settings.get(name):
                changes[name] = value
        return changes

    def _save(self) -> None:
        """Write the changed settings to the file and connect with them.

        Changed sunset settings also install or update the timer which moves
        the switch times; looking up a location needs the internet.
        """
        try:
            changes = self._changes()
            sun_changes = self._sunset_changes()
        except ValueError as e:
            messagebox.showerror('Setup', f'The settings are not saved: {e}', parent=self)
            return
        if not changes and not sun_changes:
            self.app.note('nothing changed')
            return
        if sun_changes:
            try:
                self.sun = sunset.configure(self.source, **sun_changes)
                sunset.install_timer(self.source, Path(sys.argv[0]).absolute(), self.sun)
            except (ValueError, sunset.SunsetError) as e:
                messagebox.showerror('Setup', f'The sunset settings are not saved completely: '
                                     f'{e}', parent=self)
                return
            finally:
                self.sun = self.app.sunset_settings = self.app.load_sunset()
                self._fill_sunset()
            self.app.note('sunset settings saved')
        if changes:
            try:
                settings = config.write_settings(self.source, changes)
                self.settings = config.read_settings(self.source)
            except config.ConfigError as e:
                messagebox.showerror('Setup', f'The settings are not saved: {e}', parent=self)
                return
            self.app.reconnect(settings)

    def _revert(self) -> None:
        """Fill in the settings as the file has them, dropping what was typed."""
        try:
            self.settings = config.read_settings(self.source)
        except config.ConfigError as e:
            messagebox.showerror('Setup', str(e), parent=self)
            return
        for name, variable in self.fields.items():
            value = self.settings.get(name)
            if config.SETTINGS[name] is bool:
                variable.set(value is not False)
            else:
                variable.set('' if value is None else str(value))
        self.sun = self.app.sunset_settings = self.app.load_sunset()
        self._fill_sunset()


class TimesDialog(tk.Toplevel):
    """The switch times of one device, with the means to change them."""

    def __init__(self, app: 'Application', device: Device) -> None:
        """Open the window and read the current programming."""
        super().__init__(app)
        self.app = app
        self.device = device
        self.timer: Timer | None = None
        self.title(f'{device.name} \N{EN DASH} switch times')

        body = ttk.Frame(self, padding=12)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        ttk.Label(body, text=device.name, font=app.bold).grid(row=0, column=0, sticky='w')

        self.table = ttk.Treeview(body, columns=('time', 'days', 'action', 'active'),
                                  show='headings', selectmode='browse', height=TIMES_SHOWN)
        for column, heading, width in (('time', 'Time', 150), ('days', 'Days', 160),
                                       ('action', 'Action', 120), ('active', 'Active', 70)):
            self.table.heading(column, text=heading, anchor='w')
            self.table.column(column, width=width, anchor='w')
        self.table.grid(row=1, column=0, sticky='nsew', pady=(8, 0))
        self.table.bind('<Double-1>', lambda _: self._edit())
        self.table.bind('<<TreeviewSelect>>', lambda _: self._enable())

        self.next = ttk.Label(body, text='', font=app.small, style='Subtle.TLabel')
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
        _make_modal(self, app)

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
        path = self.app.project.path(self.device)
        for entry in sorted(timer.switch_times, key=lambda e: e.time):
            mark = _sunset_mark(self.app.sunset_settings.link(path, entry.index))
            self.table.insert('', 'end', iid=str(entry.index),
                              values=(f'{entry.when[:5]}  {mark}'.strip(), entry.days,
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
            timer = schedule.remove_time(self.app.stack, self.device, entry.index)
            if (source := self.app.stack.config.source) is not None:
                sunset.unlink(source, self.app.project.path(self.device), entry.index)
        except SCHEDULE_ERRORS as e:
            messagebox.showerror('Remove switch time', str(e), parent=self)
            return
        finally:
            self.app.sunset_settings = self.app.load_sunset()
        self.show(timer)


class ActionSlider(ttk.Frame):
    """Chooses what a switch time does to a light, from off to on or full brightness.

    A dimmable light gets a position for every brightness its timer offers,
    anything else the two positions off and on.  The action of the position
    chosen is what :meth:`get` returns, in the form :func:`schedule.action`
    takes; it is empty as long as a switch time no single action describes was
    not changed.
    """

    WORDS = ('off', 'on')

    def __init__(self, master: tk.Misc, offered: list[str], current: str | None,
                 new: bool, changed: Callable[[], None]) -> None:
        """Lay out the slider at the current action, or at its top for a new switch time."""
        super().__init__(master)
        self.positions = self._positions(offered, current)
        self.changed = changed
        self.chosen = current if current in self.positions else None
        if self.chosen is None and new:
            self.chosen = self.positions[-1]
        self.scale = ttk.Scale(self, from_=0, to=len(self.positions) - 1, orient='horizontal',
                               length=SLIDER_LENGTH, command=self._moved)
        self.scale.pack(side='left')
        self.label = ttk.Label(self, width=5, anchor='e')
        self.label.pack(side='left', padx=(8, 0))
        self._show(self.positions.index(self.chosen) if self.chosen is not None else 0)

    @classmethod
    def suits(cls, offered: list[str]) -> bool:
        """Return whether the actions offered are those of a light."""
        return bool(offered) and all(a in cls.WORDS or a.endswith('%') for a in offered)

    def get(self) -> str:
        """Return the action chosen, empty if none was."""
        return self.chosen or ''

    def _positions(self, offered: list[str], current: str | None) -> list[str]:
        """Return the actions from off to on, the brightness levels if there are any.

        An action a switch time has already decides between the two kinds.
        """
        levels = sorted((a for a in offered if a.endswith('%')), key=lambda a: int(a[:-1]))
        words = [a for a in self.WORDS if a in offered]
        return words if current in words or not levels else levels

    def _moved(self, value: str) -> None:
        """Snap to the nearest position and take its action."""
        position = round(float(value))
        if self.positions[position] != self.chosen:
            self.chosen = self.positions[position]
            self.changed()
        self._show(position)

    def _show(self, position: int) -> None:
        """Put the slider on a position and name its action."""
        if float(self.scale.get()) != position:
            self.scale.set(position)
        action = self.positions[position]
        self.label.configure(text='\N{EM DASH}' if self.chosen is None
                             else 'Off' if action in ('off', '0%') else
                             'On' if action == 'on' else action)


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
        self.resizable(False, False)

        body = ttk.Frame(self, padding=12)
        body.pack(fill='both', expand=True)

        # The switch time acts either at a time of day or at the sunset or the
        # sunrise plus an offset; the latter is moved after the sun from time to
        # time.  Which of the two depends on what the switch time does.
        self.path = owner.app.project.path(device)
        self.sun = owner.app.sunset_settings
        self.link = self.sun.link(self.path, entry.index) if entry is not None else None
        self.mode = tk.StringVar(self, value='sun' if self.link is not None else 'clock')
        # The offset follows the default for the event until it is changed.
        self.offset_chosen = self.link is not None
        self.shown_event = ''

        hour, minute = (entry.time.split(':')[:2] if entry is not None and ':' in entry.time
                        else ('07', '00'))
        ttk.Label(body, text='Time').grid(row=0, column=0, sticky='w', pady=4)
        clock = ttk.Frame(body)
        clock.grid(row=0, column=1, sticky='w', pady=4)
        ttk.Radiobutton(clock, text='At', value='clock', variable=self.mode,
                        command=self._show_mode).pack(side='left', padx=(0, 6))
        self.hour = ttk.Spinbox(clock, from_=0, to=23, width=3, format='%02.0f', wrap=True)
        self.hour.set(hour)
        self.hour.pack(side='left')
        ttk.Label(clock, text=':').pack(side='left', padx=2)
        self.minute = ttk.Spinbox(clock, from_=0, to=59, width=3, format='%02.0f', wrap=True)
        self.minute.set(minute)
        self.minute.pack(side='left')
        self.original_time = (hour, minute)

        evening = ttk.Frame(body)
        evening.grid(row=1, column=1, sticky='w', pady=4)
        self.at_sun = ttk.Radiobutton(evening, text='At sunset', value='sun', width=10,
                                      variable=self.mode, command=self._show_mode)
        self.at_sun.pack(side='left', padx=(0, 6))
        self.offset = ttk.Spinbox(evening, from_=-sunset.MAX_OFFSET, to=sunset.MAX_OFFSET,
                                  increment=5, width=5, command=self._offset_chosen)
        if self.link is not None:
            self.offset.set(str(self.link.offset))
        self.offset.bind('<KeyRelease>', lambda _: self._offset_chosen())
        self.offset.pack(side='left')
        ttk.Label(evening, text='minutes after, negative for before').pack(side='left',
                                                                           padx=(6, 0))
        # A covering going up is not raised before a time of day.  The field is
        # shown for every covering, but only takes effect for one going up at
        # a time following the sunset.
        point = device.point(COVER_POINT)
        self.covering = point is not None and point.usable
        self.limit = ttk.Frame(body)
        self.limit.grid(row=2, column=1, sticky='w', pady=(0, 4))
        earliest = (self.link.earliest if self.link is not None and self.link.earliest
                    else sunset.EARLIEST_UP)
        ttk.Label(self.limit, text='Not before').pack(side='left', padx=(0, 6))
        self.earliest_hour = ttk.Spinbox(self.limit, from_=0, to=23, width=3, format='%02.0f',
                                         wrap=True, command=self._show_today)
        self.earliest_hour.set(earliest[:2])
        self.earliest_hour.pack(side='left')
        ttk.Label(self.limit, text=':').pack(side='left', padx=2)
        self.earliest_minute = ttk.Spinbox(self.limit, from_=0, to=59, width=3,
                                           format='%02.0f', wrap=True, command=self._show_today)
        self.earliest_minute.set(earliest[3:5])
        self.earliest_minute.pack(side='left')
        ttk.Label(self.limit, text='at sunrise, the covering is never raised earlier'
                  ).pack(side='left', padx=(6, 0))
        for spinbox in (self.earliest_hour, self.earliest_minute):
            spinbox.bind('<KeyRelease>', lambda _: self._show_today())
        self.today = ttk.Label(body, text='', font=owner.app.small, style='Subtle.TLabel')
        self.today.grid(row=3, column=1, sticky='w')
        if not self.sun.located:
            self.at_sun.state(['disabled'])
            self.offset.state(['disabled'])

        ttk.Label(body, text='Days').grid(row=4, column=0, sticky='nw', pady=4)
        days = ttk.Frame(body)
        days.grid(row=4, column=1, sticky='w', pady=4)
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
        ttk.Label(body, text='Action').grid(row=5, column=0, sticky='w', pady=4)
        offered = schedule.actions(device, timer)
        # A light is switched or dimmed with a slider, anything else chooses its
        # action from a list.
        self.what: ActionSlider | ttk.Combobox
        if not self.covering and ActionSlider.suits(offered):
            self.what = ActionSlider(body, offered, self.original_what, entry is None,
                                     self._show_mode)
        else:
            self.what = ttk.Combobox(body, values=offered, width=12)
            self.what.bind('<<ComboboxSelected>>', lambda _: self._show_mode())
            self.what.bind('<KeyRelease>', lambda _: self._show_mode())
            if entry is None:
                self.what.set(offered[0] if offered else '')
            elif self.original_what is not None:
                self.what.set(self.original_what)
            else:
                self.what.set('')
        self.what.grid(row=5, column=1, sticky='w', pady=4)
        if entry is not None and self.original_what is None:
            # A scene no single action describes stays as it is unless one is chosen.
            ttk.Label(body, text=f'now: {_what(timer, entry, owner.app.project, device)}',
                      font=owner.app.small, style='Subtle.TLabel').grid(row=6, column=1,
                                                                      sticky='w')

        self.active = tk.BooleanVar(self, value=entry.active if entry is not None else True)
        ttk.Checkbutton(body, text='Active', variable=self.active).grid(row=7, column=1,
                                                                        sticky='w', pady=4)

        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, columnspan=2, sticky='e', pady=(10, 0))
        ttk.Button(buttons, text='Cancel', command=self.destroy).pack(side='right')
        ttk.Button(buttons, text='OK', command=self._apply).pack(side='right', padx=(0, 4))
        self.bind('<Return>', lambda _: self._apply())
        self.bind('<Escape>', lambda _: self.destroy())

        self._show_mode()
        _make_modal(self, owner)
        self.hour.focus_set()

    def _event(self) -> str:
        """Return what the switch time follows as it is chosen, the sunset or the sunrise."""
        what = self.what.get().strip() or self.original_what or ''
        if self.link is not None and what == self.link.action:
            return self.link.event
        return sunset.event_for(what)

    def _offset_chosen(self) -> None:
        """Note that the offset was chosen, so that it no longer follows the default."""
        self.offset_chosen = True
        self._show_today()

    def _show_mode(self) -> None:
        """Enable the fields of the kind of time chosen, a time of day or the sun.

        The choice of the sun names the event the action follows, and the offset
        becomes the default for it unless one was chosen.
        """
        event = self._event()
        if event != self.shown_event:
            self.shown_event = event
            self.at_sun.configure(text=f'At {event}')
            if not self.offset_chosen:
                self.offset.set(str(sunset.default_offset(self.device, event)))
        sunny = self.mode.get() == 'sun'
        for widget in (self.hour, self.minute):
            widget.state(['disabled'] if sunny else ['!disabled'])
        if self.sun.located:
            self.offset.state(['!disabled'] if sunny else ['disabled'])
        if self.covering:
            self.limit.grid()
        else:
            self.limit.grid_remove()
        applies = sunny and self._going_up() and self.sun.located
        for widget in (self.earliest_hour, self.earliest_minute):
            widget.state(['!disabled'] if applies else ['disabled'])
        self._show_today()

    def _going_up(self) -> bool:
        """Return whether the switch time raises a covering, now or as chosen."""
        what = self.what.get().strip() or self.original_what or ''
        return what.lower() == sunset.UP

    def _earliest(self) -> str | None:
        """Return the earliest time chosen for a covering going up, None for anything else."""
        if not self._going_up():
            return None
        return schedule.parse_time(f'{self.earliest_hour.get()}:{self.earliest_minute.get()}')[:5]

    def _show_today(self) -> None:
        """Say what the sunset or sunrise plus the offset comes to today."""
        if not self.sun.located:
            self.today.configure(text='Set the location of the house in the setup to use '
                                      'the sunset and sunrise.')
            return
        if self.mode.get() != 'sun':
            self.today.configure(text='')
            return
        event = self._event()
        try:
            offset = int(self.offset.get() or 0)
            sun, on = sunset.time_at(self.sun, offset, event=event)
            earliest = self._earliest() or ''
            time = sunset.switch_time(self.sun, offset, earliest, event=event)
            limited = f' (not before {earliest})' if time != on.strftime('%H:%M') else ''
            self.today.configure(text=f'Today: {event} {sun:%H:%M}, so at {time}{limited}.  '
                                      f'The time follows the {event} from day to day.')
        except (ValueError, sunset.SunsetError):
            self.today.configure(text='')

    def _choose_days(self, days: set[int]) -> None:
        """Tick exactly the given days."""
        for i, var in enumerate(self.days):
            var.set(i in days)

    def _apply(self) -> None:
        """Program what was entered and close, or explain why it cannot be done."""
        days = {i for i, var in enumerate(self.days) if var.get()}
        what = self.what.get().strip()
        app = self.owner.app
        try:
            if not days:
                raise ValueError('choose at least one day')
            if self.entry is None and not what:
                raise ValueError('choose what the switch time does')
            if self.mode.get() == 'sun':
                timer, index = self._apply_sun(days, what)
            else:
                timer, index = self._apply_clock(days, what)
        except SCHEDULE_ERRORS as e:
            messagebox.showerror(self.title(), str(e), parent=self)
            return
        finally:
            app.sunset_settings = app.load_sunset()
        self.owner.show(timer, index)
        self.destroy()

    def _apply_clock(self, days: set[int], what: str) -> tuple[Timer, int]:
        """Program a switch time acting at a time of day."""
        app = self.owner.app
        when = schedule.parse_time(f'{self.hour.get()}:{self.minute.get()}')
        if self.entry is None:
            return schedule.add_time(app.stack, self.device, when, what, days,
                                     self.active.get())
        index = self.entry.index
        # Leaving the sunset sets the time even if the fields were not touched.
        unchanged = (self.hour.get(), self.minute.get()) == self.original_time
        timer = schedule.change_time(
            app.stack, self.device, index,
            when=None if unchanged and self.link is None else when,
            days=None if days == self.entry.day_numbers else days,
            active=None if self.active.get() == self.entry.active else self.active.get(),
            what=None if not what or what == self.original_what else what)
        if self.link is not None:
            sunset.unlink(self._source(), self.path, index)
        return timer, index

    def _apply_sun(self, days: set[int], what: str) -> tuple[Timer, int]:
        """Program a switch time following the sun, and have the timer move it."""
        app = self.owner.app
        try:
            offset = int(self.offset.get().strip() or 0)
        except ValueError:
            raise ValueError('the offset has to be a whole number of minutes') from None
        source = self._source()
        if self.entry is None:
            timer, index = sunset.add_time(app.stack, source, self.device, self.path, offset,
                                           what, days, self.active.get(), self._earliest(),
                                           self._event())
        else:
            index = self.entry.index
            timer = sunset.change_time(
                app.stack, source, self.device, self.path, index, offset,
                days=None if days == self.entry.day_numbers else days,
                active=None if self.active.get() == self.entry.active else self.active.get(),
                what=None if not what or what == self.original_what else what,
                earliest=self._earliest(), event=self._event())
        if sunset.ensure_timer(source, Path(sys.argv[0]).absolute()):
            app.note('installed the timer which moves switch times after the sunset')
        return timer, index

    def _source(self) -> Path:
        """Return the configuration file, which records the switch times following the sunset."""
        if (source := self.owner.app.stack.config.source) is None:
            raise sunset.SunsetError('the settings did not come from a file')
        return source


def _make_modal(dialog: tk.Toplevel, parent: tk.Misc) -> None:
    """Make a dialog modal, so that its parent and all windows below it take no input.

    The dialog stays above its parent and is marked as a modal dialog for the
    window manager, which then may attach it to the parent so that both move
    together, as GNOME does.  Tk does not nest grabs, so when the dialog is
    closed the grab goes back to the parent if that is a dialog itself, and so
    does the keyboard focus.
    """
    dialog.transient(parent)
    try:
        # Only X11 knows window types; elsewhere being transient suffices.
        dialog.attributes('-type', 'dialog')
    except tk.TclError:
        pass
    dialog.wait_visibility()
    dialog.grab_set()
    x11.mark_modal(dialog)

    def closed(event: tk.Event) -> None:
        # Children of the dialog report their destruction here as well.
        if event.widget is not dialog or not parent.winfo_exists():
            return
        if isinstance(parent, tk.Toplevel):
            parent.grab_set()
        parent.focus_set()

    dialog.bind('<Destroy>', closed, add='+')


def _sunset_mark(link: sunset.Link | None) -> str:
    """Return how a switch time following the sunset is marked, empty for any other."""
    if link is None:
        return ''
    sun = ('\N{WHITE SUN WITH RAYS}' if link.event == sunset.SUNRISE
           else '\N{BLACK SUN WITH RAYS}')
    return f'{sun} {link.text}'


def _license_text() -> str | None:
    """Return the full text of the license, if it can be found."""
    for path in LICENSE_FILES:
        try:
            return path.read_text(encoding='utf-8')
        except OSError:
            continue
    return None


def _clickable(area: tk.Frame, command: Callable[[], object], background: str,
               hover: str) -> None:
    """Run a command when an area is clicked anywhere, including on the text in it.

    The area is shaded while the pointer is over it, and the keyboard can
    activate it too once it has the focus.
    """
    widgets = (area, *area.winfo_children())

    def shade(inside: bool) -> None:
        for widget in widgets:
            widget.configure(background=hover if inside else background)

    for widget in widgets:
        widget.bind('<Button-1>', lambda _: command())
        widget.bind('<Enter>', lambda _: shade(True))
        widget.bind('<Leave>', lambda _: shade(False))
    area.bind('<Return>', lambda _: command())
    area.bind('<space>', lambda _: command())


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
    app = Application(stack, project)
    app.mainloop()
    # A connection made with changed settings is the interface's own to close.
    if app.stack is not stack:
        app.stack.close()
    return 0
