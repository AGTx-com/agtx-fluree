"""After index GC deletes a named graph's history sidecar: what breaks, does it persist, and does a reindex recover?

Usage: python3 -I cleanup_scope_and_recovery.py <fluree-binary> <port> <memory|file> [output.json]

Plain Fluree only. Same steps as gc_time_travel_repro.py:
an index build after every commit, one edge moved in a named graph, then additive commits to that graph until a
time-pinned read of the graph at t=1 fails. Then, without changing any data:
  1. which other reads of the ledger still work;
  2. (file storage only) whether the failure survives a server restart;
  3. whether a full reindex (POST /reindex) brings the read back;
  4. whether the failure returns after further additive commits;
  5. (file storage, REPRO_RECOVERY_BINARY set) whether another build, started on the same storage, reads the ledger,
     before and after its own full reindex.
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

cli, port, storage = sys.argv[1], sys.argv[2], sys.argv[3]
output = sys.argv[4] if len(sys.argv) > 4 else None
work = tempfile.mkdtemp(prefix='fluree-gc-scope-')
env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
log_path = os.path.join(work, 'server.log')
LEDGER, GRAPH = 'repro:main', 'urn:g'
BASE = 'http://127.0.0.1:%s' % port
record = {'storage': storage, 'steps': []}
store_args = ['--memory'] if storage == 'memory' else []
tail_args = ['--reindex-min-bytes=1'] + ([] if storage == 'memory' else ['--storage-path=' + os.path.join(work, 'data')])


def start(binary=None):
    log = open(log_path, 'a')
    server = subprocess.Popen([binary or cli, 'server', 'run', *store_args, '--listen-addr', '127.0.0.1:' + port, '--log-level',
                               'info', '--', *tail_args], cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(80):
        try:
            urllib.request.urlopen(BASE + '/health', timeout=1)
            return server, log
        except OSError:
            time.sleep(0.25)
    raise AssertionError('server not ready: ' + open(log_path).read()[-800:])


def run(*args, body=None):
    return subprocess.run([cli, '--quiet', '--no-color', *args], cwd=work, env=env, input=body, text=True,
                          capture_output=True, timeout=120)


def update(body):
    r = run('update', '-l', LEDGER, '--remote', 'local', body=body)
    assert r.returncode == 0, ('update', body[:120], r.stderr[-500:])


def indexed_through(t):
    for _ in range(400):
        text = re.sub(r'\x1b\[[0-9;]*m', '', open(log_path).read())
        done = [int(x.split('=')[1]) for x in text.split() if x.startswith('index_t=') and x.split('=')[1].isdigit()]
        if done and max(done) >= t:
            return
        time.sleep(0.05)
    raise AssertionError('no index reached t=%d' % t)


def read(name, query, expected=None):
    r = run('query', '-l', LEDGER, '--remote', 'local', '--format', 'json', body=query)
    try:
        parsed = json.loads(r.stdout)
        rows = [next(iter(b.values()))['value'] for b in parsed['results']['bindings']]
        result = {'ok': True, 'rows': rows}
    except (ValueError, KeyError, TypeError, StopIteration):
        result = {'ok': False, 'error': ' '.join((r.stderr or r.stdout).split())[-220:]}
    result.update({'read': name, 'query': query})
    if expected is not None:
        result['correct'] = result['ok'] and sorted(result['rows']) == sorted(expected)
    return result


def full_reindex():
    req = urllib.request.Request(BASE + '/v1/fluree/reindex', data=json.dumps({'ledger': LEDGER}).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return {'status': resp.status, 'body': resp.read().decode()[:300]}
    except urllib.error.HTTPError as e:
        return {'status': e.code, 'body': e.read().decode()[:300]}


AT1 = 'SELECT ?rev FROM <%s@t:1#%s> WHERE { <urn:s> <urn:sel> ?rev }' % (LEDGER, GRAPH)


def survey(label):
    reads = [
        read('named graph at t=1 (time-pinned)', AT1, ['urn:rev:1']),
        read('named graph at t=2 (time-pinned)',
             'SELECT ?rev FROM <%s@t:2#%s> WHERE { <urn:s> <urn:sel> ?rev }' % (LEDGER, GRAPH), ['urn:rev:2']),
        read('named graph, current state (GRAPH pattern)',
             'SELECT ?rev WHERE { GRAPH <%s> { <urn:s> <urn:sel> ?rev } }' % GRAPH, ['urn:rev:2']),
        read('named graph, current state (FROM <ledger#graph>)',
             'SELECT ?rev FROM <%s#%s> WHERE { <urn:s> <urn:sel> ?rev }' % (LEDGER, GRAPH), ['urn:rev:2']),
        read('default graph, current state', 'SELECT ?v WHERE { <urn:d> <urn:val> ?v }', ['2']),
        read('default graph at t=1 (time-pinned)',
             'SELECT ?v FROM <%s@t:1> WHERE { <urn:d> <urn:val> ?v }' % LEDGER, []),
        read('default graph history of <urn:d>',
             'PREFIX f: <https://ns.flur.ee/db#> SELECT ?t FROM <%s@t:1> TO <%s@t:latest> '
             'WHERE { << <urn:d> <urn:val> ?v >> f:t ?t ; f:op ?op } ORDER BY ?t' % (LEDGER, LEDGER)),
    ]
    record['steps'].append({'step': label, 'reads': reads})
    print('--- ' + label)
    for x in reads:
        shown = x['rows'] if x['ok'] else 'ERROR ' + x['error'][-110:]
        mark = '' if x.get('correct', x['ok']) else '   <-- wrong'
        print('   %-48s %s%s' % (x['read'], shown, mark))
    return reads


assert run('init').returncode == 0
server, log = start()
try:
    assert run('remote', 'add', 'local', BASE).returncode == 0
    r = run('create', LEDGER, '--remote', 'local')
    assert r.returncode == 0, r.stderr
    t = 0
    # t=1: the named-graph edge. t=2: move it, and give the default graph some history too.
    update('INSERT DATA { GRAPH <%s> { <urn:s> <urn:sel> <urn:rev:1> } }' % GRAPH); t += 1; indexed_through(t)
    update('DELETE DATA { GRAPH <%s> { <urn:s> <urn:sel> <urn:rev:1> } } ; INSERT DATA { GRAPH <%s> '
           '{ <urn:s> <urn:sel> <urn:rev:2> } } ; INSERT DATA { <urn:d> <urn:val> 1 }' % (GRAPH, GRAPH))
    t += 1; indexed_through(t)
    update('DELETE DATA { <urn:d> <urn:val> 1 } ; INSERT DATA { <urn:d> <urn:val> 2 }'); t += 1; indexed_through(t)
    survey('before the additive commits (t=%d)' % t)

    failed_at = None
    for i in range(1, 31):
        update('INSERT DATA { GRAPH <%s> { <urn:s> <urn:note> "note %d" } }' % (GRAPH, i)); t += 1; indexed_through(t)
        if not read('at t=1', AT1, ['urn:rev:1']).get('correct'):
            failed_at = i
            break
    record['firstFailureAfterAdditiveCommit'] = failed_at
    print('first failure after additive commit:', failed_at)
    if failed_at is None:
        survey('no failure after 30 additive commits (t=%d)' % t)
    else:
        survey('after the failure (t=%d)' % t)
        if storage == 'file':
            server.terminate(); server.wait(timeout=20); log.close()
            server, log = start()
            survey('after a server restart, same storage')
        reindex = full_reindex()
        record['fullReindex'] = reindex
        print('full reindex:', reindex['status'], reindex['body'][:60])
        after = survey('after a full reindex')
        record['reindexRestoredTheRead'] = bool(after[0].get('correct'))
        again = None
        for i in range(31, 61):
            update('INSERT DATA { GRAPH <%s> { <urn:s> <urn:note> "note %d" } }' % (GRAPH, i)); t += 1
            indexed_through(t)
            if not read('at t=1', AT1, ['urn:rev:1']).get('correct'):
                again = i - 30
                break
        record['failsAgainAfterFurtherAdditiveCommits'] = again
        print('after the reindex, fails again after further additive commits:', again)
        other = os.environ.get('REPRO_RECOVERY_BINARY')
        if other and storage == 'file':
            server.terminate(); server.wait(timeout=20); log.close()
            server, log = start(other)
            record['recoveryBuildUsed'] = True
            first = survey('another build started on the same storage')
            reindex = full_reindex()
            print('its full reindex:', reindex['status'], reindex['body'][:60])
            second = survey('after that build\'s full reindex')
            record['recoveryBuildReadsBeforeItsReindex'] = bool(first[0].get('correct'))
            record['recoveryBuildReadsAfterItsReindex'] = bool(second[0].get('correct'))
            still = None
            for i in range(61, 91):
                update('INSERT DATA { GRAPH <%s> { <urn:s> <urn:note> "note %d" } }' % (GRAPH, i)); t += 1
                indexed_through(t)
                if not read('at t=1', AT1, ['urn:rev:1']).get('correct'):
                    still = i - 60
                    break
            record['recoveryBuildFailsAfterFurtherCommits'] = still
            print('on that build, fails after further additive commits:', still)
finally:
    server.terminate()
    try:
        server.wait(timeout=20)
    except subprocess.TimeoutExpired:
        server.kill()
    log.close()
    text = open(log_path).read()
    record['gcPassesThatReleased'] = text.count('Garbage collection complete')
    if output:
        with open(output, 'w') as out:
            json.dump(record, out, indent=1)
sys.exit(1 if record.get('firstFailureAfterAdditiveCommit') else 0)
