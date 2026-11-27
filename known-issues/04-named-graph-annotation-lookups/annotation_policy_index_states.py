"""Policy conditions that look up an edge annotation in a named graph, across index states and run modes.

Usage: python3 -I annotation_policy_index_states.py <fluree-binary> <port> [output.json]

Plain Fluree only. Named graph <urn:g:data> holds two annotated memberships:
    alice memberOf ws {| status "active" |}        erin memberOf ws {| status "suspended" |}
<urn:x:rob> has exactly one write policy and tries to write a note about each member into <urn:g:data>.

  presence rule: allowed only if the member's membership is annotated status "active"
      ASK { $this memberOf ?ws {| status "active" |} }                      -> alice allowed, erin refused
  absence rule:  allowed only if the membership is NOT annotated status "suspended"
      ASK { $this memberOf ?ws . FILTER NOT EXISTS { $this memberOf ?ws {| status "suspended" |} } }
                                                                             -> alice allowed, erin refused
  optional rule: the same intent written with OPTIONAL
      ASK { $this memberOf ?ws . OPTIONAL { $this memberOf ?ws {| status ?st |} }
            FILTER(!BOUND(?st) || ?st != "suspended") }                      -> alice allowed, erin refused

Each rule is tried in three run modes, before and after the index step:
  direct-reindex     CLI --direct; `fluree reindex` (full rebuild)
  server-reindex     `fluree server run --memory`, requests through the server; POST /reindex (full rebuild)
  server-background  the server builds an incremental index after every commit (--reindex-min-bytes=1); no reindex
The decisions must be the same before and after: no data changes in between.

In each trial the administrator also runs the annotation lookup as an ordinary query of the named graph, by GRAPH
pattern and by FROM <ledger#graph>, before and after. Both must return alice's status "active".
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

cli, port = sys.argv[1], int(sys.argv[2])
output = sys.argv[3] if len(sys.argv) > 3 else None
LEDGER, G, ROB = 'repro:main', 'urn:g:data', 'urn:x:rob'
DATA = '''<urn:g:data> {
    <urn:x:alice> <urn:x:memberOf> <urn:x:ws> ~ <urn:x:m1> {| <urn:x:status> "active" |} .
    <urn:x:erin> <urn:x:memberOf> <urn:x:ws> ~ <urn:x:m2> {| <urn:x:status> "suspended" |} .
}
'''
RULES = {
    'presence': 'ASK { $this <urn:x:memberOf> ?ws {| <urn:x:status> "active" |} }',
    'absence': ('ASK { $this <urn:x:memberOf> ?ws . FILTER NOT EXISTS '
                '{ $this <urn:x:memberOf> ?ws {| <urn:x:status> "suspended" |} } }'),
    # The same "allowed unless suspended" rule written with OPTIONAL instead of NOT EXISTS.
    'optional': ('ASK { $this <urn:x:memberOf> ?ws . OPTIONAL { $this <urn:x:memberOf> ?ws '
                 '{| <urn:x:status> ?st |} } FILTER(!BOUND(?st) || ?st != "suspended") }'),
}
EXPECTED = {'alice': True, 'erin': False}
record = {'version': subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip(),
          'cases': []}


def trial(rule, mode, port):
    work = tempfile.mkdtemp(prefix='fluree-annpol-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
    log_path = os.path.join(work, 'server.log')
    base = 'http://127.0.0.1:%d' % port
    server = log = None
    where = ['--direct'] if mode == 'direct-reindex' else ['--remote', 'local']

    def run(*args):
        return subprocess.run([cli, '--quiet', '--no-color', *args], cwd=work, env=env, text=True,
                              capture_output=True, timeout=120)

    def wait_index(t):
        for _ in range(400):
            text = re.sub(r'\x1b\[[0-9;]*m', '', open(log_path).read())
            done = [int(x.split('=')[1]) for x in text.split()
                    if x.startswith('index_t=') and x.split('=')[1].isdigit()]
            if done and max(done) >= t:
                return
            time.sleep(0.05)
        raise AssertionError('no index reached t=%d' % t)

    counter = [0]

    def attempt(member):
        counter[0] += 1
        r = run('update', *where, '-l', LEDGER, '--format', 'sparql', '--as', ROB, '-e',
                'INSERT DATA { GRAPH <%s> { <urn:x:%s> <urn:x:note> "note %d" } }' % (G, member, counter[0]))
        return r.returncode == 0, ' '.join(r.stderr.split())[:160]

    def admin():
        out = {}
        for form, q in (('GRAPH', 'SELECT ?v WHERE { GRAPH <%s> { <urn:x:alice> <urn:x:memberOf> <urn:x:ws> '
                                  '{| <urn:x:status> ?v |} } }' % G),
                        ('FROM', 'SELECT ?v FROM <%s#%s> WHERE { <urn:x:alice> <urn:x:memberOf> <urn:x:ws> '
                                 '{| <urn:x:status> ?v |} }' % (LEDGER, G))):
            r = run('query', *where, '-l', LEDGER, '--sparql', '--format', 'json', '-e', q)
            try:
                out[form] = [b['v']['value'] for b in json.loads(r.stdout)['results']['bindings']]
            except (ValueError, KeyError, TypeError):
                out[form] = 'error: ' + ' '.join((r.stderr or r.stdout).split())[:120]
        return out

    try:
        assert run('init').returncode == 0
        if mode != 'direct-reindex':
            extra = ['--reindex-min-bytes=1'] if mode == 'server-background' else []
            log = open(log_path, 'w')
            server = subprocess.Popen([cli, 'server', 'run', '--memory', '--listen-addr', '127.0.0.1:%d' % port,
                                       '--log-level', 'info', '--', *extra], cwd=work, env=env, stdout=log,
                                      stderr=subprocess.STDOUT)
            for _ in range(80):
                try:
                    urllib.request.urlopen(base + '/health', timeout=1)
                    break
                except OSError:
                    time.sleep(0.25)
            assert run('remote', 'add', 'local', base).returncode == 0
            r = run('create', LEDGER, '--remote', 'local')
        else:
            r = run('create', LEDGER)
        assert r.returncode == 0, r.stderr
        r = run('insert', *where, '-l', LEDGER, '--format', 'trig', '-e', DATA)
        assert r.returncode == 0, r.stderr
        policy = {'@context': {'f': 'https://ns.flur.ee/db#'}, '@graph': [
            {'@id': 'urn:x:rule', '@type': ['f:AccessPolicy', 'urn:x:Policies'], 'f:action': [{'@id': 'f:modify'}],
             'f:query': {'@type': 'https://ns.flur.ee/db#sparql', '@value': RULES[rule]}},
            {'@id': ROB, 'f:policyClass': [{'@id': 'urn:x:Policies'}]}]}
        r = run('insert', *where, '-l', LEDGER, '-e', json.dumps(policy))
        assert r.returncode == 0, r.stderr

        if mode == 'server-background':
            # "Before" here means: the data is in an incremental index already (built after each commit).
            wait_index(2)
        admin_before = admin()
        before = {m: attempt(m) for m in EXPECTED}
        if mode == 'direct-reindex':
            r = run('reindex', '--direct', LEDGER)
            assert r.returncode == 0, r.stderr
            step = 'fluree reindex'
        elif mode == 'server-reindex':
            req = urllib.request.Request(base + '/v1/fluree/reindex', data=json.dumps({'ledger': LEDGER}).encode(),
                                         headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(req, timeout=120) as resp:
                assert resp.status == 200
            step = 'POST /reindex'
        else:
            # Let the background index absorb the notes written so far, then try again.
            time.sleep(1.5)
            step = 'background incremental index'
        after = {m: attempt(m) for m in EXPECTED}
        admin_after = admin()
    finally:
        if server:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
            log.close()
    queries_ok = all(x == ['active'] for x in list(admin_before.values()) + list(admin_after.values()))
    record['cases'].append({'rule': rule, 'mode': mode, 'adminQueriesBefore': admin_before,
                            'adminQueriesAfter': admin_after, 'correct': queries_ok})
    print('%-9s %-18s ordinary queries before=%s after=%s %s' % (rule, mode, admin_before, admin_after,
                                                                '' if queries_ok else 'WRONG'))
    for m, expected in EXPECTED.items():
        row = {'rule': rule, 'mode': mode, 'indexStep': step, 'member': m, 'expectedAllowed': expected,
               'allowedBefore': before[m][0], 'allowedAfter': after[m][0], 'errorAfter': after[m][1],
               'correct': before[m][0] == expected and after[m][0] == expected}
        record['cases'].append(row)
        word = lambda x: 'allowed' if x else 'refused'
        flag = ''
        if not row['correct']:
            flag = 'WRONGLY ALLOWED' if (after[m][0] and not expected) or (before[m][0] and not expected) \
                else 'wrongly refused'
        print('%-9s %-18s %-6s docs=%-8s before=%-8s after=%-8s %s' % (
            rule, mode, m, word(expected), word(before[m][0]), word(after[m][0]), flag))


print(record['version'])
n = 0
for rule in RULES:
    for mode in ('direct-reindex', 'server-reindex', 'server-background'):
        trial(rule, mode, port + n)
        n += 1
wrong = sum(not c['correct'] for c in record['cases'])
print('wrong decisions:', wrong)
if output:
    with open(output, 'w') as out:
        json.dump(record, out, indent=1)
sys.exit(1 if wrong else 0)
