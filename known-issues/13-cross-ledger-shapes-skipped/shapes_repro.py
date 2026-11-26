"""Bug 13: shapes taken from another ledger are skipped when the data ledger has not registered their IRIs.

Usage: python3 -I shapes_repro.py <fluree-binary>

One local server (in memory), two ledgers per case. ref:main holds a node shape for urn:x:Listing whose property
shape requires one urn:x:commodity that is an instance of urn:x:Commodity, plus the value set (coffee and cocoa are
commodities) in the same shapes graph. co:main enables SHACL and takes its shapes from ref:main with
f:shapesSource + f:ledger. Three writes follow: no commodity (must be rejected), coffee (must pass) and unobtainium
(must be rejected). Cases differ only in which IRIs co:main has registered beforehand.
"""
import http.client, json, os, subprocess, sys, tempfile, time

BIN = sys.argv[1]
PORT = 18995
SH = 'http://www.w3.org/ns/shacl#'
F = 'https://ns.flur.ee/db#'


def req(method, path, body=None, ctype='application/json'):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=60)
    c.request(method, path, None if body is None else body.encode(), {'Content-Type': ctype, 'Accept': 'application/json'})
    r = c.getresponse()
    t = r.read().decode()
    c.close()
    return r.status, t


def update(ledger, sparql):
    return req('POST', '/v1/fluree/update?ledger=' + ledger, sparql, 'application/sparql-update')


NAMED = ('<urn:shape:Listing> a <{sh}NodeShape> ; <{sh}targetClass> <urn:x:Listing> ; <{sh}property> <urn:shape:Listing:commodity> . '
         '<urn:shape:Listing:commodity> <{sh}path> <urn:x:commodity> ; <{sh}minCount> 1 ; <{sh}class> <urn:x:Commodity> .')
BLANK = ('<urn:shape:Listing> a <{sh}NodeShape> ; <{sh}targetClass> <urn:x:Listing> ; '
         '<{sh}property> [ <{sh}path> <urn:x:commodity> ; <{sh}minCount> 1 ; <{sh}class> <urn:x:Commodity> ] .')
DATA_IRIS = '<urn:seed> <urn:x:commodity> <urn:x:coffee> ; <urn:x:kind> <urn:x:Commodity> ; <urn:x:also> <urn:x:Listing>'
CASES = [
    # name, where the shapes live, shape form, extra IRIs seeded into co:main
    ('control: shapes in co:main itself', 'co:main', NAMED, ''),
    ('cross-ledger, fresh co:main', 'ref:main', NAMED, None),
    ('cross-ledger, co:main knows the data IRIs', 'ref:main', NAMED, DATA_IRIS + ' .'),
    ('cross-ledger, + an unrelated urn:shape: IRI', 'ref:main', NAMED, DATA_IRIS + ' , <urn:shape:Unrelated> .'),
    ('cross-ledger, + both shape node IRIs', 'ref:main', NAMED, DATA_IRIS + ' , <urn:shape:Listing> , <urn:shape:Listing:commodity> .'),
    ('cross-ledger, blank property shape, + node IRI', 'ref:main', BLANK, DATA_IRIS + ' , <urn:shape:Listing> .'),
]
WRITES = [('no commodity', 'reject', '<urn:l:0> a <urn:x:Listing> .'),
          ('coffee', 'pass', '<urn:l:1> a <urn:x:Listing> ; <urn:x:commodity> <urn:x:coffee> .'),
          ('unobtainium', 'reject', '<urn:l:2> a <urn:x:Listing> ; <urn:x:commodity> <urn:x:unobtainium> .')]

print(subprocess.run([BIN, '--version'], capture_output=True, text=True).stdout.strip())
wrong = 0
for name, shapes_ledger, shape, seed in CASES:
    work = tempfile.mkdtemp(prefix='bug13-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work, TMPDIR=work, NO_COLOR='1')
    p = subprocess.Popen([BIN, 'server', 'run', '--memory', '--listen-addr', '127.0.0.1:%d' % PORT], cwd=work, env=env,
                         stdout=open(os.path.join(work, 'server.log'), 'w'), stderr=subprocess.STDOUT)
    try:
        for _ in range(500):
            try:
                req('GET', '/health')
                break
            except OSError:
                time.sleep(0.02)
        for L in ('ref:main', 'co:main'):
            assert req('POST', '/v1/fluree/create', json.dumps({'ledger': L}))[0] in (200, 201)
        s, t = update(shapes_ledger, 'INSERT DATA { GRAPH <urn:g:shapes> { %s <urn:x:coffee> a <urn:x:Commodity> . '
                                     '<urn:x:cocoa> a <urn:x:Commodity> . } }' % shape.format(sh=SH))
        assert s == 200, t
        source = '<{f}ledger> <ref:main> ; '.format(f=F) if shapes_ledger == 'ref:main' else ''
        s, t = update('co:main', ('INSERT DATA {{ GRAPH <urn:fluree:co:main#config> {{ '
                                  '<urn:cfg:ledger> a <{f}LedgerConfig> ; <{f}shaclDefaults> <urn:cfg:shacl> . '
                                  '<urn:cfg:shacl> <{f}shaclEnabled> true ; <{f}shapesSource> <urn:cfg:sref> . '
                                  '<urn:cfg:sref> a <{f}GraphRef> ; <{f}graphSource> <urn:cfg:ssrc> . '
                                  '<urn:cfg:ssrc> {src}<{f}graphSelector> <urn:g:shapes> . }} }}').format(f=F, src=source))
        assert s == 200, t
        if seed:
            s, t = update('co:main', 'INSERT DATA { %s }' % seed)
            assert s == 200, t
        line = '%-48s' % name
        for label, expect, body in WRITES:
            s, t = update('co:main', 'INSERT DATA { %s }' % body)
            got = 'pass' if s == 200 else ('reject' if 'SHACL' in t else 'error %s' % s)
            ok = got == expect
            wrong += not ok
            line += ' | %-12s %-7s%s' % (label, got, '' if ok else ' WRONG')
        print(line)
    finally:
        p.terminate()
        p.wait(timeout=10)
print('wrong outcomes:', wrong)
sys.exit(1 if wrong else 0)
