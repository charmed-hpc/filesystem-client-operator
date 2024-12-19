#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

import asyncio
import logging
from pathlib import Path

import juju
import pytest
import yaml
from charms.filesystem_client.v0.filesystem_info import CephfsInfo, NfsInfo
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./charmcraft.yaml").read_text())
APP_NAME = METADATA["name"]

NFS_INFO = NfsInfo(hostname="192.168.1.254", path="/srv", port=65535)
CEPHFS_INFO = CephfsInfo(
    fsid="123456789-0abc-defg-hijk-lmnopqrstuvw",
    name="filesystem",
    path="/export",
    monitor_hosts=[
        "192.168.1.1:6789",
        "192.168.1.2:6789",
        "192.168.1.3:6789",
    ],
    user="user",
    key="R//appdqz4NP4Bxcc5XWrg==",
)


@pytest.mark.abort_on_fail
@pytest.mark.order(1)
async def test_build_and_deploy(ops_test: OpsTest):
    """Build the charm-under-test and deploy it together with related charms.

    Assert on the unit status before any relations/configurations take place.
    """
    # Build and deploy charm from local source folder
    charm = await ops_test.build_charm(".")
    server = await ops_test.build_charm("./tests/integration/server")

    # Deploy the charm and wait for active/idle status
    async with asyncio.TaskGroup() as tg:
        tg.create_task(
            ops_test.model.deploy(
                "ubuntu",
                application_name="ubuntu",
                base="ubuntu@24.04",
                constraints=juju.constraints.parse("virt-type=virtual-machine"),
            )
        )
        tg.create_task(
            ops_test.model.deploy(
                charm,
                application_name=APP_NAME,
                num_units=0,
            )
        )
        tg.create_task(
            ops_test.model.deploy(
                server,
                application_name="nfs-server",
                config={
                    "type": "nfs",
                },
                constraints=juju.constraints.parse("virt-type=virtual-machine"),
            )
        )
        tg.create_task(
            ops_test.model.deploy(
                server,
                application_name="cephfs-server",
                config={
                    "type": "cephfs",
                },
                constraints=juju.constraints.parse(
                    "virt-type=virtual-machine root-disk=50G mem=8G"
                ),
            )
        )
        tg.create_task(
            ops_test.model.wait_for_idle(
                apps=["nfs-server", "cephfs-server", "ubuntu"],
                status="active",
                raise_on_blocked=True,
                raise_on_error=True,
                timeout=1000,
            )
        )


@pytest.mark.abort_on_fail
@pytest.mark.order(2)
async def test_integrate(ops_test: OpsTest):
    async with asyncio.TaskGroup() as tg:
        tg.create_task(ops_test.model.integrate(f"{APP_NAME}:juju-info", "ubuntu:juju-info"))
        tg.create_task(
            ops_test.model.wait_for_idle(apps=["ubuntu"], status="active", raise_on_error=True)
        )
        tg.create_task(
            ops_test.model.wait_for_idle(apps=[APP_NAME], status="blocked", raise_on_error=True)
        )

    assert (
        ops_test.model.applications[APP_NAME].units[0].workload_status_message
        == "Missing `mountpoint` in config."
    )


@pytest.mark.abort_on_fail
@pytest.mark.order(3)
async def test_nfs(ops_test: OpsTest):
    async with asyncio.TaskGroup() as tg:
        tg.create_task(ops_test.model.integrate(f"{APP_NAME}:filesystem", "nfs-server:filesystem"))
        tg.create_task(
            ops_test.model.applications[APP_NAME].set_config(
                {"mountpoint": "/nfs", "nodev": "true", "read-only": "true"}
            )
        )
        tg.create_task(
            ops_test.model.wait_for_idle(apps=[APP_NAME], status="active", raise_on_error=True)
        )

    unit = ops_test.model.applications["ubuntu"].units[0]
    result = (await unit.ssh("ls /nfs")).strip("\n")
    assert "test-0" in result
    assert "test-1" in result
    assert "test-2" in result


@pytest.mark.abort_on_fail
@pytest.mark.order(4)
async def test_cephfs(ops_test: OpsTest):
    # Ensure the relation is removed before the config changes.
    # This guarantees that the new mountpoint is fresh.
    async with asyncio.TaskGroup() as tg:
        tg.create_task(
            ops_test.model.applications[APP_NAME].remove_relation(
                "filesystem", "nfs-server:filesystem"
            )
        )
        tg.create_task(
            ops_test.model.wait_for_idle(apps=[APP_NAME], status="blocked", raise_on_error=True)
        )

    async with asyncio.TaskGroup() as tg:
        tg.create_task(
            ops_test.model.applications[APP_NAME].set_config(
                {"mountpoint": "/cephfs", "noexec": "true", "nosuid": "true", "nodev": "false"}
            )
        )
        tg.create_task(
            ops_test.model.integrate(f"{APP_NAME}:filesystem", "cephfs-server:filesystem")
        )
        tg.create_task(
            ops_test.model.wait_for_idle(apps=[APP_NAME], status="active", raise_on_error=True)
        )

    unit = ops_test.model.applications["ubuntu"].units[0]
    result = (await unit.ssh("ls /cephfs")).strip("\n")
    assert "test-0" in result
    assert "test-1" in result
    assert "test-2" in result
