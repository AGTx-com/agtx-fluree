"""Can a W3C verifiable credential secured with JOSE be a Fluree transaction?

Usage: python3 vc_jose_probe.py <fluree-binary> [port, default 18979]

In-memory server. Each case signs a compact JWS with an Ed25519 key (header: alg EdDSA, kid = did:key, embedded
jwk) and posts it to /insert as application/jose:
  1. control: an ordinary JSON-LD insert as the payload;
  2. a VC 2.0 document as the payload, VC-JOSE header (typ vc+jwt, cty vc), with the standard remote context
     https://www.w3.org/ns/credentials/v2;
  3. the same credential with an equivalent inline context, to separate context loading from everything else;
  4. case 3's JWS posted as application/vc+jwt, the VC-JOSE media type;
  5. a credential whose payload was swapped after signing (must be refused);
  6. the standard context first, then the W3C context's own definitions of the terms used, repeated inline.
It then lists every fact, each commit's identity in #txn-meta, and what /show reports per commit.
Needs the `cryptography` package for Ed25519.
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

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

BIN = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18979
L = 'vc:main'
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


KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
DID = 'did:key:z' + b58(b'\xed\x01' + PUB)


def jws(payload, extra_header=None):
    header = {'alg': 'EdDSA', 'kid': DID, 'jwk': {'kty': 'OKP', 'crv': 'Ed25519', 'x': b64u(PUB)}}
    header.update(extra_header or {})
    signing_input = b64u(json.dumps(header).encode()) + '.' + b64u(json.dumps(payload).encode())
    return signing_input + '.' + b64u(KEY.sign(signing_input.encode()))


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


CRED_INLINE = {
    'id': '@id', 'type': '@type', 'xsd': 'http://www.w3.org/2001/XMLSchema#',
    'cred': 'https://www.w3.org/2018/credentials#',
    'VerifiableCredential': 'cred:VerifiableCredential',
    'issuer': {'@id': 'cred:issuer', '@type': '@id'},
    'validFrom': {'@id': 'cred:validFrom', '@type': 'xsd:dateTime'},
    'credentialSubject': {'@id': 'cred:credentialSubject', '@type': '@id'},
}


def credential(n, context):
    return {
        '@context': context,
        'id': 'urn:uuid:acceptance-%d' % n,
        'type': ['VerifiableCredential', 'ex:AcceptanceCredential'],
        'issuer': DID,
        'validFrom': '2026-11-27T12:00:00Z',
        'credentialSubject': {
            'id': 'urn:ex:agreement:a%d' % n,
            'ex:acceptedBy': {'id': 'urn:ex:org:c'},
            'ex:pinnedCommit': 'fluree:commit:sha256:abc%d' % n,
        },
    }


def main():
    work = tempfile.mkdtemp(prefix='vc-jose-probe-')
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
        # A signed identity with no policy is denied by default, so the signer gets an allow-all policy first
        # (unsigned setup write).
        setup = {'@context': {'f': F, 'ex': 'urn:ex:'}, '@graph': [
            {'@id': DID, 'f:policyClass': [{'@id': 'ex:AllowAll'}]},
            {'@id': 'ex:allowAll', '@type': ['f:AccessPolicy', 'ex:AllowAll'],
             'f:action': [{'@id': 'f:view'}, {'@id': 'f:modify'}], 'f:allow': True}]}
        s, r = request('POST', '/v1/fluree/insert?ledger=' + L, json.dumps(setup))
        print('setup (unsigned): HTTP %d %s' % (s, json.dumps(r)[:160]))
        vc_header = {'typ': 'vc+jwt', 'cty': 'vc'}
        cases = [
            ('1 control insert', jws({'@context': {'ex': 'urn:ex:'}, '@id': 'urn:ex:control', 'ex:name': 'control'}),
             'application/jose'),
            ('2 VC, remote v2 context', jws(credential(2, ['https://www.w3.org/ns/credentials/v2', {'ex': 'urn:ex:'}]),
                                           vc_header), 'application/jose'),
            ('3 VC, inline context', jws(credential(3, [CRED_INLINE, {'ex': 'urn:ex:'}]), vc_header),
             'application/jose'),
            ('4 VC, inline, as vc+jwt', jws(credential(4, [CRED_INLINE, {'ex': 'urn:ex:'}]), vc_header),
             'application/vc+jwt'),
        ]
        # Stopgap: the standard context first, then the W3C context's own definitions of the terms used, repeated
        # word for word (JSON-LD 1.1 allows an identical redefinition of a protected term).
        v2_subset = {'id': '@id', 'type': '@type',
                     'issuer': {'@id': 'https://www.w3.org/2018/credentials#issuer', '@type': '@id'},
                     'validFrom': {'@id': 'https://www.w3.org/2018/credentials#validFrom',
                                   '@type': 'http://www.w3.org/2001/XMLSchema#dateTime'},
                     'credentialSubject': {'@id': 'https://www.w3.org/2018/credentials#credentialSubject',
                                           '@type': '@id'}}
        cases.append(('6 VC, standard + subset', jws(credential(7, ['https://www.w3.org/ns/credentials/v2', v2_subset,
                                                                     {'ex': 'urn:ex:'}]), vc_header), 'application/jose'))
        good = jws(credential(5, [CRED_INLINE, {'ex': 'urn:ex:'}]), vc_header)
        head_, payload_, sig_ = good.split('.')
        forged = head_ + '.' + b64u(json.dumps(credential(6, [CRED_INLINE, {'ex': 'urn:ex:'}])).encode()) + '.' + sig_
        cases.append(('5 VC, payload swapped', forged, 'application/jose'))
        for name, body, ctype in cases:
            s, r = request('POST', '/v1/fluree/insert?ledger=' + L, body, ctype)
            print('case %-26s HTTP %d %s' % (name, s, json.dumps(r)[:300]))

        s, r = request('POST', '/v1/fluree/query', 'SELECT ?s ?p ?o FROM <%s> WHERE { ?s ?p ?o } ORDER BY ?s ?p' % L,
                       'application/sparql-query')
        print('facts: HTTP %d' % s)
        def short(v):
            return v['value'] if v['type'] != 'literal' else repr(v['value']) + ('^^' + v['datatype'].split('#')[-1] if 'datatype' in v else '')
        for b in (r.get('results', {}).get('bindings', []) if isinstance(r, dict) else []):
            print('     %s  %s  %s' % (short(b['s'])[:60], short(b['p']), short(b['o'])[:80]))

        q = 'SELECT ?t ?p ?o FROM <%s#txn-meta> WHERE { ?c <%st> ?t ; ?p ?o } ORDER BY ?t ?p' % (L, F)
        s, r = request('POST', '/v1/fluree/query', q, 'application/sparql-query')
        print('txn-meta: HTTP %d' % s)
        for b in (r.get('results', {}).get('bindings', []) if isinstance(r, dict) else []):
            print('     t=%s  %s  %s' % (b['t']['value'], short(b['p']), short(b['o'])[:80]))

        s, info = request('GET', '/v1/fluree/info/' + L)
        head = info.get('ledger', {}).get('t') if isinstance(info, dict) else None
        for t in range(1, (head or 0) + 1):
            s, r = request('GET', '/v1/fluree/show/%s?commit=t:%d' % (L, t))
            print('show t:%d HTTP %d asserts=%s retracts=%s' % (t, s, r.get('asserts'), r.get('retracts')))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        tail = open(os.path.join(work, 'server.log')).read()[-1500:]
        print('server log tail:', tail.replace('\n', ' | ')[-1200:])
        shutil.rmtree(work, ignore_errors=True)


main()
