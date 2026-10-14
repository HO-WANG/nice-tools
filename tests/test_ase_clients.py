from pathlib import Path

import pytest


def module():
    from nice_tools import ase_clients
    return ase_clients


def test_explicit_installed_paths_take_precedence_over_path(tmp_path, monkeypatch):
    from nice_tools import ase_profiles
    isql, bcp = tmp_path / 'isql.exe', tmp_path / 'bcp.exe'
    isql.write_bytes(b'fixture')
    bcp.write_bytes(b'fixture')
    path = tmp_path / 'ase-connections.txt'
    path.write_text(f'[sap_client]\nisql_path={isql}\nbcp_path={bcp}\n')
    monkeypatch.setattr(ase_profiles, 'configuration_path', lambda: path)
    monkeypatch.setattr(module().shutil, 'which', lambda name: 'wrong-path')
    assert module().client_config() == {'isql_path': str(isql), 'bcp_path': str(bcp)}
    status = module().client_status()
    assert status['available'] is True
    assert status['isql_path'] == str(isql) and status['bcp_path'] == str(bcp)


def test_path_discovery_requires_both_clients(tmp_path, monkeypatch):
    from nice_tools import ase_profiles
    monkeypatch.setattr(ase_profiles, 'configuration_path', lambda: tmp_path / 'absent.txt')
    isql, bcp = tmp_path / 'isql.exe', tmp_path / 'bcp.exe'
    isql.write_bytes(b'fixture')
    bcp.write_bytes(b'fixture')
    monkeypatch.setattr(module().shutil, 'which', lambda name: str(isql if name.startswith('isql') else bcp))
    assert module().client_config() == {'isql_path': str(isql), 'bcp_path': str(bcp)}
    bcp.unlink()
    assert module().client_status()['available'] is False
    with pytest.raises(ValueError):
        module().client_config()


def test_standard_sybase_ocs_bin_discovery(tmp_path, monkeypatch):
    from nice_tools import ase_profiles
    monkeypatch.setattr(ase_profiles, 'configuration_path', lambda: tmp_path / 'absent.txt')
    directory = tmp_path / 'SAP' / 'OCS-16_0' / 'bin'
    directory.mkdir(parents=True)
    for name in ('isql.exe', 'bcp.exe'):
        (directory / name).write_bytes(b'fixture')
    monkeypatch.setenv('SYBASE', str(tmp_path / 'SAP'))
    monkeypatch.setenv('SYBASE_OCS', 'OCS-16_0')
    monkeypatch.setattr(module().shutil, 'which', lambda name: None)
    assert module().client_config() == {name + '_path': str(directory / (name + '.exe')) for name in ('isql', 'bcp')}


def test_explicit_missing_path_does_not_silently_fall_back(tmp_path, monkeypatch):
    from nice_tools import ase_profiles
    path = tmp_path / 'ase-connections.txt'
    path.write_text(f'[sap_client]\nisql_path={tmp_path / "isql.exe"}\n')
    monkeypatch.setattr(ase_profiles, 'configuration_path', lambda: path)
    monkeypatch.setattr(module().shutil, 'which', lambda name: 'some-other-tool')
    status = module().client_status()
    assert status['available'] is False
    assert 'some-other-tool' not in status['detail']


def test_bad_profile_metadata_does_not_block_installed_client_discovery(tmp_path, monkeypatch):
    from nice_tools import ase_profiles
    path = tmp_path / 'ase-connections.txt'
    isql, bcp = tmp_path / 'isql.exe', tmp_path / 'bcp.exe'
    for tool in (isql, bcp):
        tool.write_bytes(b'fixture')
    path.write_text(f'[sap_client]\nisql_path={isql}\nbcp_path={bcp}\n[bad-profile]\npassword=hidden-existing-secret\n')
    monkeypatch.setattr(ase_profiles, 'configuration_path', lambda: path)
    assert module().client_config() == {'isql_path': str(isql), 'bcp_path': str(bcp)}
