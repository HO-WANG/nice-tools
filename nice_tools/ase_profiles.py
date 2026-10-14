"""Read encrypted ASE profiles beside the executable without storing keys."""

from __future__ import annotations

import configparser
import hashlib
from pathlib import Path
import os
import re
import sys
import tempfile
import threading

from . import jasypt
from .ase_jobs import connection_data


FILENAME = "ase-connections.txt"
# This bounds configuration metadata, independently of comparison/BCP files.
MAX_CONFIG_BYTES = 1024 * 1024
MAX_PROFILES = 100
JASYPT_FIELDS = ("algorithm", "iterations", "salt_generator", "iv_generator", "output_type")
RESERVED_SECTIONS = {"jasypt", "sap_client", "preferences"}
CLIENT_FIELDS = {"isql_path", "bcp_path"}
SAVE_ERROR = "접속은 성공했지만 연결 정보를 저장하지 못했습니다. 설정 파일의 형식·권한과 변경 여부를 확인하세요."
_SAVE_LOCK = threading.RLock()
PROFILE_FIELDS = {"host", "port", "user", "password", "database"}
REQUIRED_FIELDS = {"host", "port", "user", "password"}
CONFIG_ERROR = (
    "ase-connections.txt의 형식이 올바르지 않습니다. 프로필의 host·port·user·password=ENC(...)와 "
    "[jasypt] 설정을 확인하세요. 중복 항목·알 수 없는 항목·DEFAULT·평문 비밀번호는 사용할 수 없습니다."
)
READ_ERROR = "ase-connections.txt를 읽을 수 없습니다. 파일 위치·권한과 UTF-8 형식을 확인하세요."
PROFILE_ERROR = "접속 프로필이 없거나 파일이 변경되었습니다. 프로필 목록을 새로 불러오세요."
DECRYPT_ERROR = "ASE 비밀번호를 복호화할 수 없습니다. 마스터 비밀번호와 Jasypt 설정을 확인하세요."


def configuration_path() -> Path:
    """Locate the fixed profile file independently of CWD or bundle extraction."""
    try:
        directory = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                     else Path(__file__).resolve().parents[1])
        return directory / FILENAME
    except (OSError, RuntimeError):
        raise ValueError(READ_ERROR) from None


def _profile_path(path: Path | None) -> Path:
    try:
        candidate = configuration_path() if path is None else Path(path)
        return candidate.resolve()
    except (OSError, RuntimeError):
        raise ValueError(READ_ERROR) from None


def _settings(values: dict) -> dict:
    if set(values) - set(JASYPT_FIELDS):
        raise ValueError(CONFIG_ERROR)
    options = dict(values)
    if "iterations" in options:
        if not re.fullmatch(r"[0-9]{1,7}", options["iterations"]):
            raise ValueError(CONFIG_ERROR)
        options["iterations"] = int(options["iterations"])
    try:
        # Validate metadata through the existing Jasypt implementation. This
        # placeholder never encrypts/decrypts and is not a configured key.
        config = jasypt.validate_payload({"operation": "decrypt", "text": "",
                                          "password": "metadata-validation", **options})
    except (ValueError, TypeError):
        raise ValueError(CONFIG_ERROR) from None
    return {key: config[key] for key in JASYPT_FIELDS}


def _client_settings(values: dict) -> dict:
    if set(values) - CLIENT_FIELDS:
        raise ValueError(CONFIG_ERROR)
    result = {}
    for key, value in values.items():
        value = value.strip()
        if not value:
            continue
        candidate = Path(value)
        expected = key.removesuffix("_path")
        allowed = {expected + ".exe"} if os.name == "nt" else {expected, expected + ".exe"}
        if (any(char in value for char in "\r\n\x00") or not candidate.is_absolute()
                or candidate.name.casefold() not in allowed):
            raise ValueError(CONFIG_ERROR)
        result[key] = value
    return result


def _read_raw(location: Path) -> bytes | None:
    try:
        with location.open("rb") as stream:
            return stream.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ValueError(READ_ERROR) from None


def _parse_raw(raw: bytes) -> tuple[str, configparser.ConfigParser]:
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(CONFIG_ERROR)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError(READ_ERROR) from None
    if "\x00" in text:
        raise ValueError(CONFIG_ERROR)
    # An impossible (NUL) default-section name prevents DEFAULT inheritance.
    parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=None,
                                       strict=True, empty_lines_in_values=False,
                                       default_section="\x00")
    # ConfigParser's default pattern silently discards text after a header.
    parser.SECTCRE = re.compile(r"\[(?P<header>[^\[\]\r\n]+)\]\Z")
    try:
        parser.read_string(text)
    except configparser.Error:
        # Parser exceptions can contain full secret-bearing input lines.
        raise ValueError(CONFIG_ERROR) from None
    if any(name.casefold() == "default" for name in parser.sections()):
        raise ValueError(CONFIG_ERROR)
    return text, parser


def _load(path: Path | None) -> dict:
    location = _profile_path(path)
    raw = _read_raw(location)
    if raw is None:
        return {"path": location, "raw": None, "text": "", "exists": False,
                "settings": _settings({}), "profiles": [], "clients": {}, "last_profile": ""}
    text, parser = _parse_raw(raw)
    names = parser.sections()
    settings = _settings(dict(parser["jasypt"]) if parser.has_section("jasypt") else {})
    clients = _client_settings(dict(parser["sap_client"]) if parser.has_section("sap_client") else {})
    preferences = dict(parser["preferences"]) if parser.has_section("preferences") else {}
    if (set(preferences) - {"last_profile"} or any("\r" in value or "\n" in value
                                                for value in preferences.values())):
        raise ValueError(CONFIG_ERROR)
    profile_names = [name for name in names if name not in RESERVED_SECTIONS]
    if len(profile_names) > MAX_PROFILES:
        raise ValueError(CONFIG_ERROR)
    profiles = []
    for name in profile_names:
        values = dict(parser[name])
        if (not name.strip() or len(name) > 255 or any(char in name for char in "\r\n")
                or set(values) - PROFILE_FIELDS or not REQUIRED_FIELDS <= set(values)
                or any("\r" in value or "\n" in value for value in values.values())):
            raise ValueError(CONFIG_ERROR)
        ciphertext = values["password"].strip()
        if not re.fullmatch(r"ENC\([^()\r\n]+\)", ciphertext) or not ciphertext[4:-1].strip():
            raise ValueError(CONFIG_ERROR)
        try:
            if not re.fullmatch(r"[0-9]{1,5}", values["port"].strip()):
                raise ValueError(CONFIG_ERROR)
            connection = connection_data({"host": values["host"].strip(),
                                          "port": int(values["port"].strip()),
                                          "user": values["user"].strip(), "password": ""})
        except (ValueError, UnicodeError):
            raise ValueError(CONFIG_ERROR) from None
        public = {"id": hashlib.sha256(raw + b"\x00" + name.encode("utf-8")).hexdigest(),
                  "name": name, **{key: connection[key] for key in ("host", "port", "user")}}
        database = values.get("database", "").strip()
        if database:
            if len(database) > 255:
                raise ValueError(CONFIG_ERROR)
            public["database"] = database
        profiles.append({"public": public, "ciphertext": ciphertext})
    return {"path": location, "raw": raw, "text": text, "exists": True, "settings": settings,
            "profiles": profiles, "clients": clients, "last_profile": preferences.get("last_profile", "")}


def public_profiles(path: Path | None = None) -> dict:
    """Return connection choices and crypto metadata without password material."""
    document = _load(path)
    profiles = [item["public"] for item in document["profiles"]]
    selected = next((item["id"] for item in profiles if item["name"] == document["last_profile"]), None)
    return {"path": str(document["path"]), "exists": document["exists"], "profiles": profiles,
            "jasypt": document["settings"], "clients": document["clients"], "last_profile_id": selected}


def decrypt_connection(profile_id: str, master_password: str, path: Path | None = None) -> dict:
    """Decrypt one current choice using a caller-provided, memory-only master key."""
    document = _load(path)
    settings, profiles = document["settings"], document["profiles"]
    selected = next((item for item in profiles if item["public"]["id"] == profile_id), None)
    if not isinstance(profile_id, str) or selected is None:
        raise ValueError(PROFILE_ERROR)
    metadata = selected["public"]
    try:
        password = jasypt.transform({"operation": "decrypt", "text": selected["ciphertext"],
                                     "password": master_password, **settings})
        connection = connection_data({**metadata, "password": password})
    except (ValueError, TypeError, UnicodeError):
        raise ValueError(DECRYPT_ERROR) from None
    result = {"connection": {key: connection[key] for key in ("host", "port", "user", "password")},
              "profile_name": metadata["name"]}
    if "database" in metadata:
        result["database"] = metadata["database"]
    return result


def client_settings(path: Path | None = None) -> dict:
    """Configured executable locations; discovery/existence checks happen separately."""
    raw = _read_raw(_profile_path(path))
    if raw is None:
        return {}
    _, parser = _parse_raw(raw)
    # A syntactically valid but broken profile must still allow manual login
    # using installed tools. Never relax validation of the executable section.
    return _client_settings(dict(parser["sap_client"]) if parser.has_section("sap_client") else {})


def prepare_connection(connection: dict, master_password: str, path: Path | None = None) -> dict:
    """Validate and encrypt before login, without creating or modifying any file."""
    validated = connection_data(connection)
    if any(char in validated["user"] for char in "\r\n"):
        raise ValueError("사용자 이름에는 줄바꿈을 사용할 수 없습니다.")
    data = {key: validated[key] for key in ("host", "port", "user", "password")}
    # Corrupt user data must not block a manual login or be overwritten. Validate
    # the entered key using defaults; the eventual save still rejects this file.
    try:
        settings = _load(path)["settings"]
    except ValueError:
        settings = _settings({})
    ciphertext = jasypt.transform({"operation": "encrypt", "text": data["password"],
                                  "password": master_password, "wrap_enc": True, **settings})
    return {"connection": data, "ciphertext": ciphertext, "settings": settings}


def _update_section(text: str, section: str, values: dict) -> str:
    """Update known single-line INI values while retaining comments and order."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    header = re.compile(r"^\[(?P<name>[^\[\]\r\n]+)\]\s*$")
    start = end = None
    for index, line in enumerate(lines):
        match = header.fullmatch(line.rstrip("\r\n").strip())
        if match:
            if start is not None:
                end = index
                break
            if match["name"] == section:
                start = index
    if start is None:
        prefix = text + (newline if text and not text.endswith(("\n", "\r")) else "")
        if prefix and not prefix.endswith(newline * 2):
            prefix += newline
        return prefix + f"[{section}]{newline}" + "".join(f"{key} = {value}{newline}" for key, value in values.items())
    end = len(lines) if end is None else end
    pending = dict(values)
    for index in range(start + 1, end):
        # Parser validation already excludes duplicate/multiline values.
        match = re.match(r"^\s*([^#;:=\s][^:=]*?)\s*[:=]", lines[index])
        if match:
            key = match[1].strip().lower()
            if key in pending:
                lines[index] = f"{key} = {pending.pop(key)}{newline}"
    if end and not lines[end - 1].endswith(("\n", "\r")):
        lines[end - 1] += newline
    lines[end:end] = [f"{key} = {value}{newline}" for key, value in pending.items()]
    return "".join(lines)


def _write_document(document: dict, text: str) -> None:
    location = document["path"]
    raw = (b"\xef\xbb\xbf" if document["raw"] and document["raw"].startswith(b"\xef\xbb\xbf") else b"") + text.encode("utf-8")
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(SAVE_ERROR)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".ase-connections-", suffix=".tmp", dir=location.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if _read_raw(location) != document["raw"]:
            raise ValueError(SAVE_ERROR)
        os.replace(temporary, location)
        temporary = None
    except (OSError, ValueError):
        raise ValueError(SAVE_ERROR) from None
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink()
            except OSError:
                pass


def _saved_result(name: str, path: Path | None) -> dict:
    public = public_profiles(path)
    profile = next((item for item in public["profiles"] if item["name"] == name), None)
    if profile is None:
        raise ValueError(SAVE_ERROR)
    return {"saved": True, "profile_id": profile["id"], "profile_name": name}


def save_connection(prepared: dict, path: Path | None = None) -> dict:
    """Persist encrypted credentials only after the caller confirms login success."""
    with _SAVE_LOCK:
        document = _load(path)
        if document["settings"] != prepared["settings"]:
            raise ValueError(SAVE_ERROR)
        connection = prepared["connection"]
        identity = tuple(connection[key] for key in ("host", "port", "user"))
        existing = next((item["public"] for item in document["profiles"]
                         if tuple(item["public"][key] for key in ("host", "port", "user")) == identity), None)
        if existing:
            name = existing["name"]
        else:
            if len(document["profiles"]) >= MAX_PROFILES:
                raise ValueError(SAVE_ERROR)
            stem = "saved-" + hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()[:12]
            names = {item["public"]["name"] for item in document["profiles"]}
            name, counter = stem, 1
            while name in names:
                name, counter = f"{stem}-{counter}", counter + 1
        values = {key: connection[key] for key in ("host", "port", "user")}
        values["password"] = prepared["ciphertext"]
        text = _update_section(document["text"], name, values)
        text = _update_section(text, "preferences", {"last_profile": name})
        _write_document(document, text)
        return _saved_result(name, path)


def remember_connection(profile_id: str, path: Path | None = None) -> dict:
    """Record the last successfully used existing profile without touching its cipher."""
    with _SAVE_LOCK:
        document = _load(path)
        profile = next((item["public"] for item in document["profiles"] if item["public"]["id"] == profile_id), None)
        if profile is None:
            raise ValueError(PROFILE_ERROR)
        text = _update_section(document["text"], "preferences", {"last_profile": profile["name"]})
        _write_document(document, text)
        return _saved_result(profile["name"], path)
