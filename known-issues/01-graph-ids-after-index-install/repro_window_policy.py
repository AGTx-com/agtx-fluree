#!/usr/bin/env python3
"""Fluree 4.2.9: during the graph-ID window a policy condition `GRAPH <iri> { ... }` reads another graph.

    python3 -I repro_window_policy.py <fluree binary> [port, default 18775]

Standard library only. Fresh temporary directory (HOME, XDG_CONFIG_HOME, TMPDIR), in-memory server started with
`--reindex-min-bytes=100000000 --indexer-catchup-interval-secs=2` so that index builds are started by the catch-up
sweep and the window lasts seconds. Removes everything it made. Exit 1 if a policy decision inside the window is
wrong while the same request after indexing settles is right; 0 if none; 2 on setup error.

One policy, ledger-wide, in the default graph, for f:view and f:modify:
    ASK { GRAPH <urn:g:z> { $identity <urn:p:grant> true } }
Identities (same policy class, sent with the `fluree-identity` header): rob, whose grant-shaped fact is in
<urn:g:a> (must be refused); sam, whose grant is in <urn:g:z> (must be allowed); eve, no fact anywhere (must be
refused: shows that policy is enforced on this path).

  t=1  policies, identities, one fact <urn:x:item> in the default graph; indexed before going on.
  t=2  <urn:g:z> and <urn:g:a>, one fact each (z's subject sorts first), plus 200,000 filler triples.
  t=3  while t=2 is being indexed: rob's fact in a, sam's grant in z. t=4..6: small commits.
When the server logs that index t=2 was installed with later commits unindexed, each identity writes one fact
and reads <urn:x:item>; the requests must finish before the next install is logged.
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
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18775
L = 'repro:main'
Q = 'ASK { GRAPH <urn:g:z> { $identity <urn:p:grant> true } }'
F = 'https://ns.flur.ee/db#'


def request(method, path, body=None, ctype='application/json', headers=None):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=120)
    h = {'Content-Type': ctype, 'Accept': 'application/json'}
    h.update(headers or {})
    c.request(method, path, None if body is None else body.encode(), h)
    r = c.getresponse()
    text = r.read().decode()
    c.close()
    try:
        return r.status, json.loads(text)
    except ValueError:
        return r.status, text


def update(sparql, who=None):
    return request('POST', '/v1/fluree/update?ledger=' + L, sparql, 'application/sparql-update',
                   {'fluree-identity': who} if who else None)


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


def decisions():
    """[(name, right?, detail, start, end)] for each identity's write and read, sent in parallel."""
    expected = {'urn:x:rob': False, 'urn:x:sam': True, 'urn:x:eve': False}
    out = []
    lock = threading.Lock()

    def one(who, kind):
        t0 = time.time()
        if kind == 'write':
            s, r = update('INSERT DATA { <urn:x:new-%s-%d> <urn:p:note> "x" }' % (who[6:], int(t0 * 1000)), who)
            allowed = s == 200
            detail = 'HTTP %s' % s
        else:
            s, r = request('POST', '/v1/fluree/query/' + L, 'SELECT ?o WHERE { <urn:x:item> <urn:p:note> ?o }',
                           'application/sparql-query', {'fluree-identity': who})
            got = [b['o']['value'] for b in r['results']['bindings']]
            allowed = got == ['item']
            detail = 'rows %s' % got
        with lock:
            out.append(('%s %s' % (who, kind), allowed == expected[who],
                        '%s (%s; must be %s)' % ('allowed' if allowed else 'refused', detail,
                                                'allowed' if expected[who] else 'refused'), t0, time.time()))
    ths = [threading.Thread(target=one, args=(w, k)) for w in expected for k in ('write', 'read')]
    for th in ths:
        th.start()
    for th in ths:
        th.join(60)
    return sorted(out)


def main():
    work = tempfile.mkdtemp(prefix='repro-window-policy-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work, TMPDIR=work, NO_COLOR='1',
               RUST_LOG='warn,fluree::write_path=debug,fluree_db_indexer::orchestrator=info')
    log_path = os.path.join(work, 'server.log')
    log = open(log_path, 'w')
    proc = subprocess.Popen([BIN, 'server', 'run', '--memory', '--listen-addr', '127.0.0.1:%d' % PORT, '--',
                             '--reindex-min-bytes=100000000', '--indexer-catchup-interval-secs=2'],
                            cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    status = 2
    try:
        for _ in range(500):
            try:
                request('GET', '/health')
                break
            except OSError:
                time.sleep(0.02)
        print(subprocess.run([BIN, '--version'], capture_output=True, text=True).stdout.strip())
        assert request('POST', '/v1/fluree/create', json.dumps({'ledger': L}))[0] in (200, 201)
        pol = ('<urn:x:rule> a <%sAccessPolicy>, <urn:x:Policies> ; <%saction> <%sview>, <%smodify> ; '
               '<%squery> %s^^<%ssparql> .' % (F, F, F, F, F, json.dumps(Q), F))
        ids = ' '.join('<urn:x:%s> <%spolicyClass> <urn:x:Policies> .' % (w, F) for w in ('rob', 'sam', 'eve'))
        s, r = update('INSERT DATA { %s %s <urn:x:item> <urn:p:note> "item" . }' % (pol, ids))
        assert s == 200, r
        end = time.time() + 60
        while time.time() < end:   # wait until t=1 is indexed
            s, r = request('GET', '/v1/fluree/info/' + L)
            if s == 200 and r['ledger']['index-t'] >= 1:
                break
            time.sleep(0.1)
        offset = os.path.getsize(log_path)
        fill = ' '.join('<urn:f:%d> <urn:p:f> %d .' % (i, i) for i in range(200000))
        s, r = update('INSERT DATA { GRAPH <urn:g:z> { <urn:s:1> <urn:p:m> "z-1" } '
                      'GRAPH <urn:g:a> { <urn:s:2> <urn:p:m> "a-1" } %s }' % fill)
        assert s == 200, r
        assert wait_log(log_path, offset, r'Starting index build.*ledger_id=%s current_index_t=1 ' % L, 60), 'no build'
        s, r = update('INSERT DATA { GRAPH <urn:g:a> { <urn:x:rob> <urn:p:grant> true } '
                      'GRAPH <urn:g:z> { <urn:x:sam> <urn:p:grant> true } }')
        assert s == 200, r
        for n in range(4, 7):
            assert update('INSERT DATA { GRAPH <urn:g:z> { <urn:s:1> <urn:p:m> "z-%d" } '
                          'GRAPH <urn:g:a> { <urn:s:2> <urn:p:m> "a-%d" } }' % (n, n))[0] == 200
        line = wait_log(log_path, offset, r'index install ledger="%s" index_t=2 .*novelty_flakes_after=[1-9]' % L, 60)
        assert line, 'index t=2 was not installed with unindexed commits'
        opened = stamp(line)
        window = decisions()
        nxt = wait_log(log_path, offset, r'index install ledger="%s" index_t=[3-9]' % L, 60)
        closed = stamp(nxt) if nxt else float('inf')
        while True:
            s, r = request('GET', '/v1/fluree/info/' + L)
            if s == 200 and r['ledger']['index-t'] >= r['ledger']['commit-t']:
                break
            time.sleep(0.1)
        settled = decisions()
        wrong = 0
        print('window: index t=2 installed with later commits unindexed; next install %.0f ms later'
              % ((closed - opened) * 1000))
        for (name, ok, detail, t0, t1), (_, ok2, detail2, _, _) in zip(window, settled):
            inside = opened <= t0 and t1 <= closed
            bad = inside and not ok and ok2
            wrong += bad
            print('  %-20s in window: %-6s %-52s inside=%s | after settling: %-6s %s%s' % (
                name, 'right' if ok else 'WRONG', detail, inside, 'right' if ok2 else 'WRONG', detail2,
                '   <- wrong decision' if bad else ''))
        print('RESULT wrong in-window decisions with a right control: %d' % wrong)
        status = 1 if wrong else 0
    except AssertionError as e:
        print('setup error:', e)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(work, ignore_errors=True)
    sys.exit(status)


if __name__ == '__main__':
    main()
