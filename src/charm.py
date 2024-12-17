#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for the filesystem client."""

import json
import logging
from collections import Counter

import ops
from charms.filesystem_client.v0.filesystem_info import FilesystemRequires
from jsonschema import ValidationError, validate

from utils.manager import MountsManager

logger = logging.getLogger(__name__)

CONFIG_SCHEMA = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "required": ["mountpoint"],
        "properties": {
            "mountpoint": {"type": "string"},
            "noexec": {"type": "boolean"},
            "nosuid": {"type": "boolean"},
            "nodev": {"type": "boolean"},
            "read-only": {"type": "boolean"},
        },
    },
}


class StopCharmError(Exception):
    """Exception raised when a method needs to finish the execution of the charm code."""

    def __init__(self, status: ops.StatusBase):
        self.status = status


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
# simplifying the code to handle all the multiple relations.
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

    def _handle_event(self, event: ops.EventBase) -> None:  # noqa: C901
        try:
            self.unit.status = ops.MaintenanceStatus("Updating status.")

            self._ensure_installed()
            config = self._get_config()
            self._mount_filesystems(config)
        except StopCharmError as e:
            # This was the cleanest way to ensure the inner methods can still return prematurely
            # when an error occurs.
            self.app.status = e.status
            return

        self.unit.status = ops.ActiveStatus("Mounted filesystems.")

    def _ensure_installed(self):
        """Ensure the required packages are installed into the unit."""
        if not self._mounts_manager.installed:
            self.unit.status = ops.MaintenanceStatus("Installing required packages.")
            self._mounts_manager.install()

    def _get_config(self) -> dict[str, dict[str, str | bool]]:
        """Get and validate the configuration of the charm."""
        try:
            config = json.loads(str(self.config.get("mounts", "")))
            validate(config, CONFIG_SCHEMA)
            config: dict[str, dict[str, str | bool]] = config
            for fs, opts in config.items():
                for opt in ["noexec", "nosuid", "nodev", "read-only"]:
                    opts[opt] = opts.get(opt, False)
            return config
        except (json.JSONDecodeError, ValidationError) as e:
            raise StopCharmError(
                ops.BlockedStatus(f"invalid configuration for option `mounts`. reason:\n{e}")
            )

    def _mount_filesystems(self, config: dict[str, dict[str, str | bool]]):
        """Mount all available filesystems for the charm."""
        endpoints = self._filesystems.endpoints
        for fs_type, count in Counter(
            [endpoint.info.filesystem_type() for endpoint in endpoints]
        ).items():
            if count > 1:
                raise StopCharmError(
                    ops.BlockedStatus(f"Too many relations for mount type `{fs_type}`.")
                )

        self.unit.status = ops.MaintenanceStatus("Ensuring filesystems are mounted.")

        with self._mounts_manager.mounts() as mounts:
            for endpoint in endpoints:
                fs_type = endpoint.info.filesystem_type()
                if not (options := config.get(fs_type)):
                    raise StopCharmError(
                        ops.BlockedStatus(f"Missing configuration for mount type `{fs_type}`.")
                    )

                mountpoint = str(options["mountpoint"])

                opts = []
                opts.append("noexec" if options.get("noexec") else "exec")
                opts.append("nosuid" if options.get("nosuid") else "suid")
                opts.append("nodev" if options.get("nodev") else "dev")
                opts.append("ro" if options.get("read-only") else "rw")
                mounts.add(info=endpoint.info, mountpoint=mountpoint, options=opts)


if __name__ == "__main__":  # pragma: nocover
    ops.main(FilesystemClientCharm)  # type: ignore
