"""Do edge-annotation lookups in a named graph give the same answer before and after an index build?

Usage: python3 -I named_graph_annotation_repro.py <fluree-binary> [output.json]

Plain Fluree CLI in --direct mode on a fresh temporary ledger; no server, no application config.

docs/concepts/edge-annotations.md: an annotation on an edge inside a named graph "is written into that same graph,
keeping the edge's graph identity". The ledger holds:
  member   in named graph <urn:g:data>   the same edge in the default graph
  alice    status "active"               status "default-graph"
  bob      status "inactive"             (none)
  carol    status "inactive"             status "active"
  dave     status "active"               (none)

Part 1 runs each query as the administrator before any index exists and again after a full `fluree reindex`, which
builds the annotation index. The answers must not change, and a query of the named graph must never return the
default graph's annotation.

Part 2 uses a write policy whose condition is an annotation lookup: <urn:x:rob> may write a note about a member in
<urn:g:data> only if that member's membership there is annotated status "active". The policy condition is evaluated
in the written fact's graph. The same writes are tried before and after the reindex: alice and dave must be allowed,
bob and carol refused.

REPRO_INDEX_COMMAND=index uses the incremental `fluree index` instead; FLUREE_ANNOTATION_LANE (arena, enumerate,
chain) is Fluree's own switch for forcing one lookup strategy.
"""
import json
import os
import subprocess
import sys
import tempfile

cli = sys.argv[1]
output = sys.argv[2] if len(sys.argv) > 2 else None
LEDGER = 'repro:main'
G = 'urn:g:data'
ROB = 'urn:x:rob'
RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'

DATA = '''<urn:x:alice> <urn:x:memberOf> <urn:x:ws> ~ <urn:x:d1> {| <urn:x:status> "default-graph" |} .
<urn:x:carol> <urn:x:memberOf> <urn:x:ws> ~ <urn:x:d3> {| <urn:x:status> "active" |} .
<urn:g:data> {
    <urn:x:alice> <urn:x:memberOf> <urn:x:ws> ~ <urn:x:m1> {| <urn:x:status> "active" |} .
    <urn:x:bob> <urn:x:memberOf> <urn:x:ws> ~ <urn:x:m2> {| <urn:x:status> "inactive" |} .
    <urn:x:carol> <urn:x:memberOf> <urn:x:ws> ~ <urn:x:m3> {| <urn:x:status> "inactive" |} .
    <urn:x:dave> <urn:x:memberOf> <urn:x:ws> ~ <urn:x:m4> {| <urn:x:status> "active" |} .
}
'''

EDGE = '<urn:x:alice> <urn:x:memberOf> <urn:x:ws>'
QUERIES = [
    ('GRAPH pattern, constant edge, variable annotation',
     'SELECT ?v WHERE { GRAPH <%s> { %s ~ ?a {| <urn:x:status> ?v |} } }' % (G, EDGE), ['active']),
    ('GRAPH pattern, constant edge, anonymous annotation',
     'SELECT ?v WHERE { GRAPH <%s> { %s {| <urn:x:status> ?v |} } }' % (G, EDGE), ['active']),
    ('GRAPH pattern, variable subject',
     'SELECT ?v WHERE { GRAPH <%s> { ?s <urn:x:memberOf> <urn:x:ws> {| <urn:x:status> ?v |} } } ORDER BY ?v' % G,
     ['active', 'active', 'inactive', 'inactive']),
    ('GRAPH pattern, rdf:reifies form',
     'SELECT ?v WHERE { GRAPH <%s> { ?a <%sreifies> <<( ?s <urn:x:memberOf> ?o )>> ; <urn:x:status> ?v } } ORDER BY ?v'
     % (G, RDF), ['active', 'active', 'inactive', 'inactive']),
    ('FROM <ledger#graph>, constant edge, variable annotation',
     'SELECT ?v FROM <%s#%s> WHERE { %s ~ ?a {| <urn:x:status> ?v |} }' % (LEDGER, G, EDGE), ['active']),
    ('FROM <ledger#graph>, constant edge, anonymous annotation',
     'SELECT ?v FROM <%s#%s> WHERE { %s {| <urn:x:status> ?v |} }' % (LEDGER, G, EDGE), ['active']),
    ('FROM <ledger#graph>, variable subject',
     'SELECT ?v FROM <%s#%s> WHERE { ?s <urn:x:memberOf> <urn:x:ws> {| <urn:x:status> ?v |} } ORDER BY ?v'
     % (LEDGER, G), ['active', 'active', 'inactive', 'inactive']),
    ('FROM <ledger#graph>, variable subject and annotation value filter',
     'SELECT ?s FROM <%s#%s> WHERE { ?s <urn:x:memberOf> ?o {| <urn:x:status> "active" |} }' % (LEDGER, G),
     ['urn:x:alice', 'urn:x:dave']),
    ('FROM <ledger#graph>, rdf:reifies form',
     'SELECT ?v FROM <%s#%s> WHERE { ?a <%sreifies> <<( ?s <urn:x:memberOf> ?o )>> ; <urn:x:status> ?v } ORDER BY ?v'
     % (LEDGER, G, RDF), ['active', 'active', 'inactive', 'inactive']),
    ('default graph, constant edge (control)',
     'SELECT ?v WHERE { %s {| <urn:x:status> ?v |} }' % EDGE, ['default-graph']),
]

record = {'version': subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip(),
          'queries': [], 'policy': []}
work = tempfile.mkdtemp(prefix='fluree-annotation-')
env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)


def run(*args):
    return subprocess.run([cli, '--quiet', '--no-color', *args], cwd=work, env=env, text=True, capture_output=True,
                          timeout=120)


def rows(query, *who):
    r = run('query', '--direct', '-l', LEDGER, '--sparql', '--format', 'json', *who, '-e', query)
    try:
        bindings = json.loads(r.stdout)['results']['bindings']
        return sorted(next(iter(b.values()))['value'] for b in bindings if b)
    except (ValueError, KeyError, TypeError, StopIteration):
        return 'error: ' + ' '.join((r.stderr or r.stdout).split())[:160]


MEMBERS = (('alice', True), ('bob', False), ('carol', False), ('dave', True))


def notes(text):
    # A fresh note each time, so the write is never a no-op.
    return {m: run('update', '--direct', '-l', LEDGER, '--format', 'sparql', '--as', ROB, '-e',
                   'INSERT DATA { GRAPH <%s> { <urn:x:%s> <urn:x:note> "%s" } }' % (G, m, text)).returncode == 0
            for m, _ in MEMBERS}


assert run('init').returncode == 0
assert run('create', LEDGER).returncode == 0
r = run('insert', '--direct', '-l', LEDGER, '--format', 'trig', '-e', DATA)
assert r.returncode == 0, r.stderr
policy = {'@context': {'f': 'https://ns.flur.ee/db#'}, '@graph': [
    {'@id': 'urn:x:rule', '@type': ['f:AccessPolicy', 'urn:x:Policies'], 'f:action': [{'@id': 'f:modify'}],
     'f:query': {'@type': 'https://ns.flur.ee/db#sparql',
                 '@value': 'ASK { $this <urn:x:memberOf> ?ws {| <urn:x:status> "active" |} }'}},
    {'@id': ROB, 'f:policyClass': [{'@id': 'urn:x:Policies'}]}]}
r = run('insert', '--direct', '-l', LEDGER, '-e', json.dumps(policy))
assert r.returncode == 0, r.stderr

before = [rows(q) for _, q, _ in QUERIES]
policy_before = notes('seen')
r = run(os.environ.get('REPRO_INDEX_COMMAND', 'reindex'), '--direct', LEDGER)
assert r.returncode == 0, r.stderr
after = [rows(q) for _, q, _ in QUERIES]
policy_after = notes('seen again')

print(record['version'])
wrong = 0
for (name, query, expected), b, a in zip(QUERIES, before, after):
    ok = b == expected and a == expected
    wrong += not ok
    record['queries'].append({'query': name, 'sparql': query, 'expected': expected, 'beforeIndex': b,
                              'afterIndex': a, 'correct': ok})
    print('%-62s before=%-24s after=%-24s %s' % (name, b, a, '' if ok else 'WRONG (expected %s)' % expected))
for who, expected in MEMBERS:
    b, a = policy_before[who], policy_after[who]
    ok = b == expected and a == expected
    wrong += not ok
    record['policy'].append({'member': who, 'expectedAllowed': expected, 'allowedBeforeIndex': b,
                             'allowedAfterIndex': a, 'correct': ok})
    word = lambda x: 'allowed' if x else 'refused'
    print('%-62s before=%-24s after=%-24s %s' % ('policy: rob notes ' + who, word(b), word(a),
                                                 '' if ok else 'WRONG (expected %s)' % word(expected)))
print('wrong answers:', wrong)
if output:
    with open(output, 'w') as out:
        json.dump(record, out, indent=1)
sys.exit(1 if wrong else 0)
