"""Package the verified single EXE as an ordinary ZIP named nice-tools.z01.

Rename .z01 to .zip before extracting. This is one complete archive, not a
split ZIP volume. Runtime verification must finish before calling this script.
"""

import argparse
import hashlib
import os
from pathlib import Path
import tempfile
import zipfile

from verify_bundle import validate_windows_x64


def digest(stream):
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        result.update(chunk)
    return result.digest()


def package(executable, output):
    executable, output = Path(executable).resolve(), Path(output).resolve()
    validate_windows_x64(executable)
    if output.suffix.lower() != '.z01' or output == executable:
        raise ValueError('Distribution archive must have the .z01 extension.')
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.nice-tools-package-', dir=output.parent)
    os.close(descriptor)
    stage = Path(name)
    try:
        with zipfile.ZipFile(stage, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            archive.write(executable, arcname='nice-tools.exe')
        with zipfile.ZipFile(stage) as archive:
            if archive.namelist() != ['nice-tools.exe']:
                raise ValueError('Archive must contain only nice-tools.exe.')
            with archive.open('nice-tools.exe') as member, executable.open('rb') as original:
                # Reading to EOF also verifies the member CRC. No full-file
                # buffering or unverified EXE substitution is permitted.
                if digest(member) != digest(original):
                    raise ValueError('Archive EXE differs from the verified source.')
        stage.replace(output)
    finally:
        stage.unlink(missing_ok=True)
    print(f'Verified distribution: {output.name} (ordinary ZIP; contains only nice-tools.exe)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('executable', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    package(args.executable, args.output)
