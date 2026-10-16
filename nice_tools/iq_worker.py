"""Owned IQ worker: one bounded stdin request, flushed newline JSON records."""
from __future__ import annotations

import contextlib
import json
import os
import sys

from . import iq_odbc


# A 1 MiB SQL string can expand sixfold in ASCII JSON; allow metadata too.
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


class _ProtocolError(ValueError):
    pass


def _redact_values(value, password):
    # Keys and numeric fields are protocol, even if a password equals "rows"
    # or "1". An empty replacement cannot itself disclose a short password.
    if isinstance(value, str):
        return value.replace(password, '') if password else value
    if isinstance(value, dict):
        return {key: _redact_values(item, password) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_values(item, password) for item in value]
    return value


def main(stdin=None, stdout=None) -> int:
    """Run one isolated operation. No raw native or unexpected errors escape."""
    stdin = stdin if stdin is not None else getattr(sys.stdin, 'buffer', sys.stdin)
    stdout = stdout if stdout is not None else sys.stdout
    password = ''

    def record(value):
        stdout.write(json.dumps(value, ensure_ascii=True, allow_nan=False) + '\n')
        stdout.flush()

    def reject_constant(value):
        raise ValueError

    try:
        raw = stdin.read(MAX_PAYLOAD_BYTES + 1)
        size = len(raw.encode('utf-8')) if isinstance(raw, str) else len(raw)
        if size > MAX_PAYLOAD_BYTES:
            raise _ProtocolError('Worker request exceeds the metadata limit.')
        try:
            payload = json.loads(raw, parse_constant=reject_constant)
        except (ValueError, UnicodeError):
            raise _ProtocolError('Invalid worker JSON request.') from None
        if not isinstance(payload, dict):
            raise _ProtocolError('Worker request must be an object.')
        connection = payload.get('connection')
        if isinstance(connection, dict) and isinstance(connection.get('password'), str):
            password = connection['password']

        def emit(data):
            if not isinstance(data, dict):
                raise _ProtocolError('Invalid IQ progress record.')
            record({**_redact_values(data, password), 'event': 'progress'})

        # Preserve the protocol stream while discarding incidental library prints.
        with open(os.devnull, 'w', encoding='utf-8') as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                result = iq_odbc.run_operation(payload, emit=emit)
        if not isinstance(result, dict):
            raise _ProtocolError('Invalid IQ operation result.')
        record({'event': 'result', 'result': _redact_values(result, password)})
        return 0
    except Exception as error:
        detail = str(error) if isinstance(error, (iq_odbc.IqOdbcError, _ProtocolError)) else 'IQ operation failed.'
        record({'event': 'error', 'detail': _redact_values(detail, password)})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
