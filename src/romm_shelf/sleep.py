"""Sync before sleep and shutdown, using a logind delay inhibitor.

The daemon holds a "delay" lock on sleep and shutdown. When logind announces
PrepareForSleep or PrepareForShutdown, the daemon syncs and then releases the
lock, which lets the system proceed. logind only waits InhibitDelayMaxSec
(5 s by default), so this sync is kept small: only ROMs whose save or state
files changed since the daemon last synced them.
"""

import threading
from collections.abc import Callable

from jeepney import DBusAddress, HeaderFields, MatchRule, new_method_call
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import open_dbus_connection
from loguru import logger

LOGIN1 = DBusAddress("/org/freedesktop/login1", bus_name="org.freedesktop.login1",
                     interface="org.freedesktop.login1.Manager")
SIGNALS = {"PrepareForSleep": "sleep", "PrepareForShutdown": "shutdown"}


def watch(on_prepare: Callable[[str], None]) -> threading.Thread:
    """Call on_prepare("sleep" | "shutdown") before the system goes down. Runs in a daemon thread."""

    def loop():
        try:
            conn = open_dbus_connection(bus="SYSTEM", enable_fds=True)
        except Exception as e:
            logger.warning("no system bus, sync before sleep disabled: {}", e)
            return

        def inhibit():
            msg = new_method_call(LOGIN1, "Inhibit", "ssss",
                                  ("sleep:shutdown", "romm-shelf", "Sync saves with RomM", "delay"))
            return conn.send_and_get_reply(msg).body[0]

        rule = MatchRule(type="signal", interface=LOGIN1.interface, path=LOGIN1.object_path)
        conn.send_and_get_reply(message_bus.AddMatch(rule))
        with conn.filter(rule, bufsize=8) as queue:
            lock = inhibit()
            logger.info("holding a logind delay lock for sleep and shutdown")
            while True:
                msg = conn.recv_until_filtered(queue)
                what = SIGNALS.get(msg.header.fields.get(HeaderFields.member))
                if what is None:
                    continue
                if not msg.body[0]:  # resumed: take the lock again for next time
                    if lock is None:
                        lock = inhibit()
                    continue
                try:
                    on_prepare(what)
                except Exception as e:  # never keep the system from sleeping
                    logger.error("sync before {} failed: {}", what, e)
                finally:
                    if lock is not None:
                        lock.close()
                        lock = None

    t = threading.Thread(target=loop, name="romm-shelf-sleep", daemon=True)
    t.start()
    return t
