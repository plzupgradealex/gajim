# Copyright (C) 2006 Nikos Kouremenos <kourem AT gmail.com>
# Copyright (C) 2006-2007 Jean-Marie Traissard <jim AT lapin.org>
# Copyright (C) 2006-2014 Yann Leboulanger <asterix AT lagaule.org>
# Copyright (C) 2007 Lukas Petrovicky <lukas AT petrovicky.net>
#                    Julien Pivotto <roidelapluie AT gmail.com>
# Copyright (C) 2008 Jonathan Schleifer <js-gajim AT webkeks.org>
#
# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from typing import Any

import logging
import sys

from gi.repository import Gio
from gi.repository import GLib
from gi.repository import Gtk

from gajim.common import app
from gajim.common import configpaths
from gajim.common import events
from gajim.common import ged
from gajim.common.client import Client
from gajim.common.const import SimpleClientState
from gajim.common.dbus.menu import DBusMenu
from gajim.common.dbus.menu import DBusMenuItem
from gajim.common.dbus.statusnotifieritem import StatusNotifierItemService
from gajim.common.ged import EventHelper
from gajim.common.i18n import _
from gajim.common.util.status import get_global_show
from gajim.common.util.status import get_uf_show

from gajim.gtk.util.icons import get_tray_icon_name
from gajim.gtk.util.window import open_window

log = logging.getLogger("gajim.gtk.trayicon")

if sys.platform == "win32":
    import pystray
    from PIL import Image

if sys.platform == "darwin":
    from AppKit import NSImage
    from AppKit import NSStatusBar
    from AppKit import NSStatusItem
    from AppKit import NSVariableStatusItemLength
    from Foundation import NSData
    import objc


class TrayIcon:
    def __init__(self) -> None:
        app.settings.connect_signal("show_trayicon", self._on_setting_changed)

        if sys.platform == "win32":
            self._backend = WindowsTrayIcon()
        elif sys.platform == "darwin":
            # macOS has no StatusNotifierItem/AppIndicator host (those are
            # Linux-only DBus services), and Gtk.StatusIcon was removed in
            # GTK4. Use a native NSStatusItem instead. #12239
            self._backend = MacOSTrayIcon()
        else:
            self._backend = LinuxTrayIcon()

    def _on_setting_changed(self, value: bool, *args: Any) -> None:
        self._backend.set_enabled(value)

    def connect_unread_widget(self, widget: Gtk.Widget, signal: str) -> None:
        widget.connect(signal, self._on_unread_count_changed)

    def _on_unread_count_changed(self, *args: Any) -> None:
        if not app.settings.get("trayicon_notification_on_events"):
            return
        self._backend.update_state()

    def is_visible(self) -> bool:
        return self._backend.is_visible()

    def shutdown(self) -> None:
        self._backend.shutdown()


class NoneBackend:
    def update_state(self, init: bool = False) -> None:
        pass

    def set_enabled(self, enabled: bool) -> None:
        pass

    def is_visible(self) -> bool:
        return False

    def shutdown(self) -> None:
        pass


class TrayIconBackend(EventHelper):
    def __init__(self) -> None:
        EventHelper.__init__(self)

        self.register_events(
            [
                ("our-show", ged.GUI1, self._on_our_show),
                ("account-enabled", ged.GUI1, self._on_account_enabled),
            ]
        )

        for client in app.get_clients():
            client.connect_signal("state-changed", self._on_client_state_changed)

    def update_state(self, init: bool = False) -> None:
        raise NotImplementedError

    def is_visible(self) -> bool:
        raise NotImplementedError

    def set_enabled(self, enabled: bool) -> None:
        raise NotImplementedError

    def _on_account_enabled(self, event: events.AccountEnabled) -> None:
        client = app.get_client(event.account)
        client.connect_signal("state-changed", self._on_client_state_changed)

    def _on_client_state_changed(
        self, _client: Client, _signal_name: str, _state: SimpleClientState
    ) -> None:
        self.update_state()

    def _on_our_show(self, _event: events.ShowChanged) -> None:
        self.update_state()

    @staticmethod
    def _on_start_chat() -> None:
        app.app.activate_action("start-chat", GLib.Variant("as", ["", ""]))

    @staticmethod
    def _on_sounds_mute() -> None:
        current_state = app.settings.get("sounds_on")
        app.settings.set("sounds_on", not current_state)

    def _on_show_hide(self) -> None:
        if not app.window.is_visible() or app.window.is_suspended():
            app.window.show_window()
            app.window.present()
        else:
            app.window.hide_window()

    @staticmethod
    def _on_preferences() -> None:
        open_window("Preferences")

    @staticmethod
    def _on_quit() -> None:
        app.app.start_shutdown()

    @staticmethod
    def _on_status_changed(status: str) -> None:
        app.app.change_status(status=status)

    def _on_activate(self) -> None:
        self._on_show_hide()


class WindowsTrayIcon(TrayIconBackend):
    def __init__(self) -> None:
        TrayIconBackend.__init__(self)
        self._tray_icon = self._create_tray_icon()

        if app.settings.get("show_trayicon"):
            self._tray_icon.run_detached()

    def update_state(self, init: bool = False) -> None:
        if not init and app.window.get_total_unread_count():
            self._tray_icon.icon = self._get_icon("message-new")
            return

        show = get_global_show()
        self._tray_icon.icon = self._get_icon(show)

    def set_enabled(self, enabled: bool) -> None:
        self._tray_icon.stop()

        if enabled:
            self._tray_icon = self._create_tray_icon()
            self._tray_icon.run_detached()

    def is_visible(self) -> bool:
        return self._tray_icon.visible

    def shutdown(self) -> None:
        self._tray_icon.stop()

    def _create_tray_icon(self) -> pystray.Icon:
        assert pystray  # type: ignore
        menu_items: list[pystray.MenuItem] = [
            pystray.MenuItem(
                text=_("Show/Hide Window"),
                action=lambda: GLib.idle_add(self._on_show_hide),
                default=True,
            ),
            pystray.MenuItem(
                _("Status"),
                pystray.Menu(
                    pystray.MenuItem(
                        text=get_uf_show("online"),
                        action=lambda: GLib.idle_add(self._on_status_changed, "online"),
                    ),
                    pystray.MenuItem(
                        text=get_uf_show("away"),
                        action=lambda: GLib.idle_add(self._on_status_changed, "away"),
                    ),
                    pystray.MenuItem(
                        text=get_uf_show("xa"),
                        action=lambda: GLib.idle_add(self._on_status_changed, "xa"),
                    ),
                    pystray.MenuItem(
                        text=get_uf_show("dnd"),
                        action=lambda: GLib.idle_add(self._on_status_changed, "dnd"),
                    ),
                    pystray.MenuItem(
                        text=get_uf_show("offline"),
                        action=lambda: GLib.idle_add(
                            self._on_status_changed, "offline"
                        ),
                    ),
                ),
            ),
            pystray.MenuItem(
                text=_("Start Chat…"),
                action=lambda: GLib.idle_add(self._on_start_chat),
            ),
            pystray.MenuItem(
                text=_("Mute Sounds"),
                action=lambda: GLib.idle_add(self._on_sounds_mute),
                checked=self._get_sound_toggle_state,
            ),
            pystray.MenuItem(
                text=_("Preferences"),
                action=lambda: GLib.idle_add(self._on_preferences),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                text=_("Quit"),
                action=lambda: GLib.idle_add(self._on_quit),
            ),
        ]

        return pystray.Icon(
            "Gajim", icon=self._get_icon("online"), menu=pystray.Menu(*menu_items)
        )

    @staticmethod
    def _get_sound_toggle_state(_item_name: str) -> bool:
        return not app.settings.get("sounds_on")

    @staticmethod
    def _get_icon(icon_name: str) -> Image.Image:
        tray_icon_name = get_tray_icon_name(icon_name)
        path = (
            configpaths.get("ICONS")
            / "hicolor"
            / "32x32"
            / "status"
            / f"{tray_icon_name}.png"
        )
        return Image.open(path)  # type: ignore


class LinuxTrayIcon(TrayIconBackend):
    def __init__(self) -> None:
        TrayIconBackend.__init__(self)

        self._shutdown = False
        self._tray_icon: StatusNotifierItemService | None = None

        self._theme_path = None
        if not app.is_flatpak():
            self._theme_path = str(configpaths.get("ICONS"))

        Gio.bus_get(Gio.BusType.SESSION, callback=self._on_finish)

    def _on_finish(self, obj: Any, res: Gio.AsyncResult) -> None:
        try:
            connection = Gio.bus_get_finish(res)
        except Exception as error:
            log.error(error)
            return

        self._tray_icon = StatusNotifierItemService(
            connection, self._get_menu(), self._theme_path, self._on_activate
        )

        log.info("Tray icon init successful")

        if app.settings.get("show_trayicon"):
            self._tray_icon.register()
            self.update_state(init=True)

    def _get_menu(self) -> DBusMenu:
        toogle_state = int(not app.settings.get("sounds_on"))
        return DBusMenu(
            items=[
                DBusMenuItem(
                    id=1, label=_("Show/Hide Window"), callback=self._on_show_hide
                ),
                DBusMenuItem(
                    id=2,
                    label=_("Status"),
                    children_display="submenu",
                    children=[
                        DBusMenuItem(
                            id=3,
                            label=get_uf_show("online"),
                            callback=lambda: self._on_status_changed("online"),
                        ),
                        DBusMenuItem(
                            id=4,
                            label=get_uf_show("away"),
                            callback=lambda: self._on_status_changed("away"),
                        ),
                        DBusMenuItem(
                            id=5,
                            label=get_uf_show("xa"),
                            callback=lambda: self._on_status_changed("xa"),
                        ),
                        DBusMenuItem(
                            id=6,
                            label=get_uf_show("dnd"),
                            callback=lambda: self._on_status_changed("dnd"),
                        ),
                        DBusMenuItem(id=7, type="separator"),
                        DBusMenuItem(
                            id=8,
                            label=get_uf_show("offline"),
                            callback=lambda: self._on_status_changed("offline"),
                        ),
                    ],
                ),
                DBusMenuItem(
                    id=9, label=_("Start Chat…"), callback=self._on_start_chat
                ),
                DBusMenuItem(
                    id=10,
                    label=_("Mute Sounds"),
                    toggle_type="checkmark",
                    toggle_state=toogle_state,
                    callback=self._on_sounds_mute,
                ),
                DBusMenuItem(
                    id=11, label=_("Preferences"), callback=self._on_preferences
                ),
                DBusMenuItem(id=12, type="separator"),
                DBusMenuItem(id=13, label=_("Quit"), callback=self._on_quit),
            ]
        )

    def update_state(self, init: bool = False) -> None:
        if self._tray_icon is None:
            # Tray icon is not initialized
            return

        if self._shutdown:
            # Shutdown in progress, don't update icon
            return

        if not init and app.window.get_total_unread_count():
            icon_name = get_tray_icon_name("message-new")
            self._tray_icon.set_icon(icon_name)
            return

        show = get_global_show()
        icon_name = get_tray_icon_name(show)
        self._tray_icon.set_icon(icon_name)

    def set_enabled(self, enabled: bool) -> None:
        if self._tray_icon is None:
            return

        log.info("Set tray icon enabled: %s", enabled)

        if enabled:
            self._tray_icon.register()
            self.update_state(init=True)
        else:
            self._tray_icon.unregister()

    def is_visible(self) -> bool:
        if self._tray_icon is None:
            return False
        return self._tray_icon.get_visible()

    def shutdown(self) -> None:
        if self._tray_icon is None:
            return
        self._shutdown = True
        self._tray_icon.unregister()


class MacOSTrayIcon(TrayIconBackend):
    """Native macOS menu-bar item via NSStatusItem (pyobjc).

    macOS has no StatusNotifierItem/AppIndicator host (those are Linux DBus
    services), and Gtk.StatusIcon was removed in GTK4 — which is why the tray
    icon that worked in Gajim 1.9.x (GTK3) vanished in the 2.x GTK4 port. This
    backend provides the equivalent of the Windows/Linux backends using a real
    NSStatusItem: a menu-bar icon that reflects presence/unread state, with a
    Show/Hide/Status/Start Chat/Mute/Preferences/Quit menu.
    See https://dev.gajim.org/gajim/gajim/-/issues/12239
    """

    def __init__(self) -> None:
        TrayIconBackend.__init__(self)
        # Keep strong references: pyobjc objects are garbage-collected if no
        # Python reference is held, which would make the status item vanish.
        self._status_item: NSStatusItem | None = None
        self._menu: _GajimStatusMenuDelegate | None = None
        self._enabled = False

        self._status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self._menu = _GajimStatusMenuDelegate.alloc().init(self)
        self._status_item.setMenu_(self._menu.ns_menu)

        if app.settings.get("show_trayicon"):
            self.set_enabled(True)
            self.update_state(init=True)

    def update_state(self, init: bool = False) -> None:
        if self._status_item is None:
            return

        if not init and app.window.get_total_unread_count():
            icon_name = "message-new"
        else:
            icon_name = get_global_show()

        image = self._get_icon(icon_name)
        if image is not None and self._status_item is not None:
            self._status_item.setImage_(image)
            self._status_item.setToolTip_(self._get_tooltip(icon_name))

        if self._menu is not None:
            self._menu.refresh()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        if self._status_item is None:
            return
        # NSStatusItem has no hide call; clear image/menu to hide and restore on
        # enable. Recreating the system item on each toggle leaks status-bar
        # slots, so mutate in place instead.
        if enabled:
            self.update_state(init=True)
        else:
            self._status_item.setImage_(None)
            self._status_item.setMenu_(None)

    def is_visible(self) -> bool:
        return self._enabled

    def shutdown(self) -> None:
        # Remove the item from the menu bar on quit so we don't leak a slot.
        if self._status_item is not None:
            self._status_item.setImage_(None)
            self._status_item.setMenu_(None)
            NSStatusBar.systemStatusBar().removeStatusItem_(self._status_item)
            self._status_item = None
            self._menu = None

    @staticmethod
    def _get_tooltip(icon_name: str) -> str:
        if icon_name == "message-new":
            return _("Gajim – New Events")
        return f"Gajim – {get_uf_show(icon_name)}"

    @staticmethod
    def _get_icon(icon_name: str) -> NSImage | None:
        """Load a gajim-status-* PNG from the icon path as an NSImage.

        Reuses the same status PNGs the Windows backend uses, drawn as a
        menu-bar template image.
        """
        tray_icon_name = get_tray_icon_name(icon_name)
        path = (
            configpaths.get("ICONS")
            / "hicolor"
            / "32x32"
            / "status"
            / f"{tray_icon_name}.png"
        )
        if not path.is_file():
            log.warning("macOS tray icon not found: %s", path)
            return None
        data = NSData.dataWithContentsOfFile_(str(path))
        if data is None:
            log.warning("Could not read macOS tray icon: %s", path)
            return None
        image = NSImage.alloc().initWithData_(data)
        if image is None:
            return None
        # Template mode keeps the icon monochrome so it adapts to light/dark
        # menu-bar appearance, matching the macOS tray-icon convention.
        image.setTemplate_(True)
        image.setSize_((18.0, 18.0))
        return image


class _GajimStatusMenuDelegate(objc.lookUpClass("NSObject")):
    """Builds and owns the NSMenu shown when the menu-bar item is clicked.

    AppKit menu callbacks run off the GLib main loop, so all actions are
    marshalled back to the GTK thread via GLib.idle_add, exactly like the
    Windows/pystray backend does.
    """

    def init(self, owner: MacOSTrayIcon) -> _GajimStatusMenuDelegate:
        self = objc.super(_GajimStatusMenuDelegate, self).init()
        if self is None:
            return None
        self._owner = owner
        self._ns_menu_cls = objc.lookUpClass("NSMenu")
        self._item_cls = objc.lookUpClass("NSMenuItem")
        self.ns_menu = self._ns_menu_cls.alloc().init()
        self._submenu: Any = None
        self._build()
        return self

    def refresh(self) -> None:
        # The whole menu is rebuilt on refresh() so checkmark state (Mute
        # Sounds) and status reflect live settings. Rebuilding an NSMenu on
        # each open is cheap and avoids juggling individual item references.
        self.ns_menu.removeAllItems()
        self._build()

    def _build(self) -> None:
        owner = self._owner

        def add(label: str, action: Any, key: str = "") -> None:
            item = self._item_cls.alloc().initWithTitle_action_keyEquivalent_(
                label, "invoke:", key
            )
            item.setTarget_(self)
            item._gajim_action = action  # type: ignore[attr-defined]
            self.ns_menu.addItem_(item)

        def add_separator() -> None:
            self.ns_menu.addItem_(self._item_cls.separatorItem())

        add(_("Show/Hide Window"), lambda: GLib.idle_add(owner._on_show_hide))

        # Status submenu
        status_menu = self._ns_menu_cls.alloc().init()
        for show in ("online", "away", "xa", "dnd"):
            sub_item = (
                self._item_cls.alloc().initWithTitle_action_keyEquivalent_(
                    get_uf_show(show), "invoke:", ""
                )
            )
            sub_item.setTarget_(self)
            sub_item._gajim_action = (  # type: ignore[attr-defined]
                lambda s=show: GLib.idle_add(owner._on_status_changed, s)
            )
            status_menu.addItem_(sub_item)
        status_menu.addItem_(self._item_cls.separatorItem())
        offline_item = (
            self._item_cls.alloc().initWithTitle_action_keyEquivalent_(
                get_uf_show("offline"), "invoke:", ""
            )
        )
        offline_item.setTarget_(self)
        offline_item._gajim_action = (  # type: ignore[attr-defined]
            lambda: GLib.idle_add(owner._on_status_changed, "offline")
        )
        status_menu.addItem_(offline_item)

        status_parent = (
            self._item_cls.alloc().initWithTitle_action_keyEquivalent_(
                _("Status"), "", ""
            )
        )
        status_parent.setSubmenu_(status_menu)
        self.ns_menu.addItem_(status_parent)
        self._submenu = status_menu  # keep alive

        add(_("Start Chat…"), lambda: GLib.idle_add(owner._on_start_chat))

        mute_item = self._item_cls.alloc().initWithTitle_action_keyEquivalent_(
            _("Mute Sounds"), "invoke:", ""
        )
        mute_item.setTarget_(self)
        mute_item._gajim_action = (  # type: ignore[attr-defined]
            lambda: GLib.idle_add(owner._on_sounds_mute)
        )
        # Reflect current state with a checkmark
        muted = not app.settings.get("sounds_on")
        mute_item.setState_(1 if muted else 0)
        self.ns_menu.addItem_(mute_item)

        add(_("Preferences"), lambda: GLib.idle_add(owner._on_preferences))
        add_separator()
        add(_("Quit"), lambda: GLib.idle_add(owner._on_quit))

    # Single action selector bound to every item; dispatches via the per-item
    # _gajim_action closure stashed above.
    def invoke_(self, _sender) -> None:
        action = getattr(_sender, "_gajim_action", None)
        if action is not None:
            action()
