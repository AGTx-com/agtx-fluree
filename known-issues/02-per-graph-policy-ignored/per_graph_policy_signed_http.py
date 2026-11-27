"""Per-graph policy defaults over authenticated HTTP: a signed request writes into the README's locked graph.

Usage: python3 per_graph_policy_signed_http.py <fluree-binary> <port> [output.json]
Needs the `cryptography` package (Ed25519 signing).

Plain Fluree: `fluree server run --memory --data-auth-mode=optional`. The writer is a did:key identity that signs
each request as a compact JWS (application/jose), so the identity is verified by the server, not asserted by a flag.
The writer has no policy class and no policy rule exists.

Case "ledger-locked" (control): ledger-wide f:defaultAllow false with f:OverrideNone -> the signed write is refused.
Case "readme": the config from docs/ledger-config/README.md, "Different policy per graph" (ledger f:defaultAllow
true; <http://example.org/sensitive> f:defaultAllow false with f:OverrideNone) -> the signed write into the sensitive
graph should be refused, and the signed write into another graph allowed.

No token text is saved: the output records statuses only.
"""
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

CLI, PORT = sys.argv[1], int(sys.argv[2])
OUT = sys.argv[3] if len(sys.argv) > 3 else None
LEDGER = 'repro:main'
SENSITIVE, OPEN = 'http://example.org/sensitive', 'http://example.org/open'

LOCKED = '''@prefix f: <https://ns.flur.ee/db#> .
GRAPH <urn:fluree:repro:main#config> {
  <urn:fluree:repro:main:config:ledger> a f:LedgerConfig ;
    f:policyDefaults [ f:defaultAllow false ; f:overrideControl f:OverrideNone ] .
}
'''
README = '''@prefix f: <https://ns.flur.ee/db#> .
GRAPH <urn:fluree:repro:main#config> {
  <urn:fluree:repro:main:config:ledger> a f:LedgerConfig ;
    f:policyDefaults [
      f:defaultAllow true ;
      f:overrideControl f:OverrideAll
    ] ;
    f:graphOverrides (
      [ a f:GraphConfig ;
        f:targetGraph <http://example.org/sensitive> ;
        f:policyDefaults [
          f:defaultAllow false ;
          f:policySource [
            a f:GraphRef ;
            f:graphSource [ f:graphSelector f:defaultGraph ]
          ] ;
          f:overrideControl f:OverrideNone
        ]
      ]
    ) .
}
'''


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


record = {'version': subprocess.run([CLI, '--version'], capture_output=True, text=True).stdout.strip(), 'cases': {}}


def case(name, config, port):
    base = 'http://127.0.0.1:%d/v1/fluree' % port

    def request(method, path, body, content='application/json'):
        data = (json.dumps(body) if isinstance(body, (dict, list)) else body).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, data=data, method=method,
                                                               headers={'Content-Type': content}), timeout=30) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    with tempfile.TemporaryDirectory(prefix='fluree-pergraph-http-') as tmp:
        issuer, writer = keypair(), keypair()
        subprocess.run([CLI, 'init'], cwd=tmp, check=True, capture_output=True)
        with open(tmp + '/server.log', 'wb') as log:
            server = subprocess.Popen(
                [CLI, 'server', 'run', '--memory', '--listen-addr', '127.0.0.1:%d' % port, '--log-level', 'warn',
                 '--', '--data-auth-mode=optional', '--data-auth-trusted-issuer=' + issuer[0]['did']],
                stdout=log, stderr=log, cwd=tmp)
            try:
                for _ in range(100):
                    try:
                        urllib.request.urlopen('http://127.0.0.1:%d/health' % port, timeout=1)
                        break
                    except Exception:
                        time.sleep(.1)
                status, body = request('POST', '/create', {'ledger': LEDGER})
                assert status == 201, (status, body)
                # Administrator setup, unsigned: one fact per graph, then the config.
                for graph in (SENSITIVE, OPEN):
                    status, body = request('POST', '/insert/' + LEDGER, {'insert': {
                        '@graph': graph, '@id': 'urn:x:seed', 'urn:x:note': 'seeded'}})
                    assert status == 200, (status, body)
                status, body = request('POST', '/insert/' + LEDGER, config, 'application/trig')
                assert status == 200, ('config', status, body)

                rows = []
                for label, graph, expected in (('signed write -> open graph', OPEN, name == 'readme'),
                                               ('signed write -> sensitive graph', SENSITIVE, False)):
                    payload = {'insert': {'@graph': graph, '@id': 'urn:x:item', 'urn:x:note': 'written by the signer'},
                               'exp': int(time.time()) + 300, 'jti': name + '-' + graph}
                    status, body = request('POST', '/insert/' + LEDGER, jws(writer, payload), 'application/jose')
                    rows.append({'step': label, 'status': status, 'allowed': status == 200,
                                 'expectedAllowed': expected, 'asDocumented': (status == 200) == expected,
                                 'message': ' '.join(body.split())[:160] if status != 200 else ''})
                # What the administrator then finds in the sensitive graph.
                status, body = request('POST', '/query/' + LEDGER,
                                       'SELECT ?o WHERE { GRAPH <%s> { ?s <urn:x:note> ?o } } ORDER BY ?o' % SENSITIVE,
                                       'application/sparql-query')
                try:
                    stored = [b['o']['value'] for b in json.loads(body)['results']['bindings']]
                except (ValueError, KeyError, TypeError):
                    stored = 'status %s: %s' % (status, body[:120])
                record['cases'][name] = {'requests': rows, 'sensitiveGraphAfterwards': stored}
            finally:
                server.terminate()
                server.wait(timeout=20)


case('ledger-locked', LOCKED, PORT)
case('readme', README, PORT + 1)
print(record['version'])
wrong = 0
for name, c in record['cases'].items():
    for r in c['requests']:
        wrong += not r['asDocumented']
        print('%-14s %-34s HTTP %s %-8s (docs: %-7s) %s' % (
            name, r['step'], r['status'], 'allowed' if r['allowed'] else 'refused',
            'allowed' if r['expectedAllowed'] else 'refused',
            'as documented' if r['asDocumented'] else 'NOT AS DOCUMENTED'))
    print('%-14s sensitive graph afterwards: %s' % (name, c['sensitiveGraphAfterwards']))
print('results that contradict the docs:', wrong)
if OUT:
    with open(OUT, 'w') as out:
        json.dump(record, out, indent=1)
sys.exit(1 if wrong else 0)
