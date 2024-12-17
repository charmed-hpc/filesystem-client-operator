#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Test the interfaces charm library."""

import pytest
from charms.filesystem_client.v0.filesystem_info import (
    FilesystemRequires,
    _hostinfo,
)
from ops import CharmBase

FS_INTEGRATION_NAME = "filesystem"
FS_INTEGRATION_INTERFACE = "filesystem_info"
FS_CLIENT_METADATA = f"""
name: fs-client
requires:
  {FS_INTEGRATION_NAME}:
    interface: {FS_INTEGRATION_INTERFACE}
"""

FSID = "123456789-0abc-defg-hijk-lmnopqrstuvw"
NAME = "filesystem"
PATH = "/data"
MONITOR_HOSTS = [("192.168.1.1", 6789), ("192.168.1.2", 6789), ("192.168.1.3", 6789)]
USERNAME = "user"
KEY = "R//appdqz4NP4Bxcc5XWrg=="


class FsClientCharm(CharmBase):
    """Mock CephFS client charm for unit tests."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.requirer = FilesystemRequires(self, FS_INTEGRATION_NAME)
        self.framework.observe(self.requirer.on.mount_fs, lambda *_: None)
        self.framework.observe(self.requirer.on.umount_fs, lambda *_: None)


class TestInterface:
    @pytest.mark.parametrize(
        ("host", "parsed"),
        [
            ("192.168.1.1", ("192.168.1.1", None)),
            ("192.168.1.1:6789", ("192.168.1.1", 6789)),
            ("[2001:db8::2:1]", ("2001:db8::2:1", None)),
            ("[2001:db8::2:1]:9876", ("2001:db8::2:1", 9876)),
            ("192.things.com", ("192.things.com", None)),
            ("192.things.com:1234", ("192.things.com", 1234)),
        ],
    )
    def test_hostinfo(self, host: str, parsed: tuple[str, int | None]):
        assert _hostinfo(host) == parsed
