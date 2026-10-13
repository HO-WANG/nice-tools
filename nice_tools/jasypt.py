"""SunJCE / Jasypt 1.9.3의 문자열 PBE 형식과 호환되는 암호화."""

import base64
import binascii
import hashlib
import secrets
import unicodedata

from Crypto.Cipher import AES, DES
from Crypto.Util.Padding import pad, unpad

AES_ALGORITHM = "PBEWITHHMACSHA512ANDAES_256"
DES_ALGORITHM = "PBEWithMD5AndDES"
DECRYPT_ERROR = "복호화할 수 없습니다. 비밀번호·알고리즘·반복 횟수·salt/IV·출력 형식을 확인하세요."


def validate_payload(payload: dict) -> dict:
    allowed = {"operation", "text", "password", "algorithm", "iterations", "salt_generator", "iv_generator", "output_type", "wrap_enc"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("지원하는 JSON 입력 항목을 확인하세요.")
    config = {"algorithm": AES_ALGORITHM, "iterations": 1000, "salt_generator": "random",
              "iv_generator": "random", "output_type": "base64", "wrap_enc": False, **payload}
    if config.get("operation") not in ("encrypt", "decrypt"):
        raise ValueError("암호화 또는 복호화 작업을 선택하세요.")
    if not isinstance(config.get("text"), str):
        raise ValueError("처리할 문자열을 입력하세요.")
    try:
        config["text"].encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("문자열에 유효하지 않은 Unicode 문자가 있습니다.") from None
    password = config.get("password")
    if not isinstance(password, str) or not password:
        raise ValueError("비밀번호를 입력하세요.")
    password = unicodedata.normalize("NFC", password)
    if any(ord(char) < 32 or ord(char) > 126 for char in password):
        raise ValueError("Java SunJCE 호환을 위해 비밀번호는 ASCII 영문·숫자·기호·스페이스만 사용할 수 있습니다.")
    config["password"] = password
    algorithm = config["algorithm"]
    if not isinstance(algorithm, str) or algorithm.upper() not in (AES_ALGORITHM, DES_ALGORITHM.upper()):
        raise ValueError("지원하는 Jasypt 알고리즘을 선택하세요.")
    config["algorithm"] = AES_ALGORITHM if algorithm.upper() == AES_ALGORITHM else DES_ALGORITHM
    if "iv_generator" not in payload:
        config["iv_generator"] = "random" if config["algorithm"] == AES_ALGORITHM else "none"
    if type(config["iterations"]) is not int or not 1 <= config["iterations"] <= 1_000_000:
        raise ValueError("반복 횟수는 1~1,000,000 사이의 정수여야 합니다.")
    if config["salt_generator"] not in ("random", "zero"):
        raise ValueError("salt 생성기를 확인하세요.")
    if config["iv_generator"] not in ("random", "none"):
        raise ValueError("IV 생성기를 확인하세요.")
    if config["algorithm"] == AES_ALGORITHM and config["iv_generator"] != "random":
        raise ValueError("AES 알고리즘에는 RandomIvGenerator가 필요합니다.")
    if config["output_type"] not in ("base64", "hexadecimal"):
        raise ValueError("출력 형식은 Base64 또는 Hexadecimal이어야 합니다.")
    if type(config["wrap_enc"]) is not bool:
        raise ValueError("ENC 감싸기 옵션을 확인하세요.")
    return config


def make_cipher(config: dict, salt: bytes, iv: bytes):
    password = config["password"].encode("ascii")
    if config["algorithm"] == AES_ALGORITHM:
        key = hashlib.pbkdf2_hmac("sha512", password, salt, config["iterations"], dklen=32)
        return AES.new(key, AES.MODE_CBC, iv)
    digest = hashlib.md5(password + salt).digest()
    for _ in range(config["iterations"] - 1):
        digest = hashlib.md5(digest).digest()
    # SunJCE derives the DES IV from PBKDF1, even when Jasypt includes a random IV.
    return DES.new(digest[:8], DES.MODE_CBC, digest[8:])


def transform(payload: dict) -> str:
    config = validate_payload(payload)
    block = 16 if config["algorithm"] == AES_ALGORITHM else 8
    salt_size = block if config["salt_generator"] == "random" else 0
    iv_size = block if config["iv_generator"] == "random" else 0
    if config["operation"] == "encrypt":
        salt = secrets.token_bytes(block) if salt_size else bytes(block)
        iv = secrets.token_bytes(block) if iv_size else b""
        cipher = make_cipher(config, salt, iv)
        encrypted = cipher.encrypt(pad(config["text"].encode("utf-8"), block))
        raw = (salt if salt_size else b"") + iv + encrypted
        result = base64.b64encode(raw).decode("ascii") if config["output_type"] == "base64" else raw.hex().upper()
        return f"ENC({result})" if config["wrap_enc"] else result

    try:
        text = config["text"].strip()
        if text.startswith("ENC(") and text.endswith(")"):
            text = text[4:-1]
        raw = base64.b64decode("".join(text.split()), validate=True) if config["output_type"] == "base64" else bytes.fromhex(text)
        offset = salt_size + iv_size
        if len(raw) <= offset or (len(raw) - offset) % block:
            raise ValueError(DECRYPT_ERROR)
        salt = raw[:salt_size] if salt_size else bytes(block)
        iv = raw[salt_size:offset]
        cipher = make_cipher(config, salt, iv)
        return unpad(cipher.decrypt(raw[offset:]), block).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeError):
        raise ValueError(DECRYPT_ERROR) from None
