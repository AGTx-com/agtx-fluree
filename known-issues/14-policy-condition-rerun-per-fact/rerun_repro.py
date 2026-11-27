"""Does Fluree run a policy condition again for every fact, even when nothing the condition reads differs?

Usage: python3 -I rerun_repro.py <fluree-binary> [port, default 18776]

Standard library only. Fresh temporary directory (HOME, XDG_CONFIG_HOME, TMPDIR), in-memory server on 127.0.0.1.
Identities are sent with the `fluree-identity` header; every write also asks for per-policy counts with
`fluree-track-policy: true` (docs/api/headers.md), and the script reports them if the response carries them.

A team of G members, each holding a seat count. Alice and Bob administer the team; Carol administers nothing.
Each identity's only f:modify policy has a condition that reads every member (no member may hold more than 1,000
seats), so one evaluation costs time in proportion to G:

  same-inputs (Alice, Carol): reads nothing about the fact being written.
    ASK { $identity <urn:x:adminOf> <urn:x:team> .
          FILTER NOT EXISTS { ?m <urn:x:memberOf> <urn:x:team> ; <urn:x:seats> ?s . FILTER(?s > 1000) } }
  same-subject (Bob): also reads the fact's subject.
    ASK { $this <urn:x:in> ?team . $identity <urn:x:adminOf> ?team .
          FILTER NOT EXISTS { ?m <urn:x:memberOf> ?team ; <urn:x:seats> ?s . FILTER(?s > 1000) } }

Writes of K facts, three runs each with fresh values:
  same inputs  : Alice writes one fact about each of K new subjects. The condition has the same inputs and reads
                 the same state for all K facts, so one evaluation decides them all.
  same subject : Bob writes K facts about one subject that is in the team. Again one evaluation decides them all.
  no policy    : the same-inputs write with no identity, which this server does not policy-check (stage cost alone).
  refused      : Carol writes one fact; the condition is false for her, so the policy is real.

If each fact pays for its own evaluation, the time of a policy-checked write grows with K at a rate set by G,
while the unchecked write stays near its stage cost.
"""
import http.client
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

BIN = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 18776
L = 'repro:main'
F = 'https://ns.flur.ee/db#'
G = 5000
KS = (1, 10, 100, 1000)
RUNS = 3

SAME_INPUTS = ('ASK { $identity <urn:x:adminOf> <urn:x:team> . FILTER NOT EXISTS { ?m <urn:x:memberOf> <urn:x:team> ; '
               '<urn:x:seats> ?s . FILTER(?s > 1000) } }')
SAME_SUBJECT = ('ASK { $this <urn:x:in> ?team . $identity <urn:x:adminOf> ?team . FILTER NOT EXISTS { '
                '?m <urn:x:memberOf> ?team ; <urn:x:seats> ?s . FILTER(?s > 1000) } }')


def request(method, path, body=None, ctype='application/json', headers=None):
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=600)
    h = {'Content-Type': ctype, 'Accept': 'application/json'}
    h.update(headers or {})
    c.request(method, path, None if body is None else body.encode(), h)
    r = c.getresponse()
    text = r.read().decode()
    resp_headers = dict(r.getheaders())
    c.close()
    try:
        return r.status, json.loads(text), resp_headers
    except ValueError:
        return r.status, text, resp_headers


def update(sparql, who=None):
    headers = {'fluree-track-policy': 'true'}
    if who:
        headers['fluree-identity'] = who
    t0 = time.perf_counter()
    status, body, resp_headers = request('POST', '/v1/fluree/update?ledger=' + L, sparql,
                                         'application/sparql-update', headers)
    return status, body, resp_headers, (time.perf_counter() - t0) * 1000


def policy_counts(body, resp_headers):
    """Per-policy {executed, allowed} from the body or headers, if the server reported them."""
    found = {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, dict) and 'executed' in v and 'allowed' in v:
                    found[k] = (v['executed'], v['allowed'])
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(body)
    for k, v in resp_headers.items():
        if 'policy' in k.lower():
            try:
                walk(json.loads(v))
            except ValueError:
                pass
    return found


def policy(rule, cls, ask):
    return ('<%s> a <%sAccessPolicy>, <%s> ; <%saction> <%smodify> ; <%squery> %s^^<%ssparql> .'
            % (rule, F, cls, F, F, F, json.dumps(ask), F))


def main():
    work = tempfile.mkdtemp(prefix='repro-rerun-per-fact-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work, TMPDIR=work, NO_COLOR='1', RUST_LOG='warn')
    log = open(os.path.join(work, 'server.log'), 'w')
    proc = subprocess.Popen([BIN, 'server', 'run', '--memory', '--listen-addr', '127.0.0.1:%d' % PORT],
                            cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(500):
            try:
                request('GET', '/health')
                break
            except OSError:
                time.sleep(0.02)
        print(subprocess.run([BIN, '--version'], capture_output=True, text=True).stdout.strip())
        print('team of %d members; median of %d runs per write' % (G, RUNS))
        assert request('POST', '/v1/fluree/create', json.dumps({'ledger': L}))[0] in (200, 201)
        members = ' '.join('<urn:m:%05d> <urn:x:memberOf> <urn:x:team> ; <urn:x:seats> %d .' % (i, 1 + i % 50)
                           for i in range(G))
        seed = ('INSERT DATA { %s %s %s '
                '<urn:x:alice> <%spolicyClass> <urn:x:SameInputs> ; <urn:x:adminOf> <urn:x:team> . '
                '<urn:x:carol> <%spolicyClass> <urn:x:SameInputs> . '
                '<urn:x:bob> <%spolicyClass> <urn:x:SameSubject> ; <urn:x:adminOf> <urn:x:team> . '
                '<urn:x:doc> <urn:x:in> <urn:x:team> . }'
                % (policy('urn:x:rule-same-inputs', 'urn:x:SameInputs', SAME_INPUTS),
                   policy('urn:x:rule-same-subject', 'urn:x:SameSubject', SAME_SUBJECT), members, F, F, F))
        s, body, _, _ = update(seed)
        assert s == 200, body

        s, body, h, ms = update('INSERT DATA { <urn:t:carol> <urn:x:label> "carol" . }', 'urn:x:carol')
        print('refused control: carol writes 1 fact -> HTTP %d (%s)' % (s, 'refused' if s != 200 else 'ALLOWED'))

        print('%5s | %-34s | %-34s | %-12s' % ('K', 'same inputs (alice)', 'same subject (bob)', 'no policy'))
        for k in KS:
            cells = []
            for case in ('same-inputs', 'same-subject', 'no-policy'):
                times, counts, statuses = [], None, set()
                for run in range(RUNS):
                    tag = 'k%dr%d' % (k, run)
                    if case == 'same-subject':
                        facts = ' '.join('<urn:x:doc> <urn:x:note> "%s-%d" .' % (tag, i) for i in range(k))
                        who = 'urn:x:bob'
                    else:
                        facts = ' '.join('<urn:t:%s-%s-%d> <urn:x:label> "x" .' % (case, tag, i) for i in range(k))
                        who = 'urn:x:alice' if case == 'same-inputs' else None
                    s, body, h, ms = update('INSERT DATA { %s }' % facts, who)
                    statuses.add(s)
                    times.append(ms)
                    counts = policy_counts(body, h) or counts
                cell = '%7.0f ms' % statistics.median(times)
                if case != 'no-policy':
                    executed = sum(e for e, _ in counts.values()) if counts else None
                    cell += ' executed=%s' % ('n/a' if executed is None else executed)
                cell += ' http=%s' % ','.join(str(x) for x in sorted(statuses))
                cells.append(cell)
            print('%5d | %-34s | %-34s | %-12s' % (k, cells[0], cells[1], cells[2]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(work, ignore_errors=True)


main()
