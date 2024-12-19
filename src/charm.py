#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for the filesystem client."""

import logging
from dataclasses import dataclass

import ops
from charms.filesystem_client.v0.filesystem_info import FilesystemRequires

from utils.manager import MountsManager

logger = logging.getLogger(__name__)


class StopCharmError(Exception):
    """Exception raised when a method needs to finish the execution of the charm code."""

    def __init__(self, status: ops.StatusBase):
        self.status = status


@dataclass
class CharmConfig:
    """Configuration for the charm."""

    mountpoint: str
    """Location to mount the filesystem on the machine."""
    noexec: bool
    """Block execution of binaries on the filesystem."""
    nosuid: bool
    """Do not honour suid and sgid bits on the filesystem."""
    nodev: bool
    """Blocking interpretation of character and/or block"""
    read_only: bool
    """Mount filesystem as read-only."""


# Trying to use a delta charm (one method per event) proved to be a bit unwieldy, since
# we would have to handle multiple updates at once:
# - mount requests
# - umount requests
# - config changes
#
# Additionally, we would need to wait until the correct configuration
# was provided, so we would have to somehow keep track of the pending
# mount requests.
#
# A holistic charm (one method for all events) was a lot easier to deal with,
# simplifying the code to handle all the events.
class FilesystemClientCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        self._filesystems = FilesystemRequires(self, "filesystem")
        self._mounts_manager = MountsManager(self)
        framework.observe(self.on.upgrade_charm, self._handle_event)
        framework.observe(self.on.update_status, self._handle_event)
        framework.observe(self.on.config_changed, self._handle_event)
        framework.observe(self._filesystems.on.mount_filesystem, self._handle_event)
        framework.observe(self._filesystems.on.umount_filesystem, self._handle_event)

    def _handle_event(self, event: ops.EventBase) -> None:
        """Handle a Juju event."""
        try:
            self.unit.status = ops.MaintenanceStatus("Updating status.")

            # CephFS is not supported on LXD containers.
            if not self._mounts_manager.supported():
                self.unit.status = ops.BlockedStatus("Cannot mount filesystems on LXD containers.")
                return

            self._ensure_installed()
            config = self._get_config()
            self._mount_filesystems(config)
        except StopCharmError as e:
            # This was the cleanest way to ensure the inner methods can still return prematurely
            # when an error occurs.
            self.unit.status = e.status
            return

        self.unit.status = ops.ActiveStatus(f"Mounted filesystem at `{config.mountpoint}`.")

    def _ensure_installed(self):
        """Ensure the required packages are installed into the unit."""
        if not self._mounts_manager.installed:
            self.unit.status = ops.MaintenanceStatus("Installing required packages.")
            self._mounts_manager.install()

    def _get_config(self) -> CharmConfig:
        """Get and validate the configuration of the charm."""
        if not (mountpoint := self.config.get("mountpoint")):
            raise StopCharmError(ops.BlockedStatus("Missing `mountpoint` in config."))

        return CharmConfig(
            mountpoint=str(mountpoint),
            noexec=bool(self.config.get("noexec")),
            nosuid=bool(self.config.get("nosuid")),
            nodev=bool(self.config.get("nodev")),
            read_only=bool(self.config.get("read-only")),
        )

    def _mount_filesystems(self, config: CharmConfig):
        """Mount the filesystem for the charm."""
        endpoints = self._filesystems.endpoints
        if not endpoints:
            raise StopCharmError(
                ops.BlockedStatus("Waiting for an integration with a filesystem provider.")
            )

        # This is limited to 1 relation.
        endpoint = endpoints[0]

        self.unit.status = ops.MaintenanceStatus("Mounting filesystem.")

        with self._mounts_manager.mounts() as mounts:
            opts = []
            opts.append("noexec" if config.noexec else "exec")
            opts.append("nosuid" if config.nosuid else "suid")
            opts.append("nodev" if config.nodev else "dev")
            opts.append("ro" if config.read_only else "rw")
            mounts.add(info=endpoint.info, mountpoint=config.mountpoint, options=opts)


if __name__ == "__main__":  # pragma: nocover
    ops.main(FilesystemClientCharm)  # type: ignore
