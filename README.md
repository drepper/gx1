# Gira X1 Controller

Programs and libraries to control a [Gira X1](https://www.gira.de/) home
automation server: switch and dim lights, drive blinds, show the state of every
device and program switch times, from the command line or from a small
graphical interface.

They talk to the X1 over the *Device Stack API*, the WebSocket protocol the Gira
Smart Home app itself uses.  Unlike the documented IoT REST API, which needs
firmware 2.4 or later, it is available on every firmware version.  The protocol
is not published; [doc/protocol.md](doc/protocol.md) describes it as observed
and verified against a real device.

## Contents

| Path | What it is |
|------|------------|
| `x1.py` | The program: command line and graphical interface |
| `girax1/` | The Python library behind it |
| `cxx/` | A C++ library and a command line program with the same commands |
| `doc/protocol.md` | Description of the Device Stack API |
| `girax1.toml.example` | Template of the configuration file |
| `data/` | Desktop file and icon |
| `girax1.spec.in` | Template of the RPM spec file |
| `proxy.py`, `probe.py`, `x1dump.py`, `x1ctl.py`, `x1connect.py`, `x1info.py` | Development helpers, see below |

## Configuration

The connection details live in a TOML file, by default `~/.config/girax1.toml`.
It holds the credentials of the X1, so it is never part of the repository and
should only be readable by you:

    cp girax1.toml.example ~/.config/girax1.toml
    chmod 600 ~/.config/girax1.toml

Fill in the host name or address of the X1 and the user name and password the
Gira app uses.  A different file can be given with `-c FILE` or through the
`GIRAX1_CONFIG` environment variable.

The programs add an entry under `[blinds.seconds]` for every blind the first
time they run.  It records how long the blind needs from fully open to fully
closed, which closing by a percentage relies on; measure your blinds and correct
the entries.

## Using the program

Run without a command, `x1.py` opens the graphical interface.  With a command it
acts from the command line.  Devices are addressed by the room they are in and
their name, as in `Flur/Deckenlampen Flur`.  A bare name, a room with a trailing
slash such as `Küche/`, or a pattern such as `*/Rollo*` work as well.

    x1.py status                         the state of every device
    x1.py status "Küche/"                the state of one room
    x1.py list --by location             all devices, by room
    x1.py on "Flur/Deckenlampen Flur"    switch a light on
    x1.py dim 40 "Wohnzimmer/Decke"      set a brightness
    x1.py down "Küche/"                  lower every blind in the kitchen
    x1.py close 70 "*/Rollo*"            close blinds by 70 percent
    x1.py timers                         the programmed switch times
    x1.py addtime "Flur/Deckenlampen Flur" 06:30 on --days weekdays --active
    x1.py settime "Flur/Deckenlampen Flur" 1 06:45
    x1.py deltime "Flur/Deckenlampen Flur" 1

`x1.py --help` and `x1.py COMMAND --help` list every command and option.

### The graphical interface

The interface starts with one tile per room; clicking a tile opens the room.
Every device in it gets a tile with controls that suit it:

- Lights have a round button that switches them, green when on and red when
  off.  Dimmable lights are ringed by their brightness; press or drag along
  the ring to change it.
- Blinds have buttons to raise, stop and lower them.
- Below the controls are the switch times of the device.  Clicking them opens a
  window to add, change and remove switch times.

The display follows changes made from wall switches or the Gira app as they
happen.  A room with more devices than fit into the window is split into pages.

The menu on the room selection screen chooses a light or dark appearance, or
following the desktop, and leads to a setup screen for the configuration file
and to an About window.  The appearance chosen is kept in
`~/.config/girax1-gui.toml`.

The interface uses Tk, which comes with Python but is packaged separately on
some systems (`python3-tkinter` on Fedora).

## Requirements

- Python 3.12 or later.  The program and the library need nothing beyond the
  standard library.
- Only `x1info.py`, which talks to the device portal, needs the `cryptography`
  package listed in `requirements.txt`.
- On Linux, following the desktop appearance uses `gdbus` or `gsettings`.

## Installing as an RPM

    make rpm

builds packages from the working tree, with the version defined in
`girax1/__init__.py`; changes need not be committed first, but new files must
not be ignored by git.  The results are in `build/rpm/RPMS/`:

- `girax1` installs the program as `/usr/bin/x1` together with a desktop entry,
  so the interface can be started from the desktop's application menu.
- `girax1-libs` holds the shared C++ library.
- `girax1-devel` holds what is needed to build C++ programs with the library:
  the headers, the static library, the link to the shared one and a
  pkg-config file.

## The C++ implementation

`cxx/` holds a C++ version of the library and of the command line program, with
the same commands and the same configuration file but no graphical interface.
It adds `watch`, which prints value changes as they happen.  It needs a compiler for C++23 or later, GnuTLS, jsoncpp and
toml++:

    make

builds `build/x1` and the library, as `build/libgirax1.a` and
`build/libgirax1-VERSION.so`; nothing is written next to the sources.

    make install prefix=/usr libdir=/usr/lib64

installs the library, its headers and a pkg-config file (the program is not
installed, since the Python one has that name).  `DESTDIR` installs into a
staging directory.  Programs using the library need C++23 or later and build
with

    g++ -std=c++23 program.cc $(pkg-config --cflags --libs girax1)

The shared library's soname, `libgirax1.so.1`, names its interface rather than
the version.  It only changes when `SOVERSION` in the `Makefile` is raised,
which is needed whenever a change breaks programs built against an earlier
version.

## Development helpers

These were used to work out the protocol and are kept for that purpose:

- `proxy.py` sits between a client such as the Gira app and the X1 and prints
  every HTTP request and WebSocket frame, optionally terminating TLS.
- `probe.py` looks for the paths under which the X1 serves its REST API.
- `x1dump.py` dumps everything the Device Stack API will tell.
- `x1ctl.py` sends single Device Stack commands.
- `x1connect.py` tests a connection with a configuration file.
- `x1info.py` asks the device portal about the device, which also works on
  firmware too old for the REST API.

## License

Licensed under the Creative Commons Attribution-NonCommercial-NoDerivatives 4.0
International license (CC BY-NC-ND 4.0); see [LICENSE](LICENSE).  It may be
copied and passed on for non-commercial purposes, giving credit to the author,
but not in changed form.
