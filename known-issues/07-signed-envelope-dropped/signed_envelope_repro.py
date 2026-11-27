"""Is a signed request's original envelope kept with its commit on a single (non-clustered) server?

Usage: python3 signed_envelope_repro.py <fluree-binary> <port> [output.json]
Needs the `cryptography` package (Ed25519 signing); Fluree's CLI cannot sign a transaction itself.

Plain Fluree: `fluree server run` with file storage in a temporary directory, ephemeral keys, synthetic data.
docs/concepts/time-travel.md says: "Transaction JSON: Not stored by default. A transaction opts in with
TxnOpts::store_raw_txn ... and a signed transaction always keeps its envelope".

Two commits are made and the ledger's commit export (storage proxy) is then read back:
  A. an unsigned insert                                   -> no raw transaction expected
  B. an insert signed as a compact JWS (application/jose) -> the exact JWS expected

The output records only SHA-256 digests of the JWS and of the stored blobs, never the token itself. The export
does return a stored envelope when there is one: a build that resolves the pending upload before the single-server
committer clones its options prints "holds the exact JWS=True" for B with this same script.
"""
import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

CLI, PORT = sys.argv[1], sys.argv[2]
OUT = sys.argv[3] if len(sys.argv) > 3 else None
BASE = 'http://127.0.0.1:%s/v1/fluree' % PORT
LEDGER = 'envelope-repro:main'
EX = 'urn:x:'
F = 'https://ns.flur.ee/db#'


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def keypair():
    k = json.loads(subprocess.check_output([CLI, 'token', 'keygen', '--format', 'json']))
    private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(k['privateKey']['hex'].removeprefix('0x')))
    x = b64(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    return k, private, x


def jws(signer, payload):
    k, private, x = signer
    header = {'alg': 'EdDSA', 'kid': k['did'], 'jwk': {'kty': 'OKP', 'crv': 'Ed25519', 'x': x}}
    data = b64(json.dumps(header).encode()) + '.' + b64(json.dumps(payload).encode())
    return data + '.' + b64(private.sign(data.encode()))


def request(method, path, body=None, content='application/json', token=None):
    headers = {'Content-Type': content}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = None if body is None else (json.dumps(body) if isinstance(body, (dict, list)) else body).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(BASE + path, data=data, headers=headers, method=method),
                                    timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def commits(token):
    """Walk the commit export one commit per page: t -> the blobs stored with that commit."""
    found, cursor = {}, None
    for _ in range(50):
        query = '?limit=1' + ('&cursor_id=%s' % cursor if cursor else '')
        status, body = request('GET', '/commits/' + LEDGER + query, token=token)
        if status != 200:
            return status, body, found
        page = json.loads(body)
        found[page['newest_t']] = {'blobs': [base64.b64decode(v) for v in page.get('blobs', {}).values()],
                                   'missing': page.get('missing_blobs', [])}
        cursor = page.get('next_cursor_id')
        if not cursor:
            break
    return 200, '', found


record = {'binarySha256': hashlib.sha256(Path(CLI).read_bytes()).hexdigest(),
          'version': subprocess.run([CLI, '--version'], capture_output=True, text=True).stdout.strip(), 'commits': {}}
with tempfile.TemporaryDirectory(prefix='fluree-envelope-') as tmp:
    issuer = keypair()
    issuer_file = tmp + '/issuer.key'
    Path(issuer_file).write_text(issuer[0]['privateKey']['hex'])
    writer = keypair()
    subprocess.run([CLI, 'init'], cwd=tmp, check=True, capture_output=True)
    with open(tmp + '/server.log', 'wb') as log:
        server = subprocess.Popen(
            [CLI, 'server', 'run', '--listen-addr', '127.0.0.1:' + PORT, '--log-level', 'warn', '--',
             '--storage-path=' + tmp + '/storage', '--data-auth-mode=optional',
             '--data-auth-trusted-issuer=' + issuer[0]['did'], '--storage-proxy-enabled',
             '--storage-proxy-trusted-issuer=' + issuer[0]['did']], stdout=log, stderr=log, cwd=tmp)
        try:
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError('server exited: ' + Path(tmp + '/server.log').read_text()[-1500:])
                try:
                    urllib.request.urlopen('http://127.0.0.1:%s/health' % PORT, timeout=1)
                    break
                except Exception:
                    time.sleep(.1)
            status, body = request('POST', '/create', {'ledger': LEDGER})
            assert status == 201, (status, body)
            # Open policy defaults, so the signer's identity may write.
            config = {'@graph': 'urn:fluree:' + LEDGER + '#config', '@id': EX + 'config', '@type': F + 'LedgerConfig',
                      F + 'policyDefaults': {'@id': EX + 'defaults', F + 'defaultAllow': True}}
            status, body = request('POST', '/insert/' + LEDGER, {'insert': config})
            assert status == 200, (status, body)

            status, body = request('POST', '/insert/' + LEDGER, {'insert': {'@id': EX + 'a', EX + 'note': 'unsigned'}})
            assert status == 200, (status, body)
            t_plain = json.loads(body)['t']

            payload = {'insert': {'@id': EX + 'c', EX + 'note': 'signed'}, 'exp': int(time.time()) + 300,
                       'jti': 'signed-1'}
            signed = jws(writer, payload)
            status, body = request('POST', '/insert/' + LEDGER, signed, 'application/jose')
            assert status == 200, ('signed insert', status, body)
            t_signed = json.loads(body)['t']
            record['signedInsert'] = {'status': status, 't': t_signed}

            token = subprocess.check_output(
                [CLI, 'token', 'create', '--private-key', '@' + issuer_file, '--identity', 'urn:x:replicator',
                 '--expires-in', '1h', '--output', 'token', '--storage-ledger', LEDGER],
                stderr=subprocess.DEVNULL).decode().strip()
            status, body, found = commits(token)
            assert status == 200, ('commit export', status, body)
        finally:
            server.terminate()
            server.wait(timeout=20)

record['signedJwsSha256'] = hashlib.sha256(signed.encode()).hexdigest()
for label, t in (('A unsigned', t_plain), ('B signed JWS', t_signed)):
    entry = found.get(t, {'blobs': [], 'missing': []})
    texts = []
    for blob in entry['blobs']:
        try:
            texts.append(json.loads(blob))
        except Exception:
            texts.append(blob.decode('utf-8', 'replace'))
    record['commits'][label] = {
        't': t, 'rawTransactionBlobs': len(entry['blobs']), 'missing': entry['missing'],
        'holdsExactJws': signed in texts,
        'blobSha256': [hashlib.sha256(x.encode() if isinstance(x, str) else json.dumps(x).encode()).hexdigest()
                       for x in texts]}

ok = record['commits']['B signed JWS']['holdsExactJws']
record['signedEnvelopeKept'] = ok
print(record['version'])
for label, c in record['commits'].items():
    print('%-14s t=%-2s raw transaction blobs=%d holds the exact JWS=%s' % (
        label, c['t'], c['rawTransactionBlobs'], c['holdsExactJws']))
print('signed envelope kept with its commit:', record['signedEnvelopeKept'], '(docs: True)')
if OUT:
    Path(OUT).write_text(json.dumps(record, indent=1) + '\n')
sys.exit(0 if ok else 1)
