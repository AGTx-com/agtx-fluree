#!/usr/bin/env python3
"""Fluree 4.2.9: a query of one named graph returns another named graph's facts.

Standalone reproduction (Python 3 standard library only). It starts its own in-memory
`fluree server run` with default settings, in a fresh temporary directory, and removes both
when it finishes.

    python3 -I repro_graph_id_swap.py <fluree-binary> [--port N] [--rounds N] [--scale X]

Exit status: 1 if any read returned a wrong answer, 0 if none did, 2 if the run could not be set up.

What it does, per round (each round uses a fresh ledger on the same server):

  1. One commit creates two named graphs, <urn:g:z> and <urn:g:a>, with one marker each. The
     z marker's subject (<urn:s:1-1>) sorts before the a marker's (<urn:s:2-1>). The same commit
     carries filler triples in the default graph so that its background index build takes a few
     hundred milliseconds.
  2. While that build runs, five small commits each add one marker to both graphs, then one more
     commit adds filler to the default graph so that the next build is not instant.
  3. Reader threads query each graph in a tight loop, in two forms:
        POST /v1/fluree/query            SELECT ?o FROM <ledger#urn:g:z> WHERE { ?s <urn:p:m> ?o }
        POST /v1/fluree/query/<ledger>   SELECT ?o WHERE { GRAPH <urn:g:z> { ?s <urn:p:m> ?o } }
     A read is wrong if it returns a marker of the other graph, misses a marker whose commit was
     acknowledged before the read was sent, or returns a marker whose commit was not yet sent.
  4. The round ends when the index has caught up with the last commit; a final read of each graph
     is then checked again.

Why it happens (from reading the 4.2.9 source; the script only observes the symptom): the live
graph registry numbers the new graphs of a commit in alphabetical IRI order (a=3, z=4), while
the indexer numbers them in the order it first meets their flakes (z=3, a=4). Unindexed commits
are kept in memory per numeric graph ID. When the index for the first commit is installed while
the later commits are still unindexed, the registry takes the index's numbering but the later
commits stay filed under the old numbers, so each graph's query reads the other graph's recent
commits until the next index covers them.

The server log is captured with RUST_LOG=warn,fluree::write_path=debug only to confirm that the
index install happened while later commits were still unindexed; the verdict does not depend on it.
"""
import argparse
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

GRAPHS = ('urn:g:z', 'urn:g:a')
LETTER = {'urn:g:z': 'z', 'urn:g:a': 'a'}
RANK = {'urn:g:z': 1, 'urn:g:a': 2}  # subject rank: z's subjects sort first


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Client:
    def __init__(self, port):
        self.port = port
        self.conn = None

    def request(self, method, path, body=None, content_type='application/json', timeout=300):
        data = None if body is None else (body if isinstance(body, str) else json.dumps(body)).encode()
        for attempt in range(3):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)
                self.conn.request(method, path, body=data,
                                  headers={'Content-Type': content_type, 'Accept': 'application/json'})
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
                if attempt == 2:
                    raise


class Round:
    def __init__(self, port, ledger, filler_first, filler_tail, small_commits, readers):
        self.port, self.ledger = port, ledger
        self.filler_first, self.filler_tail = filler_first, filler_tail
        self.small_commits, self.readers = small_commits, readers
        self.lock = threading.Lock()
        self.sent = {g: set() for g in GRAPHS}     # markers whose commit has been sent
        self.acked = {g: set() for g in GRAPHS}    # markers whose commit was acknowledged
        self.stop = threading.Event()
        self.reads = 0
        self.wrong = []
        self.started = None
        self.last_t = 0

    # -- writes -----------------------------------------------------------------------------------------
    def update(self, client, sparql):
        status, result = client.request('POST', '/v1/fluree/update?ledger=' + urllib.parse.quote(self.ledger),
                                        sparql, 'application/sparql-update')
        if status != 200:
            raise RuntimeError('update failed: %s %s' % (status, str(result)[:300]))
        return result['t']

    def insert_turtle(self, client, turtle):
        status, result = client.request('POST', '/v1/fluree/insert?ledger=' + urllib.parse.quote(self.ledger),
                                        turtle, 'text/turtle')
        if status != 200:
            raise RuntimeError('insert failed: %s %s' % (status, str(result)[:300]))
        return result['t']

    def markers(self, n):
        with self.lock:
            for g in GRAPHS:
                self.sent[g].add('%s-%d' % (LETTER[g], n))
        return ' '.join('GRAPH <%s> { <urn:s:%d-%d> <urn:p:m> "%s-%d" }' % (g, RANK[g], n, LETTER[g], n)
                        for g in GRAPHS)

    def ack(self, n, t):
        with self.lock:
            for g in GRAPHS:
                self.acked[g].add('%s-%d' % (LETTER[g], n))
            self.last_t = max(self.last_t, t)

    # -- reads ------------------------------------------------------------------------------------------
    def read(self, client, graph, form):
        if form == 'FROM':
            path = '/v1/fluree/query'
            query = 'SELECT ?o FROM <%s#%s> WHERE { ?s <urn:p:m> ?o }' % (self.ledger, graph)
        else:
            path = '/v1/fluree/query/' + urllib.parse.quote(self.ledger)
            query = 'SELECT ?o WHERE { GRAPH <%s> { ?s <urn:p:m> ?o } }' % graph
        status, result = client.request('POST', path, query, 'application/sparql-query')
        if status != 200 or not isinstance(result, dict):
            return 'HTTP %s %s' % (status, str(result)[:200])
        return sorted(b['o']['value'] for b in result['results']['bindings'])

    def check(self, client, graph, form, where):
        with self.lock:
            must = set(self.acked[graph])
        sent_before = time.time()
        got = self.read(client, graph, form)
        with self.lock:
            may = set(self.sent[graph])
            self.reads += 1
        ok = isinstance(got, list) and must <= set(got) <= may
        if not ok:
            with self.lock:
                self.wrong.append({'where': where, 'ms': round((sent_before - self.started) * 1000, 1),
                                   'graph': graph, 'form': form, 'got': got, 'expected_at_least': sorted(must)})
        return ok

    def reader(self, index):
        client = Client(self.port)
        i = index
        while not self.stop.is_set():
            graph = GRAPHS[i % 2]
            form = 'FROM' if (i // 2) % 2 == 0 else 'GRAPH'
            self.check(client, graph, form, 'during')
            i += 1

    def info(self, client):
        status, result = client.request('GET', '/v1/fluree/info/' + urllib.parse.quote(self.ledger))
        if status != 200:
            return None
        ledger = result['ledger']
        ids = {x['iri']: x['g-id'] for x in ledger['named-graphs'] if x['iri'] in GRAPHS}
        return ledger['index-t'], ledger['commit-t'], ids

    # -- one round --------------------------------------------------------------------------------------
    def run(self):
        client = Client(self.port)
        status, result = client.request('POST', '/v1/fluree/create', {'ledger': self.ledger})
        if status not in (200, 201):
            raise RuntimeError('create failed: %s %s' % (status, result))
        filler = ' '.join('<urn:f:first-%d> <urn:p:f> %d .' % (i, i) for i in range(self.filler_first))
        first = 'INSERT DATA { %s %s }' % (self.markers(1), filler)
        tail = '\n'.join('<urn:f:tail-%d> <urn:p:f> %d .' % (i, i) for i in range(self.filler_tail))

        self.started = time.time()
        t = self.update(client, first)
        self.ack(1, t)
        threads = [threading.Thread(target=self.reader, args=(i,), daemon=True) for i in range(self.readers)]
        for thread in threads:
            thread.start()
        time.sleep(0.02)  # let the first commit's index build start before the next commits
        for n in range(2, 2 + self.small_commits):
            self.ack(n, self.update(client, 'INSERT DATA { %s }' % self.markers(n)))
        if self.filler_tail:
            self.last_t = max(self.last_t, self.insert_turtle(client, tail))

        deadline = time.time() + 120
        state = None
        while time.time() < deadline:
            state = self.info(client)
            if state and state[0] >= self.last_t:
                break
            time.sleep(0.05)
        time.sleep(0.2)
        self.stop.set()
        for thread in threads:
            thread.join(30)
        for graph in GRAPHS:
            for form in ('FROM', 'GRAPH'):
                self.check(client, graph, form, 'settled')
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('fluree', help='path to the fluree binary')
    parser.add_argument('--port', type=int, default=0, help='listen port (default: a free port)')
    parser.add_argument('--rounds', type=int, default=3, help='rounds, each on a fresh ledger (default 3)')
    parser.add_argument('--scale', type=float, default=1.0, help='multiply the filler sizes (default 1)')
    parser.add_argument('--readers', type=int, default=4, help='reader threads (default 4)')
    parser.add_argument('--keep-log', help='copy the server log to this path')
    args = parser.parse_args()

    filler_first, filler_tail = int(200000 * args.scale), int(60000 * args.scale)
    port = args.port or free_port()
    work = tempfile.mkdtemp(prefix='fluree-graph-id-repro-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work, TMPDIR=work,
               RUST_LOG='warn,fluree::write_path=debug', NO_COLOR='1')
    log_path = os.path.join(work, 'server.log')
    log = open(log_path, 'w')
    version = subprocess.run([args.fluree, '--version'], capture_output=True, text=True, env=env, cwd=work).stdout.strip()
    command = [args.fluree, 'server', 'run', '--memory', '--listen-addr', '127.0.0.1:%d' % port]
    print('build:   %s' % version)
    print('server:  %s' % ' '.join(command[1:]))
    print('shape:   first commit %d filler triples; %d small commits; tail commit %d filler triples; %d readers'
          % (filler_first, 5, filler_tail, args.readers))
    server = subprocess.Popen(command, cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    total_wrong = total_reads = 0
    try:
        for _ in range(400):
            try:
                probe = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
                probe.request('GET', '/health')
                probe.getresponse().read()
                break
            except OSError:
                if server.poll() is not None:
                    raise RuntimeError('server exited: ' + open(log_path).read()[-2000:])
                time.sleep(0.05)
        else:
            raise RuntimeError('server did not answer /health')
        for k in range(1, args.rounds + 1):
            ledger = 'repro-%d:main' % k
            rnd = Round(port, ledger, filler_first, filler_tail, 5, args.readers)
            state = rnd.run()
            log_text = re.sub(r'\x1b\[[0-9;]*m', '', open(log_path).read())
            installs = re.findall(r'index install ledger="%s" index_t=(\d+) novelty_flakes_before=\d+ '
                                  r'novelty_flakes_after=(\d+)' % re.escape(ledger), log_text)
            during = [w for w in rnd.wrong if w['where'] == 'during']
            settled = [w for w in rnd.wrong if w['where'] == 'settled']
            total_wrong += len(rnd.wrong)
            total_reads += rnd.reads
            print('round %d  ledger %s  reads %d  wrong during %d  wrong after settling %d  final (index_t, commit_t, ids) %s'
                  % (k, ledger, rnd.reads, len(during), len(settled), state))
            print('         index installs (index_t, unindexed flakes left): %s'
                  % ', '.join('(%s, %s)' % x for x in installs))
            if during:
                print('         wrong reads seen from %.1f ms to %.1f ms after the first commit was sent'
                      % (during[0]['ms'], during[-1]['ms']))
            for w in (during[:2] + during[-1:] if len(during) > 2 else during) + settled[:2]:
                print('         WRONG %-7s %-5s %s got %s; acknowledged before the read: %s'
                      % (w['where'], w['form'], w['graph'], w['got'], w['expected_at_least']))
    except Exception as failure:  # setup problem, not a verdict
        print('ERROR: %s' % failure)
        return 2
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=15)
        log.close()
        if args.keep_log:
            shutil.copyfile(log_path, args.keep_log)
        shutil.rmtree(work, ignore_errors=True)
    if total_wrong:
        print('RESULT: WRONG READS: %d of %d reads returned another graph\'s facts or missed their own'
              % (total_wrong, total_reads))
        return 1
    print('RESULT: no wrong reads in %d reads' % total_reads)
    return 0


if __name__ == '__main__':
    sys.exit(main())
