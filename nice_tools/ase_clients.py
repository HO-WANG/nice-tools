"""Discover ASE ODBC and the PC's installed SAP BCP independently."""

from __future__ import annotations

import os
from pathlib import Path
import shutil

CLIENT_ERROR = (
    "SAP bcp.exe를 찾을 수 없습니다. 파일 IN/OUT에는 SAP Open Client 설치 후 "
    "PATH 또는 ase-connections.txt의 [sap_client] bcp_path를 확인하세요."
)


def _valid_tool(path: str | Path | None, name: str) -> str | None:
    if not path:
        return None
    try:
        candidate = Path(path)
        allowed = {name + '.exe'} if os.name == 'nt' else {name, name + '.exe'}
        # Name validation must precede resolving symlinks: a configured alias to
        # a differently named utility is not a SAP command selection mechanism.
        if not candidate.is_absolute() or candidate.name.casefold() not in allowed or not candidate.is_file():
            return None
        resolved = candidate.resolve()
        if resolved.name.casefold() not in allowed:
            return None
        return str(resolved)
    except (OSError, RuntimeError):
        return None


def _sap_directories() -> list[Path]:
    root = os.environ.get('SYBASE', '')
    ocs = os.environ.get('SYBASE_OCS', '')
    directories = []
    if root:
        base = Path(root)
        if ocs:
            ocs_path = Path(ocs)
            directories.append((ocs_path if ocs_path.is_absolute() else base / ocs_path) / 'bin')
        directories.append(base / 'bin')
        try:
            directories.extend(sorted(base.glob('OCS-*/bin'), reverse=True))
        except OSError:
            pass
    elif ocs and Path(ocs).is_absolute():
        directories.append(Path(ocs) / 'bin')
    return directories


def client_config() -> dict:
    """Resolve BCP only; explicit broken paths never fall back."""
    from .ase_profiles import client_settings

    configured = client_settings()
    result = {}
    for name in ('bcp',):
        key = name + '_path'
        if key in configured:
            resolved = _valid_tool(configured[key], name)
        else:
            names = [name + '.exe'] if os.name == 'nt' else [name, name + '.exe']
            resolved = next((found for executable in names
                             if (found := _valid_tool(shutil.which(executable), name))), None)
            if resolved is None:
                resolved = next((found for directory in _sap_directories() for executable in names
                                 if (found := _valid_tool(directory / executable, name))), None)
        if resolved is None:
            raise ValueError(CLIENT_ERROR)
        result[key] = resolved
    return result


def _odbc_status() -> dict:
    from .ase_odbc import odbc_status
    return odbc_status()


def client_status() -> dict:
    """Missing BCP disables transfers without disabling ODBC browsing."""
    try:
        odbc = _odbc_status()
    except (ImportError, ValueError, OSError, RuntimeError):
        odbc = {'available': False, 'detail': 'SAP ASE ODBC 드라이버 상태를 확인하지 못했습니다.', 'drivers': []}
    result = {'available': bool(odbc.get('available')), 'odbc': odbc,
              'detail': odbc.get('detail', 'SAP ASE ODBC 드라이버를 확인하세요.'), 'bcp_available': False}
    try:
        result.update(client_config(), bcp_available=True)
    except (ValueError, OSError, RuntimeError):
        result['bcp_detail'] = CLIENT_ERROR
    if result['available']:
        result['detail'] = ('pyodbc 조회 / SAP bcp 파일 전송 사용 가능' if result['bcp_available']
                            else 'pyodbc 조회 가능 · 파일 전송에는 SAP bcp.exe가 필요합니다.')
    return result
