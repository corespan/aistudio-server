"""list_containers must not fail when a container's image tag has moved on."""
import asyncio
import types

from app.tools import docker_probes


class _Stale:
    """A container whose image id the daemon no longer knows: .image raises, as the 404 does."""
    name, status = "old-1", "running"
    attrs = {"State": {}, "Config": {"Image": "acme/svc:latest"}}
    ports = {}

    @property
    def image(self):
        raise docker_probes.docker.errors.ImageNotFound("404")


def test_list_containers_survives_a_stale_image(monkeypatch):
    client = types.SimpleNamespace(containers=types.SimpleNamespace(list=lambda all: [_Stale()]))
    monkeypatch.setattr(docker_probes, "_docker", lambda: client)
    out = asyncio.run(docker_probes.list_containers.coroutine())
    assert "old-1" in out and "image=acme/svc:latest" in out
