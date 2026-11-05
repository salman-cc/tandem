# SPDX-FileCopyrightText: 2020-2021 Aleix Pol Gonzalez <aleixpol@kde.org>
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Minimal pywayland client bindings for zkde_screencast_unstable_v1 (KWin),
# trimmed by hand from `python -m pywayland.scanner` output to the two
# virtual-output requests Tandem uses (so the generated WlOutput import is
# not needed). Protocol XML: plasma-wayland-protocols,
# src/protocols/zkde-screencast-unstable-v1.xml.
#
# Contributors: Muhammed Salman

from __future__ import annotations

import enum

from pywayland.protocol_core import Argument, ArgumentType, Interface, Proxy


class ZkdeScreencastStreamUnstableV1(Interface):
    name = "zkde_screencast_stream_unstable_v1"
    version = 6


class ZkdeScreencastUnstableV1(Interface):
    """KWin screencast manager.

    The protocol's own description warns it is a KDE implementation detail
    that may change without a version bump; it is what krfb-virtualmonitor
    uses, and it has been stable since Plasma 5.24.
    """

    name = "zkde_screencast_unstable_v1"
    version = 6

    class pointer(enum.IntEnum):
        hidden = 1
        embedded = 2
        metadata = 4


class ZkdeScreencastStreamUnstableV1Proxy(Proxy[ZkdeScreencastStreamUnstableV1]):
    interface = ZkdeScreencastStreamUnstableV1

    @ZkdeScreencastStreamUnstableV1.request()
    def close(self) -> None:
        """We are done with the stream; KWin removes the virtual output."""
        self._marshal(0)
        self._destroy()

    @ZkdeScreencastStreamUnstableV1.event()
    def closed(self) -> None:
        """The server stopped the stream; clients should call close."""
        self._post_event(0)

    @ZkdeScreencastStreamUnstableV1.event(
        Argument(ArgumentType.Uint),
    )
    def created(self, node: int) -> None:
        """The PipeWire feed exists; `node` is its node id (pre-v6 style)."""
        self._post_event(1, node)

    @ZkdeScreencastStreamUnstableV1.event(
        Argument(ArgumentType.String),
    )
    def failed(self, error: str) -> None:
        """No created event will arrive; the stream should be closed."""
        self._post_event(2, error)

    @ZkdeScreencastStreamUnstableV1.event(
        Argument(ArgumentType.Uint),
        Argument(ArgumentType.Uint),
        version=6,
    )
    def serial(self, object_serial_hi: int, object_serial_low: int) -> None:
        """The PipeWire object serial (preferred over the node id on v6)."""
        self._post_event(3, object_serial_hi, object_serial_low)


class ZkdeScreencastUnstableV1Proxy(Proxy[ZkdeScreencastUnstableV1]):
    interface = ZkdeScreencastUnstableV1

    @ZkdeScreencastUnstableV1.request()
    def destroy(self) -> None:
        """Destroy the manager object."""
        self._marshal(2)
        self._destroy()

    @ZkdeScreencastUnstableV1.request(
        Argument(ArgumentType.NewId, interface=ZkdeScreencastStreamUnstableV1),
        Argument(ArgumentType.String),
        Argument(ArgumentType.Int),
        Argument(ArgumentType.Int),
        Argument(ArgumentType.Fixed),
        Argument(ArgumentType.Uint),
        version=2,
    )
    def stream_virtual_output(self, name: str, width: int, height: int,
                              scale: float, pointer: int) -> ZkdeScreencastStreamUnstableV1Proxy:
        """Request a feed from a new virtual output (logical w/h + scale)."""
        stream = self._marshal_constructor(
            3, ZkdeScreencastStreamUnstableV1, name, width, height, scale, pointer)
        return stream

    @ZkdeScreencastUnstableV1.request(
        Argument(ArgumentType.NewId, interface=ZkdeScreencastStreamUnstableV1),
        Argument(ArgumentType.String),
        Argument(ArgumentType.String),
        Argument(ArgumentType.Int),
        Argument(ArgumentType.Int),
        Argument(ArgumentType.Fixed),
        Argument(ArgumentType.Uint),
        version=4,
    )
    def stream_virtual_output_with_description(self, name: str, description: str,
                                               width: int, height: int,
                                               scale: float, pointer: int) -> ZkdeScreencastStreamUnstableV1Proxy:
        """stream_virtual_output plus a user-visible description."""
        stream = self._marshal_constructor(
            5, ZkdeScreencastStreamUnstableV1, name, description,
            width, height, scale, pointer)
        return stream


ZkdeScreencastStreamUnstableV1.proxy_class = ZkdeScreencastStreamUnstableV1Proxy
ZkdeScreencastUnstableV1.proxy_class = ZkdeScreencastUnstableV1Proxy
