import base64
import importlib
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from nice_tools.main import app

REFERENCE = Path(__file__).parent / 'fixtures/jasypt.json'


def payload(**changes):
    return {'operation': 'encrypt', 'text': '한글🙂\nkeep spaces ', 'password': 'test-password',
            'algorithm': 'PBEWITHHMACSHA512ANDAES_256', 'iterations': 1000,
            'salt_generator': 'random', 'iv_generator': 'random', 'output_type': 'base64',
            'wrap_enc': False, **changes}


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize('case', json.loads(REFERENCE.read_text(encoding='utf-8'))['cases'])
def test_decrypts_real_java_jasypt_ciphertexts(client, case):
    config = {key: value for key, value in case.items() if key not in ('text', 'ciphertext')}
    response = client.post('/api/jasypt', json=payload(**config, operation='decrypt', text=case['ciphertext']))
    assert response.status_code == 200
    assert response.json() == {'result': case['text']}
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('case', json.loads(REFERENCE.read_text(encoding='utf-8'))['cases'])
def test_encrypts_identically_with_java_salt_and_iv(client, monkeypatch, case):
    assert client.post('/api/jasypt', json={}).status_code == 400
    module = importlib.import_module('nice_tools.jasypt')
    raw = base64.b64decode(case['ciphertext']) if case['output_type'] == 'base64' else bytes.fromhex(case['ciphertext'])
    block = 16 if 'AES' in case['algorithm'] else 8
    entropy = []
    cursor = 0
    for generator in (case['salt_generator'], case['iv_generator']):
        if generator == 'random':
            entropy.append(raw[cursor:cursor + block])
            cursor += block

    def fixed_entropy(size):
        result = entropy.pop(0)
        assert len(result) == size
        return result

    monkeypatch.setattr(module.secrets, 'token_bytes', fixed_entropy)
    config = {key: value for key, value in case.items() if key != 'ciphertext'}
    response = client.post('/api/jasypt', json=payload(**config))
    assert response.status_code == 200
    assert response.json() == {'result': case['ciphertext']}
    assert entropy == []


@pytest.mark.parametrize('algorithm,iv', [('PBEWITHHMACSHA512ANDAES_256','random'),('PBEWithMD5AndDES','none')])
def test_random_encryption_enc_wrapper_and_round_trip(client, algorithm, iv):
    config = payload(algorithm=algorithm, iv_generator=iv, wrap_enc=True)
    first = client.post('/api/jasypt', json=config)
    second = client.post('/api/jasypt', json=config)
    assert first.status_code == second.status_code == 200
    encrypted = first.json()['result']
    assert encrypted.startswith('ENC(') and encrypted.endswith(')')
    assert encrypted != second.json()['result']
    response = client.post('/api/jasypt', json=config | {'operation': 'decrypt', 'text': ' \n'+encrypted+'\n '})
    assert response.status_code == 200
    assert response.json()['result'] == config['text']


def test_legacy_algorithm_defaults_to_no_iv_like_jasypt(client, monkeypatch):
    cases = json.loads(REFERENCE.read_text(encoding='utf-8'))['cases']
    case = next(case for case in cases if case['algorithm'] == 'PBEWithMD5AndDES' and case['salt_generator'] == 'random' and case['iv_generator'] == 'none' and case['output_type'] == 'base64')
    module = importlib.import_module('nice_tools.jasypt')
    salt = base64.b64decode(case['ciphertext'])[:8]
    monkeypatch.setattr(module.secrets, 'token_bytes', lambda size: salt)
    response = client.post('/api/jasypt', json={'operation': 'encrypt', 'algorithm': case['algorithm'], 'text': case['text'], 'password': case['password']})
    assert response.status_code == 200
    assert response.json()['result'] == case['ciphertext']


@pytest.mark.parametrize('changes', [
    {'password': ''}, {'password': '한글'}, {'password': 'key\t'}, {'password': None},
    {'text': None}, {'text': '\ud800'}, {'password': '\ud800'}, {'operation': 'unknown'},
    {'algorithm': 'arbitrary'}, {'iterations': 0}, {'iterations': -1}, {'iterations': True},
    {'iterations': '1000'}, {'iterations': 1000001}, {'salt_generator': 'other'},
    {'iv_generator': 'none'}, {'output_type': 'other'}, {'wrap_enc': 'yes'}, {'extra': 'unexpected'},
])
def test_invalid_settings_do_not_echo_secrets(client, changes):
    request = payload() | {'password': 'never-return-this-key'} | changes
    response = client.post('/api/jasypt', content=json.dumps(request), headers={'Content-Type': 'application/json'})
    assert response.status_code == 400
    assert 'never-return-this-key' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert isinstance(response.json()['detail'], str)


@pytest.mark.parametrize('text', ['', 'not ciphertext', 'ENC(broken)', 'ENC(ENC(abcd))', 'AA==', 'abc@xyz'])
def test_malformed_ciphertext_is_a_helpful_error(client, text):
    response = client.post('/api/jasypt', json=payload(operation='decrypt', text=text))
    assert response.status_code == 400
    assert '복호화' in response.json()['detail']


def test_wrong_password_is_not_reported_as_plaintext(client):
    case = json.loads(REFERENCE.read_text(encoding='utf-8'))['cases'][0]
    response = client.post('/api/jasypt', json=payload(operation='decrypt', text=case['ciphertext'], password='wrong-password'))
    assert response.status_code == 400
    assert set(response.json()) == {'detail'}
    assert 'wrong-password' not in response.text


@pytest.mark.parametrize('body,headers', [('not json', {'Content-Type':'application/json'}), ('[]', {'Content-Type':'application/json'}), ('{}', {'Content-Type':'text/plain'})])
def test_bad_requests_are_sanitized(client, body, headers):
    response = client.post('/api/jasypt', content=body, headers=headers)
    assert response.status_code == 400
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('body', ['{"iterations":' + '9' * 5000 + '}', '[' * 5000 + '0' + ']' * 5000], ids=['huge-integer','deep-json'])
def test_extreme_json_inputs_are_sanitized(body):
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post('/api/jasypt', content=body, headers={'Content-Type': 'application/json'})
    assert response.status_code == 400
    assert response.headers['cache-control'] == 'no-store'
    assert isinstance(response.json()['detail'], str)
