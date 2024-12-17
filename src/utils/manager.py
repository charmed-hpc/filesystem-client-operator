# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manage machine mounts and dependencies."""

import contextlib
import logging
import os
import pathlib
import subprocess
from dataclasses import dataclass
from ipaddress import AddressValueError, IPv6Address
from typing import Generator, List, Optional, Union

import charms.operator_libs_linux.v0.apt as apt
import charms.operator_libs_linux.v1.systemd as systemd
import ops
from charms.filesystem_client.v0.filesystem_info import CephfsInfo, FilesystemInfo, NfsInfo

_logger = logging.getLogger(__name__)


class Error(Exception):
    """Raise if Storage client manager encounters an error."""

    @property
    def name(self):
        """Get a string representation of the error plus class name."""
        return f"<{type(self).__module__}.{type(self).__name__}>"

    @property
    def message(self):
        """Return the message passed as an argument."""
        return self.args[0]

    def __repr__(self):
        """Return the string representation of the error."""
        return f"<{type(self).__module__}.{type(self).__name__} {self.args}>"


@dataclass(frozen=True)
class MountInfo:
    """Mount information.

    Notes:
        See `man fstab` for description of field types.
    """

    endpoint: str
    mountpoint: str
    fstype: str
    options: str
    freq: str
    passno: str


@dataclass
class _MountInfo:
    endpoint: str
    options: [str]


class Mounts:
    """Collection of mounts that need to be managed by the `MountsManager`."""

    _mounts: dict[str, _MountInfo]

    def __init__(self):
        self._mounts = {}

    def add(
        self,
        info: FilesystemInfo,
        mountpoint: Union[str, os.PathLike],
        options: Optional[List[str]] = None,
    ) -> None:
        """Add a mount to the list of managed mounts.

        Args:
            info: Share information required to mount the share.
            mountpoint: System location to mount the share.
            options: Mount options to pass when mounting the share.

        Raises:
            Error: Raised if the mount operation fails.
        """
        if options is None:
            options = []

        endpoint, additional_opts = _get_endpoint_and_opts(info)
        options = sorted(options + additional_opts)

        self._mounts[str(mountpoint)] = _MountInfo(endpoint=endpoint, options=options)


class MountsManager:
    """Manager for mounted filesystems in the current system."""

    def __init__(self, charm: ops.CharmBase):
        # Lazily initialized
        self._pkgs = None
        self._master_file = pathlib.Path(f"/etc/auto.master.d/{charm.app.name}.autofs")
        self._autofs_file = pathlib.Path(f"/etc/auto.{charm.app.name}")

    @property
    def _packages(self) -> List[apt.DebianPackage]:
        """List of packages required by the client."""
        if not self._pkgs:
            self._pkgs = [
                apt.DebianPackage.from_system(pkg)
                for pkg in ["ceph-common", "nfs-common", "autofs"]
            ]
        return self._pkgs

    @property
    def installed(self) -> bool:
        """Check if the required packages are installed."""
        for pkg in self._packages:
            if not pkg.present:
                return False

        if not self._master_file.exists or not self._autofs_file.exists:
            return False

        return True

    def install(self):
        """Install the required mount packages.

        Raises:
            Error: Raised if this failed to change the state of any of the required packages.
        """
        try:
            for pkg in self._packages:
                pkg.ensure(apt.PackageState.Present)
        except (apt.PackageError, apt.PackageNotFoundError) as e:
            _logger.error(
                f"failed to change the state of the required packages. reason:\n{e.message}"
            )
            raise Error(e.message)

        try:
            self._master_file.touch(mode=0o600)
            self._autofs_file.touch(mode=0o600)
            self._master_file.write_text(f"/- {self._autofs_file}")
        except IOError as e:
            _logger.error(f"failed to create the required autofs files. reason:\n{e}")
            raise Error("failed to create the required autofs files")

    def supported(self) -> bool:
        """Check if underlying base supports mounting shares."""
        try:
            result = subprocess.run(
                ["systemd-detect-virt"], stdout=subprocess.PIPE, check=True, text=True
            )
            if "lxc" in result.stdout:
                # Cannot mount shares inside LXD containers.
                return False
            else:
                return True
        except subprocess.CalledProcessError:
            _logger.warning("Could not detect execution in virtualized environment")
            return True

    @contextlib.contextmanager
    def mounts(self, force_mount=False) -> Generator[Mounts, None, None]:
        """Get the list of `Mounts` that need to be managed by the `MountsManager`.

        It will initially contain no mounts, and any mount that is added to
        `Mounts` will be mounted by the manager. Mounts that were
        added on previous executions will get removed if they're not added again
        to the `Mounts` object.
        """
        mounts = Mounts()
        yield mounts
        # This will not resume if the caller raised an exception, which
        # should be enough to ensure the file is not written if the charm entered
        # an error state.
        new_autofs = "\n".join(
            (
                f"{mountpoint} -{','.join(info.options)} {info.endpoint}"
                for mountpoint, info in sorted(mounts._mounts.items())
            )
        )

        old_autofs = self._autofs_file.read_text()

        # Avoid restarting autofs if the config didn't change.
        if not force_mount and new_autofs == old_autofs:
            return

        try:
            for mount in mounts._mounts.keys():
                pathlib.Path(mount).mkdir(parents=True, exist_ok=True)
            self._autofs_file.write_text(new_autofs)
            systemd.service_reload("autofs", restart_on_failure=True)
        except systemd.SystemdError as e:
            _logger.error(f"failed to mount filesystems. reason:\n{e}")
            if "Operation not permitted" in str(e) and not self.supported():
                raise Error("mounting shares not supported on LXD containers")
            raise Error("failed to mount filesystems")


def _get_endpoint_and_opts(info: FilesystemInfo) -> tuple[str, [str]]:
    match info:
        case NfsInfo(hostname=hostname, port=port, path=path):
            try:
                IPv6Address(hostname)
                # Need to add brackets if the hostname is IPv6
                hostname = f"[{hostname}]"
            except AddressValueError:
                pass

            endpoint = f"{hostname}:{path}"
            options = [f"port={port}"] if port else []
        case CephfsInfo(
            fsid=fsid, name=name, path=path, monitor_hosts=mons, user=user, key=secret
        ):
            mon_addr = "/".join(mons)
            endpoint = f"{user}@{fsid}.{name}={path}"
            options = [
                "fstype=ceph",
                f"mon_addr={mon_addr}",
                f"secret={secret}",
            ]
        case _:
            raise Error(f"unsupported filesystem type `{info.filesystem_type()}`")

    return endpoint, options
