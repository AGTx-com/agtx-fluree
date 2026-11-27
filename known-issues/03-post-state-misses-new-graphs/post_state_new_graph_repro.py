"""Can a post-state policy condition see a named graph that the same write creates?

Usage: python3 -I post_state_new_graph_repro.py <fluree-binary> [output.json]

Plain Fluree CLI in --direct mode on fresh temporary ledgers; no server, no application config.

docs/security/policy-model.md, "Condition state": with f:queryState f:postState the condition reads "committed +
this transaction's staged flakes". The only f:modify policy that applies to <urn:x:rob> is such a condition, and it
requires the written item to carry an owner statement inside a named graph:

    ASK { GRAPH <GRAPH> { $this <urn:x:owner> $identity } }

Rob writes one item with that owner statement. The write must be allowed whether <GRAPH> already exists or is
created by this write.
"""
import json
import os
import subprocess
import sys
import tempfile

cli = sys.argv[1]
output = sys.argv[2] if len(sys.argv) > 2 else None
LEDGER = 'repro:main'
ROB = 'urn:x:rob'
GRAPH = 'urn:g:items'
record = {'version': subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip(),
          'cases': []}


def one(name, ask, write, graph_exists, expected, absence=False):
    work = tempfile.mkdtemp(prefix='fluree-poststate-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
    steps = []

    def run(step, *args):
        r = subprocess.run([cli, '--quiet', '--no-color', *args], cwd=work, env=env, text=True, capture_output=True,
                           timeout=120)
        steps.append({'step': step, 'args': list(args), 'exitCode': r.returncode, 'stdout': r.stdout.strip()[:500],
                      'stderr': r.stderr.strip()[:500]})
        return r

    assert run('init', 'init').returncode == 0
    assert run('create', 'create', LEDGER).returncode == 0
    if graph_exists:
        seed = 'INSERT DATA { GRAPH <%s> { <urn:x:earlier> <urn:x:owner> <urn:x:someone> } }' % GRAPH
        assert run('seed the graph', 'update', '--direct', '-l', LEDGER, '--format', 'sparql', '-e',
                   seed).returncode == 0
    policy = {'@context': {'f': 'https://ns.flur.ee/db#'}, '@graph': [
        {'@id': 'urn:x:rule', '@type': ['f:AccessPolicy', 'urn:x:Policies'],
         'f:action': [{'@id': 'f:modify'}],
         'f:queryState': {'@id': 'f:postState'},
         'f:query': {'@type': 'https://ns.flur.ee/db#sparql', '@value': ask}},
        {'@id': ROB, 'f:policyClass': [{'@id': 'urn:x:Policies'}]}]}
    r = run('policy', 'insert', '--direct', '-l', LEDGER, '-e', json.dumps(policy))
    assert r.returncode == 0, r.stderr
    w = run('write as rob', 'update', '--direct', '-l', LEDGER, '--format', 'sparql', '--as', ROB, '-e', write)
    allowed = w.returncode == 0
    record['cases'].append({'case': name, 'graphExistsBefore': graph_exists, 'ask': ask, 'write': write,
                            'expectedAllowed': expected, 'allowed': allowed, 'asDocumented': allowed == expected,
                            'steps': steps})
    print('%-74s %-8s (docs: %-7s) %s' % (name, 'allowed' if allowed else 'refused',
                                           'allowed' if expected else 'refused',
                                           'as documented' if allowed == expected else 'NOT AS DOCUMENTED'))
    return allowed == expected


print(record['version'])
OWNED = 'ASK { GRAPH <%s> { $this <urn:x:owner> $identity } }' % GRAPH
OWN_WRITE = 'INSERT DATA { GRAPH <%s> { <urn:x:item> <urn:x:owner> <%s> } }' % (GRAPH, ROB)
OTHER_WRITE = 'INSERT DATA { GRAPH <%s> { <urn:x:item> <urn:x:owner> <urn:x:mallory> } }' % GRAPH
# A condition that passes only when something is ABSENT from the post-state graph.
NOT_FLAGGED = ('ASK { $this <urn:x:owner> ?anyone . FILTER NOT EXISTS { GRAPH <%s> { $this <urn:x:flag> "blocked" } } }'
               % GRAPH)
FLAGGED_WRITE = ('INSERT DATA { GRAPH <%s> { <urn:x:item> <urn:x:owner> <%s> ; <urn:x:flag> "blocked" } }'
                 % (GRAPH, ROB))

ok = True
ok &= one('graph exists already; rob writes an item he owns (control)', OWNED, OWN_WRITE, True, True)
ok &= one('graph exists already; rob writes an item someone else owns (control)', OWNED, OTHER_WRITE, True, False)
ok &= one('graph is created by this write; rob writes an item he owns', OWNED, OWN_WRITE, False, True)
ok &= one('graph is created by this write; rob writes an item someone else owns', OWNED, OTHER_WRITE, False, False)
# Absence check: the write carries the "blocked" flag, so it must be refused in both situations.
ok &= one('absence rule, graph exists already; rob writes a blocked item (control)', NOT_FLAGGED, FLAGGED_WRITE,
          True, False)
ok &= one('absence rule, graph is created by this write; rob writes a blocked item', NOT_FLAGGED, FLAGGED_WRITE,
          False, False)
if output:
    with open(output, 'w') as out:
        json.dump(record, out, indent=1)
sys.exit(0 if ok else 1)
