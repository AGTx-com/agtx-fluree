"""In which graph does a policy condition's unqualified pattern run: on writes, and on reads?

Usage: python3 -I policy_condition_graph_context.py <fluree-binary> [output.json]

Plain Fluree CLI in --direct mode on fresh temporary ledgers. One policy, with no GRAPH clause in its condition:
    ASK { $identity <urn:x:mayUse> true }
It is installed for both f:view and f:modify. Named graphs <urn:g:x> and <urn:g:y> each hold one item.

  setup "permission only in X":        <urn:x:rob> <urn:x:mayUse> true   is stored in <urn:g:x> only
  setup "permission in X and default": the same fact is also stored in the default graph

If the condition runs in the graph of the fact being checked, Rob can use X and not Y in both setups. The script
reports what Rob can write and what he can read, so the two paths can be compared.
"""
import json
import os
import subprocess
import sys
import tempfile

cli = sys.argv[1]
output = sys.argv[2] if len(sys.argv) > 2 else None
LEDGER, ROB = 'repro:main', 'urn:x:rob'
record = {'version': subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip(),
          'setups': {}}


def setup(name, in_default):
    work = tempfile.mkdtemp(prefix='fluree-graphctx-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)

    def run(*args):
        return subprocess.run([cli, '--quiet', '--no-color', *args], cwd=work, env=env, text=True,
                              capture_output=True, timeout=120)

    assert run('init').returncode == 0
    assert run('create', LEDGER).returncode == 0
    seed = ('INSERT DATA { GRAPH <urn:g:x> { <urn:x:rob> <urn:x:mayUse> true . <urn:x:itemX> <urn:x:note> "in X" } '
            'GRAPH <urn:g:y> { <urn:x:itemY> <urn:x:note> "in Y" } %s }'
            % ('<urn:x:rob> <urn:x:mayUse> true .' if in_default else ''))
    r = run('update', '--direct', '-l', LEDGER, '--format', 'sparql', '-e', seed)
    assert r.returncode == 0, r.stderr
    policy = {'@context': {'f': 'https://ns.flur.ee/db#'}, '@graph': [
        {'@id': 'urn:x:rule', '@type': ['f:AccessPolicy', 'urn:x:Policies'],
         'f:action': [{'@id': 'f:view'}, {'@id': 'f:modify'}],
         'f:query': {'@type': 'https://ns.flur.ee/db#sparql', '@value': 'ASK { $identity <urn:x:mayUse> true }'}},
        {'@id': ROB, 'f:policyClass': [{'@id': 'urn:x:Policies'}]}]}
    r = run('insert', '--direct', '-l', LEDGER, '-e', json.dumps(policy))
    assert r.returncode == 0, r.stderr

    result = {}
    for g in ('x', 'y'):
        r = run('query', '--direct', '-l', LEDGER, '--sparql', '--format', 'json', '--as', ROB, '-e',
                'SELECT ?o WHERE { GRAPH <urn:g:%s> { ?s <urn:x:note> ?o } }' % g)
        try:
            result['read ' + g.upper()] = len(json.loads(r.stdout)['results']['bindings']) > 0
        except (ValueError, KeyError, TypeError):
            result['read ' + g.upper()] = 'error: ' + ' '.join(r.stderr.split())[:100]
    for g in ('x', 'y'):
        r = run('update', '--direct', '-l', LEDGER, '--format', 'sparql', '--as', ROB, '-e',
                'INSERT DATA { GRAPH <urn:g:%s> { <urn:x:new> <urn:x:note> "by rob" } }' % g)
        result['write ' + g.upper()] = r.returncode == 0
    record['setups'][name] = result
    return result


print(record['version'])
print('%-30s %-9s %-9s %-9s %-9s' % ('setup', 'write X', 'write Y', 'read X', 'read Y'))
consistent = True
for name, in_default in (('permission only in X', False), ('permission in X and default', True)):
    r = setup(name, in_default)
    word = lambda v: ('yes' if v else 'no') if isinstance(v, bool) else v
    print('%-30s %-9s %-9s %-9s %-9s' % (name, word(r['write X']), word(r['write Y']), word(r['read X']),
                                         word(r['read Y'])))
    consistent &= (r['write X'] == r['read X']) and (r['write Y'] == r['read Y'])
record['readsAndWritesAgree'] = consistent
print('reads and writes decided the same way:', consistent)
if output:
    with open(output, 'w') as out:
        json.dump(record, out, indent=1)
sys.exit(0 if consistent else 1)
