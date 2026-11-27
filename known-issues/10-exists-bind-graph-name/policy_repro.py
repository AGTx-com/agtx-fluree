"""Does a view policy whose FILTER EXISTS computes its GRAPH name with BIND decide like the same query run alone?

Usage: python3 -I policy_repro.py <fluree-binary> [output.json]

Plain Fluree CLI in --direct mode, one fresh temporary ledger per condition and mode; no server, no application
config. Modes: "novelty" (nothing indexed) and "indexed" (`fluree reindex` after the data and policy are written).

Two workspaces, a document in each, and memberships, each workspace's in its own named graph. bob is a member of ws1
in ws1's graph. alice is a member of ws2 in ws2's graph; her ws1 membership is recorded only in ws2's graph (a foreign
value the graph-correlated conditions must not count for ws1). dana has no membership.

The only policy is a view policy on ex:workspace. Each reader runs `SELECT ?d WHERE { ?d ex:workspace ?w }` and the
visible documents are compared with the documents for which the same condition, run as an ordinary query by the
administrator with the identity and value written in, answers true.
"""
import json
import os
import subprocess
import sys
import tempfile

cli = sys.argv[1]
output = sys.argv[2] if len(sys.argv) > 2 else None
LEDGER = 'repro:main'
EX = 'http://example.org/ns/'

SEED = '''PREFIX ex: <http://example.org/ns/>
INSERT DATA {
  ex:doc1 ex:workspace ex:ws1 .
  ex:doc2 ex:workspace ex:ws2 .
  GRAPH <http://example.org/members/ws1> { ex:bob ex:memberOf ex:ws1 . ex:alice ex:memberOf ex:ws2 . }
  GRAPH <http://example.org/members/ws2> { ex:alice ex:memberOf ex:ws2 . ex:alice ex:memberOf ex:ws1 . }
}'''

PREFIX = 'PREFIX ex: <http://example.org/ns/> '
CONDITIONS = [
    ('BIND computes the GRAPH name (the defect shape)',
     PREFIX + 'ASK { FILTER EXISTS { BIND(IRI(CONCAT("http://example.org/members/", '
     'STRAFTER(STR($value), "http://example.org/ns/"))) AS ?g) GRAPH ?g { $identity ex:memberOf $value } } }'),
    ('BIND computes the expected name, compared after GRAPH ?g (control)',
     PREFIX + 'ASK { FILTER EXISTS { BIND(CONCAT("http://example.org/members/", '
     'STRAFTER(STR($value), "http://example.org/ns/")) AS ?expected) GRAPH ?g { $identity ex:memberOf $value } '
     'FILTER(STR(?g) = ?expected) } }'),
    ('BIND computes the GRAPH name, no FILTER EXISTS wrapper',
     PREFIX + 'ASK { BIND(IRI(CONCAT("http://example.org/members/", '
     'STRAFTER(STR($value), "http://example.org/ns/"))) AS ?g) GRAPH ?g { $identity ex:memberOf $value } }'),
    ('membership in any graph (control)',
     PREFIX + 'ASK { FILTER EXISTS { GRAPH ?g { $identity ex:memberOf $value } } }'),
    ('NOT EXISTS, BIND computes the GRAPH name (hide from members)',
     PREFIX + 'ASK { FILTER NOT EXISTS { BIND(IRI(CONCAT("http://example.org/members/", '
     'STRAFTER(STR($value), "http://example.org/ns/"))) AS ?g) GRAPH ?g { $identity ex:memberOf $value } } }'),
    ('NOT EXISTS, expected name compared after GRAPH ?g (control)',
     PREFIX + 'ASK { FILTER NOT EXISTS { BIND(CONCAT("http://example.org/members/", '
     'STRAFTER(STR($value), "http://example.org/ns/")) AS ?expected) GRAPH ?g { $identity ex:memberOf $value } '
     'FILTER(STR(?g) = ?expected) } }'),
]
READERS = ['bob', 'alice', 'dana']
DOCS = {'doc1': 'ws1', 'doc2': 'ws2'}

record = {'version': subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip(),
          'cases': []}


def one(name, ask, mode):
    work = tempfile.mkdtemp(prefix='fluree-boundgraph-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
    steps = []

    def run(step, *args):
        r = subprocess.run([cli, '--quiet', '--no-color', *args], cwd=work, env=env, text=True, capture_output=True,
                           timeout=120)
        steps.append({'step': step, 'args': list(args), 'exitCode': r.returncode, 'stdout': r.stdout.strip()[:800],
                      'stderr': r.stderr.strip()[:800]})
        return r

    assert run('init', 'init').returncode == 0
    assert run('create', 'create', LEDGER).returncode == 0
    r = run('seed', 'update', '--direct', '-l', LEDGER, '--format', 'sparql', '-e', SEED)
    assert r.returncode == 0, r.stderr
    graph = [{'@id': EX + 'rule', '@type': ['f:AccessPolicy', EX + 'Policies'],
              'f:action': [{'@id': 'f:view'}],
              'f:onProperty': [{'@id': EX + 'workspace'}],
              'f:query': {'@type': 'https://ns.flur.ee/db#sparql', '@value': ask}}]
    graph += [{'@id': EX + who, 'f:policyClass': [{'@id': EX + 'Policies'}]} for who in READERS]
    r = run('policy', 'insert', '--direct', '-l', LEDGER, '-e',
            json.dumps({'@context': {'f': 'https://ns.flur.ee/db#'}, '@graph': graph}))
    assert r.returncode == 0, r.stderr
    if mode == 'indexed':
        r = run('reindex', 'reindex', '--direct', LEDGER)
        assert r.returncode == 0, r.stderr
    rows = []
    for who in READERS:
        expected = []
        for doc, ws in DOCS.items():
            plain = run('ordinary query %s %s' % (who, doc), 'query', '--direct', '-l', LEDGER, '--sparql', '--format',
                        'json', '-e', ask.replace('$identity', '<%s%s>' % (EX, who)).replace('$value', '<%s%s>' % (EX, ws)))
            try:
                if json.loads(plain.stdout).get('boolean'):
                    expected.append(doc)
            except ValueError:
                expected.append('?query-error')
        seen = run('read as %s' % who, 'query', '--direct', '-l', LEDGER, '--sparql', '--format', 'json', '--as',
                   EX + who, '-e', PREFIX + 'SELECT ?d WHERE { ?d ex:workspace ?w }')
        try:
            visible = sorted(b['d']['value'].rsplit('/', 1)[-1].rsplit(':', 1)[-1] for b in json.loads(seen.stdout)['results']['bindings'])
        except (ValueError, KeyError):
            visible = ['?read-error: %s' % (seen.stderr.strip() or seen.stdout.strip())[:200]]
        rows.append({'reader': who, 'expected': sorted(expected), 'visible': visible,
                     'consistent': sorted(expected) == visible})
    record['cases'].append({'condition': name, 'mode': mode, 'ask': ask, 'readers': rows, 'steps': steps})
    return rows


print(record['version'])
wrong = 0
for name, ask in CONDITIONS:
    for mode in ('novelty', 'indexed'):
        rows = one(name, ask, mode)
        bad = [r for r in rows if not r['consistent']]
        wrong += len(bad)
        print('%-70s %-8s %s' % (name, mode, '; '.join('%s expected=%s saw=%s%s' % (
            r['reader'], r['expected'], r['visible'], '' if r['consistent'] else ' WRONG') for r in rows)))
print('reader decisions that contradict the ordinary query:', wrong)
if output:
    with open(output, 'w') as out:
        json.dump(record, out, indent=1)
sys.exit(1 if wrong else 0)
