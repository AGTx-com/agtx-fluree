"""Why is `VALUES ?g { <iri> } GRAPH ?g { $identity <p> true }` refused as a policy condition?

Usage: python3 -I values_graph_first_batch_repro.py <fluree-binary> [output.json]

Plain Fluree CLI in --direct mode, one fresh temporary ledger per shape, nothing indexed (novelty only); no server.
Each ASK is run
  1. as an ordinary query by the administrator, with the identity written in as a constant: the query's answer;
  2. as the f:query of the only f:modify policy that applies to <urn:x:rob>, who then inserts one fact.
The write must be allowed exactly when the ordinary query answers true. Exit status 1 if any shape disagrees.

Seed facts that matter here: <urn:x:rob> <urn:x:may> true is in graphs <urn:g:b> and <urn:grp:4>; another subject has
it in <urn:g:a> and <urn:grp:1>. An unbound `GRAPH ?g` visits the graphs in the order g:a, g:b, grp:1, grp:4, so
<urn:g:b> is the FIRST graph in which rob matches and <urn:grp:4> the SECOND.

Part 2 runs ordinary queries only (no policy): the same condition with the identity written as an explicit
`VALUES ?identity { <urn:x:rob> }`, as the policy executor injects it, plus `--explain` plans showing where the
planner puts each VALUES relative to the GRAPH operator.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

cli = sys.argv[1]
output = sys.argv[2] if len(sys.argv) > 2 else None
LEDGER = 'repro:main'
ROB = 'urn:x:rob'

SEED = '''INSERT DATA {
  <urn:x:rob> <urn:x:roleB> true .
  <urn:x:other> <urn:x:roleA> true .
  GRAPH <urn:g:a> { <urn:x:other> <urn:x:may> true }
  GRAPH <urn:g:b> { <urn:x:rob> <urn:x:may> true }
  <urn:grp:1> <urn:x:kind> <urn:x:Group> . <urn:grp:2> <urn:x:kind> <urn:x:Group> .
  <urn:grp:3> <urn:x:kind> <urn:x:Group> . <urn:grp:4> <urn:x:kind> <urn:x:Group> .
  <urn:x:rob> <urn:x:memberOf> <urn:grp:4> .
  <urn:x:first> <urn:x:memberOf> <urn:grp:1> .
  GRAPH <urn:grp:1> { <urn:x:first> <urn:x:may> true }
  GRAPH <urn:grp:4> { <urn:x:rob> <urn:x:may> true }
  GRAPH <urn:g:b> { <urn:x:rob> <urn:x:level> 5 ; <urn:x:name> "rob" ; <urn:x:team> <urn:grp:4> }
}'''

# (name, ASK used as the policy condition)
POLICY_SHAPES = [
    # The value names the SECOND graph in which rob matches.
    ('VALUES ?g {grp:4}, GRAPH ?g with $identity inside',
     'ASK { VALUES ?g { <urn:grp:4> } GRAPH ?g { $identity <urn:x:may> true } }'),
    # The value names the FIRST graph in which rob matches.
    ('VALUES ?g {g:b}, GRAPH ?g with $identity inside',
     'ASK { VALUES ?g { <urn:g:b> } GRAPH ?g { $identity <urn:x:may> true } }'),
    ('VALUES ?g {grp:4 g:b}, GRAPH ?g with $identity inside',
     'ASK { VALUES ?g { <urn:grp:4> <urn:g:b> } GRAPH ?g { $identity <urn:x:may> true } }'),
    ('VALUES ?g {g:a}: rob is not in that graph (control: must be refused)',
     'ASK { VALUES ?g { <urn:g:a> } GRAPH ?g { $identity <urn:x:may> true } }'),
    ('BIND ?g, GRAPH ?g with $identity inside',
     'ASK { BIND(<urn:grp:4> AS ?g) GRAPH ?g { $identity <urn:x:may> true } }'),
    ('GRAPH ?g with $identity inside, then VALUES ?g',
     'ASK { GRAPH ?g { $identity <urn:x:may> true } VALUES ?g { <urn:grp:4> } }'),
    ('two VALUES (?g and an unrelated ?x), GRAPH ?g with $identity inside',
     'ASK { VALUES ?g { <urn:grp:4> } VALUES ?x { 1 } GRAPH ?g { $identity <urn:x:may> true } }'),
    ('VALUES ?g, group { GRAPH ?g with $identity inside }',
     'ASK { VALUES ?g { <urn:grp:4> } { GRAPH ?g { $identity <urn:x:may> true } } }'),
    ('group { VALUES ?g }, GRAPH ?g with $identity inside',
     'ASK { { VALUES ?g { <urn:grp:4> } } GRAPH ?g { $identity <urn:x:may> true } }'),
    ('sub-SELECT around VALUES ?g, GRAPH ?g with $identity inside',
     'ASK { { SELECT ?g WHERE { VALUES ?g { <urn:grp:4> } } } GRAPH ?g { $identity <urn:x:may> true } }'),
    ('VALUES ?g, default-graph triple on ?g, GRAPH ?g with $identity inside',
     'ASK { VALUES ?g { <urn:grp:4> } $identity <urn:x:memberOf> ?g . GRAPH ?g { $identity <urn:x:may> true } }'),
    ('VALUES ?g, default-graph triple on ?g, no GRAPH',
     'ASK { VALUES ?g { <urn:grp:4> } $identity <urn:x:memberOf> ?g }'),
    # $identity is NOT used inside the GRAPH block.
    ('VALUES ?g, GRAPH ?g without $identity inside',
     'ASK { VALUES ?g { <urn:grp:4> } GRAPH ?g { ?who <urn:x:may> true } }'),
    ('VALUES ?g, GRAPH ?g without $identity inside, FILTER ?who = $identity',
     'ASK { VALUES ?g { <urn:grp:4> } GRAPH ?g { ?who <urn:x:may> true } FILTER(?who = $identity) }'),
    # VALUES binds a subject used inside a constant GRAPH.
    ('VALUES ?s, constant GRAPH using ?s',
     'ASK { VALUES ?s { <urn:x:rob> } GRAPH <urn:grp:4> { ?s <urn:x:may> true } }'),
    ('VALUES ?s, constant GRAPH using ?s, FILTER ?s = $identity',
     'ASK { VALUES ?s { <urn:x:rob> } GRAPH <urn:grp:4> { ?s <urn:x:may> true } FILTER(?s = $identity) }'),
    # VALUES-bound ?g only compared in a FILTER.
    ('VALUES ?g, constant GRAPH, FILTER on ?g',
     'ASK { VALUES ?g { <urn:grp:4> } GRAPH <urn:grp:4> { $identity <urn:x:may> true } FILTER(?g = <urn:grp:4>) }'),
    ('VALUES ?g, GRAPH ?h, FILTER ?g = ?h',
     'ASK { VALUES ?g { <urn:grp:4> } GRAPH ?h { $identity <urn:x:may> true } FILTER(?g = ?h) }'),
]

# Ordinary queries only: the identity as the explicit VALUES the policy executor injects.
ORDINARY = [
    'ASK { VALUES ?identity { <urn:x:rob> } VALUES ?g { <urn:grp:4> } GRAPH ?g { ?identity <urn:x:may> true } }',
    'ASK { VALUES ?g { <urn:grp:4> } VALUES ?identity { <urn:x:rob> } GRAPH ?g { ?identity <urn:x:may> true } }',
    'ASK { VALUES (?identity ?g) { (<urn:x:rob> <urn:grp:4>) } GRAPH ?g { ?identity <urn:x:may> true } }',
    'ASK { VALUES ?identity { <urn:x:rob> } BIND(<urn:grp:4> AS ?g) GRAPH ?g { ?identity <urn:x:may> true } }',
    'ASK { VALUES ?g { <urn:grp:4> } GRAPH ?g { <urn:x:rob> <urn:x:may> true } }',
    'ASK { VALUES ?identity { <urn:x:rob> } GRAPH ?g { ?identity <urn:x:may> true } VALUES ?g { <urn:grp:4> } }',
]
GRAPH_ORDER = 'SELECT ?g ?s WHERE { GRAPH ?g { ?s <urn:x:may> true } }'
EXPLAIN = [
    'ASK { VALUES ?identity { <urn:x:rob> } VALUES ?g { <urn:grp:4> } GRAPH ?g { ?identity <urn:x:may> true } }',
    'ASK { VALUES ?g { <urn:grp:4> } GRAPH ?g { <urn:x:rob> <urn:x:may> true } }',
    'ASK { VALUES ?identity { <urn:x:rob> } VALUES ?g { <urn:grp:4> } GRAPH ?g { ?who <urn:x:may> true } }',
]

record = {'version': subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip(),
          'policyShapes': [], 'ordinaryQueries': [], 'graphOrder': None, 'explain': []}


class Ledger:
    def __init__(self):
        self.work = tempfile.mkdtemp(prefix='fluree-values-graph-')
        self.env = dict(os.environ, HOME=self.work, XDG_CONFIG_HOME=self.work)
        self.steps = []

    def run(self, step, *args):
        r = subprocess.run([cli, '--quiet', '--no-color', *args], cwd=self.work, env=self.env, text=True,
                           capture_output=True, timeout=120)
        self.steps.append({'step': step, 'args': list(args), 'exitCode': r.returncode,
                           'stdout': r.stdout.strip()[:500], 'stderr': r.stderr.strip()[:500]})
        return r

    def seed(self):
        assert self.run('init', 'init').returncode == 0
        assert self.run('create', 'create', LEDGER).returncode == 0
        assert self.run('seed', 'update', '--direct', '-l', LEDGER, '--format', 'sparql', '-e', SEED).returncode == 0

    def ask(self, query):
        r = self.run('ordinary query', 'query', '--direct', '-l', LEDGER, '--sparql', '--format', 'json', '-e', query)
        try:
            return json.loads(r.stdout).get('boolean')
        except ValueError:
            return None

    def close(self):
        shutil.rmtree(self.work, ignore_errors=True)


def physical(node):
    """Operator names of an explain plan, root first."""
    names = [node.get('op')]
    for child in node.get('children', []):
        names += physical(child['node'])
    return names


print(record['version'])
print('Part 1: policy condition against the query answer (novelty only)')
wrong = 0
for name, ask in POLICY_SHAPES:
    ledger = Ledger()
    ledger.seed()
    policy = {'@context': {'f': 'https://ns.flur.ee/db#'}, '@graph': [
        {'@id': 'urn:x:rule', '@type': ['f:AccessPolicy', 'urn:x:Policies'],
         'f:action': [{'@id': 'f:modify'}],
         'f:query': {'@type': 'https://ns.flur.ee/db#sparql', '@value': ask}},
        {'@id': ROB, 'f:policyClass': [{'@id': 'urn:x:Policies'}]}]}
    r = ledger.run('policy', 'insert', '--direct', '-l', LEDGER, '-e', json.dumps(policy))
    assert r.returncode == 0, r.stderr
    answer = ledger.ask(ask.replace('$identity', '<%s>' % ROB))
    write = ledger.run('write as rob', 'update', '--direct', '-l', LEDGER, '--format', 'sparql', '--as', ROB, '-e',
                       'INSERT DATA { <urn:x:item> <urn:x:note> "written by rob" }')
    allowed = write.returncode == 0
    ok = answer == allowed
    wrong += not ok
    record['policyShapes'].append({'shape': name, 'mode': 'novelty', 'ask': ask, 'ordinaryQueryAnswer': answer,
                                   'writeAllowed': allowed, 'consistent': ok, 'steps': ledger.steps})
    print('%-72s query=%-5s write=%-7s %s' % (name, answer, 'allowed' if allowed else 'refused',
                                              '' if ok else 'INCONSISTENT'))
    ledger.close()

print('Part 2: ordinary queries with the identity as an explicit VALUES (no policy)')
for mode in ('novelty', 'indexed'):
    ledger = Ledger()
    ledger.seed()
    if mode == 'indexed':
        r = ledger.run('index', 'index', '--direct', LEDGER)
        assert r.returncode == 0, r.stderr
    for query in ORDINARY:
        answer = ledger.ask(query)
        record['ordinaryQueries'].append({'mode': mode, 'query': query, 'answer': answer})
        print('%-8s answer=%-5s %s' % (mode, answer, query))
    if mode == 'novelty':
        r = ledger.run('graph order', 'query', '--direct', '-l', LEDGER, '--sparql', '--format', 'json', '-e',
                       GRAPH_ORDER)
        try:
            rows = [[b['g']['value'], b['s']['value']] for b in json.loads(r.stdout)['results']['bindings']]
        except (ValueError, KeyError):
            rows = None
        record['graphOrder'] = rows
        print('order in which an unbound GRAPH ?g returns the graphs:', rows)
        for query in EXPLAIN:
            r = ledger.run('explain', 'query', '--direct', '-l', LEDGER, '--sparql', '--explain', '--format', 'json',
                           '-e', query)
            try:
                plan = json.loads(r.stdout[:r.stdout.rindex('}') + 1])['plan']
                ops = physical(plan['physical'])
                logical = [p.get('kind') + (str(p.get('vars')) if p.get('kind') == 'values' else '')
                           for p in plan['logical']]
            except (ValueError, KeyError):
                plan, ops, logical = None, None, None
            record['explain'].append({'query': query, 'logicalOrder': logical, 'physicalRootFirst': ops,
                                      'plan': plan})
            print('explain: %s\n   logical order: %s\n   physical, root first: %s' % (query, logical, ops))
    ledger.close()

print('cases where the policy decision contradicts the query answer:', wrong)
if output:
    with open(output, 'w') as out:
        json.dump(record, out, indent=1)
sys.exit(1 if wrong else 0)
