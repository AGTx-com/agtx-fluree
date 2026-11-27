"""Stock-visible repro: index GC deletes a named-graph history sidecar that the current index still uses.

Its check is a time-pinned read of the named graph (FROM <ledger@t:1#graph>), because stock Fluree does not
answer a FROM NAMED history query.

Usage: python3 -I gc_time_travel_repro.py <fluree-binary> <port> [extra server args...]
Plain Fluree only (no application config). Starts `fluree server run --memory` with an index build after every
commit, moves one edge in a named graph (so the graph's leaf gets a history sidecar), then makes only
additive commits to the same graph. Each additive commit rewrites the leaf while its history stays
byte-identical, so the old sidecar CID is named as garbage although the new root still references it.
After more than the GC ceiling's worth of index versions, the history query is run after every commit.
Prints the first failure, or "no failure". Everything lives in a temporary directory.
"""
import json
import re
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

cli, port, extra = sys.argv[1], sys.argv[2], sys.argv[3:]
work = tempfile.mkdtemp(prefix='fluree-gc-repro-')
env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
log = open(os.path.join(work, 'server.log'), 'w')
server = subprocess.Popen([cli, 'server', 'run', '--memory', '--listen-addr', '127.0.0.1:' + port,
                           '--log-level', 'info', '--', '--reindex-min-bytes=1', *extra], cwd=work, env=env,
                          stdout=log, stderr=subprocess.STDOUT)
LEDGER = 'repro:main'
GRAPH = 'urn:g'


def run(*args, body=None):
    return subprocess.run([cli, '--quiet', '--no-color', *args], cwd=work, env=env, input=body, text=True,
                          capture_output=True, timeout=60)


def update(body):
    r = run('update', '-l', LEDGER, '--remote', 'local', body=body)
    assert r.returncode == 0, ('update', body[:120], r.stderr[-500:])


def indexed_through(t):
    # Wait until the server log shows an index at or beyond t, so each commit gets its own index version.
    for _ in range(200):
        text = re.sub(r'\x1b\[[0-9;]*m', '', open(os.path.join(work, 'server.log')).read())
        done = [int(x.split('=')[1]) for x in text.split() if x.startswith('index_t=') and x.split('=')[1].isdigit()]
        if done and max(done) >= t:
            return
        time.sleep(0.05)
    raise AssertionError('no index reached t=%d; log tail: %s' % (t, text[-800:]))


def history(t):
    # Stock-visible read: the named graph as it stood at t=1 (before the move), via a time-pinned dataset.
    query = ('SELECT ?rev FROM <%s@t:1#%s> WHERE { <urn:s> <urn:sel> ?rev }' % (LEDGER, GRAPH))
    return run('query', '-l', LEDGER, '--remote', 'local', '--format', 'json', body=query)


try:
    for _ in range(40):
        try:
            urllib.request.urlopen('http://127.0.0.1:%s/health' % port, timeout=1)
            break
        except OSError:
            time.sleep(0.25)
    assert run('init').returncode == 0
    assert run('remote', 'add', 'local', 'http://127.0.0.1:' + port).returncode == 0
    r = run('create', LEDGER, '--remote', 'local')
    assert r.returncode == 0, r.stderr
    t = 0
    # t=1: the edge; t=2: move it (a retraction gives the graph's leaf a history sidecar).
    update('INSERT DATA { GRAPH <%s> { <urn:s> <urn:sel> <urn:rev:1> } }' % GRAPH); t += 1; indexed_through(t)
    update('DELETE DATA { GRAPH <%s> { <urn:s> <urn:sel> <urn:rev:1> } } ; '
           'INSERT DATA { GRAPH <%s> { <urn:s> <urn:sel> <urn:rev:2> } }' % (GRAPH, GRAPH)); t += 1; indexed_through(t)
    r0 = history(t)
    print('time-travel read before additive commits:', r0.returncode, (r0.stdout or r0.stderr)[:300].strip())
    changes = int(os.environ.get('REPRO_CHANGES', '30'))
    for i in range(1, changes + 1):
        # Additive only: the same graph's leaf is rewritten, its history is unchanged.
        update('INSERT DATA { GRAPH <%s> { <urn:s> <urn:note> "note %d" } }' % (GRAPH, i)); t += 1; indexed_through(t)
        r = history(t)
        if r.returncode != 0 or '"error"' in r.stdout[:200]:
            print('FAILURE after additive commit %d (t=%d): %s' % (i, t, (r.stderr or r.stdout)[-400:].strip()))
            sys.exit(1)
    rows = json.loads(r.stdout)
    rows = rows['results']['bindings'] if isinstance(rows, dict) else rows
    print('no failure after %d additive commits (t=%d); history rows: %d' % (changes, t, len(rows)))
finally:
    server.terminate()
    server.wait(timeout=10)
    log.close()
    gc = [l for l in open(os.path.join(work, 'server.log')) if 'Garbage collection complete' in l]
    print('GC passes that released something:', len(gc))
