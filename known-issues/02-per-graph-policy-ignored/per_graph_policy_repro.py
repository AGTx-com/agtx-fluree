"""Per-graph policy defaults (f:GraphConfig + f:policyDefaults) are not applied to writes.

Usage: python3 -I per_graph_policy_repro.py <fluree-binary> [output.json]

Plain Fluree CLI in --direct mode on fresh temporary ledgers; no server, no application config.

A request with no identity is the local administrator in --direct mode and is never subject to policy, so those
rows are informational. The test subject is the identity <urn:x:rob> (`--as`), which has no policy class.

Case "ledger-locked" (control): ledger-wide f:defaultAllow false with f:OverrideNone. Rob's writes and reads must be
refused. It shows that an `--as` request is subject to the config's policy defaults.

Case "readme": the config from docs/ledger-config/README.md, "Different policy per graph", copied verbatim apart
from the ledger name: ledger-wide f:defaultAllow true, and <http://example.org/sensitive> overridden to
f:defaultAllow false with f:OverrideNone. The README says: "The sensitive graph requires policy rules and cannot be
overridden at query time. All other graphs remain open." No policy rule exists, so Rob's writes into the sensitive
graph should be refused and its facts hidden from him, while the other graph stays open to him.
"""
import json
import os
import subprocess
import sys
import tempfile

cli = sys.argv[1]
output = sys.argv[2] if len(sys.argv) > 2 else None
LEDGER = 'repro:main'
SENSITIVE = 'http://example.org/sensitive'
OPEN = 'http://example.org/open'
ROB = 'urn:x:rob'

LOCKED = '''@prefix f: <https://ns.flur.ee/db#> .
GRAPH <urn:fluree:repro:main#config> {
  <urn:fluree:repro:main:config:ledger> a f:LedgerConfig ;
    f:policyDefaults [
      f:defaultAllow false ;
      f:overrideControl f:OverrideNone
    ] .
}
'''

README = '''@prefix f: <https://ns.flur.ee/db#> .

GRAPH <urn:fluree:repro:main#config> {
  <urn:fluree:repro:main:config:ledger> a f:LedgerConfig ;
    f:policyDefaults [
      f:defaultAllow true ;
      f:overrideControl f:OverrideAll
    ] ;
    f:graphOverrides (
      [ a f:GraphConfig ;
        f:targetGraph <http://example.org/sensitive> ;
        f:policyDefaults [
          f:defaultAllow false ;
          f:policySource [
            a f:GraphRef ;
            f:graphSource [ f:graphSelector f:defaultGraph ]
          ] ;
          f:overrideControl f:OverrideNone
        ]
      ]
    ) .
}
'''

record = {'version': subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip(),
          'cases': {}}


def case(name, config):
    work = tempfile.mkdtemp(prefix='fluree-pergraph-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
    steps = []
    record['cases'][name] = steps

    def run(step, *args, expected=None):
        r = subprocess.run([cli, '--quiet', '--no-color', *args], cwd=work, env=env, text=True, capture_output=True,
                           timeout=120)
        allowed = r.returncode == 0
        steps.append({'step': step, 'args': list(args), 'exitCode': r.returncode, 'stdout': r.stdout.strip()[:600],
                      'stderr': r.stderr.strip()[:600], 'expectedAllowed': expected, 'allowed': allowed,
                      'asDocumented': None if expected is None else allowed == expected})
        return r

    def write(step, graph, value, *who, expected):
        update = 'INSERT DATA { GRAPH <%s> { <urn:x:item> <urn:x:note> "%s" } }' % (graph, value)
        return run(step, 'update', '--direct', '-l', LEDGER, '--format', 'sparql', *who, '-e', update,
                   expected=expected)

    def read(step, graph, *who, form='pattern', expected_rows=None):
        if form == 'pattern':
            query = 'SELECT ?o WHERE { GRAPH <%s> { ?s <urn:x:note> ?o } } ORDER BY ?o' % graph
        else:
            query = 'SELECT ?o FROM <%s#%s> WHERE { ?s <urn:x:note> ?o } ORDER BY ?o' % (LEDGER, graph)
        r = run(step, 'query', '--direct', '-l', LEDGER, '--sparql', '--format', 'json', *who, '-e', query)
        last = steps[-1]
        try:
            rows = [b['o']['value'] for b in json.loads(r.stdout)['results']['bindings']]
        except (ValueError, KeyError, TypeError):
            rows = None
        last['rows'] = rows
        last['expectedRows'] = expected_rows
        if expected_rows is not None:
            last['asDocumented'] = rows is not None and len(rows) == expected_rows or (rows is None and expected_rows == 0)
        return rows

    assert run('init', 'init').returncode == 0
    assert run('create', 'create', LEDGER).returncode == 0
    # Seed before the config exists: the identity record, and one fact in each graph.
    seed = ('INSERT DATA { <%s> a <urn:x:User> . GRAPH <%s> { <urn:x:seed> <urn:x:note> "seeded" } '
            'GRAPH <%s> { <urn:x:seed> <urn:x:note> "seeded" } }' % (ROB, SENSITIVE, OPEN))
    assert run('seed', 'update', '--direct', '-l', LEDGER, '--format', 'sparql', '-e', seed).returncode == 0
    r = run('config', 'insert', '--direct', '-l', LEDGER, '--format', 'trig', '-e', config)
    assert r.returncode == 0, r.stderr
    return run, write, read


# Control: a ledger-wide lock refuses Rob's requests.
run, write, read = case('ledger-locked', LOCKED)
write('no identity (administrator) -> sensitive graph', SENSITIVE, 'a', expected=None)
write('as rob -> sensitive graph', SENSITIVE, 'b', '--as', ROB, expected=False)
write('as rob with --default-allow -> sensitive graph', SENSITIVE, 'c', '--as', ROB, '--default-allow',
      expected=False)
read('as rob: read sensitive graph, GRAPH pattern', SENSITIVE, '--as', ROB, expected_rows=0)

# The README's per-graph lock.
run, write, read = case('readme', README)
write('no identity (administrator) -> sensitive graph', SENSITIVE, 'a', expected=None)
write('as rob -> open graph', OPEN, 'b', '--as', ROB, expected=True)
write('as rob -> sensitive graph', SENSITIVE, 'c', '--as', ROB, expected=False)
write('as rob with --default-allow -> sensitive graph', SENSITIVE, 'd', '--as', ROB, '--default-allow',
      expected=False)
read('as rob: read open graph, GRAPH pattern', OPEN, '--as', ROB, expected_rows=2)
read('as rob: read sensitive graph, GRAPH pattern', SENSITIVE, '--as', ROB, expected_rows=0)
read('as rob: read sensitive graph, FROM <ledger#graph>', SENSITIVE, '--as', ROB, form='from', expected_rows=0)
read('no identity (administrator): sensitive graph contents', SENSITIVE)

print(record['version'])
wrong = 0
for name, steps in record['cases'].items():
    for s in steps:
        if s['step'] in ('init', 'create', 'seed', 'config'):
            continue
        if 'rows' in s:
            got = 'rows=%s' % (s['rows'] if s['rows'] is not None else 'error: ' + ' '.join(s['stderr'].split())[:60])
            want = '' if s['expectedRows'] is None else '(docs: %d rows)' % s['expectedRows']
        else:
            got = 'allowed' if s['allowed'] else 'refused'
            want = '' if s['expectedAllowed'] is None else '(docs: %s)' % ('allowed' if s['expectedAllowed'] else 'refused')
        verdict = '' if s['asDocumented'] is None else ('as documented' if s['asDocumented'] else 'NOT AS DOCUMENTED')
        wrong += s['asDocumented'] is False
        print('%-14s %-52s %-38s %-16s %s' % (name, s['step'], got, want, verdict))
print('results that contradict the docs:', wrong)
if output:
    with open(output, 'w') as out:
        json.dump(record, out, indent=1)
sys.exit(1 if wrong else 0)
