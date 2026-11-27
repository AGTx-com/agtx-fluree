"""Minimal check: an ordinary policy condition refuses every identity beyond the first 1,000 that satisfy it.

Usage: python3 -I first_batch_minimal_repro.py <fluree-binary> [users] [output.json]

Plain Fluree CLI in --direct mode on a fresh temporary ledger; no server. Written independently of
first_batch_breadth_repro.py to confirm its headline result.

`users` users (default 1200) are stored, every one a User in a department with level 5. One policy applies to all:
    ASK { $identity <urn:x:kind> <urn:x:User> ; <urn:x:dept> ?d ; <urn:x:level> ?lv FILTER(?lv >= 3) }
Every user satisfies it, so every user must be allowed to write and to read. The script tries the first user,
user 999, user 1000 and the last user, and also runs the condition as an ordinary query for each.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

cli = sys.argv[1]
users = int(sys.argv[2]) if len(sys.argv) > 2 else 1200
output = sys.argv[3] if len(sys.argv) > 3 else None
LEDGER = 'repro:main'
CONDITION = 'ASK { $identity <urn:x:kind> <urn:x:User> ; <urn:x:dept> ?d ; <urn:x:level> ?lv FILTER(?lv >= 3) }'
work = tempfile.mkdtemp(prefix='fluree-firstbatch-min-')
env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
record = {'version': subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip(),
          'users': users, 'checks': []}


def run(*args):
    return subprocess.run([cli, '--quiet', '--no-color', *args], cwd=work, env=env, text=True, capture_output=True,
                          timeout=300)


def user(i):
    return 'urn:u:%05d' % i


try:
    assert run('init').returncode == 0
    assert run('create', LEDGER).returncode == 0
    rows = ''.join('<%s> a <urn:x:Thing> ; <urn:x:kind> <urn:x:User> ; <urn:x:dept> <urn:d:%d> ; <urn:x:level> 5 ; '
                   '<https://ns.flur.ee/db#policyClass> <urn:x:Policies> .\n' % (user(i), i % 7) for i in range(users))
    path = os.path.join(work, 'users.ttl')
    with open(path, 'w') as f:
        f.write(rows)
    r = run('insert', '--direct', '-l', LEDGER, '--format', 'turtle', '-f', path)
    assert r.returncode == 0, r.stderr[-400:]
    policy = {'@context': {'f': 'https://ns.flur.ee/db#'}, '@graph': [
        {'@id': 'urn:x:rule', '@type': ['f:AccessPolicy', 'urn:x:Policies'],
         'f:action': [{'@id': 'f:modify'}, {'@id': 'f:view'}],
         'f:query': {'@type': 'https://ns.flur.ee/db#sparql', '@value': CONDITION}}]}
    r = run('insert', '--direct', '-l', LEDGER, '-e', json.dumps(policy))
    assert r.returncode == 0, r.stderr[-400:]
    r = run('insert', '--direct', '-l', LEDGER, '--format', 'turtle', '-e', '<urn:x:doc> <urn:x:title> "hello" .')
    assert r.returncode == 0, r.stderr[-400:]

    print(record['version'], '| users:', users)
    print('%-12s %-14s %-10s %-10s' % ('user', 'query answer', 'write', 'read'))
    for i in sorted({0, min(999, users - 1), min(1000, users - 1), users - 1}):
        who = user(i)
        q = run('query', '--direct', '-l', LEDGER, '--sparql', '--format', 'json', '-e',
                CONDITION.replace('$identity', '<%s>' % who))
        try:
            answer = json.loads(q.stdout).get('boolean')
        except ValueError:
            answer = None
        w = run('update', '--direct', '-l', LEDGER, '--format', 'sparql', '--as', who, '-e',
                'INSERT DATA { <urn:x:note-%d> <urn:x:text> "written by user %d" }' % (i, i))
        rd = run('query', '--direct', '-l', LEDGER, '--sparql', '--format', 'json', '--as', who, '-e',
                 'SELECT ?t WHERE { <urn:x:doc> <urn:x:title> ?t }')
        try:
            can_read = len(json.loads(rd.stdout)['results']['bindings']) == 1
        except (ValueError, KeyError, TypeError):
            can_read = None
        check = {'user': i, 'queryAnswer': answer, 'writeAllowed': w.returncode == 0, 'canRead': can_read,
                 'consistent': answer == (w.returncode == 0) == bool(can_read)}
        record['checks'].append(check)
        print('%-12s %-14s %-10s %-10s %s' % (i, answer, 'allowed' if check['writeAllowed'] else 'refused',
                                              'yes' if can_read else 'no', '' if check['consistent'] else 'WRONG'))
finally:
    shutil.rmtree(work, ignore_errors=True)
wrong = sum(not c['consistent'] for c in record['checks'])
print('users decided against the condition\'s own answer:', wrong, 'of', len(record['checks']))
if output:
    with open(output, 'w') as out:
        json.dump(record, out, indent=1)
sys.exit(1 if wrong else 0)
