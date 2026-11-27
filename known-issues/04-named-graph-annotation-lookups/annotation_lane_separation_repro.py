"""Why does a named-graph edge-annotation policy condition refuse through a server from the first attempt?

Usage: python3 -I annotation_lane_separation_repro.py <fluree-binary> <base-port> [output.json]
       (uses ports base-port .. base-port+11; stay inside 18750-18799)

Named graph <urn:g:data> holds   alice memberOf ws {| status "active" |}   erin memberOf ws {| status "suspended" |}.
<urn:x:rob> has one write policy and writes a note about one member into <urn:g:data>. Rules:
  presence   ASK { $this memberOf ?ws {| status "active" |} }                              alice yes, erin no
  optional   ASK { $this memberOf ?ws . OPTIONAL { $this memberOf ?ws {| status ?st |} }
                   FILTER(!BOUND(?st) || ?st != "suspended") }                              alice yes, erin no
  notexists  ASK { $this memberOf ?ws . FILTER NOT EXISTS { $this memberOf ?ws {| status "suspended" |} } }
  presence-default   the presence rule with the same data and note in the DEFAULT graph (control)

The candidates for the server-mode refusal are separated by what each run changes:
  * whether an index (with its annotation arena) exists when rob writes: the server's background indexer is on
    by default (`--indexing-enabled`, `--reindex-min-bytes` 100), `--indexing-enabled=false` turns it off;
  * which annotation lane the policy condition takes: stock's own `FLUREE_ANNOTATION_LANE=chain` switch pins the
    generic join chain while leaving everything else (including the single-batch policy read) unchanged;
    `=arena` and `=enumerate` force the two arena lanes where their gates hold (direct mode only);
  * the lane actually taken, read from stock's own DEBUG events `annotation delegate gates` / `annotation
    delegate lane` (RUST_LOG=fluree_db_query::default_graph_source=debug).
Direct (CLI) attempts each run on a fresh copy of the ledger, so every attempt sees the same state. Server
attempts try erin before alice, and wait for the background index to catch up between attempts when it is on.

Exit status 1 if any decision differs from the documented one.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

cli, base_port = sys.argv[1], int(sys.argv[2])
output = sys.argv[3] if len(sys.argv) > 3 else None
LEDGER, ROB = 'repro:main', 'urn:x:rob'
EXPECTED = {'erin': False, 'alice': True}
DEBUG = 'info,fluree_db_query::default_graph_source=debug'
RULES = {
    'presence': 'ASK { $this <urn:x:memberOf> ?ws {| <urn:x:status> "active" |} }',
    'optional': ('ASK { $this <urn:x:memberOf> ?ws . OPTIONAL { $this <urn:x:memberOf> ?ws {| <urn:x:status> ?st |} } '
                 'FILTER(!BOUND(?st) || ?st != "suspended") }'),
    'notexists': ('ASK { $this <urn:x:memberOf> ?ws . FILTER NOT EXISTS '
                  '{ $this <urn:x:memberOf> ?ws {| <urn:x:status> "suspended" |} } }'),
    'presence-default': 'ASK { $this <urn:x:memberOf> ?ws {| <urn:x:status> "active" |} }',
}


def data(rule):
    body = ('<urn:x:alice> <urn:x:memberOf> <urn:x:ws> ~ <urn:x:m1> {| <urn:x:status> "active" |} .\n'
            '<urn:x:erin> <urn:x:memberOf> <urn:x:ws> ~ <urn:x:m2> {| <urn:x:status> "suspended" |} .\n')
    return body if rule == 'presence-default' else '<urn:g:data> {\n' + body + '}\n'


def note_update(rule, member, n):
    triple = '<urn:x:%s> <urn:x:note> "note %d"' % (member, n)
    return 'INSERT DATA { %s }' % (triple if rule == 'presence-default' else 'GRAPH <urn:g:data> { %s }' % triple)


def policy_json(rule):
    return json.dumps({'@context': {'f': 'https://ns.flur.ee/db#'}, '@graph': [
        {'@id': 'urn:x:rule', '@type': ['f:AccessPolicy', 'urn:x:Policies'], 'f:action': [{'@id': 'f:modify'}],
         'f:query': {'@type': 'https://ns.flur.ee/db#sparql', '@value': RULES[rule]}},
        {'@id': ROB, 'f:policyClass': [{'@id': 'urn:x:Policies'}]}]})


def lanes(text):
    text = re.sub(r'\x1b\[[0-9;]*m', '', text)
    out = []
    for line in text.splitlines():
        m = re.search(r'annotation delegate lane lane="(\w+)"', line)
        if m:
            out.append(m.group(1))
        m = re.search(r'annotation delegate gates (arena=\w+ store=\w+ history=\w+ overlay_empty=\w+)', line)
        if m:
            out.append('gates[' + m.group(1) + ']')
    return out


record = {'version': subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip(),
          'attempts': [], 'adminQueries': [], 'serverIndexing': []}


def add(rule, mode, state, member, allowed, lane_events, detail=''):
    ok = allowed == EXPECTED[member]
    record['attempts'].append({'rule': rule, 'mode': mode, 'state': state, 'member': member,
                               'expectedAllowed': EXPECTED[member], 'allowed': allowed, 'correct': ok,
                               'annotationLane': lane_events, 'detail': detail})
    flag = '' if ok else ('WRONGLY ALLOWED' if allowed else 'wrongly refused')
    print('%-16s %-15s %-22s %-6s expected=%-8s got=%-8s lanes=%s %s' % (
        rule, mode, state, member, 'allowed' if EXPECTED[member] else 'refused',
        'allowed' if allowed else 'refused', lane_events, flag))


# ------------------------------------------------------------------ direct CLI
def direct(rule):
    work = tempfile.mkdtemp(prefix='fluree-annlane-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work, TMPDIR=work)
    env.pop('RUST_LOG', None)
    env.pop('FLUREE_ANNOTATION_LANE', None)

    def run(wd, e, *args, verbose=False):
        return subprocess.run([cli, '--verbose' if verbose else '--quiet', '--no-color', *args], cwd=wd, env=e,
                              text=True, capture_output=True, timeout=120)
    try:
        assert run(work, env, 'init').returncode == 0
        assert run(work, env, 'create', LEDGER).returncode == 0
        assert run(work, env, 'insert', '--direct', '-l', LEDGER, '--format', 'trig', '-e', data(rule)).returncode == 0
        assert run(work, env, 'insert', '--direct', '-l', LEDGER, '-e', policy_json(rule)).returncode == 0
        # Each state starts from its own copy of the unindexed ledger. (A `fluree reindex` run after a
        # `fluree index` was observed to leave the index without an annotation arena, so the two are kept apart.)
        for state, steps, lane_env in (('unindexed', [], None), ('fluree index', ['index'], None),
                                       ('fluree reindex', ['reindex'], None),
                                       ('fluree index, then reindex', ['index', 'reindex'], None),
                                       ('fluree reindex, lane=chain', ['reindex'], 'chain'),
                                       ('fluree reindex, lane=arena', ['reindex'], 'arena'),
                                       ('fluree reindex, lane=enumerate', ['reindex'], 'enumerate')):
            state_dir = tempfile.mkdtemp(prefix='fluree-annlane-state-')
            shutil.rmtree(state_dir)
            shutil.copytree(work, state_dir, symlinks=True)
            senv = dict(env, HOME=state_dir, XDG_CONFIG_HOME=state_dir, TMPDIR=state_dir)
            for step in steps:
                r = run(state_dir, senv, step, '--direct', LEDGER)
                assert r.returncode == 0, r.stderr
            for n, member in enumerate(EXPECTED):
                copy = tempfile.mkdtemp(prefix='fluree-annlane-copy-')
                shutil.rmtree(copy)
                shutil.copytree(state_dir, copy, symlinks=True)
                e = dict(env, HOME=copy, XDG_CONFIG_HOME=copy, TMPDIR=copy, RUST_LOG=DEBUG)
                if lane_env:
                    e['FLUREE_ANNOTATION_LANE'] = lane_env
                try:
                    r = run(copy, e, 'update', '--direct', '-l', LEDGER, '--format', 'sparql', '--as', ROB, '-e',
                            note_update(rule, member, n), verbose=True)
                finally:
                    shutil.rmtree(copy, ignore_errors=True)
                add(rule, 'direct', state, member, r.returncode == 0, lanes(r.stderr))
            if state == 'fluree reindex' and rule == 'presence':
                # The administrator's ordinary queries of the same named graph, with the same lane events.
                e = dict(senv, RUST_LOG=DEBUG)
                for form, q in (('GRAPH', 'SELECT ?m WHERE { GRAPH <urn:g:data> { ?m <urn:x:memberOf> ?ws '
                                          '{| <urn:x:status> "active" |} } }'),
                                ('FROM', 'SELECT ?m FROM <%s#urn:g:data> WHERE { ?m <urn:x:memberOf> ?ws '
                                         '{| <urn:x:status> "active" |} }' % LEDGER)):
                    r = run(state_dir, e, 'query', '--direct', '-l', LEDGER, '--sparql', '--format', 'json', '-e', q,
                            verbose=True)
                    try:
                        rows = [b['m']['value'] for b in json.loads(r.stdout)['results']['bindings']]
                    except (ValueError, KeyError):
                        rows = 'error: ' + ' '.join(r.stderr.split())[-160:]
                    record['adminQueries'].append({'state': state, 'form': form, 'query': q, 'rows': rows,
                                                   'annotationLane': lanes(r.stderr)})
                    print('%-16s %-15s %-22s admin %-5s rows=%s lanes=%s' % (rule, 'direct', state, form, rows,
                                                                             lanes(r.stderr)))
            shutil.rmtree(state_dir, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------ server
def server(rule, mode, port):
    work = tempfile.mkdtemp(prefix='fluree-annlane-srv-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work, TMPDIR=work)
    env.pop('RUST_LOG', None)
    env.pop('FLUREE_ANNOTATION_LANE', None)
    senv = dict(env, RUST_LOG=DEBUG)
    if mode == 'server-lane=chain':
        senv['FLUREE_ANNOTATION_LANE'] = 'chain'
    extra = ['--indexing-enabled=false'] if mode == 'server-noindex' else []
    log_path = os.path.join(work, 'server.log')
    base = 'http://127.0.0.1:%d' % port
    log = open(log_path, 'w')
    srv = subprocess.Popen([cli, 'server', 'run', '--memory', '--listen-addr', '127.0.0.1:%d' % port, '--', *extra],
                           cwd=work, env=senv, stdout=log, stderr=subprocess.STDOUT)

    def run(*args):
        return subprocess.run([cli, '--quiet', '--no-color', *args], cwd=work, env=env, text=True,
                              capture_output=True, timeout=120)

    def log_text():
        with open(log_path) as f:
            return re.sub(r'\x1b\[[0-9;]*m', '', f.read())

    def index_t():
        done = [int(x) for x in re.findall(r'Successfully indexed ledger ledger_id=\S+ index_t=(\d+)', log_text())]
        return max(done) if done else 0

    def wait_index(t):
        for _ in range(300):
            if index_t() >= t:
                return True
            time.sleep(0.05)
        return False

    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + '/health', timeout=1)
                break
            except OSError:
                time.sleep(0.25)
        assert run('init').returncode == 0
        assert run('remote', 'add', 'local', base).returncode == 0
        assert run('create', LEDGER, '--remote', 'local').returncode == 0
        assert run('insert', '--remote', 'local', '-l', LEDGER, '--format', 'trig', '-e', data(rule)).returncode == 0
        assert run('insert', '--remote', 'local', '-l', LEDGER, '-e', policy_json(rule)).returncode == 0
        t = 2
        states = [('before reindex', None), ('after POST /reindex', 'reindex')] if mode == 'server-noindex' \
            else [('background index', None)]
        for state, step in states:
            if step:
                req = urllib.request.Request(base + '/v1/fluree/reindex', data=json.dumps({'ledger': LEDGER}).encode(),
                                             headers={'Content-Type': 'application/json'}, method='POST')
                with urllib.request.urlopen(req, timeout=120) as resp:
                    assert resp.status == 200
            for n, member in enumerate(EXPECTED):
                caught_up = wait_index(t) if mode != 'server-noindex' else None
                mark = len(log_text())
                r = run('update', '--remote', 'local', '-l', LEDGER, '--format', 'sparql', '--as', ROB, '-e',
                        note_update(rule, member, n))
                time.sleep(0.2)
                allowed = r.returncode == 0
                if allowed:
                    t += 1
                detail = 'index_t=%d before the attempt' % index_t() if mode != 'server-noindex' else ''
                if caught_up is False:
                    detail += ' (index had not caught up)'
                add(rule, mode, state, member, allowed, lanes(log_text()[mark:]), detail)
        text = log_text()
        record['serverIndexing'].append({'rule': rule, 'mode': mode,
                                         'fullRebuildStarted': 'starting full rebuild path' in text,
                                         'incrementalStarted': 'attempting incremental index' in text,
                                         'indexedT': sorted(set(int(x) for x in re.findall(
                                             r'Successfully indexed ledger ledger_id=\S+ index_t=(\d+)', text)))})
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=15)
        except subprocess.TimeoutExpired:
            srv.kill()
        log.close()
        shutil.rmtree(work, ignore_errors=True)


print(record['version'])
port = base_port
for rule in RULES:
    direct(rule)
    for mode in ('server-default', 'server-noindex', 'server-lane=chain'):
        server(rule, mode, port)
        port += 1
wrong = sum(not a['correct'] for a in record['attempts'])
print('wrong decisions:', wrong)
if output:
    with open(output, 'w') as out:
        json.dump(record, out, indent=1)
sys.exit(1 if wrong else 0)
