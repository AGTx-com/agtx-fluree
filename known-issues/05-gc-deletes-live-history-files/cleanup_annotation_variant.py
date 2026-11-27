"""Does index GC also delete files that edge-annotation reads still need (default graph and named graph)?

Usage: python3 -I cleanup_annotation_variant.py <fluree-binary> <port> [output.json]

Plain Fluree only: `fluree server run --memory -- --reindex-min-bytes=1` (an index build after every commit).
The GC fix that protects named-graph history files also expands "annotation branches", so this checks whether the
same loss can be seen through annotations. An annotated edge is written in the default graph and in a named graph,
each annotation's status is replaced in two steps (add the new value, then remove the old, so the annotation is
never left empty and has history), a full reindex builds the annotation index, and then only
additive commits follow. After every commit a set of annotation reads is repeated; any error or changed answer is
reported with the commit at which it first appeared.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

cli, port = sys.argv[1], sys.argv[2]
output = sys.argv[3] if len(sys.argv) > 3 else None
work = tempfile.mkdtemp(prefix='fluree-gc-ann-')
env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
log_path = os.path.join(work, 'server.log')
LEDGER, G = 'repro:main', 'urn:g'
BASE = 'http://127.0.0.1:%s' % port
record = {'firstProblems': {}, 'baseline': {}}


def run(*args, body=None):
    return subprocess.run([cli, '--quiet', '--no-color', *args], cwd=work, env=env, input=body, text=True,
                          capture_output=True, timeout=120)


def write(fmt, body):
    r = run('insert' if fmt == 'trig' else 'update', '-l', LEDGER, '--remote', 'local', '--format', fmt, body=body)
    assert r.returncode == 0, (body[:100], r.stderr[-400:])


def indexed_through(t):
    for _ in range(400):
        text = re.sub(r'\x1b\[[0-9;]*m', '', open(log_path).read())
        done = [int(x.split('=')[1]) for x in text.split() if x.startswith('index_t=') and x.split('=')[1].isdigit()]
        if done and max(done) >= t:
            return
        time.sleep(0.05)
    raise AssertionError('no index reached t=%d' % t)


READS = {
    'default graph, annotation now':
        'SELECT ?v WHERE { <urn:s> <urn:rel> <urn:o> {| <urn:status> ?v |} }',
    'default graph, annotation at t=1':
        'SELECT ?v FROM <%s@t:1> WHERE { <urn:s> <urn:rel> <urn:o> {| <urn:status> ?v |} }' % LEDGER,
    'default graph, annotation at t=3':
        'SELECT ?v FROM <%s@t:3> WHERE { <urn:s> <urn:rel> <urn:o> {| <urn:status> ?v |} }' % LEDGER,
    'named graph, annotation now':
        'SELECT ?v WHERE { GRAPH <%s> { <urn:s> <urn:rel> <urn:o> {| <urn:status> ?v |} } }' % G,
    'named graph, annotation at t=2':
        'SELECT ?v FROM <%s@t:2#%s> WHERE { <urn:s> <urn:rel> <urn:o> {| <urn:status> ?v |} }' % (LEDGER, G),
    'named graph, annotation at t=5':
        'SELECT ?v FROM <%s@t:5#%s> WHERE { <urn:s> <urn:rel> <urn:o> {| <urn:status> ?v |} }' % (LEDGER, G),
    'default graph, annotation history':
        'PREFIX f: <https://ns.flur.ee/db#> SELECT ?v FROM <%s@t:1> TO <%s@t:latest> '
        'WHERE { << <urn:m0> <urn:status> ?v >> f:t ?t ; f:op ?op } ORDER BY ?t ?op' % (LEDGER, LEDGER),
}


def reads():
    out = {}
    for name, q in READS.items():
        r = run('query', '-l', LEDGER, '--remote', 'local', '--format', 'json', body=q)
        try:
            out[name] = sorted(next(iter(b.values()))['value'] for b in json.loads(r.stdout)['results']['bindings'])
        except (ValueError, KeyError, TypeError, StopIteration):
            out[name] = 'ERROR ' + ' '.join((r.stderr or r.stdout).split())[-150:]
    return out


assert run('init').returncode == 0
log = open(log_path, 'w')
server = subprocess.Popen([cli, 'server', 'run', '--memory', '--listen-addr', '127.0.0.1:' + port, '--log-level', 'info',
                           '--', '--reindex-min-bytes=1'], cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
try:
    for _ in range(80):
        try:
            urllib.request.urlopen(BASE + '/health', timeout=1)
            break
        except OSError:
            time.sleep(0.25)
    assert run('remote', 'add', 'local', BASE).returncode == 0
    assert run('create', LEDGER, '--remote', 'local').returncode == 0
    t = 0
    write('trig', '<urn:s> <urn:rel> <urn:o> ~ <urn:m0> {| <urn:status> "first" |} .'); t += 1; indexed_through(t)
    write('trig', '<%s> { <urn:s> <urn:rel> <urn:o> ~ <urn:m1> {| <urn:status> "first" |} . }' % G)
    t += 1; indexed_through(t)
    write('sparql', 'INSERT DATA { <urn:m0> <urn:status> "second" }'); t += 1; indexed_through(t)
    write('sparql', 'DELETE DATA { <urn:m0> <urn:status> "first" }'); t += 1; indexed_through(t)
    write('sparql', 'INSERT DATA { GRAPH <%s> { <urn:m1> <urn:status> "second" } }' % G); t += 1; indexed_through(t)
    write('sparql', 'DELETE DATA { GRAPH <%s> { <urn:m1> <urn:status> "first" } }' % G); t += 1; indexed_through(t)
    req = urllib.request.Request(BASE + '/v1/fluree/reindex', data=json.dumps({'ledger': LEDGER}).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=120) as resp:
        assert resp.status == 200
    baseline = reads()
    record['baseline'] = baseline
    print('baseline after setup and a full reindex (t=%d):' % t)
    for k, v in baseline.items():
        print('   %-36s %s' % (k, v))
    for i in range(1, 41):
        # Additive only, touching both subjects' leaves and both annotations' subjects.
        write('sparql', 'INSERT DATA { <urn:s> <urn:note> "n%d" . <urn:m0> <urn:note> "n%d" . '
                        'GRAPH <%s> { <urn:s> <urn:note> "n%d" . <urn:m1> <urn:note> "n%d" } }' % (i, i, G, i, i))
        t += 1; indexed_through(t)
        now = reads()
        for k, v in now.items():
            if v != baseline[k] and k not in record['firstProblems']:
                record['firstProblems'][k] = {'afterAdditiveCommit': i, 't': t, 'got': v, 'baseline': baseline[k]}
                print('CHANGED after additive commit %d (t=%d): %-34s %s' % (i, t, k, str(v)[-170:]))
finally:
    server.terminate()
    try:
        server.wait(timeout=20)
    except subprocess.TimeoutExpired:
        server.kill()
    log.close()
    record['gcPassesThatReleased'] = open(log_path).read().count('Garbage collection complete')
print('reads that changed or failed:', len(record['firstProblems']), '| GC passes that released something:',
      record['gcPassesThatReleased'])
if output:
    with open(output, 'w') as out:
        json.dump(record, out, indent=1)
sys.exit(1 if record['firstProblems'] else 0)
