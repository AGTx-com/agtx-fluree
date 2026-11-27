"""Do the signed-transaction examples in Fluree's docs work?

Usage: python3 credential_docs_probe.py <fluree-binary> [port, default 18981]

In-memory server, default settings. An Ed25519 did:key gets an allow-all policy (an unsigned setup write), then:
  A. the Verifiable Credential transaction from docs/transactions/signed-transactions.md (an Ed25519Signature2020
     proof), posted to /upsert as application/vc+ld+json, as the page says;
  B. the same credential posted as application/json;
  C. the same credential with its proof value replaced by garbage, to see whether the proof is ever examined;
  D. an ES256 JWS, the algorithm docs/concepts/verifiable-data.md uses in its examples and lists as supported,
     properly signed with a P-256 key;
  E. an EdDSA JWS carrying an ordinary insert (control).
Then it lists the subjects that were written. Needs the `cryptography` package.
"""
import base64
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

BIN = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18981
L = 'docs:main'
F = 'https://ns.flur.ee/db#'
B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def b58(data):
    n = int.from_bytes(data, 'big')
    out = ''
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return '1' * (len(data) - len(data.lstrip(b'\0'))) + out


ED = Ed25519PrivateKey.generate()
ED_PUB = ED.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
DID = 'did:key:z' + b58(b'\xed\x01' + ED_PUB)


def jws_eddsa(payload):
    header = {'alg': 'EdDSA', 'jwk': {'kty': 'OKP', 'crv': 'Ed25519', 'x': b64u(ED_PUB)}}
    signing_input = b64u(json.dumps(header).encode()) + '.' + b64u(json.dumps(payload).encode())
    return signing_input + '.' + b64u(ED.sign(signing_input.encode()))


def jws_es256(payload):
    key = ec.generate_private_key(ec.SECP256R1())
    nums = key.public_key().public_numbers()
    header = {'alg': 'ES256', 'jwk': {'kty': 'EC', 'crv': 'P-256', 'x': b64u(nums.x.to_bytes(32, 'big')),
                                      'y': b64u(nums.y.to_bytes(32, 'big'))}}
    signing_input = b64u(json.dumps(header).encode()) + '.' + b64u(json.dumps(payload).encode())
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    return signing_input + '.' + b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))


def request(method, path, body=None, ctype='application/json'):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=60)
    c.request(method, path, None if body is None else body.encode(), {'Content-Type': ctype, 'Accept': 'application/json'})
    r = c.getresponse()
    text = r.read().decode()
    c.close()
    try:
        return r.status, json.loads(text)
    except ValueError:
        return r.status, text


def docs_credential(proof_value):
    """The credential from docs/transactions/signed-transactions.md, with this probe's DID."""
    return {
        '@context': ['https://www.w3.org/2018/credentials/v1'],
        'type': ['VerifiableCredential'],
        'issuer': DID,
        'issuanceDate': '2024-01-22T10:00:00Z',
        'credentialSubject': {
            'id': DID,
            'flureeTransaction': {
                '@context': {'ex': 'http://example.org/ns/'},
                '@graph': [{'@id': 'ex:alice', 'schema:name': 'Alice'}],
            },
        },
        'proof': {
            'type': 'Ed25519Signature2020',
            'created': '2024-01-22T10:00:00Z',
            'verificationMethod': DID + '#' + DID.split(':')[-1],
            'proofPurpose': 'authentication',
            'proofValue': proof_value,
        },
    }


def main():
    work = tempfile.mkdtemp(prefix='credential-docs-probe-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work, TMPDIR=work, NO_COLOR='1', RUST_LOG='warn')
    log = open(os.path.join(work, 'server.log'), 'w')
    proc = subprocess.Popen([BIN, 'server', 'run', '--memory', '--listen-addr', '127.0.0.1:%d' % PORT],
                            cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(500):
            try:
                request('GET', '/health')
                break
            except OSError:
                time.sleep(0.02)
        print(subprocess.run([BIN, '--version'], capture_output=True, text=True).stdout.strip())
        print('signer:', DID)
        assert request('POST', '/v1/fluree/create', json.dumps({'ledger': L}))[0] in (200, 201)
        setup = {'@context': {'f': F, 'ex': 'urn:ex:'}, '@graph': [
            {'@id': DID, 'f:policyClass': [{'@id': 'ex:AllowAll'}]},
            {'@id': 'ex:allowAll', '@type': ['f:AccessPolicy', 'ex:AllowAll'],
             'f:action': [{'@id': 'f:view'}, {'@id': 'f:modify'}], 'f:allow': True}]}
        s, r = request('POST', '/v1/fluree/insert?ledger=' + L, json.dumps(setup))
        print('setup (unsigned): HTTP %d' % s)

        ctx = {'ex': 'http://example.org/ns/', 'schema': 'http://schema.org/'}
        cases = [
            ('A docs VC, vc+ld+json', '/upsert', json.dumps(docs_credential('z58DAdFfa9SkqZMVP')),
             'application/vc+ld+json'),
            ('B docs VC, json', '/upsert', json.dumps(docs_credential('z58DAdFfa9SkqZMVP')), 'application/json'),
            ('C docs VC, garbage proof', '/upsert', json.dumps(docs_credential('not-a-proof')),
             'application/vc+ld+json'),
            ('D ES256 JWS', '/insert', jws_es256({'@context': ctx, '@id': 'ex:es256', 'schema:name': 'ES256'}),
             'application/jose'),
            ('E EdDSA JWS (control)', '/insert', jws_eddsa({'@context': ctx, '@id': 'ex:eddsa', 'schema:name': 'EdDSA'}),
             'application/jose'),
        ]
        for name, route, body, ctype in cases:
            s, r = request('POST', '/v1/fluree%s?ledger=%s' % (route, L), body, ctype)
            detail = ('t=%s' % r.get('t')) if (s == 200 and isinstance(r, dict)) else json.dumps(r)[:240]
            print('case %-30s HTTP %d %s' % (name, s, detail))

        q = 'SELECT DISTINCT ?s FROM <%s> WHERE { ?s ?p ?o } ORDER BY ?s' % L
        s, r = request('POST', '/v1/fluree/query', q, 'application/sparql-query')
        subjects = [b['s']['value'] for b in r.get('results', {}).get('bindings', [])] if isinstance(r, dict) else r
        print('subjects written:', subjects)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(work, ignore_errors=True)


main()
