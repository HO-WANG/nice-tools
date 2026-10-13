"""Developer-only bidirectional checks against the real Jasypt 1.9.3 JAR."""

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from nice_tools.jasypt import transform

JAR_URL = 'https://repo.maven.apache.org/maven2/org/jasypt/jasypt/1.9.3/jasypt-1.9.3.jar'
JAR_SHA256 = 'f481fbb8dd8ce754bfde7552af4fcbe8c5e303d53663bb3d8ce9d4338e0e55aa'


def java_transform(java, jar, operation, case, text):
    encode = lambda value: base64.b64encode(value.encode('utf-8')).decode('ascii')
    command = [java, '--class-path', str(jar), str(ROOT / 'tests/reference/JasyptOracle.java'),
               operation, case['algorithm'], encode(case['password']), str(case['iterations']),
               case['salt_generator'], case['iv_generator'], case['output_type'], encode(text)]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=20)
    if result.returncode:
        raise RuntimeError('Java Jasypt interoperability check failed: ' + result.stderr.strip())
    return base64.b64decode(result.stdout.strip()).decode('utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jar', type=Path, help='Existing verified Jasypt 1.9.3 JAR; omit to download')
    parser.add_argument('--java', default='java', help='Java 17+ JDK executable used only for tests')
    args = parser.parse_args()
    if not shutil.which(args.java):
        raise RuntimeError('A Java 17+ JDK is required for this developer-only interoperability check.')
    version = subprocess.run([args.java, '-version'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    major = re.search(r'\bversion "(?:1\.)?(\d+)', version.stdout + version.stderr)
    if version.returncode or major is None or int(major[1]) < 17:
        raise RuntimeError('This developer-only check requires Java 17+ JDK; use --java to select it instead of the system Java.')
    with tempfile.TemporaryDirectory(prefix='nice-tools-jasypt-') as directory:
        jar = args.jar
        if jar is None:
            jar = Path(directory) / 'jasypt-1.9.3.jar'
            with urllib.request.urlopen(JAR_URL, timeout=30) as response:
                jar.write_bytes(response.read())
        if hashlib.sha256(jar.read_bytes()).hexdigest() != JAR_SHA256:
            raise RuntimeError('Jasypt JAR SHA256 does not match the pinned reference.')
        cases = json.loads((ROOT / 'tests/fixtures/jasypt.json').read_text(encoding='utf-8'))['cases']
        for case in cases:
            config = {key: value for key, value in case.items() if key not in ('text', 'ciphertext')}
            java_encrypted = java_transform(args.java, jar, 'encrypt', case, case['text'])
            if transform(config | {'operation': 'decrypt', 'text': java_encrypted}) != case['text']:
                raise RuntimeError(f'Python could not decrypt Java output for {case["algorithm"]}, {case["salt_generator"]}/{case["iv_generator"]}, {case["output_type"]}.')
            python_encrypted = transform(config | {'operation': 'encrypt', 'text': case['text']})
            if java_transform(args.java, jar, 'decrypt', case, python_encrypted) != case['text']:
                raise RuntimeError(f'Java could not decrypt Python output for {case["algorithm"]}, {case["salt_generator"]}/{case["iv_generator"]}, {case["output_type"]}.')
        print(f'Java Jasypt 1.9.3 interoperability: {len(cases)} configurations passed in both directions.')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Make failures readable from the public Actions annotations as well as logs.
        message = str(exc).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print('::error::Jasypt interoperability: ' + message, flush=True)
        raise
