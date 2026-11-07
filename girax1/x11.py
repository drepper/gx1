"""Telling an X11 window manager that a dialog is modal, and asking it whether a window is
minimized.

Tk makes a dialog transient for its parent and marks it as a dialog, but has
no way to set the modal state of the Extended Window Manager Hints.  Window
managers such as GNOME's attach a modal dialog to its parent, so that both
move together and the parent cannot be handled on its own, only when that
state is set.  This module asks the window manager for it the way the hints
prescribe for a window already shown: with a client message to the root
window.  Tk also does not learn that a window it minimized before it was ever
shown is minimized, which the window manager records in the ``WM_STATE``
property of the window.  Xlib is used directly through ctypes, so nothing beyond the standard
library is needed; where it is not available, or the window system is not
X11, nothing is done.
"""

import ctypes
import ctypes.util
import tkinter as tk

# Constants of Xlib and of the Extended Window Manager Hints.
CLIENT_MESSAGE = 33
SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
SUBSTRUCTURE_REDIRECT_MASK = 1 << 20
NET_WM_STATE_ADD = 1
SOURCE_APPLICATION = 1
SUCCESS = 0
ICONIC_STATE = 3


class _ClientMessage(ctypes.Structure):
    """XClientMessageEvent, with the data as 32-bit items."""

    _fields_ = [('type', ctypes.c_int), ('serial', ctypes.c_ulong), ('send_event', ctypes.c_int),
                ('display', ctypes.c_void_p), ('window', ctypes.c_ulong),
                ('message_type', ctypes.c_ulong), ('format', ctypes.c_int),
                ('data', ctypes.c_long * 5)]


class _Event(ctypes.Union):
    """XEvent, which is padded to 24 longs whatever kind of event it holds."""

    _fields_ = [('client', _ClientMessage), ('pad', ctypes.c_long * 24)]


def _xlib() -> ctypes.CDLL | None:
    """Load Xlib and declare the functions used, or return None if it is missing."""
    if (name := ctypes.util.find_library('X11')) is None:
        return None
    try:
        lib = ctypes.cdll.LoadLibrary(name)
    except OSError:
        return None
    lib.XOpenDisplay.restype = ctypes.c_void_p
    lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
    lib.XInternAtom.restype = ctypes.c_ulong
    lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.XDefaultRootWindow.restype = ctypes.c_ulong
    lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    lib.XQueryTree.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                               ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p),
                               ctypes.POINTER(ctypes.c_uint)]
    lib.XFree.argtypes = [ctypes.c_void_p]
    lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
                               ctypes.c_void_p]
    lib.XFlush.argtypes = [ctypes.c_void_p]
    lib.XGetWindowProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long,
        ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p)]
    return lib


def _wrapper(lib: ctypes.CDLL, display: int, window: tk.Misc) -> int | None:
    """Return the window Tk wraps a toplevel in, the one the window manager knows."""
    root, wrapper = ctypes.c_ulong(), ctypes.c_ulong()
    children, count = ctypes.c_void_p(), ctypes.c_uint()
    if not lib.XQueryTree(display, window.winfo_id(), ctypes.byref(root), ctypes.byref(wrapper),
                          ctypes.byref(children), ctypes.byref(count)):
        return None
    if children:
        lib.XFree(children)
    return wrapper.value


def is_iconic(window: tk.Toplevel | tk.Tk) -> bool | None:
    """Return whether the window manager keeps a toplevel minimized, None if it cannot say."""
    if window.tk.call('tk', 'windowingsystem') != 'x11' or (lib := _xlib()) is None:
        return None
    display = lib.XOpenDisplay(window.winfo_screen().encode())
    if not display:
        return None
    try:
        if (wrapper := _wrapper(lib, display, window)) is None:
            return None
        atom = lib.XInternAtom(display, b'WM_STATE', 0)
        kind, size = ctypes.c_ulong(), ctypes.c_int()
        items, left, data = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
        if lib.XGetWindowProperty(display, wrapper, atom, 0, 2, 0, atom, ctypes.byref(kind),
                                  ctypes.byref(size), ctypes.byref(items), ctypes.byref(left),
                                  ctypes.byref(data)) != SUCCESS:
            return None
        try:
            if size.value != 32 or items.value < 1 or not data:
                return None
            # Items of 32 bits come as longs, whatever their size.
            return ctypes.cast(data, ctypes.POINTER(ctypes.c_long))[0] == ICONIC_STATE
        finally:
            if data:
                lib.XFree(data)
    finally:
        lib.XCloseDisplay(display)


def mark_modal(window: tk.Toplevel) -> bool:
    """Ask the window manager to treat a shown dialog as modal; return whether it was asked.

    The window has to be mapped.  Tk puts every toplevel into a wrapper window
    of its own, and that wrapper is what the window manager knows.
    """
    if window.tk.call('tk', 'windowingsystem') != 'x11' or (lib := _xlib()) is None:
        return False
    display = lib.XOpenDisplay(window.winfo_screen().encode())
    if not display:
        return False
    try:
        if (wrapper := _wrapper(lib, display, window)) is None:
            return False
        event = _Event()
        event.client.type = CLIENT_MESSAGE
        event.client.window = wrapper
        event.client.message_type = lib.XInternAtom(display, b'_NET_WM_STATE', 0)
        event.client.format = 32
        event.client.data[0] = NET_WM_STATE_ADD
        event.client.data[1] = lib.XInternAtom(display, b'_NET_WM_STATE_MODAL', 0)
        event.client.data[3] = SOURCE_APPLICATION
        lib.XSendEvent(display, lib.XDefaultRootWindow(display), 0,
                       SUBSTRUCTURE_NOTIFY_MASK | SUBSTRUCTURE_REDIRECT_MASK, ctypes.byref(event))
        lib.XFlush(display)
        return True
    finally:
        lib.XCloseDisplay(display)
