"""What provenance does a signed write leave over HTTP, and do the provenance queries in Fluree's docs find it?

Usage: python3 provenance_probe.py <fluree-binary> [port, default 18982]

In-memory server, default settings. An Ed25519 did:key gets an allow-all policy (an unsigned setup write), then:
  1. a signed insert (compact EdDSA JWS, application/jose) in envelope form, with an f:author user claim;
  2. a signed insert with no claim;
  3. an unsigned insert in envelope form with an f:author claim;
  4. an unsigned insert naming the same DID through opts.identity.
It then runs, before and after POST /v1/fluree/reindex:
  - the "Query provenance" query from docs/transactions/signed-transactions.md, verbatim (default graph);
  - the same query against the ledger's #txn-meta graph;
  - the page's later audit query (with f:assert), against #txn-meta;
  - every f:identity, f:author, f:time and f:txn value in #txn-meta, by t.
Needs the `cryptography` package.
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
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18982
L = 'prov:main'
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


def jws(payload):
    header = {'alg': 'EdDSA', 'jwk': {'kty': 'OKP', 'crv': 'Ed25519', 'x': b64u(PUB)}}
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


DOCS_QUERY = """PREFIX f: <https://ns.flur.ee/db#>

SELECT ?t ?author ?timestamp
WHERE {
  ?commit f:t ?t ;
          f:author ?author ;
          f:timestamp ?timestamp .
}
ORDER BY DESC(?t)"""

DOCS_AUDIT_QUERY = """PREFIX f: <https://ns.flur.ee/db#>
SELECT ?t ?author ?timestamp ?action
FROM <%s#txn-meta>
WHERE {
  ?commit f:t ?t ;
          f:author ?author ;
          f:timestamp ?timestamp .
  ?commit f:assert ?assertion .
  ?assertion ?predicate ?object .
}
ORDER BY DESC(?t)""" % L


def rows(query):
    s, r = request('POST', '/v1/fluree/query', query, 'application/sparql-query')
    if s != 200 or not isinstance(r, dict):
        return 'HTTP %d %s' % (s, json.dumps(r)[:200])
    return r.get('results', {}).get('bindings', [])


def report(label):
    print('--- %s' % label)
    print('docs "Query provenance", verbatim (default graph):', rows(DOCS_QUERY.replace(
        'WHERE', 'FROM <%s>\nWHERE' % L)))
    print('docs "Query provenance", against #txn-meta:', rows(DOCS_QUERY.replace('WHERE', 'FROM <%s#txn-meta>\nWHERE' % L)))
    print('docs audit query (f:assert), against #txn-meta:', rows(DOCS_AUDIT_QUERY))
    q = ('PREFIX f: <%s>\nSELECT ?t ?p ?o FROM <%s#txn-meta> WHERE { ?c f:t ?t ; ?p ?o . '
         'FILTER(?p IN (f:identity, f:author, f:time, f:txn, f:signature, f:timestamp)) } ORDER BY ?t ?p' % (F, L))
    for b in rows(q) if not isinstance(rows(q), str) else []:
        print('    t=%s  %s  %s' % (b['t']['value'], b['p']['value'].split('#')[-1], b['o']['value'][:70]))


def main():
    work = tempfile.mkdtemp(prefix='provenance-probe-')
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
        print('t=1 setup (unsigned): HTTP %d' % request('POST', '/v1/fluree/insert?ledger=' + L, json.dumps(setup))[0])
        ctx = {'ex': 'http://example.org/', 'f': F}
        writes = [
            ('signed, with an f:author claim', jws({'@context': ctx, '@graph': [{'@id': 'ex:a', 'ex:v': 1}],
                                                    'f:author': 'alice-claim'}), 'application/jose'),
            ('signed, no claim', jws({'@context': ctx, '@graph': [{'@id': 'ex:b', 'ex:v': 2}]}), 'application/jose'),
            ('unsigned, with an f:author claim', json.dumps({'@context': ctx, '@graph': [{'@id': 'ex:c', 'ex:v': 3}],
                                                             'f:author': 'bob-claim'}), 'application/json'),
            ('unsigned, opts.identity = the DID', json.dumps({'@context': ctx, '@graph': [{'@id': 'ex:d', 'ex:v': 4}],
                                                              'opts': {'identity': DID}}), 'application/json'),
        ]
        for name, body, ctype in writes:
            s, r = request('POST', '/v1/fluree/insert?ledger=' + L, body, ctype)
            detail = ('t=%s' % r.get('t')) if (s == 200 and isinstance(r, dict)) else json.dumps(r)[:200]
            print('write: %-36s HTTP %d %s' % (name, s, detail))
        report('before reindex')
        s, r = request('POST', '/v1/fluree/reindex', json.dumps({'ledger': L}))
        print('reindex: HTTP %d %s' % (s, json.dumps(r)[:160]))
        report('after reindex')
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(work, ignore_errors=True)


main()
