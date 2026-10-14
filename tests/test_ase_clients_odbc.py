"""Database browsing and SAP BCP have independent installed prerequisites."""
from pathlib import Path

import pytest

from nice_tools import ase_clients, ase_profiles


@pytest.fixture
def installed(tmp_path, monkeypatch):
    config = tmp_path / "ase-connections.txt"
    bcp = tmp_path / "bcp.exe"
    bcp.write_bytes(b"fixture")
    monkeypatch.setattr(ase_profiles, "configuration_path", lambda: config)
    monkeypatch.setattr(ase_clients.shutil, "which", lambda name: None)
    monkeypatch.delenv("SYBASE", raising=False)
    monkeypatch.delenv("SYBASE_OCS", raising=False)
    monkeypatch.setattr(ase_clients, "_odbc_status", lambda: {
        "available": True, "detail": "ODBC ready", "driver": "Adaptive Server Enterprise",
        "drivers": ["Adaptive Server Enterprise"],
    }, raising=False)
    return config, bcp


def test_bcp_explicit_path_works_without_isql(installed):
    config, bcp = installed
    config.write_text(f"[sap_client]\nbcp_path={bcp}\nisql_path=obsolete-invalid-path\n")
    assert ase_clients.client_config() == {"bcp_path": str(bcp)}
    result = ase_clients.client_status()
    assert result["available"] and result["bcp_available"]
    assert result["odbc"]["driver"] == "Adaptive Server Enterprise"
    assert "isql_path" not in result


def test_bcp_path_discovery_never_looks_for_isql(installed, monkeypatch):
    _, bcp = installed
    calls = []
    def locate(name):
        calls.append(name)
        return str(bcp) if name.startswith("bcp") else None
    monkeypatch.setattr(ase_clients.shutil, "which", locate)
    assert ase_clients.client_config() == {"bcp_path": str(bcp)}
    assert all(name.startswith("bcp") for name in calls)


def test_ocs_bin_discovery_requires_only_bcp(installed, monkeypatch):
    config, _ = installed
    folder = config.parent / "SAP" / "OCS-16_0" / "bin"
    folder.mkdir(parents=True)
    (folder / "bcp.exe").write_bytes(b"fixture")
    monkeypatch.setenv("SYBASE", str(config.parent / "SAP"))
    monkeypatch.setenv("SYBASE_OCS", "OCS-16_0")
    assert ase_clients.client_config() == {"bcp_path": str(folder / "bcp.exe")}


def test_missing_bcp_still_allows_odbc_browsing(installed):
    result = ase_clients.client_status()
    assert result["available"] is True
    assert result["bcp_available"] is False
    assert "bcp_path" not in result


def test_explicit_missing_bcp_never_uses_other_path(installed, monkeypatch):
    config, bcp = installed
    config.write_text(f"[sap_client]\nbcp_path={config.parent / 'missing' / 'bcp.exe'}\n")
    monkeypatch.setattr(ase_clients.shutil, "which", lambda name: str(bcp))
    with pytest.raises(ValueError):
        ase_clients.client_config()
    assert ase_clients.client_status()["bcp_available"] is False


def test_bcp_alone_cannot_enable_database_connect(installed, monkeypatch):
    config, bcp = installed
    config.write_text(f"[sap_client]\nbcp_path={bcp}\n")
    monkeypatch.setattr(ase_clients, "_odbc_status", lambda: {
        "available": False, "detail": "Install the SAP ASE ODBC driver", "drivers": [],
    })
    result = ase_clients.client_status()
    assert result["available"] is False and result["bcp_available"] is True
    assert "ODBC" in result["detail"]


def test_invalid_other_profile_does_not_disable_bcp(installed):
    config, bcp = installed
    config.write_text(f"[sap_client]\nbcp_path={bcp}\n[broken-profile]\npassword=invalid\n")
    assert ase_clients.client_config() == {"bcp_path": str(bcp)}
