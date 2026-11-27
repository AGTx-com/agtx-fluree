"""Which ordinary, unsigned JSON-LD writes does the server mistake for a Verifiable Credential?

Usage: python3 credential_shape_probe.py <fluree-binary> [port, default 18983]

In-memory server, default settings, no signing. Each case is an ordinary unsigned insert posted to /insert as
application/json:
  1. one node typed schema:EducationalOccupationalCredential, a schema.org class;
  2. the same node inside an "@graph" envelope;
  3. one node typed ex:Credentialing (the word only inside a longer name);
  4. one node typed with an array, ["ex:Thing", "ex:CredentialRecord"];
  5. one node whose @type is written through a "type" alias;
  6. one node with a property named jws (mapped to an IRI by the context);
  7. one node typed ex:Certificate (control);
  8. one node typed ex:Thing (control).
Then it lists the subjects that were written. Standard library only.
"""
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

BIN = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18983
L = 'shape:main'


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


def main():
    work = tempfile.mkdtemp(prefix='credential-shape-probe-')
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
        assert request('POST', '/v1/fluree/create', json.dumps({'ledger': L}))[0] in (200, 201)
        ctx = {'ex': 'http://example.org/', 'schema': 'http://schema.org/'}
        cases = [
            ('1 schema.org Credential class', {'@context': ctx, '@id': 'ex:diploma',
                                               '@type': 'schema:EducationalOccupationalCredential', 'schema:name': 'BSc'}),
            ('2 same, in @graph', {'@context': ctx, '@graph': [{'@id': 'ex:diploma2',
                                   '@type': 'schema:EducationalOccupationalCredential', 'schema:name': 'BSc'}]}),
            ('3 ex:Credentialing', {'@context': ctx, '@id': 'ex:process', '@type': 'ex:Credentialing',
                                    'schema:name': 'Review'}),
            ('4 type array', {'@context': ctx, '@id': 'ex:record', '@type': ['ex:Thing', 'ex:CredentialRecord'],
                              'schema:name': 'Record'}),
            ('5 "type" alias', {'@context': dict(ctx, type='@type'), '@id': 'ex:badge', 'type': 'ex:BadgeCredential',
                                'schema:name': 'Badge'}),
            ('6 a property named jws', {'@context': dict(ctx, jws='http://example.org/jws'), '@id': 'ex:token',
                                        'jws': 'eyJhbGciOi...'}),
            ('7 ex:Certificate (control)', {'@context': ctx, '@id': 'ex:cert', '@type': 'ex:Certificate',
                                            'schema:name': 'ISO'}),
            ('8 ex:Thing (control)', {'@context': ctx, '@id': 'ex:thing', '@type': 'ex:Thing', 'schema:name': 'Thing'}),
        ]
        for name, body in cases:
            s, r = request('POST', '/v1/fluree/insert?ledger=' + L, json.dumps(body))
            detail = ('t=%s' % r.get('t')) if (s == 200 and isinstance(r, dict)) else json.dumps(r)[:240]
            print('case %-32s HTTP %d %s' % (name, s, detail))
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
