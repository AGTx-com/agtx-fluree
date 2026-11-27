#!/usr/bin/env python3
"""Fluree 4.2.9: during the graph-ID window SHACL validation counts another named graph's values.

    python3 -I repro_window_shacl.py <fluree binary> [port, default 18776]

Standard library only. Fresh temporary directory (HOME, XDG_CONFIG_HOME, TMPDIR), file-storage server started with
`--reindex-min-bytes=100000000 --indexer-catchup-interval-secs=2` so that index builds are started by the catch-up
sweep and the window lasts seconds. Removes everything it made. Exit 1 if a SHACL decision inside the window is
wrong while the same check on a fresh ledger with no race is right; 0 if none; 2 on setup error.

SHACL on (f:ValidationReject); shapes in the default graph: sh:targetNode <urn:x:k1> and <urn:x:k2>,
sh:path <urn:p:one>, sh:maxCount 1.
  t=1  config and shapes; indexed before going on.
  t=2  <urn:g:z> and <urn:g:a>, one fact each (z's subject sorts first), plus 200,000 filler triples.
  t=3  while t=2 is being indexed: <urn:x:k1> <urn:p:one> "first" in z, and <urn:x:k2> <urn:p:one> "in-a" in a.
When the server logs that index t=2 was installed with later commits unindexed, two writes are sent into z:
  k1 a second value  -> must be rejected (z would hold two values);
  k2 one value       -> must be accepted (z holds none; a's value is another graph's).
The control is the same ledger shape built with no race. Then the window ledger is read again after indexing
settles and after a server restart, and GET /validate reports on <urn:g:z>.
"""
import calendar
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

BIN = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18776
F = 'https://ns.flur.ee/db#'
SH = 'http://www.w3.org/ns/shacl#'


def request(method, path, body=None, ctype='application/json'):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=120)
    c.request(method, path, None if body is None else body.encode(), {'Content-Type': ctype, 'Accept': 'application/json'})
    r = c.getresponse()
    text = r.read().decode()
    c.close()
    try:
        return r.status, json.loads(text)
    except ValueError:
        return r.status, text


def update(ledger, sparql):
    return request('POST', '/v1/fluree/update?ledger=' + ledger, sparql, 'application/sparql-update')


def values(ledger):
    s, r = request('POST', '/v1/fluree/query/' + ledger, 'SELECT ?s ?o WHERE { GRAPH <urn:g:z> { ?s <urn:p:one> ?o } }',
                   'application/sparql-query')
    return sorted('%s=%s' % (b['s']['value'][6:], b['o']['value']) for b in r['results']['bindings'])


def validate(ledger):
    s, r = request('POST', '/v1/fluree/validate/' + ledger, json.dumps({'graph': 'urn:g:z'}))
    return 'conforms=%s violations=%s focus=%s' % (r.get('conforms'), r.get('violations'),
                                                   sorted(set(x['focus_node'] for x in r.get('results', []))))


def wait_log(path, offset, pattern, timeout):
    end = time.time() + timeout
    with open(path, 'rb') as f:
        f.seek(offset)
        while time.time() < end:
            pos = f.tell()
            raw = f.readline()
            if not raw.endswith(b'\n'):
                f.seek(pos)
                time.sleep(0.0005)
                continue
            if re.search(pattern, raw.decode(errors='replace')):
                return raw.decode()
    return None


def stamp(line):
    m = re.match(r'(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d\.\d+)Z', line)
    y, mo, d, h, mi, s = m.groups()
    return calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), 0)) + float(s)


def settle(ledger):
    while True:
        s, r = request('GET', '/v1/fluree/info/' + ledger)
        if s == 200 and r['ledger']['index-t'] >= r['ledger']['commit-t']:
            return
        time.sleep(0.1)


def writes(ledger):
    """[(name, right?, detail, start, end)], the two writes sent in parallel."""
    cases = [('k1 second value', 'INSERT DATA { GRAPH <urn:g:z> { <urn:x:k1> <urn:p:one> "second" } }', False),
             ('k2 one value', 'INSERT DATA { GRAPH <urn:g:z> { <urn:x:k2> <urn:p:one> "z-only" } }', True)]
    out = []

    def one(name, sparql, must):
        t0 = time.time()
        s, r = update(ledger, sparql)
        accepted = s == 200
        msg = '' if accepted else ' '.join(str(r.get('error', r) if isinstance(r, dict) else r).split())[:70]
        out.append((name, accepted == must, '%s (HTTP %s%s; must be %s)' % (
            'accepted' if accepted else 'rejected', s, ': ' + msg if msg else '', 'accepted' if must else 'rejected'),
            t0, time.time()))
    ths = [threading.Thread(target=one, args=c) for c in cases]
    for th in ths:
        th.start()
    for th in ths:
        th.join(60)
    return sorted(out)


def build(ledger, log_path, race):
    assert request('POST', '/v1/fluree/create', json.dumps({'ledger': ledger}))[0] in (200, 201)
    shapes = ' '.join('<urn:x:shape%d> a <%sNodeShape> ; <%stargetNode> <urn:x:k%d> ; <%sproperty> <urn:x:ps%d> . '
                      '<urn:x:ps%d> <%spath> <urn:p:one> ; <%smaxCount> 1 .' % (i, SH, SH, i, SH, i, i, SH, SH)
                      for i in (1, 2))
    s, r = update(ledger, 'INSERT DATA { GRAPH <urn:fluree:%s#config> { <urn:x:cfg> a <%sLedgerConfig> ; '
                          '<%sshaclDefaults> [ <%sshaclEnabled> true ; <%svalidationMode> <%sValidationReject> ] . } %s }'
                  % (ledger, F, F, F, F, F, shapes))
    assert s == 200, r
    settle(ledger)
    offset = os.path.getsize(log_path)
    fill = ' '.join('<urn:f:%d> <urn:p:f> %d .' % (i, i) for i in range(200000 if race else 10))
    s, r = update(ledger, 'INSERT DATA { GRAPH <urn:g:z> { <urn:s:1> <urn:p:m> "z-1" } '
                          'GRAPH <urn:g:a> { <urn:s:2> <urn:p:m> "a-1" } %s }' % fill)
    assert s == 200, r
    if race:
        assert wait_log(log_path, offset, r'Starting index build.*ledger_id=%s current_index_t=1 ' % ledger, 60), 'no build'
    s, r = update(ledger, 'INSERT DATA { GRAPH <urn:g:z> { <urn:x:k1> <urn:p:one> "first" } '
                          'GRAPH <urn:g:a> { <urn:x:k2> <urn:p:one> "in-a" } }')
    assert s == 200, r
    for n in range(4, 7):
        assert update(ledger, 'INSERT DATA { GRAPH <urn:g:z> { <urn:s:1> <urn:p:m> "z-%d" } '
                              'GRAPH <urn:g:a> { <urn:s:2> <urn:p:m> "a-%d" } }' % (n, n))[0] == 200
    if not race:
        settle(ledger)
        return writes(ledger), None
    line = wait_log(log_path, offset, r'index install ledger="%s" index_t=2 .*novelty_flakes_after=[1-9]' % ledger, 60)
    assert line, 'index t=2 was not installed with unindexed commits'
    result = writes(ledger)
    nxt = wait_log(log_path, offset, r'index install ledger="%s" index_t=[3-9]' % ledger, 60)
    return result, (stamp(line), stamp(nxt) if nxt else float('inf'))


def main():
    work = tempfile.mkdtemp(prefix='repro-window-shacl-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work, TMPDIR=work, NO_COLOR='1',
               RUST_LOG='warn,fluree::write_path=debug,fluree_db_indexer::orchestrator=info')
    procs = []

    def start(n):
        log_path = os.path.join(work, 'server-%d.log' % n)
        log = open(log_path, 'w')
        proc = subprocess.Popen([BIN, 'server', 'run', '--listen-addr', '127.0.0.1:%d' % PORT, '--',
                                 '--storage-path=' + os.path.join(work, 'store'), '--reindex-min-bytes=100000000',
                                 '--indexer-catchup-interval-secs=2'], cwd=work, env=env, stdout=log,
                                stderr=subprocess.STDOUT)
        procs.append((proc, log))
        for _ in range(1000):
            try:
                request('GET', '/health')
                return log_path
            except OSError:
                time.sleep(0.02)
        raise AssertionError('server did not start')

    def stop():
        proc, log = procs.pop()
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    status = 2
    try:
        assert subprocess.run([BIN, '--quiet', '--no-color', 'init'], cwd=work, env=env, capture_output=True).returncode == 0
        log_path = start(1)
        print(subprocess.run([BIN, '--version'], capture_output=True, text=True).stdout.strip())
        window, (opened, closed) = build('repro-race:main', log_path, True)
        control, _ = build('repro-control:main', log_path, False)
        print('window: index t=2 installed with later commits unindexed; next install %.0f ms later'
              % ((closed - opened) * 1000))
        wrong = 0
        for (name, ok, detail, t0, t1), (_, ok2, detail2, _, _) in zip(window, control):
            inside = opened <= t0 and t1 <= closed
            bad = inside and not ok and ok2
            wrong += bad
            print('  %-16s in window: %-6s %-58s inside=%s\n  %-16s no race:   %-6s %s%s' % (
                name, 'right' if ok else 'WRONG', detail, inside, '', 'right' if ok2 else 'WRONG', detail2,
                '\n  %-16s ^ wrong decision' % '' if bad else ''))
        settle('repro-race:main')
        print('stored in z after settling: %s; validate z: %s' % (values('repro-race:main'), validate('repro-race:main')))
        stop()
        start(2)
        settle('repro-race:main')
        print('stored in z after restart:  %s; validate z: %s' % (values('repro-race:main'), validate('repro-race:main')))
        print('control ledger in z:        %s; validate z: %s' % (values('repro-control:main'), validate('repro-control:main')))
        print('RESULT wrong in-window decisions with a right control: %d' % wrong)
        status = 1 if wrong else 0
    except AssertionError as e:
        print('setup error:', e)
    finally:
        while procs:
            stop()
        shutil.rmtree(work, ignore_errors=True)
    sys.exit(status)


if __name__ == '__main__':
    main()
