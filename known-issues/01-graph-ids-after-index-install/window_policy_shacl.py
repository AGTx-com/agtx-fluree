#!/usr/bin/env python3
"""Do policy checks and SHACL validation read the wrong named graph during the graph-ID window? (Fluree 4.2.9)

    python3 -I window_policy_shacl.py <fluree-binary> <port> <case> [--runs N] [--scale X] [--out PREFIX]

Standard library only; self-contained (copies what it needs from scope_graph_id_swap.py). One file-storage server
per invocation, with HOME, XDG_CONFIG_HOME and TMPDIR pointed at a fresh temporary directory that is removed at the
end. Plain `urn:` names. The identity is sent with the `fluree-identity` header on an unauthenticated request (the
documented path for an anonymous private server); every case carries a request that must be refused or filtered on
that path, so enforcement is shown, not assumed.

Each run uses its own ledger:
  t=1  setup, default graph (and config graph) only: policies and identities, or SHACL config and shape. The script
       waits for this commit to be indexed, so the later index build is the only one in flight.
  t=2  creates <urn:g:z> and <urn:g:a> with one marker each (z's subject sorts first, so the indexer numbers z
       before a, while the live registry numbers a before z), plus filler in the default graph so its build is slow.
  t=3..7  small commits adding a marker to both graphs while t=2 is being indexed; t=3 also carries the case's
       decisive fact ("tail fact"). t=8 is a filler commit, so the next build is not instant.
The moment the server's debug log shows "index install ... index_t=2 ... novelty_flakes_after=N>0" (the window
opens: commits 3..8 are unindexed and still filed under the old graph numbers) the requests below are sent in
parallel. A request counts as inside the window only if it started after that log line and finished before the next
install line for the ledger. An admin marker read of <urn:g:z> and an admin read of the decisive fact are sent with
them, to show that the swap is live for that ledger at that moment.

Controls, both run every time: (1) the same requests on the same ledger after indexing has settled; (2) the same
setup and commits on a fresh ledger with no race (everything indexed before the requests). After all runs the server
is restarted and stored states (and, for SHACL, GET /validate) are read again.

Cases:
  policy-allow   P1a. Policy (ledger-wide, default graph, f:view + f:modify):
                 ASK { GRAPH <urn:g:z> { $identity <urn:p:grant> true } }. Tail fact: the grant-shaped fact in
                 <urn:g:a>. Rob must be refused (write) and filtered (read).
  policy-refuse  P1b. Same policy. Tail fact: Rob's grant in <urn:g:z>. Rob must be allowed (write and read).
                 Both policy cases also send Eve (same policy class, no grant anywhere): always refused/filtered.
  policy-filter  P2. Rob's view policies: <urn:p:title> only when GRAPH <urn:g:z> { $this <urn:p:public> true };
                 <urn:p:m> always (f:allow true); nothing else (fluree-default-allow: false). doc-0 is public in z
                 from t=2 (indexed); tail: doc-2 public in z, doc-1 public in a, and a <urn:p:secret> fact in each
                 named graph. Rob reads all titles, and all of <urn:g:z> and <urn:g:a>.
  shacl-accept   S1a. SHACL on (reject), shape sh:targetNode <urn:x:k>, sh:path <urn:p:one>, sh:maxCount 1.
                 Tail fact: <urn:x:k> <urn:p:one> "first" in z. Window write: a second value in z; must be rejected.
  shacl-reject   S1b. Same shape. Tail fact: <urn:x:k> <urn:p:one> "in-a" in a. Window write: one value in z; must
                 be accepted.
  shacl-graph-setting  S2. SHACL on (reject, OverrideAll) with f:graphOverrides turning it off for <urn:g:a>; shape
                 maxCount 1 on <urn:x:k1> and <urn:x:k2>. Window writes: two values of k1 into z (docs: rejected),
                 two values of k2 into a (docs: accepted).

Exit status: 1 if any in-window decision was wrong while its controls were right; 0 otherwise; 2 on setup error.
"""
import argparse
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
import urllib.parse

Z, A = 'urn:g:z', 'urn:g:a'
RUST_LOG = 'warn,fluree::write_path=debug,fluree_db_indexer::orchestrator=info'
FSPARQL = 'https://ns.flur.ee/db#sparql'
LONG_FLAGS = ['--reindex-min-bytes=100000000', '--indexer-catchup-interval-secs=2']


# ---------------------------------------------------------------------------------------------------------
class Client:
    def __init__(self, port):
        self.port, self.conn = port, None

    def request(self, method, path, body=None, ctype='application/json', headers=None, timeout=300):
        data = None if body is None else (body if isinstance(body, str) else json.dumps(body)).encode()
        h = {'Content-Type': ctype, 'Accept': 'application/json'}
        h.update(headers or {})
        for attempt in range(4):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)
                self.conn.request(method, path, body=data, headers=h)
                response = self.conn.getresponse()
                text = response.read().decode()
                try:
                    return response.status, json.loads(text)
                except ValueError:
                    return response.status, text
            except (http.client.HTTPException, OSError):
                if self.conn is not None:
                    self.conn.close()
                self.conn = None
                if attempt == 3:
                    raise
                time.sleep(0.05)


class Server:
    def __init__(self, binary, port, work, extra=()):
        self.binary, self.port, self.work, self.extra = binary, port, work, list(extra)
        self.storage = os.path.join(work, 'store')
        self.proc, self.runs, self.logs = None, 0, []

    def start(self):
        self.runs += 1
        env = dict(os.environ, HOME=self.work, XDG_CONFIG_HOME=self.work, TMPDIR=self.work, RUST_LOG=RUST_LOG,
                   NO_COLOR='1')
        self.log_path = os.path.join(self.work, 'server-%d.log' % self.runs)
        self.logs.append(self.log_path)
        self.log = open(self.log_path, 'w')
        if not os.path.isdir(os.path.join(self.work, '.fluree')):
            r = subprocess.run([self.binary, '--quiet', '--no-color', 'init'], cwd=self.work, env=env,
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError('fluree init failed: %s %s' % (r.stdout, r.stderr))
        self.command = ['server', 'run', '--listen-addr', '127.0.0.1:%d' % self.port, '--',
                        '--storage-path=' + self.storage] + self.extra
        self.proc = subprocess.Popen([self.binary] + self.command, cwd=self.work, env=env, stdout=self.log,
                                     stderr=subprocess.STDOUT)
        for _ in range(1500):
            try:
                c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=1)
                c.request('GET', '/health')
                c.getresponse().read()
                return self
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.02)
        raise RuntimeError('server did not start: ' + open(self.log_path).read()[-2000:])

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=20)
        if self.proc:
            self.log.close()
        self.proc = None

    def log_text(self):
        return open(self.log_path).read()

    def waiter(self):
        """A log tail that starts at the current end of the log (create it before the event it waits for)."""
        return LogWaiter(self.log_path)


class LogWaiter:
    def __init__(self, path):
        self.path = path
        self.offset = os.path.getsize(path)

    def wait_for(self, pattern, timeout=60):
        end = time.time() + timeout
        with open(self.path, 'rb') as f:
            f.seek(self.offset)
            while time.time() < end:
                pos = f.tell()
                raw = f.readline()
                if not raw.endswith(b'\n'):
                    f.seek(pos)
                    time.sleep(0.0005)
                    continue
                self.offset = f.tell()
                line = raw.decode(errors='replace')
                if pattern.search(line):
                    return line
        return None


def ts(line):
    m = re.match(r'(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d\.\d+)Z', line)
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    return calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), 0)) + float(s)


def installs(log_text, ledger):
    out = []
    pat = re.compile(r'index install ledger="%s" index_t=(\d+) novelty_flakes_before=\d+ novelty_flakes_after=(\d+)'
                     % re.escape(ledger))
    for line in log_text.splitlines():
        m = pat.search(line)
        if m:
            out.append((ts(line), int(m.group(1)), int(m.group(2))))
    return out


# ---------------------------------------------------------------------------------------------------------
def policy_ttl(name, rules, identities):
    """INSERT DATA text for policies [(iri, class, actions, onProperty|None, query|None, allow|None)]."""
    parts = []
    for iri, cls, actions, prop, query, allow in rules:
        s = '<%s> a <https://ns.flur.ee/db#AccessPolicy>, <%s> ; <https://ns.flur.ee/db#action> %s' % (
            iri, cls, ', '.join('<https://ns.flur.ee/db#%s>' % a for a in actions))
        if prop:
            s += ' ; <https://ns.flur.ee/db#onProperty> <%s>' % prop
        if query:
            s += ' ; <https://ns.flur.ee/db#query> %s^^<%s>' % (json.dumps(query), FSPARQL)
        if allow is not None:
            s += ' ; <https://ns.flur.ee/db#allow> %s' % ('true' if allow else 'false')
        parts.append(s + ' .')
    for who, cls in identities:
        parts.append('<%s> <https://ns.flur.ee/db#policyClass> <%s> .' % (who, cls))
    return ' '.join(parts)


def shacl_setup(ledger_name, override_a=False, nodes=('urn:x:k',)):
    cfg = '[ <https://ns.flur.ee/db#shaclEnabled> true ; <https://ns.flur.ee/db#validationMode> ' \
          '<https://ns.flur.ee/db#ValidationReject>%s ]' % (
              ' ; <https://ns.flur.ee/db#overrideControl> <https://ns.flur.ee/db#OverrideAll>' if override_a else '')
    over = ''
    if override_a:
        over = (' ; <https://ns.flur.ee/db#graphOverrides> [ a <https://ns.flur.ee/db#GraphConfig> ; '
                '<https://ns.flur.ee/db#targetGraph> <%s> ; <https://ns.flur.ee/db#shaclDefaults> '
                '[ <https://ns.flur.ee/db#shaclEnabled> false ] ]' % A)
    shapes = []
    for i, n in enumerate(nodes):
        shapes.append('<urn:x:shape%d> a <http://www.w3.org/ns/shacl#NodeShape> ; '
                      '<http://www.w3.org/ns/shacl#targetNode> <%s> ; <http://www.w3.org/ns/shacl#property> '
                      '<urn:x:ps%d> . <urn:x:ps%d> <http://www.w3.org/ns/shacl#path> <urn:p:one> ; '
                      '<http://www.w3.org/ns/shacl#maxCount> 1 .' % (i, n, i, i))
    return ('INSERT DATA { GRAPH <urn:fluree:%s#config> { <urn:x:cfg> a <https://ns.flur.ee/db#LedgerConfig> ; '
            '<https://ns.flur.ee/db#shaclDefaults> %s%s . } %s }' % (ledger_name, cfg, over, ' '.join(shapes)))


GRANT_Q = 'ASK { GRAPH <urn:g:z> { $identity <urn:p:grant> true } }'
PUBLIC_Q = 'ASK { GRAPH <urn:g:z> { $this <urn:p:public> true } }'


def case_def(case, ledger_name):
    """setup INSERT DATA, extra triples for t=2, tail fact for t=3, and request builders."""
    if case in ('policy-allow', 'policy-refuse'):
        setup = 'INSERT DATA { %s <urn:x:item> <urn:p:note> "item" . }' % policy_ttl(
            ledger_name, [('urn:x:rule', 'urn:x:Policies', ('view', 'modify'), None, GRANT_Q, None)],
            [('urn:x:rob', 'urn:x:Policies'), ('urn:x:eve', 'urn:x:Policies')])
        tail = 'GRAPH <%s> { <urn:x:rob> <urn:p:grant> true }' % (A if case == 'policy-allow' else Z)
        return {'setup': setup, 't2_extra': '', 'tail': tail}
    if case == 'policy-filter':
        setup = 'INSERT DATA { %s %s }' % (policy_ttl(ledger_name, [
            ('urn:x:vtitle', 'urn:x:Readers', ('view',), 'urn:p:title', PUBLIC_Q, None),
            ('urn:x:vm', 'urn:x:Readers', ('view',), 'urn:p:m', None, True)],
            [('urn:x:rob', 'urn:x:Readers')]),
            ' '.join('<urn:x:doc-%d> <urn:p:title> "title-%d" .' % (i, i) for i in range(3)))
        t2 = 'GRAPH <%s> { <urn:x:doc-0> <urn:p:public> true }' % Z
        tail = ('GRAPH <%s> { <urn:x:doc-2> <urn:p:public> true . <urn:x:sz> <urn:p:secret> "z-secret" } '
                'GRAPH <%s> { <urn:x:doc-1> <urn:p:public> true . <urn:x:sa> <urn:p:secret> "a-secret" }' % (Z, A))
        return {'setup': setup, 't2_extra': t2, 'tail': tail}
    if case in ('shacl-accept', 'shacl-reject'):
        tail = ('GRAPH <%s> { <urn:x:k> <urn:p:one> "first" }' % Z if case == 'shacl-accept'
                else 'GRAPH <%s> { <urn:x:k> <urn:p:one> "in-a" }' % A)
        return {'setup': shacl_setup(ledger_name), 't2_extra': '', 'tail': tail}
    if case == 'shacl-graph-setting':
        return {'setup': shacl_setup(ledger_name, override_a=True, nodes=('urn:x:k1', 'urn:x:k2', 'urn:x:k3', 'urn:x:k4')),
                't2_extra': '', 'tail': ''}
    raise SystemExit('unknown case ' + case)


# ---------------------------------------------------------------------------------------------------------
class Ledger:
    def __init__(self, port, name):
        self.port, self.name = port, name
        self.c = Client(port)
        self.lock = threading.Lock()
        self.n = 0
        self.sent = {Z: set(), A: set()}
        self.acked = {Z: set(), A: set()}
        self.last_t = 0

    def create(self):
        s, r = self.c.request('POST', '/v1/fluree/create', {'ledger': self.name})
        if s not in (200, 201):
            raise RuntimeError('create %s: %s %s' % (self.name, s, r))
        return self

    def update(self, sparql, client=None, headers=None, must=True):
        s, r = (client or self.c).request('POST', '/v1/fluree/update?ledger=' + urllib.parse.quote(self.name), sparql,
                                          'application/sparql-update', headers)
        if s == 200 and isinstance(r, dict):
            with self.lock:
                self.last_t = max(self.last_t, r['t'])
        elif must:
            raise RuntimeError('update %s: %s %s' % (self.name, s, str(r)[:400]))
        return s, r

    def turtle(self, body):
        s, r = self.c.request('POST', '/v1/fluree/insert?ledger=' + urllib.parse.quote(self.name), body, 'text/turtle')
        if s != 200:
            raise RuntimeError('turtle %s: %s %s' % (self.name, s, str(r)[:300]))
        with self.lock:
            self.last_t = max(self.last_t, r['t'])
        return r['t']

    def markers(self, extra='', filler=0):
        with self.lock:
            self.n += 1
            n = self.n
            self.sent[Z].add('z-%d' % n)
            self.sent[A].add('a-%d' % n)
        fill = ' '.join('<urn:f:%d-%d> <urn:p:f> %d .' % (n, i, i) for i in range(filler))
        _, r = self.update('INSERT DATA { GRAPH <%s> { <urn:s:1-%d> <urn:p:m> "z-%d" } GRAPH <%s> { <urn:s:2-%d> '
                           '<urn:p:m> "a-%d" } %s %s }' % (Z, n, n, A, n, n, extra, fill))
        with self.lock:
            self.acked[Z].add('z-%d' % n)
            self.acked[A].add('a-%d' % n)
        return r['t']

    def info(self):
        s, r = self.c.request('GET', '/v1/fluree/info/' + urllib.parse.quote(self.name))
        if s != 200:
            return None
        led = r['ledger']
        return led['index-t'], led['commit-t'], {x['iri']: x['g-id'] for x in led['named-graphs'] if x['g-id'] >= 3}

    def settle(self, timeout=180):
        end = time.time() + timeout
        while time.time() < end:
            state = self.info()
            if state and state[0] >= self.last_t:
                return state
            time.sleep(0.05)
        return self.info()


def rows(status, r):
    if status != 200 or not isinstance(r, dict):
        return 'HTTP %s %s' % (status, ' '.join(str(r).split())[:200])
    if 'boolean' in r:
        return r['boolean']
    # One string per row: the bound values in alphabetical order of the variable names (?o before ?p before ?s).
    return sorted(' '.join(b[v]['value'] for v in sorted(r['head']['vars']) if v in b) for b in r['results']['bindings'])


# ---------------------------------------------------------------------------------------------------------
# Requests. Each is (name, kind, fn(client) -> (status, body), expected, role); role is 'decision', 'enforcement'
# (a must-refuse/must-filter control on the identity path) or 'probe' (admin read showing the window).
def query(ledger, sparql, identity=None, deny_default=False):
    headers = {}
    if identity:
        headers['fluree-identity'] = identity
    if deny_default:
        headers['fluree-default-allow'] = 'false'

    def fn(client):
        return client.request('POST', '/v1/fluree/query/' + urllib.parse.quote(ledger.name), sparql,
                              'application/sparql-query', headers)
    return fn


def write(ledger, sparql, identity=None):
    headers = {'fluree-identity': identity} if identity else None

    def fn(client):
        return ledger.update(sparql, client, headers, must=False)
    return fn


def marker_probe(ledger):
    with ledger.lock:
        must = set(ledger.acked[Z])
    return ('admin marker read of z', 'markers', query(ledger, 'SELECT ?o WHERE { GRAPH <%s> { ?s <urn:p:m> ?o } }' % Z),
            must, 'probe')


def requests_for(case, ledger, phase):
    tag = '%s-%s' % (ledger.name.split(':')[0], phase.replace(' ', '-'))
    out = [marker_probe(ledger)]
    if case in ('policy-allow', 'policy-refuse'):
        allowed = case == 'policy-refuse'
        out += [
            ('admin ASK of the condition', 'ask', query(ledger, GRANT_Q.replace('$identity', '<urn:x:rob>')), allowed, 'probe'),
            ('rob write (f:modify)', 'write', write(ledger, 'INSERT DATA { <urn:x:new-rob-%s> <urn:p:note> "by rob" }' % tag,
                                                   'urn:x:rob'), allowed, 'decision'),
            ('rob read (f:view)', 'rows', query(ledger, 'SELECT ?o WHERE { <urn:x:item> <urn:p:note> ?o }', 'urn:x:rob'),
             ['item'] if allowed else [], 'decision'),
            ('eve write (no grant)', 'write', write(ledger, 'INSERT DATA { <urn:x:new-eve-%s> <urn:p:note> "by eve" }' % tag,
                                                   'urn:x:eve'), False, 'enforcement'),
            ('eve read (no grant)', 'rows', query(ledger, 'SELECT ?o WHERE { <urn:x:item> <urn:p:note> ?o }', 'urn:x:eve'),
             [], 'enforcement'),
        ]
    elif case == 'policy-filter':
        with ledger.lock:
            zm, am = set(ledger.acked[Z]), set(ledger.acked[A])
        out += [
            ('admin read of z public marks', 'rows', query(ledger, 'SELECT ?s WHERE { GRAPH <%s> { ?s <urn:p:public> true } }' % Z),
             ['urn:x:doc-0', 'urn:x:doc-2'], 'probe'),
            ('rob read of titles (default graph)', 'rows', query(ledger, 'SELECT ?s ?o WHERE { ?s <urn:p:title> ?o }',
                                                                'urn:x:rob', True),
             ['title-0 urn:x:doc-0', 'title-2 urn:x:doc-2'], 'decision'),
            ('rob read of all of z', 'graphfacts', query(ledger, 'SELECT ?p ?o WHERE { GRAPH <%s> { ?s ?p ?o } }' % Z,
                                                         'urn:x:rob', True), ('z', zm), 'decision'),
            ('rob read of all of a', 'graphfacts', query(ledger, 'SELECT ?p ?o WHERE { GRAPH <%s> { ?s ?p ?o } }' % A,
                                                         'urn:x:rob', True), ('a', am), 'decision'),
            ('rob read of secrets (enforcement)', 'rows', query(ledger, 'SELECT ?o WHERE { GRAPH ?g { ?s <urn:p:secret> ?o } }',
                                                               'urn:x:rob', True), [], 'enforcement'),
        ]
    elif case in ('shacl-accept', 'shacl-reject'):
        existing = ['first'] if case == 'shacl-accept' else []
        expected = case == 'shacl-reject'
        if phase == 'after settle':
            # Right iff the write keeps k at <= 1 value in z, judged from the settled state read now.
            now = rows(*ledger.c.request('POST', '/v1/fluree/query/' + urllib.parse.quote(ledger.name),
                                         'SELECT ?o WHERE { GRAPH <%s> { <urn:x:k> <urn:p:one> ?o } }' % Z,
                                         'application/sparql-query'))
            existing, expected = now, (isinstance(now, list) and len(now) == 0)
        value = {'in window': 'second', 'after settle': 'third', 'no race': 'second'}[phase] \
            if case == 'shacl-accept' else {'in window': 'z-only', 'after settle': 'z-only-2', 'no race': 'z-only'}[phase]
        out += [
            ('admin read of k in z', 'rows', query(ledger, 'SELECT ?o WHERE { GRAPH <%s> { <urn:x:k> <urn:p:one> ?o } }' % Z),
             existing, 'probe'),
            ('write one more value of k into z', 'write',
             write(ledger, 'INSERT DATA { GRAPH <%s> { <urn:x:k> <urn:p:one> "%s" } }' % (Z, value)),
             expected, 'decision'),
        ]
    elif case == 'shacl-graph-setting':
        kz, ka = ('urn:x:k1', 'urn:x:k2') if phase != 'after settle' else ('urn:x:k3', 'urn:x:k4')
        out += [
            ('two values of %s into z (SHACL on)' % kz, 'write',
             write(ledger, 'INSERT DATA { GRAPH <%s> { <%s> <urn:p:one> "v1", "v2" } }' % (Z, kz)), False, 'decision'),
            ('two values of %s into a (override: off)' % ka, 'write',
             write(ledger, 'INSERT DATA { GRAPH <%s> { <%s> <urn:p:one> "v1", "v2" } }' % (A, ka)), True, 'decision'),
        ]
    return out


def judge(kind, expected, status, body):
    """(decision value, right?)"""
    if kind == 'write':
        if status == 200:
            got = True
        elif status in (400, 403, 422):
            got = False
        else:
            return 'HTTP %s' % status, False
        return got, (expected is None or got == expected)
    got = rows(status, body)
    if kind == 'ask':
        return got, got == expected
    if kind == 'rows':
        return got, got == expected
    if kind == 'markers':
        if isinstance(got, str):
            return got, False
        return got, expected <= set(got) and all(x.startswith('z-') for x in got)
    if kind == 'graphfacts':
        tag, must = expected
        if isinstance(got, str):
            return got, False
        ok = all(x.startswith('%s-' % tag) and x.endswith(' urn:p:m') for x in got) and set(m + ' urn:p:m' for m in must) <= set(got)
        return got, ok
    raise ValueError(kind)


def fire(reqs, port):
    """Send the probes in parallel, then the decision and enforcement requests in parallel (so that a probe never
    races a write it reads); [(name, kind, expected, role, start, end, status, body)]."""
    probes = [r for r in reqs if r[4] == 'probe']
    rest = [r for r in reqs if r[4] != 'probe']
    return fire_parallel(probes, port) + fire_parallel(rest, port)


def fire_parallel(reqs, port):
    results = [None] * len(reqs)

    def one(i, req):
        name, kind, fn, expected, role = req
        client = Client(port)
        t0 = time.time()
        try:
            status, body = fn(client)
        except Exception as e:  # noqa: BLE001
            status, body = 0, 'exception %s' % e
        results[i] = (name, kind, expected, role, t0, time.time(), status, body)
    threads = [threading.Thread(target=one, args=(i, r)) for i, r in enumerate(reqs)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(120)
    return results


def record(results, window=None, base=0.0):
    out = []
    for name, kind, expected, role, t0, t1, status, body in results:
        got, ok = judge(kind, expected, status, body)
        item = {'request': name, 'role': role, 'right': ok, 'got': got if not isinstance(got, set) else sorted(got),
                'expected': (sorted(expected) if isinstance(expected, set) else
                             [expected[0], sorted(expected[1])] if isinstance(expected, tuple) else expected),
                'status': status, 'start_ms': round((t0 - base) * 1000, 1), 'end_ms': round((t1 - base) * 1000, 1)}
        if status != 200:
            item['message'] = ' '.join(str(body).split())[:220]
        if window:
            item['inside_window'] = window[0] <= t0 and t1 <= window[1]
        out.append(item)
    return out


# ---------------------------------------------------------------------------------------------------------
def build(ledger, case, scale, race, server, long_window=False):
    d = case_def(case, ledger.name)
    ledger.update(d['setup'])
    ledger.settle()
    f1, f2 = int(200000 * scale), int(60000 * scale)
    if not race:
        ledger.markers(d['t2_extra'])
        ledger.markers(d['tail'])
        for _ in range(4):
            ledger.markers()
        ledger.settle()
        return None
    pat = re.compile(r'index install ledger="%s" index_t=2 novelty_flakes_before=\d+ novelty_flakes_after=([1-9]\d*)'
                     % re.escape(ledger.name))
    holder = {}
    trigger = server.waiter()

    def watcher():
        line = trigger.wait_for(pat, timeout=120)
        holder['line'] = line
        holder['seen'] = time.time()
        if line:
            holder['results'] = fire(requests_for(case, ledger, 'in window'), ledger.port)
    th = threading.Thread(target=watcher, daemon=True)
    th.start()
    started = server.waiter()
    holder['t0'] = time.time()
    t2 = ledger.markers(d['t2_extra'], filler=f1)
    if long_window:
        # The sweep starts the build of t=2; the tail commits must land while it runs.
        line = started.wait_for(re.compile(r'Starting index build.*ledger_id=%s current_index_t=1 ' % re.escape(ledger.name)),
                                timeout=60)
        holder['build_started'] = line is not None
    else:
        time.sleep(0.02)
    holder['tail_t'] = ledger.markers(d['tail'])
    for _ in range(4):
        ledger.markers()
    if not long_window:
        ledger.turtle('\n'.join('<urn:f:x-%d> <urn:p:f> %d .' % (i, i) for i in range(f2)))
    th.join(150)
    holder['t2'] = t2
    return holder


def run_case(args, out):
    work = tempfile.mkdtemp(prefix='fluree-wps-')
    server = Server(args.binary, args.port, work, LONG_FLAGS if args.long_window else ())
    wrong_in_window, controls_wrong, missing = 0, 0, 0
    ledgers = []
    try:
        server.start()
        out['server'] = server.command
        for k in range(args.runs):
            run = {'run': k + 1}
            ledger = Ledger(args.port, '%s-%d:main' % (args.case, k + 1)).create()
            h = build(ledger, args.case, args.scale, True, server, args.long_window)
            ledger.settle()
            ins = installs(server.log_text(), ledger.name)
            base = h['t0']
            win = None
            for i, (t_, it, left) in enumerate(ins):
                if it == 2 and left > 0:
                    win = (t_, ins[i + 1][0] if i + 1 < len(ins) else float('inf'))
                    break
            run['installs_ms'] = [(round((x - base) * 1000, 1), it, left) for x, it, left in ins]
            run['tail_fact_commit_t'] = h['tail_t']
            run['window_ms'] = None if not win else [round((win[0] - base) * 1000, 1), round((win[1] - base) * 1000, 1)]
            run['trigger_seen_ms'] = round((h['seen'] - base) * 1000, 1) if h.get('line') else None
            run['in_window'] = record(h.get('results') or [], win, base)
            time.sleep(0.2)
            ledger.settle()
            run['after_settle'] = record(fire(requests_for(args.case, ledger, 'after settle'), args.port), None, base)
            # S1 after-settle: the write is right iff it keeps k at <= 1 value in z (computed from the state now).
            ctl = Ledger(args.port, '%s-%d-norace:main' % (args.case, k + 1)).create()
            build(ctl, args.case, args.scale, False, server)
            run['no_race'] = record(fire(requests_for(args.case, ctl, 'no race'), args.port), None, time.time())
            run['final_info'] = ledger.info()
            out['runs'].append(run)
            ledgers.append((ledger, ctl))
            print_run(args.case, run)
        for phase in ('settled', 'after restart'):
            if phase == 'after restart':
                server.stop()
                server.start()
            for ledger, ctl in ledgers:
                for led in (ledger, ctl):
                    led.c = Client(args.port)
                    led.settle()
                    st = stored_state(args.case, led)
                    out.setdefault('stored', {}).setdefault(led.name, {})[phase] = st
        for ledger, ctl in ledgers:
            for led in (ledger, ctl):
                print('  stored %-34s settled %s' % (led.name, json.dumps(out['stored'][led.name]['settled'])[:600]))
                print('  stored %-34s restart %s' % ('', json.dumps(out['stored'][led.name]['after restart'])[:600]))
        for run in out['runs']:
            ctl_ok = all(x['right'] for x in run['after_settle'] + run['no_race'] if x['role'] != 'probe')
            controls_wrong += not ctl_ok
            for x in run['in_window']:
                if not x.get('inside_window'):
                    if x['role'] != 'probe':
                        missing += 1
                    continue
                if x['role'] in ('decision', 'enforcement') and not x['right'] and ctl_ok:
                    wrong_in_window += 1
        out['summary'] = summarize(out)
        print_summary(out)
    finally:
        server.stop()
        keep_logs(args, server)
        shutil.rmtree(work, ignore_errors=True)
    out['wrong_in_window'], out['runs_with_wrong_controls'], out['decisions_outside_window'] = \
        wrong_in_window, controls_wrong, missing
    return wrong_in_window


def stored_state(case, led):
    c = led.c
    q = lambda s: rows(*c.request('POST', '/v1/fluree/query/' + urllib.parse.quote(led.name), s, 'application/sparql-query'))
    if case.startswith('policy-allow') or case == 'policy-refuse':
        return {'notes written by rob/eve': q('SELECT ?s WHERE { ?s <urn:p:note> ?o FILTER(?s != <urn:x:item>) }')}
    if case == 'policy-filter':
        return {'z public': q('SELECT ?s WHERE { GRAPH <%s> { ?s <urn:p:public> true } }' % Z)}
    st = {'k values in z': q('SELECT ?s ?o WHERE { GRAPH <%s> { ?s <urn:p:one> ?o } }' % Z),
          'k values in a': q('SELECT ?s ?o WHERE { GRAPH <%s> { ?s <urn:p:one> ?o } }' % A)}
    for g in (Z, A):
        s, r = c.request('POST', '/v1/fluree/validate/' + urllib.parse.quote(led.name), {'graph': g})
        if s == 200 and isinstance(r, dict):
            st['validate ' + g] = {'conforms': r.get('conforms'), 'violations': r.get('violations'), 't': r.get('t'),
                                   'focus': sorted(set(x.get('focus_node') for x in r.get('results', [])))}
        else:
            st['validate ' + g] = 'HTTP %s %s' % (s, str(r)[:200])
    return st


def summarize(out):
    s = {}
    for run in out['runs']:
        for phase in ('in_window', 'after_settle', 'no_race'):
            for x in run[phase]:
                key = '%s | %s' % (x['role'], x['request'])
                d = s.setdefault(key, {})
                p = d.setdefault(phase, {'n': 0, 'right': 0, 'outside_window': 0})
                if phase == 'in_window' and not x.get('inside_window'):
                    p['outside_window'] += 1
                    continue
                p['n'] += 1
                p['right'] += x['right']
    return s


def print_run(case, run):
    print('  run %d: installs (ms, index_t, unindexed flakes left) %s; window %s ms; trigger seen at %s ms; tail fact t=%s'
          % (run['run'], run['installs_ms'], run['window_ms'], run['trigger_seen_ms'], run['tail_fact_commit_t']))
    for phase in ('in_window', 'after_settle', 'no_race'):
        for x in run[phase]:
            print('    %-12s %-11s %-42s %-5s got=%s expected=%s %s%s' % (
                phase, x['role'], x['request'], 'RIGHT' if x['right'] else 'WRONG', json.dumps(x['got'])[:160],
                json.dumps(x['expected'])[:120], ('[%.1f..%.1f ms, inside=%s]' % (x['start_ms'], x['end_ms'], x['inside_window']))
                if phase == 'in_window' else '[took %.1f ms]' % (x['end_ms'] - x['start_ms']), (' msg=' + x['message'][:120]) if x.get('message') and not x['right'] else ''))


def print_summary(out):
    print('SUMMARY (right / counted; in-window counts only requests that started and finished inside the window)')
    for key, d in out['summary'].items():
        print('  %-58s %s' % (key, '  '.join('%s %d/%d%s' % (ph, v['right'], v['n'],
              (' (+%d outside)' % v['outside_window']) if v['outside_window'] else '') for ph, v in d.items())))


def keep_logs(args, server):
    """Keep server logs without the per-read 'store load phases' debug lines; say how many were removed."""
    if not args.out:
        return
    for i, path in enumerate(server.logs):
        if not os.path.exists(path):
            continue
        kept, removed = [], 0
        for line in open(path):
            if 'store load phases' in line:
                removed += 1
            else:
                kept.append(line)
        with open('%s.server-%d.log' % (args.out, i + 1), 'w') as f:
            f.write("# %d 'store load phases' debug lines (one per read) removed to save space\n" % removed)
            f.writelines(kept)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('binary')
    p.add_argument('port', type=int)
    p.add_argument('case', choices=['policy-allow', 'policy-refuse', 'policy-filter', 'shacl-accept', 'shacl-reject',
                                    'shacl-graph-setting'])
    p.add_argument('--runs', type=int, default=3)
    p.add_argument('--scale', type=float, default=2.0)
    p.add_argument('--long-window', action='store_true', help='server flags %s: the t=2 build is started by the catch-up sweep and the window lasts until the next sweep (seconds), with no tail filler' % ' '.join(LONG_FLAGS))
    p.add_argument('--out', help='write PREFIX.json and keep server logs as PREFIX.server-N.log')
    args = p.parse_args()
    version = subprocess.run([args.binary, '--version'], capture_output=True, text=True).stdout.strip()
    out = {'version': version, 'case': args.case, 'scale': args.scale, 'long_window': args.long_window, 'runs': [],
           'date': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    print('%s | case %s | scale %s | runs %d | long window %s' % (version, args.case, args.scale, args.runs, args.long_window))
    try:
        wrong = run_case(args, out)
    except Exception as failure:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        out['error'] = str(failure)
        wrong = None
    if args.out:
        with open(args.out + '.json', 'w') as f:
            json.dump(out, f, indent=1, default=str)
    print('RESULT case=%s wrong_in_window=%s runs_with_wrong_controls=%s decisions_outside_window=%s' % (
        args.case, wrong, out.get('runs_with_wrong_controls'), out.get('decisions_outside_window')))
    sys.exit(2 if wrong is None else (1 if wrong else 0))


if __name__ == '__main__':
    main()
