# Copyright (C) 2003-2014 Yann Leboulanger <asterix AT lagaule.org>
# Copyright (C) 2005-2006 Nikos Kouremenos <kourem AT gmail.com>
# Copyright (C) 2007 Jean-Marie Traissard <jim AT lapin.org>
# Copyright (C) 2008 Mateusz Biliński <mateusz AT bilinski.it>
# Copyright (C) 2008 Thorsten P. 'dGhvcnN0ZW5wIEFUIHltYWlsIGNvbQ==\n'.decode("base64")  # noqa: E501
# Copyright (C) 2018 Philipp Hörist <philipp AT hoerist.com>
#
# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from typing import cast

import ctypes
import ctypes.util
import logging
import sys
import time

from gi.repository import Gio
from gi.repository import GLib
from gi.repository import GObject

from gajim.common import app
from gajim.common.const import Display
from gajim.common.const import IdleState

log = logging.getLogger("gajim.c.idle")


class IdleMonitor:
    def __init__(self):
        self._extended_away = False

    def get_idle_sec(self) -> int:
        raise NotImplementedError

    def set_extended_away(self, state: bool) -> None:
        self._extended_away = state

    def is_extended_away(self) -> bool:
        return self._extended_away


class DBusFreedesktop(IdleMonitor):
    def __init__(self) -> None:
        IdleMonitor.__init__(self)
        self._last_idle_time = 0

        log.debug("Connecting to org.freedesktop.ScreenSaver")
        self._dbus_proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.freedesktop.ScreenSaver",
            "/org/freedesktop/ScreenSaver",
            "org.freedesktop.ScreenSaver",
            None,
        )
        log.debug("Connected")

        # Only the following call will trigger exceptions if the D-Bus
        # interface/method/... does not exist. Using the failing method
        # for class init to allow other idle monitors to be used on failure.
        self._get_idle_sec_fail()
        log.debug("Test successful")

    def _get_idle_sec_fail(self) -> int:
        (idle_time,) = cast(
            tuple[int],
            self._dbus_proxy.call_sync(
                "GetSessionIdleTime", None, Gio.DBusCallFlags.NO_AUTO_START, -1, None
            ),
        )

        return idle_time // 1000

    def get_idle_sec(self) -> int:
        try:
            self._last_idle_time = self._get_idle_sec_fail()
        except GLib.Error as error:
            log.warning(
                "org.freedesktop.ScreenSaver.GetSessionIdleTime() failed: %s", error
            )

        return self._last_idle_time


class DBusGnome(IdleMonitor):
    def __init__(self) -> None:
        IdleMonitor.__init__(self)
        self._last_idle_time = 0

        log.debug("Connecting to org.gnome.Mutter.IdleMonitor")
        self._dbus_proxy = Gio.DBusProxy.new_for_bus_sync(
            Gio.BusType.SESSION,
            Gio.DBusProxyFlags.NONE,
            None,
            "org.gnome.Mutter.IdleMonitor",
            "/org/gnome/Mutter/IdleMonitor/Core",
            "org.gnome.Mutter.IdleMonitor",
            None,
        )
        log.debug("Connected")

        # Only the following call will trigger exceptions if the D-Bus
        # interface/method/... does not exist. Using the failing method
        # for class init to allow other idle monitors to be used on failure.
        self._get_idle_sec_fail()
        log.debug("Test successful")

    def _get_idle_sec_fail(self) -> int:
        (idle_time,) = cast(
            tuple[int],
            self._dbus_proxy.call_sync(
                "GetIdletime", None, Gio.DBusCallFlags.NO_AUTO_START, -1, None
            ),
        )

        return idle_time // 1000

    def get_idle_sec(self) -> int:
        try:
            self._last_idle_time = self._get_idle_sec_fail()
        except GLib.Error as error:
            log.warning("org.gnome.Mutter.IdleMonitor.GetIdletime() failed: %s", error)

        return self._last_idle_time


class Xss(IdleMonitor):
    def __init__(self) -> None:
        IdleMonitor.__init__(self)

        class XScreenSaverInfo(ctypes.Structure):
            _fields_ = [
                ("window", ctypes.c_ulong),
                ("state", ctypes.c_int),
                ("kind", ctypes.c_int),
                ("til_or_since", ctypes.c_ulong),
                ("idle", ctypes.c_ulong),
                ("eventMask", ctypes.c_ulong),
            ]

        XScreenSaverInfo_p = ctypes.POINTER(XScreenSaverInfo)

        display_p = ctypes.c_void_p
        xid = ctypes.c_ulong
        c_int_p = ctypes.POINTER(ctypes.c_int)

        lib_x11_path = ctypes.util.find_library("X11")
        if lib_x11_path is None:
            raise OSError("libX11 could not be found.")

        lib_x11 = ctypes.cdll.LoadLibrary(lib_x11_path)
        lib_x11.XOpenDisplay.restype = display_p
        lib_x11.XOpenDisplay.argtypes = (ctypes.c_char_p,)
        lib_x11.XDefaultRootWindow.restype = xid
        lib_x11.XDefaultRootWindow.argtypes = (display_p,)

        lib_xss_path = ctypes.util.find_library("Xss")
        if lib_xss_path is None:
            raise OSError("libXss could not be found.")

        self._lib_xss = ctypes.cdll.LoadLibrary(lib_xss_path)
        self._lib_xss.XScreenSaverQueryExtension.argtypes = (
            display_p,
            c_int_p,
            c_int_p,
        )
        self._lib_xss.XScreenSaverAllocInfo.restype = XScreenSaverInfo_p
        self._lib_xss.XScreenSaverQueryInfo.argtypes = (
            display_p,
            xid,
            XScreenSaverInfo_p,
        )

        self._dpy_p = lib_x11.XOpenDisplay(None)
        if self._dpy_p is None:
            raise OSError("Could not open X Display.")

        _event_basep = ctypes.c_int()
        _error_basep = ctypes.c_int()
        extension = self._lib_xss.XScreenSaverQueryExtension(
            self._dpy_p, ctypes.byref(_event_basep), ctypes.byref(_error_basep)
        )
        if extension == 0:
            raise OSError("XScreenSaver Extension not available on display.")

        self._xss_info_p = self._lib_xss.XScreenSaverAllocInfo()
        if self._xss_info_p is None:
            raise OSError("XScreenSaverAllocInfo: Out of Memory.")

        self.root_window = lib_x11.XDefaultRootWindow(self._dpy_p)

    def get_idle_sec(self) -> int:
        info = self._lib_xss.XScreenSaverQueryInfo(
            self._dpy_p, self.root_window, self._xss_info_p
        )
        if info == 0:
            return info
        return self._xss_info_p.contents.idle // 1000


class Windows(IdleMonitor):
    def __init__(self) -> None:
        IdleMonitor.__init__(self)
        self._OpenInputDesktop = ctypes.windll.user32.OpenInputDesktop
        self._CloseDesktop = ctypes.windll.user32.CloseDesktop
        self._SystemParametersInfo = ctypes.windll.user32.SystemParametersInfoW
        self._GetTickCount = ctypes.windll.kernel32.GetTickCount
        self._GetLastInputInfo = ctypes.windll.user32.GetLastInputInfo

        self._locked_time = None

        class LastInputInfo(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        self._lastInputInfo = LastInputInfo()
        self._lastInputInfo.cbSize = ctypes.sizeof(self._lastInputInfo)

    def get_idle_sec(self) -> int:
        self._GetLastInputInfo(ctypes.byref(self._lastInputInfo))
        return int(self._GetTickCount() - self._lastInputInfo.dwTime) // 1000

    def set_extended_away(self, state: bool) -> None:
        raise NotImplementedError

    def is_extended_away(self) -> bool:
        # Check if Screen Saver is running
        # 0x72 is SPI_GETSCREENSAVERRUNNING
        saver_runing = ctypes.c_int(0)
        info = self._SystemParametersInfo(0x72, 0, ctypes.byref(saver_runing), 0)
        if info and saver_runing.value:
            return True

        # Check if Screen is locked
        # Also a UAC prompt counts as locked
        # So just return True if we are more than 10 seconds locked
        desk = self._OpenInputDesktop(0, False, 0)
        unlocked = bool(desk)
        self._CloseDesktop(desk)

        if unlocked:
            self._locked_time = None
            return False

        if self._locked_time is None:
            self._locked_time = time.time()
            return False

        threshold = time.time() - 10
        return threshold > self._locked_time


class Darwin(IdleMonitor):
    """macOS idle monitor using CoreGraphics CGEventSourceSecondsSinceLastEventType.

    Extended-away is reported while the screen is locked or the Mac sleeps.
    The state is tracked via NSWorkspace notifications (system sleep / wake,
    display sleep / wake) and distributed notifications (screenIsLocked /
    screenIsUnlocked, posted by loginwindow).
    """

    def __init__(self) -> None:
        IdleMonitor.__init__(self)

        lib_path = ctypes.util.find_library("CoreGraphics")
        if lib_path is None:
            raise OSError("CoreGraphics could not be found.")

        self._cg = ctypes.cdll.LoadLibrary(lib_path)
        self._cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
        self._cg.CGEventSourceSecondsSinceLastEventType.argtypes = [
            ctypes.c_uint32,  # eventSourceStateID
            ctypes.c_uint32,  # eventType
        ]
        log.debug("Idle monitor via CoreGraphics initialized")

        # Extended-away is driven by notifications rather than the manual
        # setter used by some platforms. Start unlocked/awake.
        self._extended_away = False

        # Notification registration is best-effort: if pyobjc is unavailable
        # or observer setup fails, idle-time reporting still works (XA falls
        # back to the idle-time threshold in IdleMonitorManager._poll).
        self._observer = None
        try:
            self._register_notifications()
        except Exception as error:
            log.info("Darwin extended-away notifications unavailable: %s", error)

    def _register_notifications(self) -> None:
        # Imported lazily so the module stays importable on platforms without
        # pyobjc; the Darwin monitor is only constructed on macOS.
        import objc
        from AppKit import NSWorkspace
        from AppKit import NSWorkspaceDidWakeNotification
        from AppKit import NSWorkspaceScreensDidSleepNotification
        from AppKit import NSWorkspaceScreensDidWakeNotification
        from AppKit import NSWorkspaceWillSleepNotification
        from Foundation import NSDistributedNotificationCenter

        # Build the NSObject observer subclass lazily. Defining it at module
        # scope would require pyobjc to be importable at import time on every
        # platform; building it here keeps non-darwin builds dependency-free.
        DarwinIdleObserver = objc.lookUpClass("NSObject")

        class _Observer(DarwinIdleObserver):  # type: ignore[misc, valid-type]
            # Holds a strong reference to the monitor and flips its
            # _extended_away flag from the main run loop.
            def initWithMonitor_(self, monitor: Darwin) -> _Observer:
                self = objc.super(_Observer, self).init()  # type: ignore[name-defined]
                if self is None:
                    return None
                self._monitor = monitor
                return self

            def sleepOrWake_(self, notification) -> None:
                # NSWorkspaceWillSleepNotification -> away;
                # NSWorkspaceDidWakeNotification -> awake.
                self._monitor._extended_away = (
                    notification.name() == NSWorkspaceWillSleepNotification
                )
                log.info(
                    "Darwin extended-away (sleep): %s",
                    self._monitor._extended_away,
                )

            def screensSleepOrWake_(self, notification) -> None:
                # Display sleep alone (lid closed / display off) is not a locked
                # session; only clear the flag when the displays wake, to keep
                # state consistent after the system wakes from sleep.
                if notification.name() == NSWorkspaceScreensDidWakeNotification:
                    self._monitor._extended_away = False
                    log.info("Darwin extended-away (screens woke): cleared")

            def screenLockChanged_(self, notification) -> None:
                # 'com.apple.screenIsLocked' / 'com.apple.screenIsUnlocked' are
                # distributed notifications posted by loginwindow.
                self._monitor._extended_away = (
                    notification.name() == "com.apple.screenIsLocked"
                )
                log.info(
                    "Darwin extended-away (lock): %s",
                    self._monitor._extended_away,
                )

        # Hold a strong reference so the observer is not garbage-collected.
        self._observer = _Observer.alloc().initWithMonitor_(self)

        ws_center = NSWorkspace.sharedWorkspace().notificationCenter()
        ws_center.addObserver_selector_name_object_(
            self._observer, "sleepOrWake:", NSWorkspaceWillSleepNotification, None
        )
        ws_center.addObserver_selector_name_object_(
            self._observer, "sleepOrWake:", NSWorkspaceDidWakeNotification, None
        )
        ws_center.addObserver_selector_name_object_(
            self._observer,
            "screensSleepOrWake:",
            NSWorkspaceScreensDidSleepNotification,
            None,
        )
        ws_center.addObserver_selector_name_object_(
            self._observer,
            "screensSleepOrWake:",
            NSWorkspaceScreensDidWakeNotification,
            None,
        )

        dist_center = NSDistributedNotificationCenter.defaultCenter()
        dist_center.addObserver_selector_name_object_(
            self._observer, "screenLockChanged:", "com.apple.screenIsLocked", None
        )
        dist_center.addObserver_selector_name_object_(
            self._observer, "screenLockChanged:", "com.apple.screenIsUnlocked", None
        )

    def get_idle_sec(self) -> int:
        # kCGEventSourceStateHIDSystemState = 1
        # kCGAnyInputEventType = ~0 (UINT32_MAX)
        idle_time = self._cg.CGEventSourceSecondsSinceLastEventType(1, 0xFFFFFFFF)
        return int(idle_time)

    def set_extended_away(self, state: bool) -> None:
        # Extended-away is driven by system notifications on macOS, not by the
        # manual setter. Raise to match the Windows monitor's contract.
        raise NotImplementedError

    def is_extended_away(self) -> bool:
        return self._extended_away


class IdleMonitorManager(GObject.Object):
    __gsignals__ = {
        "state-changed": (
            GObject.SignalFlags.RUN_LAST | GObject.SignalFlags.ACTION,
            None,
            (),
        )
    }

    def __init__(self):
        GObject.Object.__init__(self)
        self.set_interval()
        self._state = IdleState.AWAKE
        self._idle_monitor = self._get_idle_monitor()

        if self.is_available():
            GLib.timeout_add_seconds(5, self._poll)

    def set_interval(self, away_interval: int = 60, xa_interval: int = 120) -> None:

        log.info("Set interval: away: %s, xa: %s", away_interval, xa_interval)
        self._away_interval = away_interval
        self._xa_interval = xa_interval

    def set_extended_away(self, state: bool) -> None:
        if self._idle_monitor is None:
            raise ValueError("No idle monitor available")

        self._idle_monitor.set_extended_away(state)

    def is_available(self) -> bool:
        return self._idle_monitor is not None

    @property
    def state(self) -> IdleState:
        if not self.is_available():
            return IdleState.UNKNOWN
        return self._state

    def is_xa(self) -> bool:
        return self.state == IdleState.XA

    def is_away(self) -> bool:
        return self.state == IdleState.AWAY

    def is_awake(self) -> bool:
        return self.state == IdleState.AWAKE

    def is_unknown(self) -> bool:
        return self.state == IdleState.UNKNOWN

    @staticmethod
    def _get_idle_monitor() -> IdleMonitor | None:
        if sys.platform == "win32":
            return Windows()

        if sys.platform == "darwin":
            try:
                return Darwin()
            except OSError as error:
                log.info("Idle time via CoreGraphics not available: %s", error)
            return None

        try:
            return DBusFreedesktop()
        except GLib.Error as error:
            log.info(
                "Idle time via org.freedesktop.Screensaver not available: %s", error
            )

        try:
            return DBusGnome()
        except GLib.Error as error:
            log.info(
                "Idle time via org.gnome.Mutter.IdleMonitor not available: %s", error
            )

        if app.is_display(Display.WAYLAND):
            return None

        try:
            return Xss()
        except OSError as error:
            log.info("Idle time via XScreenSaverInfo not available: %s", error)

        return None

    def get_idle_sec(self) -> int:
        if self._idle_monitor is None:
            raise ValueError("No idle monitor available")
        return self._idle_monitor.get_idle_sec()

    def _poll(self) -> bool:
        """
        Check to see if we should change state
        """
        assert self._idle_monitor is not None

        if self._idle_monitor.is_extended_away():
            log.info("Extended Away: Screensaver or Locked Screen")
            self._set_state(IdleState.XA)
            return True

        idle_time = self.get_idle_sec()

        # xa is stronger than away so check for xa first
        if idle_time > self._xa_interval:
            self._set_state(IdleState.XA)
        elif idle_time > self._away_interval:
            self._set_state(IdleState.AWAY)
        else:
            self._set_state(IdleState.AWAKE)
        return True

    def _set_state(self, state: IdleState) -> None:
        if self._state == state:
            return

        self._state = state
        log.info("State changed: %s", state)
        self.emit("state-changed")


Monitor = IdleMonitorManager()
