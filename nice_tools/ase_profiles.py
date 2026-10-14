"""Read encrypted ASE profiles beside the executable without storing keys."""

from __future__ import annotations

import configparser
import hashlib
from pathlib import Path
import re
import sys

from . import jasypt
from .ase_jobs import connection_data


FILENAME = "ase-connections.txt"
# This bounds configuration metadata, independently of comparison/BCP files.
MAX_CONFIG_BYTES = 1024 * 1024
MAX_PROFILES = 100
JASYPT_FIELDS = ("algorithm", "iterations", "salt_generator", "iv_generator", "output_type")
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


def _load(path: Path | None) -> tuple[Path, bool, dict, list[dict]]:
    location = _profile_path(path)
    try:
        with location.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        return location, False, _settings({}), []
    except OSError:
        raise ValueError(READ_ERROR) from None
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
    names = parser.sections()
    if any(name.casefold() == "default" for name in names):
        raise ValueError(CONFIG_ERROR)
    settings = _settings(dict(parser["jasypt"]) if parser.has_section("jasypt") else {})
    profile_names = [name for name in names if name != "jasypt"]
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
    return location, True, settings, profiles


def public_profiles(path: Path | None = None) -> dict:
    """Return connection choices and crypto metadata without password material."""
    location, exists, settings, profiles = _load(path)
    return {"path": str(location), "exists": exists,
            "profiles": [item["public"] for item in profiles], "jasypt": settings}


def decrypt_connection(profile_id: str, master_password: str, path: Path | None = None) -> dict:
    """Decrypt one current choice using a caller-provided, memory-only master key."""
    _, _, settings, profiles = _load(path)
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
