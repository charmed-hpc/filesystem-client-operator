# Copyright 2024 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Library to manage integrations between filesystem providers and consumers.

This library contains the FilesystemProvides and FilesystemRequires classes for managing an
integration between a filesystem server operator and a filesystem client operator.

## FilesystemInfo (filesystem mount information)

This abstract class defines the methods that a filesystem type must expose for providers and
consumers. Any subclass of this class will be compatible with the other methods exposed
by the interface library, but the server and the client are the ones responsible for deciding which
filesystems to support.

## FilesystemRequires (filesystem client)

This class provides a uniform interface for charms that need to mount or unmount filesystems,
and convenience methods for consuming data sent by a filesystem server charm.

### Defined events

- `mount_filesystem`: Event emitted when the filesystem is ready to be mounted.
- `umount_filesystem`: Event emitted when the filesystem needs to be unmounted.

### Example

``python
import ops
from charms.filesystem_client.v0.filesystem_info import (
    FilesystemRequires,
    MountFilesystemEvent,
)


class StorageClientCharm(ops.CharmBase):
    # Application charm that needs to mount filesystems.

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        # Charm events defined in the FilesystemRequires class.
        self._fs = FilesystemRequires(self, "filesystem")
        self.framework.observe(
            self._fs.on.mount_fs,
            self._on_mount_fs,
        )

    def _on_mount_fs(self, event: MountFilesystemEvent) -> None:
        # Handle when new filesystem server is connected.

        endpoint = event.endpoint

        self.mount("/mnt", endpoint.info)

        self.unit.status = ops.ActiveStatus("Mounted filesystem at `/mnt`.")
```

## FilesystemProvides (filesystem server)

This library provides a uniform interface for charms that expose filesystems.

> __Note:__ It is the responsibility of the provider charm to have
> the implementation for creating a new filesystem share. FilesystemProvides just exposes
> the interface for the integration.

### Example

```python
import ops
from charms.filesystem_client.v0.filesystem_info import (
    FilesystemProvides,
    NfsInfo,
)

class StorageServerCharm(ops.CharmBase):
    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self._filesystem = FilesystemProvides(self, "filesystem", "server-peers")
        framework.observe(self.on.start, self._on_start)

    def _on_start(self, event: ops.StartEvent):
        # Handle start event.
        self._filesystem.set_info(NfsInfo("192.168.1.254", 65535, "/srv"))
        self.unit.status = ops.ActiveStatus()
```
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from ipaddress import AddressValueError, IPv6Address
from typing import List, Optional, TypeVar
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunsplit

import ops
from ops.charm import (
    CharmBase,
    CharmEvents,
    RelationChangedEvent,
    RelationDepartedEvent,
    RelationEvent,
    RelationJoinedEvent,
)
from ops.framework import EventSource, Object
from ops.model import Model, Relation

__all__ = [
    "FilesystemInfoError",
    "ParseUriError",
    "FilesystemInfo",
    "NfsInfo",
    "CephfsInfo",
    "Endpoint",
    "FilesystemEvent",
    "MountFilesystemEvent",
    "UmountFilesystemEvent",
    "FilesystemRequiresEvents",
    "FilesystemRequires",
    "FilesystemProvides",
]

# The unique Charmhub library identifier, never change it
LIBID = "todo"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 1

_logger = logging.getLogger(__name__)


class FilesystemInfoError(Exception):
    """Exception raised when an operation failed."""


class ParseUriError(FilesystemInfoError):
    """Exception raised when a parse operation from an URI failed."""


# Design-wise, this class represents the grammar that relations use to
# share data between providers and requirers:
#
# key = 1*( unreserved )
# value = 1*( unreserved / ":" / "/" / "?" / "#" / "[" / "]" / "@" / "!" / "$"
#       / "'" / "(" / ")" / "*" / "+" / "," / ";" )
# options = key "=" value ["&" options]
# host-port = host [":" port]
# hosts = host-port [',' hosts]
# authority = [userinfo "@"] "(" hosts ")"
# URI = scheme "://" authority path-absolute ["?" options]
#
# Unspecified grammar rules are given by [RFC 3986](https://datatracker.ietf.org/doc/html/rfc3986#appendix-A).
#
# This essentially leaves 5 components that the library can use to share data:
# - scheme: representing the type of filesystem.
# - hosts: representing the list of hosts where the filesystem lives. For NFS it should be a single element,
#   but CephFS and Lustre use more than one endpoint.
# - user: Any kind of authentication user that the client must specify to mount the filesystem.
# - path: The internally exported path of each filesystem. Could be optional if a filesystem exports its
#   whole tree, but at the very least NFS, CephFS and Lustre require an export path.
# - options: Some filesystems will require additional options for its specific mount command (e.g. Ceph).
#
# Putting all together, this allows sharing the required data using simple URI strings:
# ```
# <scheme>://<user>@(<host>,*)/<path>/?<options>
#
# nfs://(192.168.1.1:65535)/export
# ceph://fsuser@(192.168.1.1,192.168.1.2,192.168.1.3)/export?fsid=asdf1234&auth=plain:QWERTY1234&filesystem=fs_name
# ceph://fsuser@(192.168.1.1,192.168.1.2,192.168.1.3)/export?fsid=asdf1234&auth=secret:YXNkZnF3ZXJhc2RmcXdlcmFzZGZxd2Vy&filesystem=fs_name
# lustre://(192.168.227.11%40tcp1,192.168.227.12%40tcp1)/export
# ```
#
# Note how in the Lustre URI we needed to escape the `@` symbol on the hosts to conform with the URI syntax.
@dataclass(init=False, frozen=True)
class _UriData:
    """Raw data from the endpoint URI of a relation."""

    scheme: str
    """Scheme used to identify a filesystem.

    This will mostly correspond to the option `fstype` for the `mount` command.
    """

    hosts: [str]
    """List of hosts where the filesystem is deployed on."""

    user: str
    """User to connect to the filesystem."""

    path: str
    """Path exported by the filesystem."""

    options: dict[str, str]
    """Additional options that could be required to mount the filesystem."""

    def __init__(
        self,
        scheme: str,
        hosts: [str],
        user: str = "",
        path: str = "/",
        options: dict[str, str] = {},
    ):
        if not scheme:
            raise FilesystemInfoError("scheme cannot be empty")
        if len(hosts) == 0:
            raise FilesystemInfoError("list of hosts cannot be empty")

        # Strictly convert to the required types to avoid passing through weird data.
        object.__setattr__(self, "scheme", str(scheme))
        object.__setattr__(self, "hosts", [str(host) for host in hosts])
        object.__setattr__(self, "user", str(user) if user else "")
        object.__setattr__(self, "path", str(path) if path else "/")
        object.__setattr__(
            self, "options", {str(k): str(v) for k, v in options.items()} if options else {}
        )

    @classmethod
    def from_uri(cls, uri: str) -> "_UriData":
        """Convert an URI string into a `_UriData`."""
        _logger.debug(f"_UriData.from_uri: parsing `{uri}`")

        result = urlparse(uri, allow_fragments=False)
        scheme = str(result.scheme or "")
        user = unquote(result.username or "")
        hostname = unquote(result.hostname or "")

        if not hostname or hostname[0] != "(" or hostname[-1] != ")":
            raise ParseUriError(f"invalid list of hosts for endpoint `{uri}`")

        hosts = hostname[1:-1].split(",")
        path = unquote(result.path or "")
        try:
            options = {
                key: ",".join(values)
                for key, values in parse_qs(result.query, strict_parsing=True).items()
            }
        except ValueError:
            raise ParseUriError(f"invalid options for endpoint `{uri}`")
        try:
            return _UriData(scheme=scheme, user=user, hosts=hosts, path=path, options=options)
        except FilesystemInfoError as e:
            raise ParseUriError(*e.args)

    def __str__(self) -> str:
        user = quote(self.user)
        hostname = quote(",".join(self.hosts))
        path = quote(self.path)
        netloc = f"{user}@({hostname})" if user else f"({hostname})"
        query = urlencode(self.options)
        return urlunsplit((self.scheme, netloc, path, query, None))


def _hostinfo(host: str) -> tuple[str, Optional[int]]:
    """Parse a host string into the hostname and the port."""
    _logger.debug(f"_hostinfo: parsing `{host}`")
    if len(host) == 0:
        raise ParseUriError("invalid empty host")

    pos = 0
    if host[pos] == "[":
        # IPv6
        pos = host.find("]", pos)
        if pos == -1:
            raise ParseUriError("unclosed bracket for host")
        hostname = host[1:pos]
        pos = pos + 1
    else:
        # IPv4 or DN
        pos = host.find(":", pos)
        if pos == -1:
            pos = len(host)
        hostname = host[:pos]

    if pos == len(host):
        return hostname, None

    # more characters after the hostname <==> port

    if host[pos] != ":":
        raise ParseUriError("expected `:` after IPv6 address")
    try:
        port = int(host[pos + 1 :])
    except ValueError:
        raise ParseUriError("expected int after `:` in host")

    return hostname, port


T = TypeVar("T", bound="FilesystemInfo")


class FilesystemInfo(ABC):
    """Information to mount a filesystem.

    This is an abstract class that exposes a set of required methods. All filesystems that
    can be handled by this library must derive this abstract class.
    """

    @classmethod
    @abstractmethod
    def from_uri(cls: type[T], uri: str, model: Model) -> T:
        """Convert an URI string into a `FilesystemInfo` object."""

    @abstractmethod
    def to_uri(self, model: Model) -> str:
        """Convert this `FilesystemInfo` object into an URI string."""

    def grant(self, model: Model, relation: ops.Relation):
        """Grant permissions for a certain relation to any secrets that this `FilesystemInfo` has.

        This is an optional method because not all filesystems will require secrets to
        be mounted on the client.
        """

    @classmethod
    @abstractmethod
    def filesystem_type(cls) -> str:
        """Get the string identifier of this filesystem type."""


@dataclass(frozen=True)
class NfsInfo(FilesystemInfo):
    """Information required to mount an NFS share."""

    hostname: str
    """Hostname where the NFS server can be reached."""

    port: Optional[int]
    """Port where the NFS server can be reached."""

    path: str
    """Path exported by the NFS server."""

    @classmethod
    def from_uri(cls, uri: str, _model: Model) -> "NfsInfo":
        """See :py:meth:`FilesystemInfo.from_uri` for documentation on this method."""
        _logger.debug(f"NfsInfo.from_uri: parsing `{uri}`")

        info = _UriData.from_uri(uri)

        if info.scheme != cls.filesystem_type():
            raise ParseUriError(
                "could not parse `EndpointInfo` with incompatible scheme into `NfsInfo`"
            )

        path = info.path

        if info.user:
            _logger.warning("ignoring user info on nfs endpoint info")

        if len(info.hosts) > 1:
            _logger.info("multiple hosts specified. selecting the first one")

        if info.options:
            _logger.warning("ignoring endpoint options on nfs endpoint info")

        hostname, port = _hostinfo(info.hosts[0])
        return NfsInfo(hostname=hostname, port=port, path=path)

    def to_uri(self, _model: Model) -> str:
        """See :py:meth:`FilesystemInfo.to_uri` for documentation on this method."""
        try:
            IPv6Address(self.hostname)
            host = f"[{self.hostname}]"
        except AddressValueError:
            host = self.hostname

        hosts = [host + f":{self.port}" if self.port else ""]

        return str(_UriData(scheme=self.filesystem_type(), hosts=hosts, path=self.path))

    @classmethod
    def filesystem_type(cls) -> str:
        """See :py:meth:`FilesystemInfo.fs_type` for documentation on this method."""
        return "nfs"


@dataclass(frozen=True)
class CephfsInfo(FilesystemInfo):
    """Information required to mount a CephFS share."""

    fsid: str
    """Cluster identifier."""

    name: str
    """Name of the exported filesystem."""

    path: str
    """Path exported within the filesystem."""

    monitor_hosts: [str]
    """List of reachable monitor hosts."""

    user: str
    """Ceph user authorized to access the filesystem."""

    key: str
    """Cephx key for the authorized user."""

    @classmethod
    def from_uri(cls, uri: str, model: Model) -> "CephfsInfo":
        """See :py:meth:`FilesystemInfo.from_uri` for documentation on this method."""
        _logger.debug(f"CephfsInfo.from_uri: parsing `{uri}`")
        info = _UriData.from_uri(uri)

        if info.scheme != cls.filesystem_type():
            raise ParseUriError("could not parse uri with incompatible scheme into `CephfsInfo`")

        path = info.path

        if not (user := info.user):
            raise ParseUriError("missing user in uri for `CephfsInfo")

        if not (name := info.options.get("name")):
            raise ParseUriError("missing name in uri for `CephfsInfo`")

        if not (fsid := info.options.get("fsid")):
            raise ParseUriError("missing fsid in uri for `CephfsInfo`")

        monitor_hosts = info.hosts

        if not (auth := info.options.get("auth")):
            raise ParseUriError("missing auth info in uri for `CephsInfo`")

        try:
            kind, data = auth.split(":", 1)
        except ValueError:
            raise ParseUriError("Could not get the kind of auth info")

        if kind == "secret":
            key = model.get_secret(id=auth).get_content(refresh=True)["key"]
        elif kind == "plain":
            # Enables being able to pass data from reactive charms (such as `ceph-fs`), since
            # they don't support secrets.
            key = data
        else:
            raise ParseUriError(f"Invalid kind `{kind}` for auth info")

        return CephfsInfo(
            fsid=fsid, name=name, path=path, monitor_hosts=monitor_hosts, user=user, key=key
        )

    def to_uri(self, model: Model) -> str:
        """See :py:meth:`FilesystemInfo.to_uri` for documentation on this method."""
        secret = self._get_or_create_auth_secret(model)

        options = {
            "fsid": self.fsid,
            "name": self.name,
            "auth": secret.id,
            "auth-rev": str(secret.get_info().revision),
        }

        return str(
            _UriData(
                scheme=self.filesystem_type(),
                hosts=self.monitor_hosts,
                path=self.path,
                user=self.user,
                options=options,
            )
        )

    def grant(self, model: Model, relation: Relation):
        """See :py:meth:`FilesystemInfo.grant` for documentation on this method."""
        secret = self._get_or_create_auth_secret(model)

        secret.grant(relation)

    @classmethod
    def filesystem_type(cls) -> str:
        """See :py:meth:`FilesystemInfo.fs_type` for documentation on this method."""
        return "cephfs"

    def _get_or_create_auth_secret(self, model: Model) -> ops.Secret:
        try:
            secret = model.get_secret(label="auth")
            secret.set_content({"key": self.key})
        except ops.SecretNotFoundError:
            secret = model.app.add_secret(
                {"key": self.key},
                label="auth",
                description="Cephx key to authenticate against the CephFS share",
            )
        return secret


@dataclass
class Endpoint:
    """Endpoint data exposed by a filesystem server."""

    info: FilesystemInfo
    """Filesystem information required to mount this endpoint."""

    uri: str
    """Raw URI exposed by this endpoint."""
    # Right now this is unused on the client, but having the raw uri
    # available was useful on a previous version of the charm, so leaving
    # this exposed just in case we need it in the future.


def _uri_to_fs_info(uri: str, model: Model) -> FilesystemInfo:
    scheme = uri.split("://", maxsplit=1)[0]
    if scheme == NfsInfo.filesystem_type():
        return NfsInfo.from_uri(uri, model)
    elif scheme == CephfsInfo.filesystem_type():
        return CephfsInfo.from_uri(uri, model)
    else:
        raise FilesystemInfoError(f"unsupported filesystem type `{scheme}`")


class FilesystemEvent(RelationEvent):
    """Base event for filesystem-related events."""

    @property
    def endpoint(self) -> Optional[Endpoint]:
        """Get endpoint info."""
        if not (uri := self.relation.data[self.relation.app].get("endpoint")):
            return
        return Endpoint(_uri_to_fs_info(uri, self.framework.model), uri)


class MountFilesystemEvent(FilesystemEvent):
    """Emit when a filesystem is ready to be mounted."""


class UmountFilesystemEvent(FilesystemEvent):
    """Emit when a filesystem needs to be unmounted."""


class FilesystemRequiresEvents(CharmEvents):
    """Events that FS servers can emit."""

    mount_filesystem = EventSource(MountFilesystemEvent)
    umount_filesystem = EventSource(UmountFilesystemEvent)


class _BaseInterface(Object):
    """Base methods required for filesystem integration interfaces."""

    def __init__(self, charm: CharmBase, relation_name) -> None:
        super().__init__(charm, relation_name)
        self.charm = charm
        self.app = charm.model.app
        self.unit = charm.unit
        self.relation_name = relation_name

    @property
    def relations(self) -> List[Relation]:
        """Get list of active relations associated with the relation name."""
        result = []
        for relation in self.charm.model.relations[self.relation_name]:
            try:
                # Exclude relations that don't have any data yet
                _ = repr(relation.data)
                result.append(relation)
            except RuntimeError:
                continue
        return result


class FilesystemRequires(_BaseInterface):
    """Consumer-side interface of filesystem integrations."""

    on = FilesystemRequiresEvents()

    def __init__(self, charm: CharmBase, relation_name: str) -> None:
        super().__init__(charm, relation_name)
        self.framework.observe(charm.on[relation_name].relation_changed, self._on_relation_changed)
        self.framework.observe(charm.on[relation_name].relation_broken, self._on_relation_broken)

    def _on_relation_changed(self, event: RelationChangedEvent) -> None:
        """Handle when the databag between client and server has been updated."""
        _logger.debug("Emitting `MountShare` event from `RelationChanged` hook")
        self.on.mount_filesystem.emit(event.relation, app=event.app, unit=event.unit)

    def _on_relation_broken(self, event: RelationDepartedEvent) -> None:
        """Handle when server departs integration."""
        _logger.debug("Emitting `UmountShare` event from `RelationDeparted` hook")
        self.on.umount_filesystem.emit(event.relation, app=event.app, unit=event.unit)

    @property
    def endpoints(self) -> List[Endpoint]:
        """List of endpoints exposed by all the relations of this charm."""
        result = []
        for relation in self.relations:
            if not (uri := relation.data[relation.app].get("endpoint")):
                continue
            result.append(Endpoint(info=_uri_to_fs_info(uri, self.model), uri=uri))
        return result


class FilesystemProvides(_BaseInterface):
    """Provider-side interface of filesystem integrations."""

    def __init__(self, charm: CharmBase, relation_name: str, peer_relation_name: str) -> None:
        super().__init__(charm, relation_name)
        self._peer_relation_name = peer_relation_name
        self.framework.observe(charm.on[relation_name].relation_joined, self._update_relation)

    def set_info(self, info: FilesystemInfo) -> None:
        """Set information to mount a filesystem.

        Args:
            info: Information required to mount the filesystem.

        Notes:
            Only the application leader unit can set the filesystem data.
        """
        if not self.unit.is_leader():
            return

        uri = info.to_uri(self.model)

        self._endpoint = uri

        for relation in self.relations:
            info.grant(self.model, relation)
            relation.data[self.app]["endpoint"] = uri

    def _update_relation(self, event: RelationJoinedEvent) -> None:
        if not self.unit.is_leader() or not (endpoint := self._endpoint):
            return

        fs_info = _uri_to_fs_info(endpoint, self.model)
        fs_info.grant(self.model, event.relation)

        event.relation.data[self.app]["endpoint"] = endpoint

    @property
    def _peers(self) -> Optional[ops.Relation]:
        """Fetch the peer relation."""
        return self.model.get_relation(self._peer_relation_name)

    @property
    def _endpoint(self) -> str:
        endpoint = self._get_state("endpoint")
        return "" if endpoint is None else endpoint

    @_endpoint.setter
    def _endpoint(self, endpoint: str) -> None:
        self._set_state("endpoint", endpoint)

    def _get_state(self, key: str) -> Optional[str]:
        """Get a value from the global state."""
        if not self._peers:
            return None

        return self._peers.data[self.app].get(key)

    def _set_state(self, key: str, data: str) -> None:
        """Insert a value into the global state."""
        if not self._peers:
            raise FilesystemInfoError(
                "Peer relation can only be accessed after the relation is established"
            )

        self._peers.data[self.app][key] = data
