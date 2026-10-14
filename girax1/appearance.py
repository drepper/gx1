"""Light and dark appearance of the graphical interface.

The interface can be shown light or dark, or follow the setting of the
desktop.  What was chosen is remembered in a small file of its own, kept apart
from the configuration file which holds the credentials.
"""

import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# The choices offered, in the order they are shown.
LIGHT = 'light'
DARK = 'dark'
AUTO = 'auto'
CHOICES = (LIGHT, DARK, AUTO)

# Where the choice is remembered.
SETTINGS = Path(os.environ.get('XDG_CONFIG_HOME', '~/.config')).expanduser() / 'girax1-gui.toml'

# How long asking the desktop for its setting may take, in seconds.
QUERY_TIMEOUT = 2


@dataclass(frozen=True, slots=True)
class Palette:
    """The colours of one appearance."""

    dark: bool
    background: str
    text: str
    muted: str
    subtle: str
    field: str
    button: str
    button_active: str
    border: str
    select: str
    select_text: str
    disabled: str
    times: str
    hover: str
    inactive: str
    ring_track: str


LIGHT_PALETTE = Palette(dark=False, background='#dcdad5', text='#000000', muted='#666666',
                        subtle='#4d4d4d', field='#ffffff', button='#e6e4df',
                        button_active='#f2f1ee', border='#9e9a91', select='#4a6984',
                        select_text='#ffffff', disabled='#a3a3a3', times='#f4f4f4',
                        hover='#e2e8f0', inactive='#7f7f7f', ring_track='#a8a8a8')

DARK_PALETTE = Palette(dark=True, background='#202226', text='#e6e6e6', muted='#9a9a9a',
                       subtle='#b4b4b4', field='#2b2e33', button='#34373d',
                       button_active='#41454c', border='#4b4f56', select='#3d6a99',
                       select_text='#ffffff', disabled='#666a70', times='#2a2d32',
                       hover='#38414f', inactive='#7c7f85', ring_track='#565b62')


def load() -> str:
    """Return the appearance chosen last time, following the desktop if there is none."""
    try:
        with SETTINGS.open('rb') as fp:
            choice = tomllib.load(fp).get('appearance', AUTO)
    except (OSError, tomllib.TOMLDecodeError):
        return AUTO
    return choice if choice in CHOICES else AUTO


def save(choice: str) -> None:
    """Remember the appearance chosen.  Failing to do so is not worth an error."""
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(f'appearance = "{choice}"\n', encoding='utf-8')
    except OSError:
        pass


def palette(choice: str) -> Palette:
    """Return the colours for a choice, asking the desktop when it is to be followed."""
    if choice == AUTO:
        return DARK_PALETTE if system_prefers_dark() else LIGHT_PALETTE
    return DARK_PALETTE if choice == DARK else LIGHT_PALETTE


def system_prefers_dark() -> bool:
    """Return whether the desktop is set to a dark appearance."""
    if sys.platform == 'darwin':
        return _run('defaults', 'read', '-g', 'AppleInterfaceStyle').strip() == 'Dark'
    if sys.platform == 'win32':
        return _windows_prefers_dark()
    # The desktop portal answers for every desktop that implements it, with
    # 1 meaning dark.  GNOME's own setting covers systems without the portal.
    answer = _run('gdbus', 'call', '--session', '--dest', 'org.freedesktop.portal.Desktop',
                  '--object-path', '/org/freedesktop/portal/desktop', '--method',
                  'org.freedesktop.portal.Settings.ReadOne', 'org.freedesktop.appearance',
                  'color-scheme')
    if 'uint32' in answer:
        return 'uint32 1' in answer
    if 'dark' in _run('gsettings', 'get', 'org.gnome.desktop.interface', 'color-scheme'):
        return True
    return 'dark' in _run('gsettings', 'get', 'org.gnome.desktop.interface', 'gtk-theme').lower()


def _windows_prefers_dark() -> bool:
    """Return whether Windows is set to show applications dark."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as key:
            return winreg.QueryValueEx(key, 'AppsUseLightTheme')[0] == 0
    except (ImportError, OSError):
        return False


def _run(*command: str) -> str:
    """Run a command and return what it prints, or nothing if it cannot be run."""
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=QUERY_TIMEOUT,
                              check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ''
