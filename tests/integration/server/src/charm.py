#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the application."""

import json
import logging
import os
import socket
import subprocess
import textwrap
from pathlib import Path

import ops
from charms.filesystem_client.v0.filesystem_info import CephfsInfo, FilesystemProvides, NfsInfo
from tenacity import retry, stop_after_attempt, wait_exponential

_logger = logging.getLogger(__name__)


def _exec_commands(cmds: [[str]]) -> None:
    for cmd in cmds:
        _exec_command(cmd)


def _exec_command(cmd: [str]) -> str:
    _logger.info(f"Executing `{' '.join(cmd)}`")
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    try:
        output = subprocess.check_output(cmd, text=True, env=env)
    except subprocess.CalledProcessError as e:
        _logger.error(e.output)
        raise Exception(f"Failed to execute command `{' '.join(cmd)}` in instance")
    _logger.info(output)
    return output


class FilesystemServerCharm(ops.CharmBase):
    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self._filesystem = FilesystemProvides(self, "filesystem", "server-peers")
        framework.observe(self.on.start, self._on_start)

    def _on_start(self, event: ops.StartEvent):
        """Handle start event."""
        _logger.info(self.config.get("type"))
        typ = self.config["type"]
        if "nfs" == typ:
            info = self._deploy_nfs()
        elif "cephfs" == typ:
            info = self._deploy_cephfs()
        else:
            raise ValueError("invalid filesystem type")

        self._filesystem.set_info(info)

        self.unit.status = ops.ActiveStatus()

    def _deploy_nfs(self) -> NfsInfo:
        _exec_commands(
            [
                ["apt", "update", "-y"],
                ["apt", "upgrade", "-y"],
                ["apt", "install", "-y", "nfs-kernel-server"],
            ]
        )

        exports = textwrap.dedent(
            """
            /srv     *(ro,sync,subtree_check)
            /data    *(rw,sync,no_subtree_check,no_root_squash)
            """
        ).strip("\n")
        _logger.info(f"Uploading the following /etc/exports file:\n{exports}")
        Path("/etc/exports").write_text(exports)
        _logger.info("starting NFS server")
        Path("/data").mkdir()

        _exec_commands([["exportfs", "-a"], ["systemctl", "restart", "nfs-kernel-server"]])

        for i in range(3):
            Path(f"/data/test-{i}").touch()

        self.unit.set_ports(111, 2049, ops.Port("udp", 111), ops.Port("udp", 2049))
        public_ip = socket.gethostbyname(socket.gethostname())

        return NfsInfo(hostname=public_ip, port=2049, path="/data")

    def _deploy_cephfs(self) -> CephfsInfo:
        fs_name = "filesystem"
        fs_user = "fs-client"
        fs_path = "/"

        @retry(wait=wait_exponential(max=6), stop=stop_after_attempt(20))
        def _mount_cephfs() -> None:
            # Need to extract this into its own function to apply the tenacity decorator
            # Wait until the cluster is ready to mount the filesystem.
            status = json.loads(_exec_command(["microceph.ceph", "-s", "-f", "json"]))
            if status["health"]["status"] != "HEALTH_OK":
                raise Exception("CephFS is not available")

            _exec_command(["mount", "-t", "ceph", f"admin@.{fs_name}={fs_path}", "/mnt"])

        _exec_commands(
            [
                ["ln", "-s", "/bin/true"],
                ["apt", "update", "-y"],
                ["apt", "upgrade", "-y"],
                ["apt", "install", "-y", "ceph-common"],
                ["snap", "install", "microceph"],
                ["microceph", "cluster", "bootstrap"],
                ["microceph", "disk", "add", "loop,2G,3"],
                ["microceph.ceph", "osd", "pool", "create", f"{fs_name}_data"],
                ["microceph.ceph", "osd", "pool", "create", f"{fs_name}_metadata"],
                ["microceph.ceph", "fs", "new", fs_name, f"{fs_name}_metadata", f"{fs_name}_data"],
                [
                    "microceph.ceph",
                    "fs",
                    "authorize",
                    fs_name,
                    f"client.{fs_user}",
                    fs_path,
                    "rw",
                ],
                # Need to generate the test files inside microceph itself.
                [
                    "ln",
                    "-sf",
                    "/var/snap/microceph/current/conf/ceph.client.admin.keyring",
                    "/etc/ceph/ceph.client.admin.keyring",
                ],
                [
                    "ln",
                    "-sf",
                    "/var/snap/microceph/current/conf/ceph.keyring",
                    "/etc/ceph/ceph.keyring",
                ],
                ["ln", "-sf", "/var/snap/microceph/current/conf/ceph.conf", "/etc/ceph/ceph.conf"],
            ]
        )

        _mount_cephfs()

        for i in range(3):
            Path(f"/mnt/test-{i}").touch()

        self.unit.set_ports(3300, 6789)

        status = json.loads(_exec_command(["microceph.ceph", "-s", "-f", "json"]))
        fsid = status["fsid"]
        key = _exec_command(["microceph.ceph", "auth", "print-key", f"client.{fs_user}"])
        hostname = socket.gethostbyname(socket.gethostname())

        return CephfsInfo(
            fsid=fsid,
            name=fs_name,
            path=fs_path,
            monitor_hosts=[
                hostname + ":3300",
                hostname + ":6789",
            ],
            user=fs_user,
            key=key,
        )


if __name__ == "__main__":  # pragma: nocover
    ops.main(FilesystemServerCharm)  # type: ignore
