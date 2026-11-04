"""gateway/kc.py prune: which Keycloak clients it may delete. Offline."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("kc", Path(__file__).resolve().parents[2] / "gateway" / "kc.py")
kc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kc)


def test_only_idle_self_registered_clients_are_pruned():
    clients = [{"id": "1", "clientId": "117c745f-6b11-4a7f-8cae-d1145d895462"},   # registered, idle
               {"id": "2", "clientId": "0c40f03d-6f45-4c91-8dfd-737e126d58dd"},   # registered, in use
               {"id": "3", "clientId": "aistudio-mcp-clients"},
               {"id": "4", "clientId": "admin-cli"}]
    gone = kc.idle_registered(clients, lambda cid: 2 if cid == "2" else 0)
    assert [x["id"] for x in gone] == ["1"]


def test_a_first_admin_is_made_only_when_no_real_admin_exists():
    assert kc.needs_first_admin([])
    assert kc.needs_first_admin([{"username": "mcp-check-admin"}])
    assert not kc.needs_first_admin([{"username": "mcp-check-admin"}, {"username": "rsingh"}])
