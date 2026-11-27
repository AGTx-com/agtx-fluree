#!/usr/bin/env python3
"""Scope tests for the Fluree 4.2.9 graph-ID swap (see repro_graph_id_swap.py for the minimal repro).

    python3 -I scope_graph_id_swap.py <fluree-binary> <port> <case> [--out PREFIX] [--scale X]

Cases:
  reads              One commit creates <urn:g:z> and <urn:g:a> (z's subject sorts first); small commits and a
                     filler commit follow while its index builds. Readers cover: FROM <ledger#g>, GRAPH <g>,
                     GRAPH ?g enumeration, time-pinned FROM <ledger@t:K#g> (K=1 indexed; K=3, K=6 unindexed),
                     the default graph, and a history range over the default graph.
  one-per-commit     The first commit has no named graph; then one commit creates <urn:g:z>, the next creates
                     <urn:g:a>, all while the first commit's index builds.
  one-per-commit-alpha  Control: same, but <urn:g:a> is created before <urn:g:z> (alphabetical order).
  file-storage       The `reads` write shape on file storage (--storage-path) instead of --memory; plus a
                     restart after settling and a re-read.
  restart-unindexed  File storage. Run 1 has indexing off and leaves the graph-creating commit unindexed.
                     Run 2 (indexing on) loads the ledger while its catch-up build runs and commits more.
                     Run 3 restarts again and re-reads.
  restart-unindexed-off  Control: run 2 also has indexing off.
  long-window        --reindex-min-bytes=100000000 --indexer-catchup-interval-secs=2: the first build is
                     started by the periodic catch-up sweep; small commits land during it; readers run until
                     the next build closes the window.
  writes             File storage. Five dependent writes, each on its own ledger, each sent the moment the server
                     log shows that the first index was installed while later commits were unindexed:
                     DELETE ... WHERE over z, INSERT ... WHERE copying z, DELETE/INSERT ... WHERE editing z,
                     DELETE DATA of one z fact, and a TriG upsert of one z subject. The same writes on fresh
                     ledgers with no index race are the control. Committed flakes (GET /show) are recorded;
                     final graphs are read after settling and again after a server restart.

Exit status: 1 if any wrong read (or, for `writes`, any wrong final state) was seen, else 0; 2 on setup error.
"""
import argparse
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

Z, A, D = 'urn:g:z', 'urn:g:a', ''          # '' is the default graph
TAG = {Z: 'z', A: 'a', D: 'd'}
RANK = {Z: 1, A: 2}
RUST_LOG = 'warn,fluree::write_path=debug,fluree_db_indexer::orchestrator=info'
F = 'PREFIX f: <https://ns.flur.ee/db#> '


# ---------------------------------------------------------------------------------------------------------
class Client:
    def __init__(self, port):
        self.port, self.conn = port, None

    def request(self, method, path, body=None, ctype='application/json', timeout=300):
        data = None if body is None else (body if isinstance(body, str) else json.dumps(body)).encode()
        for attempt in range(4):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)
                self.conn.request(method, path, body=data, headers={'Content-Type': ctype, 'Accept': 'application/json'})
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
    def __init__(self, binary, port, work, storage=None, extra=()):
        self.binary, self.port, self.work, self.storage, self.extra = binary, port, work, storage, list(extra)
        self.proc, self.runs, self.logs = None, 0, []

    def start(self, extra=None):
        self.runs += 1
        env = dict(os.environ, HOME=self.work, XDG_CONFIG_HOME=self.work, TMPDIR=self.work, RUST_LOG=RUST_LOG,
                   NO_COLOR='1')
        self.log_path = os.path.join(self.work, 'server-%d.log' % self.runs)
        self.logs.append(self.log_path)
        self.log = open(self.log_path, 'w')
        if self.storage and not os.path.isdir(os.path.join(self.work, '.fluree')):
            r = subprocess.run([self.binary, '--quiet', '--no-color', 'init'], cwd=self.work, env=env,
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise RuntimeError('fluree init failed: %s %s' % (r.stdout, r.stderr))
        args = ['server', 'run'] + ([] if self.storage else ['--memory']) + ['--listen-addr', '127.0.0.1:%d' % self.port]
        tail = (['--storage-path=' + self.storage] if self.storage else []) + list(self.extra if extra is None else extra)
        if tail:
            args += ['--'] + tail
        self.command = args
        self.proc = subprocess.Popen([self.binary] + args, cwd=self.work, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(600):
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

    def log_text(self, run=None):
        path = self.logs[-1] if run is None else self.logs[run - 1]
        return open(path).read()


def ts(line):
    m = re.match(r'(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d\.\d+)Z', line)
    if not m:
        return None
    import calendar
    y, mo, d, h, mi, s = m.groups()
    return calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), 0)) + float(s)


def installs(log_text, ledger):
    """[(wall time, index_t, unindexed flakes left)] for one ledger, from the debug install lines."""
    out = []
    pat = re.compile(r'index install ledger="%s" index_t=(\d+) novelty_flakes_before=\d+ novelty_flakes_after=(\d+)'
                     % re.escape(ledger))
    for line in log_text.splitlines():
        m = pat.search(line)
        if m:
            out.append((ts(line), int(m.group(1)), int(m.group(2))))
    return out


# ---------------------------------------------------------------------------------------------------------
class Ledger:
    def __init__(self, port, name):
        self.port, self.name = port, name
        self.c = Client(port)
        self.lock = threading.Lock()
        self.n = 0
        self.sent = {}     # graph -> set(marker)
        self.acked = {}    # graph -> [(t, marker)]
        self.last_t = 0
        self.acked_t = 0   # t of the newest acknowledged marker commit

    def create(self):
        s, r = self.c.request('POST', '/v1/fluree/create', {'ledger': self.name})
        if s not in (200, 201):
            raise RuntimeError('create %s: %s %s' % (self.name, s, r))
        return self

    def update(self, sparql, client=None):
        s, r = (client or self.c).request('POST', '/v1/fluree/update?ledger=' + urllib.parse.quote(self.name), sparql,
                                          'application/sparql-update')
        if s != 200:
            raise RuntimeError('update %s: %s %s' % (self.name, s, str(r)[:400]))
        with self.lock:
            self.last_t = max(self.last_t, r['t'])
        return r['t']

    def send(self, path, body, ctype, client=None):
        s, r = (client or self.c).request('POST', '/v1/fluree/%s?ledger=%s' % (path, urllib.parse.quote(self.name)), body, ctype)
        if s != 200:
            raise RuntimeError('%s %s: %s %s' % (path, self.name, s, str(r)[:400]))
        with self.lock:
            self.last_t = max(self.last_t, r['t'])
        return r['t']

    def commit(self, graphs, filler=0):
        """One commit with a new marker in each graph of `graphs` (D = default graph), plus filler triples."""
        with self.lock:
            self.n += 1
            n = self.n
            for g in graphs:
                self.sent.setdefault(g, set()).add('%s-%d' % (TAG[g], n))
        parts = []
        for g in graphs:
            if g == D:
                parts.append('<urn:s:d> <urn:p:m> "d-%d" .' % n)
            else:
                parts.append('GRAPH <%s> { <urn:s:%d-%d> <urn:p:m> "%s-%d" }' % (g, RANK[g], n, TAG[g], n))
        fill = ' '.join('<urn:f:%d-%d> <urn:p:f> %d .' % (n, i, i) for i in range(filler))
        t = self.update('INSERT DATA { %s %s }' % (' '.join(parts), fill))
        with self.lock:
            for g in graphs:
                self.acked.setdefault(g, []).append((t, '%s-%d' % (TAG[g], n)))
            self.acked_t = max(self.acked_t, t)
        return t

    def filler(self, count):
        with self.lock:
            self.n += 1
            n = self.n
        body = '\n'.join('<urn:f:%d-%d> <urn:p:f> %d .' % (n, i, i) for i in range(count))
        t = self.send('insert', body, 'text/turtle')
        with self.lock:
            self.acked_t = max(self.acked_t, t)   # every earlier marker commit is acknowledged by now
        return t

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

    # -- reads: each returns a sorted list (or an error string) -------------------------------------------
    def q(self, client, path, sparql):
        s, r = client.request('POST', path, sparql, 'application/sparql-query')
        if s != 200 or not isinstance(r, dict):
            return 'HTTP %s %s' % (s, str(r)[:160])
        return r['results']['bindings']

    def read(self, client, spec):
        kind = spec[0]
        L = self.name
        if kind == 'from':
            rows = self.q(client, '/v1/fluree/query', 'SELECT ?o FROM <%s#%s> WHERE { ?s <urn:p:m> ?o }' % (L, spec[1]))
        elif kind == 'graph':
            rows = self.q(client, '/v1/fluree/query/' + L, 'SELECT ?o WHERE { GRAPH <%s> { ?s <urn:p:m> ?o } }' % spec[1])
        elif kind == 'pinned':
            rows = self.q(client, '/v1/fluree/query', 'SELECT ?o FROM <%s@t:%d#%s> WHERE { ?s <urn:p:m> ?o }'
                          % (L, spec[2], spec[1]))
        elif kind == 'enum':
            rows = self.q(client, '/v1/fluree/query/' + L, 'SELECT ?g ?o WHERE { GRAPH ?g { ?s <urn:p:m> ?o } }')
            if isinstance(rows, str):
                return rows
            return sorted('%s %s' % (b['g']['value'], b['o']['value']) for b in rows)
        elif kind == 'default':
            rows = self.q(client, '/v1/fluree/query', 'SELECT ?o FROM <%s> WHERE { ?s <urn:p:m> ?o }' % L)
        elif kind == 'history':
            rows = self.q(client, '/v1/fluree/query', F + 'SELECT ?s ?o ?t ?op FROM <%s@t:1> TO <%s@t:latest> WHERE { '
                          '<< ?s <urn:p:m> ?o >> f:t ?t . << ?s <urn:p:m> ?o >> f:op ?op . }' % (L, L))
            if isinstance(rows, str):
                return rows
            return sorted('%s %s t=%s %s' % (b['s']['value'], b['o']['value'], b['t']['value'], b['op']['value'])
                          for b in rows)
        else:
            raise ValueError(kind)
        if isinstance(rows, str):
            return rows
        return sorted(b['o']['value'] for b in rows)

    def bounds(self, spec):
        """(must, may) for a read issued now: `must` is known before the read, `may` is taken after it."""
        kind = spec[0]
        with self.lock:
            acked = {g: list(v) for g, v in self.acked.items()}
            sent = {g: set(v) for g, v in self.sent.items()}
        if kind in ('from', 'graph'):
            return set(m for _, m in acked.get(spec[1], [])), None
        if kind == 'pinned':
            if self.acked_t < spec[2]:
                return None, None
            exact = set(m for t, m in acked.get(spec[1], []) if t <= spec[2])
            return exact, exact
        if kind == 'enum':
            return set('%s %s' % (g, m) for g in (Z, A) for _, m in acked.get(g, [])), None
        if kind == 'default':
            return set(m for _, m in acked.get(D, [])), None
        if kind == 'history':
            return set('urn:s:d %s t=%d true' % (m, t) for t, m in acked.get(D, [])), None
        raise ValueError(kind)

    def may(self, spec):
        kind = spec[0]
        with self.lock:
            sent = {g: set(v) for g, v in self.sent.items()}
        if kind in ('from', 'graph'):
            return sent.get(spec[1], set())
        if kind == 'enum':
            return set('%s %s' % (g, m) for g in (Z, A) for m in sent.get(g, set()))
        if kind == 'default':
            return sent.get(D, set())
        if kind == 'history':
            return None  # t of an unacknowledged marker is unknown: checked by the must set and the subject only
        return None


def spec_name(spec):
    return ' '.join(str(x) for x in spec)


class Readers:
    def __init__(self, ledger, specs, threads, start_time):
        self.ledger, self.specs, self.threads = ledger, specs, threads
        self.start_time = start_time
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.stats = {spec_name(s): {'reads': 0, 'wrong': 0, 'first': None, 'last': None, 'examples': []} for s in specs}
        self.first_wrong = threading.Event()
        self.pool = []
        self.log = []   # (start, end, spec name, ok) of every 'during' read

    def check(self, client, spec, phase):
        must, exact = self.ledger.bounds(spec)
        if must is None:
            return None
        t0 = time.time()
        got = self.ledger.read(client, spec)
        may = exact if exact is not None else self.ledger.may(spec)
        if isinstance(got, str):
            # A graph not yet created answers 404; that is right while nothing has been acknowledged in it.
            ok = not must and 'HTTP 404' in got and 'Graph not found' in got
        else:
            g = set(got)
            ok = must <= g and (may is None or g <= may)
            if spec[0] == 'history' and ok:
                ok = all(x.startswith('urn:s:d ') for x in g)
        key = spec_name(spec) + ('' if phase == 'during' else ' [%s]' % phase)
        t1 = time.time()
        with self.lock:
            if phase == 'during':
                self.log.append((t0, t1, spec_name(spec), ok))
            st = self.stats.setdefault(key, {'reads': 0, 'wrong': 0, 'first': None, 'last': None, 'examples': []})
            st['reads'] += 1
            if not ok:
                ms = round((t0 - self.start_time) * 1000, 1)
                st['wrong'] += 1
                st['first'] = ms if st['first'] is None else st['first']
                st['last'] = ms
                example = {'ms': ms, 'got': got, 'missing': sorted(must - set(got)) if not isinstance(got, str) else None,
                           'unexpected': sorted(set(got) - may) if (may is not None and not isinstance(got, str)) else None}
                if len(st['examples']) < 2:
                    st['examples'].append(example)
                st['last_example'] = example
                self.first_wrong.set()
        return ok

    def run_thread(self, offset):
        client = Client(self.ledger.port)
        i = offset
        while not self.stop.is_set():
            self.check(client, self.specs[i % len(self.specs)], 'during')
            i += 1

    def run_spec(self, spec):
        client = Client(self.ledger.port)
        while not self.stop.is_set():
            self.check(client, spec, 'during')

    def start(self):
        # One thread per spec, so a slow read form cannot starve a fast one; `threads` round-robin threads more.
        # ROUND_ROBIN_ONLY=1 instead runs only `threads` threads that each cycle through every spec (the shape of
        # the earlier poller), to show how slow read forms starve fast ones.
        for spec in ([] if os.environ.get('ROUND_ROBIN_ONLY') == '1' else self.specs):
            th = threading.Thread(target=self.run_spec, args=(spec,), daemon=True)
            th.start()
            self.pool.append(th)
        extra = self.threads if os.environ.get('ROUND_ROBIN_ONLY') == '1' else max(0, self.threads - len(self.specs))
        for k in range(extra):
            th = threading.Thread(target=self.run_thread, args=(k,), daemon=True)
            th.start()
            self.pool.append(th)

    def finish(self):
        self.stop.set()
        for th in self.pool:
            th.join(60)

    def final(self, phase):
        client = Client(self.ledger.port)
        for spec in self.specs:
            self.check(client, spec, phase)

    def wrong_total(self):
        return sum(v['wrong'] for v in self.stats.values())


# ---------------------------------------------------------------------------------------------------------
def report_round(out, name, ledger, readers, server, log_run=None):
    log = server.log_text(log_run)
    ins = installs(log, ledger.name)
    windows = []
    spans = []
    for i, (t_, it, left) in enumerate(ins):
        if left > 0 and i + 1 < len(ins):
            windows.append(round((ins[i + 1][0] - t_) * 1000, 1))
            spans.append((t_, ins[i + 1][0]))
    inside = {}
    for a, b, spec_key, ok in readers.log:
        if any(w0 <= a and b <= w1 for w0, w1 in spans):
            d = inside.setdefault(spec_key, [0, 0])
            d[0] += 1
            d[1] += (not ok)
    out['rounds'].append({'case': name, 'ledger': ledger.name, 'installs': [(round((t_ - readers.start_time) * 1000, 1), it, left) for t_, it, left in ins],
                          'install_gaps_ms_after_installs_with_unindexed_tail': windows,
                          'stats': readers.stats, 'inside_window': inside, 'final_info': ledger.info()})
    print('  %s %s installs(ms after first commit, index_t, unindexed flakes left)=%s'
          % (name, ledger.name, [(round((t_ - readers.start_time) * 1000), it, left) for t_, it, left in ins]))
    print('    window(s) between an install that left unindexed commits and the next install: %s ms' % windows)
    for key, st in readers.stats.items():
        w = inside.get(key)
        line = '    %-34s reads %6d wrong %5d' % (key, st['reads'], st['wrong'])
        if '[' not in key:
            line += ' | inside window: reads %4d wrong %4d' % tuple(w or (0, 0))
        if st['wrong']:
            line += '  (%.0f..%.0f ms)  first: %s  last: %s' % (st['first'], st['last'], json.dumps(st['examples'][0])[:200],
                                                              json.dumps(st['last_example'])[:200])
        print(line)
    return readers.wrong_total()


def race_shape(ledger, scale, start_readers, one_per_commit=None):
    """First commit with filler (slow build); small commits while it builds; a filler commit to slow the next build."""
    f1, f2 = int(200000 * scale), int(60000 * scale)
    t0 = time.time()
    if one_per_commit is None:
        ledger.commit([Z, A, D], filler=f1)
    else:
        ledger.commit([D], filler=f1)
    start_readers(t0)
    time.sleep(0.02)
    if one_per_commit is not None:
        for g in one_per_commit:
            ledger.commit([g, D])
        for _ in range(4):
            ledger.commit([Z, A, D])
    else:
        for _ in range(5):
            ledger.commit([Z, A, D])
    ledger.filler(f2)
    return t0


READ_SPECS = [('from', Z), ('from', A), ('graph', Z), ('graph', A), ('enum',), ('pinned', Z, 1), ('pinned', A, 1),
              ('pinned', Z, 3), ('pinned', A, 3), ('pinned', Z, 6), ('pinned', A, 6), ('pinned', Z, 7), ('pinned', A, 7),
              ('default',), ('history',)]
BASIC_SPECS = [('from', Z), ('from', A), ('graph', Z), ('graph', A), ('enum',)]


def case_simple(args, out, name, specs, storage=False, one_per_commit=None, rounds=1, restart_check=False):
    work = tempfile.mkdtemp(prefix='fluree-gid-scope-')
    store = os.path.join(work, 'store') if storage else None
    server = Server(args.binary, args.port, work, storage=store)
    wrong = 0
    try:
        server.start()
        out['server'] = server.command
        ledgers = []
        for k in range(rounds):
            ledger = Ledger(args.port, '%s-%d:main' % (name, k + 1)).create()
            holder = {}

            def start_readers(t0, ledger=ledger):
                holder['r'] = Readers(ledger, specs, args.readers, t0)
                holder['r'].start()
            race_shape(ledger, args.scale, start_readers, one_per_commit)
            ledger.settle()
            time.sleep(0.2)
            readers = holder['r']
            readers.finish()
            readers.final('settled')
            time.sleep(3)
            readers.final('settled+3s')
            wrong += report_round(out, name, ledger, readers, server)
            ledgers.append((ledger, readers))
        if restart_check:
            server.stop()
            server.start()
            for ledger, readers in ledgers:
                ledger.c = Client(args.port)
                readers.final('after restart')
                st = {k: v for k, v in readers.stats.items() if k.endswith('[after restart]')}
                bad = sum(v['wrong'] for v in st.values())
                wrong += bad
                print('  %s after restart: %d reads, %d wrong' % (ledger.name, sum(v['reads'] for v in st.values()), bad))
                out['rounds'].append({'ledger': ledger.name, 'after_restart': st})
    finally:
        server.stop()
        keep_logs(args, server)
        shutil.rmtree(work, ignore_errors=True)
    return wrong


def keep_logs(args, server):
    if args.out:
        for i, path in enumerate(server.logs):
            if os.path.exists(path):
                shutil.copyfile(path, '%s.server-%d.log' % (args.out, i + 1))


def case_restart(args, out, indexing_in_run2):
    name = 'restart-unindexed' + ('' if indexing_in_run2 else '-off')
    work = tempfile.mkdtemp(prefix='fluree-gid-scope-')
    store = os.path.join(work, 'store')
    server = Server(args.binary, args.port, work, storage=store)
    wrong = 0
    try:
        server.start(extra=['--indexing-enabled=false'])
        out['server_run1'] = server.command
        ledger = Ledger(args.port, name + ':main').create()
        ledger.commit([Z, A, D], filler=int(200000 * args.scale))
        ledger.commit([Z, A, D])
        ledger.commit([Z, A, D])
        print('  run 1 (indexing off) info %s' % (ledger.info(),))
        server.stop()

        server.start(extra=[] if indexing_in_run2 else ['--indexing-enabled=false'])
        out['server_run2'] = server.command
        ledger.c = Client(args.port)
        t0 = time.time()
        readers = Readers(ledger, BASIC_SPECS, args.readers, t0)
        first = readers.check(Client(args.port), ('from', Z), 'first read after restart')
        first_ms = (time.time() - t0) * 1000
        readers.start()
        for _ in range(5):   # land while the start-up catch-up build runs (no /info call before: it is slow)
            ledger.commit([Z, A, D])
        print('  run 2 first read ok=%s after %.0f ms; five commits done %.0f ms after it started'
              % (first, first_ms, (time.time() - t0) * 1000))
        ledger.filler(int(60000 * args.scale))
        if indexing_in_run2:
            ledger.settle()
        else:
            time.sleep(1.5)
        time.sleep(0.2)
        readers.finish()
        readers.final('settled')
        wrong += report_round(out, name, ledger, readers, server)
        server.stop()

        server.start()
        out['server_run3'] = server.command
        ledger.c = Client(args.port)
        readers.final('after second restart')
        if True:
            ledger.settle()
            readers.final('after second restart, settled')
        st = {k: v for k, v in readers.stats.items() if 'after second restart' in k}
        bad = sum(v['wrong'] for v in st.values())
        wrong += bad
        print('  run 3: %d reads, %d wrong; info %s' % (sum(v['reads'] for v in st.values()), bad, ledger.info()))
        out['rounds'].append({'ledger': ledger.name, 'after_second_restart': st})
    finally:
        server.stop()
        keep_logs(args, server)
        shutil.rmtree(work, ignore_errors=True)
    return wrong


def case_long_window(args, out):
    work = tempfile.mkdtemp(prefix='fluree-gid-scope-')
    server = Server(args.binary, args.port, work,
                    extra=['--reindex-min-bytes=100000000', '--indexer-catchup-interval-secs=2'])
    wrong = 0
    try:
        server.start()
        out['server'] = server.command
        ledger = Ledger(args.port, 'long-window:main').create()
        t0 = time.time()
        ledger.commit([Z, A, D], filler=int(200000 * args.scale))
        readers = Readers(ledger, READ_SPECS, 0, t0)
        # wait for the catch-up sweep to start the first build, then commit while it runs
        pat = re.compile(r'Starting index build.*ledger_id=long-window:main current_index_t=0')
        end = time.time() + 30
        while time.time() < end and not pat.search(server.log_text()):
            time.sleep(0.002)
        started = time.time()
        print('  first build started %.0f ms after the first commit' % ((started - t0) * 1000))
        for _ in range(6):   # t=2..7, so that t=7 is the head and pinned t=6 is in the past
            ledger.commit([Z, A, D])
        readers.start()
        ledger.settle(timeout=60)
        time.sleep(0.5)
        readers.finish()
        readers.final('settled')
        wrong += report_round(out, 'long-window', ledger, readers, server)
    finally:
        server.stop()
        keep_logs(args, server)
        shutil.rmtree(work, ignore_errors=True)
    return wrong


def case_restart_long(args, out):
    """Restart with unindexed commits, a raised --reindex-min-bytes and the default catch-up interval."""
    name = 'restart-long'
    work = tempfile.mkdtemp(prefix='fluree-gid-scope-')
    server = Server(args.binary, args.port, work, storage=os.path.join(work, 'store'))
    wrong = 0
    try:
        server.start(extra=['--indexing-enabled=false'])
        out['server_run1'] = server.command
        ledger = Ledger(args.port, name + ':main').create()
        ledger.commit([Z, A, D], filler=int(200000 * args.scale))
        ledger.commit([Z, A, D])
        ledger.commit([Z, A, D])
        server.stop()
        server.start(extra=['--reindex-min-bytes=100000000'])
        out['server_run2'] = server.command
        ledger.c = Client(args.port)
        t0 = time.time()
        readers = Readers(ledger, [('from', Z), ('from', A)], 0, t0)
        client = Client(args.port)
        readers.check(client, ('from', Z), 'first read after restart')
        for _ in range(5):
            ledger.commit([Z, A, D])
        print('  run 2: five commits done %.0f ms after the first read started' % ((time.time() - t0) * 1000))
        samples = []
        end = t0 + args.timeout
        settled_at = None
        while time.time() < end:
            for spec in readers.specs:
                ok = readers.check(client, spec, 'during')
                samples.append((round(time.time() - t0, 2), spec_name(spec), ok))
            ins = installs(server.log_text(), ledger.name)
            if ins and ins[-1][1] >= ledger.last_t and ins[-1][2] == 0 and all(x[2] for x in samples[-2:]):
                settled_at = time.time() - t0
                break
            time.sleep(0.25)
        readers.final('settled')
        bad = [x for x in samples if not x[2]]
        print('  sampled %d reads over %.0f s, %d wrong; first wrong at %s s, last wrong at %s s; settled at %s s'
              % (len(samples), samples[-1][0] if samples else 0, len(bad), bad[0][0] if bad else None,
                 bad[-1][0] if bad else None, None if settled_at is None else round(settled_at, 1)))
        out['samples_summary'] = {'reads': len(samples), 'wrong': len(bad), 'first_wrong_s': bad[0][0] if bad else None,
                                  'last_wrong_s': bad[-1][0] if bad else None, 'settled_s': settled_at}
        wrong += report_round(out, name, ledger, readers, server)
    finally:
        server.stop()
        keep_logs(args, server)
        shutil.rmtree(work, ignore_errors=True)
    return wrong


# ---------------------------------------------------------------------------------------------------------
WRITES = {
    'delete-where': ('update', 'DELETE { GRAPH <urn:g:z> { ?s <urn:p:m> ?o } } WHERE { GRAPH <urn:g:z> { ?s <urn:p:m> ?o } }'),
    'copy-where': ('update', 'INSERT { GRAPH <urn:g:copy> { ?s <urn:p:m> ?o } } WHERE { GRAPH <urn:g:z> { ?s <urn:p:m> ?o } }'),
    'edit-where': ('update', 'DELETE { GRAPH <urn:g:z> { ?s <urn:p:m> ?o } } INSERT { GRAPH <urn:g:z> { ?s <urn:p:m> ?n } } '
                             'WHERE { GRAPH <urn:g:z> { ?s <urn:p:m> ?o } BIND(CONCAT(?o, "-edited") AS ?n) }'),
    'delete-data': ('update', 'DELETE DATA { GRAPH <urn:g:z> { <urn:s:1-3> <urn:p:m> "z-3" } }'),
    'upsert': ('upsert', 'GRAPH <urn:g:z> { <urn:s:1-3> <urn:p:m> "z-3-new" . }'),
}


def expected_after(kind, z, a):
    """What each write must leave, given the markers z and a held before it (correct semantics)."""
    z, a = set(z), set(a)
    if kind == 'delete-where':
        return {'z': set(), 'a': a, 'copy': set()}
    if kind == 'copy-where':
        return {'z': z, 'a': a, 'copy': z}
    if kind == 'edit-where':
        return {'z': {m + '-edited' for m in z}, 'a': a, 'copy': set()}
    if kind == 'delete-data':
        return {'z': z - {'z-3'}, 'a': a, 'copy': set()}
    if kind == 'upsert':
        return {'z': (z - {'z-3'}) | {'z-3-new'}, 'a': a, 'copy': set()}
    raise ValueError(kind)


def graph_values(ledger, client, graph):
    rows = ledger.q(client, '/v1/fluree/query', 'SELECT ?o FROM <%s#%s> WHERE { ?s <urn:p:m> ?o }' % (ledger.name, graph))
    if isinstance(rows, str):
        return [] if 'not found' in rows.lower() else rows
    return sorted(b['o']['value'] for b in rows)


def show(ledger, t):
    s, r = ledger.c.request('GET', '/v1/fluree/show/%s?commit=t:%d' % (urllib.parse.quote(ledger.name), t))
    if s != 200:
        return 'HTTP %s %s' % (s, str(r)[:200])
    return [f for f in r.get('flakes', []) if f[1] == 'urn:p:m']


def do_write(ledger, kind, client=None):
    how, body = WRITES[kind]
    if how == 'update':
        return ledger.update(body, client)
    return ledger.send('upsert', body, 'application/trig', client)


def case_writes(args, out):
    work = tempfile.mkdtemp(prefix='fluree-gid-scope-')
    store = os.path.join(work, 'store')
    server = Server(args.binary, args.port, work, storage=store)
    results = []
    try:
        server.start()
        out['server'] = server.command
        for kind in WRITES:
            # --- in the window -------------------------------------------------------------------------------
            ledger = Ledger(args.port, 'w-%s:main' % kind).create()
            holder = {}

            def start_readers(t0, ledger=ledger):
                holder['r'] = Readers(ledger, [('from', Z), ('from', A)], 2, t0)
                holder['r'].start()
            fired = {}
            pat = re.compile(r'index install ledger="%s" index_t=1 novelty_flakes_before=\d+ novelty_flakes_after=([1-9]\d*)'
                             % re.escape(ledger.name))

            def fire(ledger=ledger, kind=kind, fired=fired):
                end = time.time() + 60
                while time.time() < end and not pat.search(server.log_text()):
                    time.sleep(0.001)
                with ledger.lock:
                    fired['z_before'] = sorted(m for _, m in ledger.acked.get(Z, []))
                    fired['a_before'] = sorted(m for _, m in ledger.acked.get(A, []))
                fired['sent'] = time.time()
                fired['t'] = do_write(ledger, kind, Client(ledger.port))
                fired['done'] = time.time()
            watcher = threading.Thread(target=fire, daemon=True)
            watcher.start()
            t0 = race_shape(ledger, args.scale, start_readers)
            watcher.join(90)
            ledger.settle()
            time.sleep(0.2)
            holder['r'].finish()
            ins = installs(server.log_text(), ledger.name)
            # Window evidence that the write itself cannot disturb: reads of z sent after the first install and
            # finished before the write was sent; reads of a (no write here targets a) between the first two installs.
            w0 = ins[0][0] if ins else fired['sent']
            w1 = ins[1][0] if len(ins) > 1 else fired['done']
            evidence = {}
            for a_, b_, key, ok in holder['r'].log:
                if (key == 'from urn:g:z' and w0 <= a_ and b_ <= fired['sent']) or (key == 'from urn:g:a' and w0 <= a_ and b_ <= w1):
                    d = evidence.setdefault(key, [0, 0])
                    d[0] += 1
                    d[1] += (not ok)
            commits = [int(x) for x in re.findall(r'commit ledger=%s t=(\d+) ' % re.escape(ledger.name), server.log_text())]
            results.append({'kind': kind, 'mode': 'in window', 'ledger': ledger, 'commit_ts_in_log': commits,
                            'sent_ms': round((fired['sent'] - t0) * 1000, 1), 'done_ms': round((fired['done'] - t0) * 1000, 1),
                            'write_t': fired['t'], 'z_before': fired['z_before'], 'a_before': fired['a_before'],
                            'installs_ms': [(round((x - t0) * 1000, 1), it, left) for x, it, left in ins],
                            'window_reads': evidence})
            # --- control: same data, no index race --------------------------------------------------------
            ctl = Ledger(args.port, 'c-%s:main' % kind).create()
            ctl.commit([Z, A, D])
            for _ in range(5):
                ctl.commit([Z, A, D])
            ctl.settle()
            zb = sorted(m for _, m in ctl.acked[Z])
            ab = sorted(m for _, m in ctl.acked[A])
            t = do_write(ctl, kind)
            ctl.settle()
            commits = [int(x) for x in re.findall(r'commit ledger=%s t=(\d+) ' % re.escape(ctl.name), server.log_text())]
            results.append({'kind': kind, 'mode': 'control', 'ledger': ctl, 'write_t': t, 'z_before': zb, 'a_before': ab,
                            'commit_ts_in_log': commits})
        bad = 0
        for phase in ('settled', 'after restart'):
            if phase == 'after restart':
                server.stop()
                server.start()
            client = Client(args.port)
            for r in results:
                led = r['ledger']
                led.c = client
                state = {g: graph_values(led, client, 'urn:g:' + g) for g in ('z', 'a', 'copy')}
                exp = expected_after(r['kind'], r['z_before'], r['a_before'])
                ok = all(not isinstance(state[g], str) and set(state[g]) == exp[g] for g in exp)
                r.setdefault('final', {})[phase] = {'state': state, 'ok': ok,
                                                    'expected': {g: sorted(v) for g, v in exp.items()}}
                if phase == 'settled':
                    r['committed'] = show(led, r['write_t'])
                if not ok:
                    bad += 1
        for r in results:
            led = r.pop('ledger')
            r['ledger'] = led.name
            print('  %-12s %-9s write answered t=%s; commits in server log: t=%s%s' % (r['kind'], r['mode'], r['write_t'], r['commit_ts_in_log'],
                  ('\n      sent %.1f ms, answered %.1f ms after the first commit; installs (ms, index_t, unindexed flakes left) %s;'
                    '\n      window reads [reads, wrong]: z after the first install and before the write, a between the first two installs: %s'
                   % (r['sent_ms'], r['done_ms'], r['installs_ms'], r['window_reads'])) if r['mode'] == 'in window' else ''))
            print('      before:   z=%s a=%s' % (r['z_before'], r['a_before']))
            print('      committed (urn:p:m flakes): %s' % json.dumps(r['committed'])[:900])
            for phase, f in r['final'].items():
                print('      %-13s ok=%s z=%s a=%s copy=%s' % (phase, f['ok'], f['state']['z'], f['state']['a'], f['state']['copy']))
                if not f['ok']:
                    print('      %-13s expected z=%s a=%s copy=%s' % ('', f['expected']['z'], f['expected']['a'], f['expected']['copy']))
        out['writes'] = results
        return bad
    finally:
        server.stop()
        keep_logs(args, server)
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument('binary')
    p.add_argument('port', type=int)
    p.add_argument('case')
    p.add_argument('--out', help='write PREFIX.json and keep server logs as PREFIX.server-N.log')
    p.add_argument('--scale', type=float, default=1.0)
    p.add_argument('--readers', type=int, default=6)
    p.add_argument('--rounds', type=int, default=1)
    p.add_argument('--timeout', type=float, default=720, help='restart-long: seconds to keep sampling')
    p.add_argument('--kinds', help='reads case only: comma list of read kinds to run (from,graph,enum,pinned,default,history)')
    args = p.parse_args()
    version = subprocess.run([args.binary, '--version'], capture_output=True, text=True).stdout.strip()
    out = {'version': version, 'case': args.case, 'scale': args.scale, 'rounds': [],
           'date': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    print('%s | case %s | scale %s' % (version, args.case, args.scale))
    try:
        if args.case == 'reads':
            specs = [x for x in READ_SPECS if not args.kinds or x[0] in args.kinds.split(',')]
            wrong = case_simple(args, out, 'reads', specs, rounds=args.rounds)
        elif args.case == 'one-per-commit':
            wrong = case_simple(args, out, 'one-per-commit', BASIC_SPECS, one_per_commit=[Z, A], rounds=args.rounds)
        elif args.case == 'one-per-commit-alpha':
            wrong = case_simple(args, out, 'one-per-commit-alpha', BASIC_SPECS, one_per_commit=[A, Z], rounds=args.rounds)
        elif args.case == 'file-storage':
            wrong = case_simple(args, out, 'file-storage', BASIC_SPECS, storage=True, rounds=args.rounds, restart_check=True)
        elif args.case == 'restart-unindexed':
            wrong = case_restart(args, out, True)
        elif args.case == 'restart-unindexed-off':
            wrong = case_restart(args, out, False)
        elif args.case == 'restart-long':
            wrong = case_restart_long(args, out)
        elif args.case == 'long-window':
            wrong = case_long_window(args, out)
        elif args.case == 'writes':
            wrong = case_writes(args, out)
        else:
            raise SystemExit('unknown case ' + args.case)
    except Exception as failure:
        import traceback
        traceback.print_exc()
        out['error'] = str(failure)
        wrong = None
    out['wrong'] = wrong
    if args.out:
        with open(args.out + '.json', 'w') as f:
            json.dump(out, f, indent=1, default=str)
    print('RESULT case=%s wrong=%s' % (args.case, wrong))
    sys.exit(2 if wrong is None else (1 if wrong else 0))


if __name__ == '__main__':
    main()
