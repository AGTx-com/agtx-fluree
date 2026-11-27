"""Which index state gives which history answer for a re-sent, unchanged fact?

Usage: python3 -I history_resend_by_index_state.py <fluree-binary> <port> [output.json]

Plain Fluree, `fluree server run --memory`, default graph only, plain urn: names. Background indexing is held
back with `--reindex-min-bytes=100000000`, so each index step below happens only when the script asks for it;
`--indexer-catchup-interval-secs=2` lets the periodic sweep run the one incremental build the script waits for.

Steps, for the single fact <urn:x:s> <urn:x:link> <urn:x:b>:
  0  insert the fact and re-send it at once, on a separate subject: nothing is indexed in between.
  1  insert the fact (t=N), then POST /reindex (a full rebuild): the fact is in the index.
  2  re-send the fact unchanged (t=N+1) and ask for its history at once: the re-sent copy is only in memory.
  3  wait for the sweep's incremental index build to absorb the re-sent copy, then ask again.
  4  POST /reindex (a full rebuild), then ask again.
The commit made by the re-send is decoded with GET /show to count its assertions and retractions.

Set RESEND_GRAPH=<graph IRI> to run the same steps inside a named graph. History of a named graph is asked
with FROM NAMED <ledger#graph>, which stock Fluree does not answer, so that variant is only for a build that
supports it.

Exit status: 1 if the history answers of steps 2, 3 and 4 are not all the same, else 0.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

cli, port = sys.argv[1], sys.argv[2]
output = sys.argv[3] if len(sys.argv) > 3 else None
base = 'http://127.0.0.1:%s/v1/fluree' % port
work = tempfile.mkdtemp(prefix='fluree-resend-states-')
env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
log = open(os.path.join(work, 'server.log'), 'w')
server = subprocess.Popen([cli, 'server', 'run', '--memory', '--listen-addr', '127.0.0.1:' + port, '--log-level', 'info',
                           '--', '--reindex-min-bytes=100000000', '--indexer-catchup-interval-secs=2'],
                          cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
L = 'repro:main'
G = os.environ.get('RESEND_GRAPH')
record = {'version': None, 'steps': []}


def call(method, path, body=None, content_type='application/json'):
    data = None if body is None else (body if isinstance(body, str) else json.dumps(body)).encode()
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={'Content-Type': content_type, 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            status, text = response.status, response.read().decode()
    except urllib.error.HTTPError as failure:
        status, text = failure.code, failure.read().decode()
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def update(sparql):
    status, result = call('POST', '/update?ledger=' + urllib.parse.quote(L), sparql, 'application/sparql-update')
    assert status == 200, (sparql, status, result)
    return result.get('t') if isinstance(result, dict) else None


def insert(subject):
    triple = '<%s> <urn:x:link> <urn:x:b>' % subject
    return update('INSERT DATA { %s }' % ('GRAPH <%s> { %s }' % (G, triple) if G else triple))


def info():
    status, result = call('GET', '/info/' + L)
    assert status == 200, result
    return result['ledger']['commit-t'], result['ledger']['index-t']


def history(subject):
    pattern = '<< <%s> <urn:x:link> ?o >> f:t ?t ; f:op ?op' % subject
    named = 'FROM NAMED <%s#%s> ' % (L, G) if G else ''
    where = 'GRAPH <%s#%s> { %s }' % (L, G, pattern) if G else pattern
    query = ('PREFIX f: <https://ns.flur.ee/db#>\nSELECT ?t ?op FROM <%s@t:1> TO <%s@t:latest> %s'
             'WHERE { %s } ORDER BY ?t ?op' % (L, L, named, where))
    status, result = call('POST', '/query', query, 'application/sparql-query')
    assert status == 200, result
    return [('t%s' % b['t']['value'], 'assert' if b['op']['value'] == 'true' else 'retract')
            for b in result['results']['bindings']]


def commit_ops(t):
    status, result = call('GET', '/show/%s?commit=t:%d' % (L, t))
    flakes = result.get('flakes', []) if isinstance(result, dict) else []
    mine = [f for f in flakes if 'urn:x:link' in json.dumps(f)]
    ops = [next((x for x in reversed(f) if isinstance(x, bool)), None) for f in mine]   # a named-graph flake ends
    return {'assertions': ops.count(True), 'retractions': ops.count(False)}             # with its graph, not its op


def note(step, what, answer, **extra):
    commit_t, index_t = info()
    record['steps'].append(dict(step=step, what=what, history=answer, commitT=commit_t, indexT=index_t, **extra))
    print('%-2s %-62s commit t=%-2s index t=%-2s history %s' % (step, what, commit_t, index_t, answer))


status = 2
try:
    for _ in range(120):
        try:
            urllib.request.urlopen('http://127.0.0.1:%s/health' % port, timeout=1)
            break
        except OSError:
            time.sleep(0.25)
    record['version'] = subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip()
    for args in (['init'], ['remote', 'add', 'local', 'http://127.0.0.1:' + port], ['create', L, '--remote', 'local']):
        r = subprocess.run([cli, '--quiet', '--no-color', *args], cwd=work, env=env, capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 0, (args, r.stdout, r.stderr)
    print(record['version'], '| graph:', G or 'default')
    record['graph'] = G

    insert('urn:x:early')
    insert('urn:x:early')
    note('0', 'insert and re-send at once, nothing indexed in between', history('urn:x:early'))

    first_t = insert('urn:x:s')
    assert call('POST', '/reindex', {'ledger': L})[0] == 200
    note('1', 'insert, then full reindex', history('urn:x:s'))

    insert('urn:x:s')
    resend_t = info()[0]
    in_memory = history('urn:x:s')
    note('2', 're-send unchanged; the copy is only in memory', in_memory, resendCommit=commit_ops(resend_t))

    deadline = time.time() + 90
    while time.time() < deadline:
        commit_t, index_t = info()
        if index_t >= commit_t:
            break
        time.sleep(0.5)
    incremental = history('urn:x:s')
    note('3', 'after the incremental index build absorbed the copy', incremental)

    assert call('POST', '/reindex', {'ledger': L})[0] == 200
    rebuilt = history('urn:x:s')
    note('4', 'after a full reindex', rebuilt)

    record['resendCommit'] = commit_ops(resend_t)
    print('the re-send commit (t=%s) holds: %s' % (resend_t, record['resendCommit']))
    same = in_memory == incremental == rebuilt
    record['sameAnswerInEveryIndexState'] = same
    print('same history answer in steps 2, 3 and 4:', same)
    status = 0 if same else 1
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    log.close()
    shutil.rmtree(work, ignore_errors=True)
if output:
    with open(output, 'w') as out:
        json.dump(record, out, indent=1)
sys.exit(status)
