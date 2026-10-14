"""Read bounded, nonsecret SAP sql.ini alias metadata without making logins."""

from __future__ import annotations

import hashlib
import ipaddress
import os
from pathlib import Path
import re


MAX_CONFIG_BYTES = 1024 * 1024
MAX_ALIASES = 1024
_HEADER = re.compile(r"^\[([^\]\r\n]+)\]\s*(?:[;#].*)?$")


class _IniError(ValueError):
    """Static, public-safe reasons; never construct these from file values."""


def _client_settings():
    from .ase_profiles import client_settings
    return client_settings()


def _configuration_path():
    from .ase_profiles import configuration_path
    return configuration_path()


def _bcp_path():
    from .ase_clients import client_config
    try:
        return client_config().get("bcp_path")
    except (ValueError, OSError, RuntimeError):
        return None


def _path(value):
    try:
        if not isinstance(value, (str, Path)):
            raise ValueError
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in str(value)):
            raise ValueError
        candidate = Path(value)
        if not candidate.is_absolute():
            raise ValueError
        resolved = candidate.resolve()
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in str(resolved)):
            raise ValueError
        return resolved
    except (OSError, RuntimeError, ValueError, TypeError):
        raise _IniError("sql.ini의 올바른 절대 경로를 지정하세요.") from None


def _sdk_candidates(tool):
    if not tool:
        return []
    try:
        path = _path(tool)
    except _IniError:
        return []
    if path.name.casefold() not in {"bcp", "bcp.exe"} or path.parent.name.casefold() != "bin":
        return []
    sdk = path.parent.parent
    if sdk.name.casefold().startswith("ocs-"):
        return [sdk.parent / "ini" / "sql.ini", sdk / "ini" / "sql.ini"]
    return [sdk / "ini" / "sql.ini"]


def _discover():
    settings = _client_settings()
    if "sql_ini_path" in settings:
        # Selecting a broken explicit file must never switch to another server
        # catalog merely because a different installation also has sql.ini.
        return _path(settings["sql_ini_path"])
    candidates = []
    sybase = os.environ.get("SYBASE", "")
    if sybase:
        try:
            candidates.append(_path(sybase) / "ini" / "sql.ini")
        except _IniError:
            pass
    candidates.append(_path(_configuration_path()).parent / "sql.ini")
    candidates.extend(_sdk_candidates(_bcp_path()))
    seen = set()
    for candidate in candidates:
        path = _path(candidate)
        if path in seen:
            continue
        seen.add(path)
        try:
            if path.exists():
                return path
        except OSError:
            raise _IniError("sql.ini 경로를 확인하지 못했습니다.") from None
    return None


def _decode(raw):
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeError:
        try:
            text = raw.decode("cp949", errors="strict")
        except UnicodeError:
            raise _IniError("sql.ini를 UTF-8 또는 CP949 텍스트로 읽지 못했습니다.") from None
    if any((ord(char) < 32 and char not in "\t\r\n") or 127 <= ord(char) <= 159 or char in "\u2028\u2029" for char in text):
        raise _IniError("sql.ini에 올바르지 않은 제어 문자가 있습니다.")
    return text


def _parse(text):
    aliases = []
    names = set()
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("["):
            header = _HEADER.fullmatch(line)
            if header is None:
                raise _IniError("sql.ini의 별칭 헤더 형식이 올바르지 않습니다.")
            name = header.group(1).strip()
            if not name or len(name) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in name):
                raise _IniError("sql.ini의 별칭 이름이 올바르지 않습니다.")
            if name.casefold() in names:
                raise _IniError("sql.ini에 중복된 별칭 헤더가 있습니다.")
            if len(aliases) >= MAX_ALIASES:
                raise _IniError("sql.ini의 별칭 수가 지원 범위를 초과했습니다.")
            names.add(name.casefold())
            current = {"name": name, "queries": [], "unsupported": False}
            aliases.append(current)
        else:
            if current is None or "=" not in line:
                raise _IniError("sql.ini의 연결 설정 형식이 올바르지 않습니다.")
            key, value = line.split("=", 1)
            key = key.strip().casefold()
            if key == "query":
                current["queries"].append(value.strip())
            elif key != "master":
                current["unsupported"] = True
    if not aliases:
        raise _IniError("sql.ini에 선택할 서버 별칭이 없습니다.")
    return aliases


def _endpoint(query):
    fields = [field.strip() for field in query.split(",")]
    if len(fields) != 3:
        raise _IniError("query 연결에 추가 옵션 또는 올바르지 않은 항목이 있어 사용할 수 없습니다.")
    protocol, host, port = fields
    if protocol.casefold() != "nlwnsck":
        raise _IniError("NLWNSCK TCP 연결만 지원합니다.")
    if not host or len(host) > 253:
        raise _IniError("query의 서버 주소가 올바르지 않습니다.")
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", host):
            raise _IniError("query의 서버 주소가 올바르지 않습니다.") from None
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise _IniError("query의 포트는 1~65535 사이의 숫자여야 합니다.")
    return host, int(port)


def _public_entries(aliases, fingerprint):
    result = []
    for alias in aliases:
        token = hashlib.sha256(fingerprint + b"\0" + alias["name"].encode("utf-8")).hexdigest()
        entry = {"id": token, "name": alias["name"], "available": False, "detail": ""}
        try:
            if alias["unsupported"]:
                raise _IniError("지원하지 않는 연결 설정이 포함되어 있어 사용할 수 없습니다.")
            queries = alias["queries"]
            if not queries:
                raise _IniError("query 연결이 없습니다. master 연결은 대신 사용하지 않습니다.")
            if len(queries) != 1:
                raise _IniError("여러 query 연결이 있어 하나의 서버로 선택할 수 없습니다.")
            host, port = _endpoint(queries[0])
            entry.update(host=host, port=port, available=True, detail="사용 가능한 TCP 연결")
        except _IniError as error:
            entry["detail"] = str(error)
        result.append(entry)
    return result


def sql_ini_status(path=None):
    """Return public alias metadata, including visibly unsupported entries."""
    result = {"path": None, "found": False, "entries": [], "detail": ""}
    try:
        candidate = _discover() if path is None else _path(path)
        if candidate is None:
            result["detail"] = "sql.ini를 찾지 못했습니다. 파일을 선택하거나 SAP 클라이언트 경로를 확인하세요."
            return result
        result["path"] = str(candidate)
        try:
            with candidate.open("rb") as stream:
                raw = stream.read(MAX_CONFIG_BYTES + 1)
        except FileNotFoundError:
            raise _IniError("지정한 sql.ini 파일을 찾지 못했습니다.") from None
        except OSError:
            raise _IniError("sql.ini 파일을 읽지 못했습니다. 경로와 읽기 권한을 확인하세요.") from None
        if len(raw) > MAX_CONFIG_BYTES:
            raise _IniError("sql.ini 파일이 지원하는 설정 파일 크기를 초과했습니다.")
        aliases = _parse(_decode(raw))
        fingerprint = hashlib.sha256(str(candidate).encode("utf-8") + b"\0" + raw).digest()
        result.update(found=True, entries=_public_entries(aliases, fingerprint), detail="sql.ini 서버 별칭을 읽었습니다.")
    except _IniError as error:
        result["detail"] = str(error)
    except (ValueError, TypeError, OSError, RuntimeError, UnicodeError):
        result["detail"] = "sql.ini 설정을 확인하지 못했습니다. 파일 경로와 형식을 확인하세요."
    return result
