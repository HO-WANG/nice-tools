"""Strict SAP sql.ini metadata parsing and deterministic local discovery."""

import importlib
import io
from pathlib import Path

import pytest


VALID = "[ASE_UAT]\nmaster=NLWNSCK,ignored.internal,1111\nquery=NLWNSCK,192.0.2.23,3000\n"


def sqlini_module():
    try:
        return importlib.import_module("nice_tools.ase_sqlini")
    except ImportError:
        pytest.fail("The SAP sql.ini metadata reader is not implemented")


@pytest.fixture
def discovery(tmp_path, monkeypatch):
    module = sqlini_module()
    application = tmp_path / "app"
    application.mkdir()
    monkeypatch.setattr(module, "_client_settings", lambda: {})
    monkeypatch.setattr(module, "_configuration_path", lambda: application / "ase-connections.txt")
    monkeypatch.setattr(module, "_bcp_path", lambda: None)
    monkeypatch.delenv("SYBASE", raising=False)
    return module, application


def write_ini(folder, text=VALID, encoding="utf-8"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "sql.ini"
    path.write_text(text, encoding=encoding)
    return path


def test_single_query_endpoint_ignores_master_and_preserves_alias(discovery, tmp_path):
    module, _ = discovery
    path = write_ini(tmp_path, VALID)
    result = module.sql_ini_status(path)
    assert result["found"] is True and result["path"] == str(path.resolve())
    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert {key: entry[key] for key in ("name", "host", "port", "available")} == {
        "name": "ASE_UAT", "host": "192.0.2.23", "port": 3000, "available": True,
    }
    assert entry["id"] and "ASE_UAT" not in entry["id"]
    assert "ignored.internal" not in str(result)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "cp949"])
def test_comments_crlf_indentation_and_legacy_korean_aliases(discovery, tmp_path, encoding):
    module, _ = discovery
    path = write_ini(tmp_path, "; comment\r\n# comment\r\n\r\n[연구 UAT]\r\n  MASTER=not,a,supported,master\r\n\tQuErY = nLwNsCk , ase.internal , 05000\r\n", encoding)
    result = module.sql_ini_status(path)
    assert result["found"]
    assert result["entries"][0]["name"] == "연구 UAT"
    assert result["entries"][0]["host"] == "ase.internal"
    assert result["entries"][0]["port"] == 5000


@pytest.mark.parametrize("query", [
    "", "master=NLWNSCK,ase.internal,5000", "query=NLWNSCK,ase.internal,5000\nquery=NLWNSCK,ase.internal,5001",
    "query=NLWNSCK,ase.internal,5000,ssl=private-secret", "query=NLWNSCK,ase.internal,5000,",
    "query=NLNMP,\\\\server\\pipe\\ase,0", "query=NLWNSCK,ase.internal,service-name",
    "query=NLWNSCK,ase.internal,0", "query=NLWNSCK,ase.internal,65536", "query=NLWNSCK,ase.internal,-1",
    "query=NLWNSCK,ase.internal,5000;private-secret", "query=NLWNSCK,unsafe;private-secret,5000",
    "query=NLWNSCK,host with spaces,5000", "query=NLWNSCK,서버,5000",
    "query=NLWNSCK,ase.internal,5000\npassword=private-secret",
])
def test_unsupported_aliases_stay_visible_without_endpoint_or_secret(discovery, tmp_path, query):
    module, _ = discovery
    path = write_ini(tmp_path, "[unsupported]\n" + query + "\n")
    result = module.sql_ini_status(path)
    assert result["found"] is True
    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry["name"] == "unsupported" and entry["available"] is False
    assert entry["detail"]
    assert "host" not in entry and "port" not in entry
    assert "private-secret" not in str(result)
    assert "password=" not in str(result)


@pytest.mark.parametrize("host", ["localhost", "192.0.2.23", "ase_1.internal", "::1", "2001:db8::123"])
def test_supported_endpoints_are_accepted_by_the_actual_connection_validator(discovery, tmp_path, host):
    from nice_tools.ase_jobs import connection_data

    module, _ = discovery
    result = module.sql_ini_status(write_ini(tmp_path, f"[server]\nquery=NLWNSCK,{host},3000\n"))
    entry = result["entries"][0]
    assert entry["available"]
    accepted = connection_data({"host": entry["host"], "port": entry["port"], "user": "test", "password": ""})
    assert accepted["host"] == entry["host"] and accepted["port"] == entry["port"]


@pytest.mark.parametrize("text", [
    "", "\n; comments only\n", "query=NLWNSCK,ase.internal,5000\n", "[bad\nquery=x\n",
    "[bad] password=private-secret\nquery=x\n", "[bad]\nthis is private-secret\n",
    VALID + "[ASE_UAT]\nquery=NLWNSCK,other,5000\n", VALID + "[ase_uat]\nquery=NLWNSCK,other,5000\n",
    "[bad\0alias]\nquery=NLWNSCK,ase.internal,5000\n",
])
def test_empty_or_malformed_files_cannot_be_persisted_and_never_reflect_raw_lines(discovery, tmp_path, text):
    module, _ = discovery
    result = module.sql_ini_status(write_ini(tmp_path, text))
    assert result["found"] is False and result["entries"] == []
    assert result["detail"] and "private-secret" not in str(result)


def test_metadata_size_limit_rejects_complete_file_instead_of_truncating(discovery, tmp_path, monkeypatch):
    module, _ = discovery
    monkeypatch.setattr(module, "MAX_CONFIG_BYTES", len(VALID.encode("utf-8")))
    result = module.sql_ini_status(write_ini(tmp_path, VALID + "; tail beyond limit\n"))
    assert result["found"] is False and result["entries"] == []


def test_invalid_encoding_and_read_errors_are_sanitized(discovery, tmp_path, monkeypatch):
    module, _ = discovery
    path = tmp_path / "sql.ini"
    path.write_bytes(b"\xff\xff\xff")
    assert module.sql_ini_status(path)["found"] is False

    def forbidden_read(self, *args, **kwargs):
        raise PermissionError("private-secret arbitrary OS error")

    monkeypatch.setattr(Path, "open", forbidden_read)
    result = module.sql_ini_status(path)
    assert not result["found"] and "private-secret" not in str(result)


def test_opaque_ids_are_stable_and_change_with_file_path_or_content(discovery, tmp_path):
    module, _ = discovery
    path = write_ini(tmp_path / "one")
    first = module.sql_ini_status(path)["entries"][0]["id"]
    assert module.sql_ini_status(path)["entries"][0]["id"] == first
    other = write_ini(tmp_path / "two")
    assert module.sql_ini_status(other)["entries"][0]["id"] != first
    path.write_text(VALID + "; changed file\n", encoding="utf-8")
    assert module.sql_ini_status(path)["entries"][0]["id"] != first


def test_explicit_configured_path_has_priority_and_broken_path_never_falls_back(discovery, tmp_path, monkeypatch):
    module, application = discovery
    configured = write_ini(tmp_path / "configured", "[explicit]\nquery=NLWNSCK,configured,1234\n")
    write_ini(application)
    monkeypatch.setattr(module, "_client_settings", lambda: {"sql_ini_path": str(configured)})
    assert module.sql_ini_status()["entries"][0]["name"] == "explicit"
    configured.unlink()
    result = module.sql_ini_status()
    assert not result["found"] and result["entries"] == []
    assert result["path"] == str(configured.resolve())


def test_sybase_ini_precedes_application_and_uses_no_recursive_search(discovery, tmp_path, monkeypatch):
    module, application = discovery
    sybase = tmp_path / "SAP"
    write_ini(sybase / "ini", "[sybase]\nquery=NLWNSCK,sybase,1234\n")
    write_ini(application)
    write_ini(sybase / "unrelated" / "deep", "[unexpected]\nquery=NLWNSCK,wrong,1234\n")
    monkeypatch.setenv("SYBASE", str(sybase))
    assert module.sql_ini_status()["entries"][0]["name"] == "sybase"
    (sybase / "ini" / "sql.ini").unlink()
    assert module.sql_ini_status()["entries"][0]["name"] == "ASE_UAT"
    (application / "sql.ini").unlink()
    assert module.sql_ini_status()["found"] is False


def test_application_folder_discovery_is_independent_of_working_directory(discovery, tmp_path, monkeypatch):
    module, application = discovery
    path = write_ini(application)
    other = tmp_path / "working directory"
    other.mkdir()
    monkeypatch.chdir(other)
    assert module.sql_ini_status()["path"] == str(path.resolve())


def test_bcp_sdk_known_parent_ini_is_discovered_last(discovery, tmp_path, monkeypatch):
    module, _ = discovery
    tool = tmp_path / "SAP" / "OCS-16_0" / "bin" / "bcp.exe"
    path = write_ini(tmp_path / "SAP" / "ini")
    monkeypatch.setattr(module, "_bcp_path", lambda: str(tool))
    assert module.sql_ini_status()["path"] == str(path.resolve())


def test_missing_file_and_direct_relative_path_are_graceful(discovery, tmp_path):
    module, _ = discovery
    result = module.sql_ini_status()
    assert result["found"] is False and result["entries"] == []
    assert result["path"] is None
    assert module.sql_ini_status(tmp_path / "missing.ini")["found"] is False
    assert module.sql_ini_status("relative/sql.ini")["found"] is False


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_non_ini_unicode_separators_cannot_create_hidden_usable_entries(discovery, tmp_path, separator):
    module, _ = discovery
    path = write_ini(tmp_path, "[hidden]" + separator + "query=NLWNSCK,example.internal,5000\n")
    result = module.sql_ini_status(path)
    assert result["found"] is False and result["entries"] == []


@pytest.mark.parametrize("separator", ["\x7f", "\u0085", "\u009f", "\u2028", "\u2029"])
@pytest.mark.parametrize("as_string", [False, True])
def test_unsafe_path_separators_are_rejected_before_read_and_never_reflected(discovery, tmp_path, monkeypatch, separator, as_string):
    module, _ = discovery
    path = tmp_path / ("private-secret" + separator + "sql.ini")
    reads = []

    def content(self, *args, **kwargs):
        reads.append(self)
        return io.BytesIO(VALID.encode("utf-8"))

    monkeypatch.setattr(Path, "open", content)
    result = module.sql_ini_status(str(path) if as_string else path)
    assert result["found"] is False and result["path"] is None and result["entries"] == []
    assert "private-secret" not in str(result)
    assert reads == []


def test_canonical_path_controls_cannot_bypass_validation_via_resolving(discovery, tmp_path, monkeypatch):
    module, _ = discovery
    selected = tmp_path / "selected-sql.ini"
    resolved = tmp_path / "private-secret\u2028target.ini"
    reads = []
    monkeypatch.setattr(Path, "resolve", lambda self: resolved)

    def content(self, *args, **kwargs):
        reads.append(self)
        return io.BytesIO(VALID.encode("utf-8"))

    monkeypatch.setattr(Path, "open", content)
    result = module.sql_ini_status(selected)
    assert result["found"] is False and result["path"] is None
    assert "private-secret" not in str(result) and reads == []
