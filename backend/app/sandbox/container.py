"""
Docker container lifecycle management for sandbox databases.

Each sandbox container runs in an isolated environment with:
  - Dedicated bridge network (one network per sandbox)
  - CPU limit of 1.0 cores
  - Memory limit of 512 MB
  - Ephemeral named volumes (destroyed with the container)
  - Read-only root filesystem where the database allows it
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import docker
from docker.errors import NotFound

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Image / dialect specifications
# ---------------------------------------------------------------------------

DIALECT_SPECS: dict[str, dict[str, Any]] = {
    "postgres": {
        "image": "postgres:16-alpine",
        "env": {
            "POSTGRES_USER": "sandbox",
            "POSTGRES_PASSWORD": "sandbox",
            "POSTGRES_DB": "sandbox",
        },
        "port": 5432,
        "mem_limit": "512m",
        "mem_reservation": "256m",
        "read_only": True,
        "tmpfs": {"/tmp": "size=64M", "/var/run/postgresql": "size=16M"},
        # PostgreSQL stores data in this directory — must be a writable volume
        "data_mount": "/var/lib/postgresql/data",
    },
    "oracle": {
        "image": "gvenzl/oracle-xe:21-slim-faststart",
        "env": {
            "ORACLE_PASSWORD": "SandboxPwd1",
            "APP_USER": "sandbox",
            "APP_USER_PASSWORD": "SandboxPwd1",
        },
        "port": 1521,
        # The upstream Oracle XE image exits immediately at 512 MiB. Keep the
        # CPU and isolation limits the same, but give Oracle enough memory to
        # boot so the sandbox can actually execute SQL.
        "mem_limit": "2g",
        "mem_reservation": "512m",
        # Oracle writes extensively to system directories;
        # read_only is not practical without deep image customisation.
        "read_only": False,
        "tmpfs": {},
        "data_mount": "/opt/oracle/oradata",
    },
}


class SandboxContainer:
    """Wraps a single Docker container and its associated network + volume.

    Calling ``start()`` pulls / runs the image, creates a dedicated bridge
    network and an ephemeral named volume, and applies resource constraints.
    ``stop()`` tears everything down — no orphan resources are left behind.
    """

    def __init__(self, dialect: str) -> None:
        if dialect not in DIALECT_SPECS:
            raise ValueError(f"Unsupported dialect: {dialect!r}")
        self.dialect = dialect
        self.spec = DIALECT_SPECS[dialect]

        self.container_id: str | None = None
        self.network_id: str | None = None
        self.volume_id: str | None = None
        self._attached_container: str | None = os.getenv("SANDBOX_ATTACH_CONTAINER") or None

        # Host / port the database is reachable at (via Docker DNS)
        self.host: str = ""
        self.port: int = 0

        self._suffix: str = uuid.uuid4().hex[:8]
        # Docker Desktop on Windows can take longer than the SDK's default
        # 60s HTTP timeout when starting or pulling database images.
        docker_timeout = int(os.getenv("SANDBOX_DOCKER_CLIENT_TIMEOUT", "300"))
        self._client = docker.from_env(timeout=docker_timeout)

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def start(self, publish_port: bool = False) -> None:
        """Pull the image, create network + volume, run the container.

        Parameters
        ----------
        publish_port:
            If *True* a random host port is bound so the database is
            reachable from ``localhost``.  This is useful during
            integration testing when the test harness runs on the host.
            Production deployments should keep this *False* and let the
            backend connect over Docker's internal DNS.
        """
        self._ensure_image()
        self._create_network()
        self._create_volume()
        self._run_container(publish_port=publish_port)

    def is_running(self) -> bool:
        """Return *True* if the container is still in ``running`` state."""
        if not self.container_id:
            return False
        try:
            c = self._client.containers.get(self.container_id)
            return bool(c.status == "running")
        except NotFound:
            return False

    def stop(self) -> None:
        """Force-stop and remove the container, network, and volume."""
        self._remove_container()
        self._remove_network()
        self._remove_volume()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_image(self) -> None:
        """Pull the image if it is not already present locally."""
        image_tag = self.spec["image"]
        logger.info("Ensuring image %s is available …", image_tag)
        try:
            self._client.images.get(image_tag)
            logger.info("Image %s found locally.", image_tag)
        except docker.errors.ImageNotFound:
            logger.info("Pulling %s (this may take a while) …", image_tag)
            self._client.images.pull(image_tag)
            logger.info("Image %s pulled.", image_tag)

    def _create_network(self) -> None:
        """Create a dedicated bridge network for this sandbox.

        Each sandbox gets its own network so that containers cannot
        communicate with each other, providing network-level isolation.
        """
        name = f"nlsql-sandbox-net-{self._suffix}"
        net = self._client.networks.create(
            name,
            driver="bridge",
            internal=False,
            labels={"nlsql-sandbox": "true", "dialect": self.dialect},
        )
        self.network_id = net.id
        logger.debug("Created network %s", name)

    def _create_volume(self) -> None:
        """Create an ephemeral named volume for database data.

        Using a named volume (instead of a bind mount) means Docker
        manages the storage location.  The volume is destroyed when
        the sandbox is torn down, ensuring no data persists.
        """
        name = f"nlsql-sandbox-vol-{self._suffix}"
        vol = self._client.volumes.create(
            name,
            labels={"nlsql-sandbox": "true", "dialect": self.dialect},
        )
        self.volume_id = vol.id
        logger.debug("Created volume %s", name)

    def _run_container(self, publish_port: bool) -> None:
        """Create and start the database container."""
        container_name = f"nlsql-sandbox-{self._suffix}"

        # Build the volumes mapping: bind the named volume to the data dir
        volumes: dict[str, dict[str, str]] = {}

        # On Windows Docker Desktop we need the volume name, not the ID
        # Docker SDK accepts both — use the volume's short name.
        vol_name = f"nlsql-sandbox-vol-{self._suffix}"
        volumes[vol_name] = {"bind": self.spec["data_mount"], "mode": "rw"}

        kwargs: dict[str, Any] = {
            "image": self.spec["image"],
            "name": container_name,
            "environment": self.spec["env"],
            "network": f"nlsql-sandbox-net-{self._suffix}",
            "cpu_quota": 100000,  # 1 CPU = 100 000 µs per 100 000 µs period
            "cpu_period": 100000,
            "mem_limit": self.spec["mem_limit"],
            "mem_reservation": self.spec["mem_reservation"],
            "pids_limit": 256,
            "detach": True,
            "security_opt": ["no-new-privileges:true"],
            "labels": {
                "nlsql-sandbox": "true",
                "dialect": self.dialect,
            },
            "volumes": volumes,
        }

        attach_to_backend = self._attached_container is not None

        if publish_port and not attach_to_backend:
            # Random loopback-only host port: useful for tests, not exposed on the LAN.
            kwargs["ports"] = {f"{self.spec['port']}/tcp": ("127.0.0.1", None)}
        else:
            kwargs["ports"] = None

        if self.spec["read_only"]:
            kwargs["read_only"] = True
        if self.spec["tmpfs"]:
            kwargs["tmpfs"] = self.spec["tmpfs"]

        container = self._client.containers.run(**kwargs)
        self.container_id = container.id

        if attach_to_backend:
            self._client.networks.get(self.network_id).connect(self._attached_container)

        # Record reachability info
        if attach_to_backend:
            self.host = container_name
            self.port = self.spec["port"]
        elif publish_port:
            container.reload()
            port_key = f"{self.spec['port']}/tcp"
            host_bindings = container.attrs["NetworkSettings"]["Ports"].get(port_key, [])
            if host_bindings and len(host_bindings) > 0:
                host_ip = host_bindings[0].get("HostIp", "127.0.0.1")
                self.host = "127.0.0.1" if host_ip in {"", "0.0.0.0"} else host_ip
                self.port = int(host_bindings[0]["HostPort"])
            else:
                self.host = container_name
                self.port = self.spec["port"]
        else:
            self.host = container_name  # Docker DNS
            self.port = self.spec["port"]

        logger.info(
            "Started sandbox container %s (%s) on %s:%s",
            container_name,
            self.dialect,
            self.host,
            self.port,
        )

    def _remove_container(self) -> None:
        if not self.container_id:
            return
        try:
            c = self._client.containers.get(self.container_id)
            c.remove(force=True, v=True)
            logger.debug("Removed container %s", self.container_id[:12])
        except NotFound:
            pass
        self.container_id = None

    def _remove_network(self) -> None:
        if not self.network_id:
            return
        try:
            network = self._client.networks.get(self.network_id)
            if self._attached_container is not None:
                try:
                    network.disconnect(self._attached_container, force=True)
                except Exception:
                    logger.debug("Backend container already detached from sandbox network")
            network.remove()
            logger.debug("Removed network %s", self.network_id[:12])
        except NotFound:
            pass
        self.network_id = None

    def _remove_volume(self) -> None:
        if not self.volume_id:
            return
        try:
            self._client.volumes.get(self.volume_id).remove(force=True)
            logger.debug("Removed volume %s", self.volume_id[:12])
        except NotFound:
            pass
        self.volume_id = None

    def __del__(self) -> None:
        self.stop()
