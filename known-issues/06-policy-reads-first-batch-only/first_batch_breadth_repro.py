"""How ordinary must a policy condition be to hit the first-batch-only decision? (breadth)

Usage: python3 -I first_batch_breadth_repro.py <fluree-binary> [output.json]

Plain Fluree CLI in --direct mode; no server. Each ledger lives in a fresh temporary directory with HOME,
XDG_CONFIG_HOME and TMPDIR pointed at it. Every policy condition is checked against the condition's own answer:
the same ASK run by the administrator (no --as, never policy-checked) with the special variables written in as
constants. Each condition is stored as its own policy with its own class; identities carry every class and one
policy is selected per request with --policy-class.

  modify   the identity inserts one fact; the write must be allowed exactly when the query answers true.
           Each attempt runs on a fresh copy of the ledger directory, so attempts do not change the state.
  view     the identity reads one fact (or counts facts); it must be visible exactly when the query answers true.

Index states: unindexed (novelty only), after `fluree index`, after `fluree reindex`, and after `fluree index`
followed by one more commit (index plus newer commits).

Part A, small data: VALUES-at-root shapes and controls, SPARQL and JSON-LD conditions.
Part B, 3,000 users and 3,000 documents: the injected special-variable VALUES itself lands above a property join
  or a GRAPH; no VALUES is written in the condition. Positions 0, 999, 1000 and 2999 are tried.
Part C, consequence for a reader: a view policy that is wrongly refused hides a flag, and the reader's own
  FILTER NOT EXISTS query then returns a row it should not.

Exit status 1 if any policy decision contradicts the query's own answer.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

cli = sys.argv[1]
output = sys.argv[2] if len(sys.argv) > 2 else None
LEDGER = 'repro:main'
ROB = 'urn:x:rob'
F = 'https://ns.flur.ee/db#'
STATES = ('unindexed', 'index', 'reindex', 'index+newer')
N = 3000
POSITIONS = (0, 999, 1000, N - 1)


class Ledger:
    def __init__(self, src=None):
        self.work = tempfile.mkdtemp(prefix='fluree-first-batch-')
        if src is not None:
            shutil.rmtree(self.work)
            shutil.copytree(src.work, self.work, symlinks=True)
        self.env = dict(os.environ, HOME=self.work, XDG_CONFIG_HOME=self.work, TMPDIR=self.work)
        self.env.pop('RUST_LOG', None)
        self.env.pop('FLUREE_ANNOTATION_LANE', None)

    def run(self, *args, timeout=600):
        return subprocess.run([cli, '--quiet', '--no-color', *args], cwd=self.work, env=self.env, text=True,
                              capture_output=True, timeout=timeout)

    def ok(self, *args):
        r = self.run(*args)
        assert r.returncode == 0, (args[:4], r.stderr[:500])
        return r

    def create(self):
        self.ok('init')
        self.ok('create', LEDGER)

    def sparql_file(self, text):
        path = os.path.join(self.work, 'data-%d.ru' % time.time_ns())
        with open(path, 'w') as f:
            f.write(text)
        self.ok('update', '--direct', '-l', LEDGER, '--format', 'sparql', '-f', path)
        os.remove(path)

    def jsonld_file(self, obj):
        path = os.path.join(self.work, 'data-%d.jsonld' % time.time_ns())
        with open(path, 'w') as f:
            json.dump(obj, f)
        self.ok('insert', '--direct', '-l', LEDGER, '--format', 'jsonld', '-f', path)
        os.remove(path)

    def set_state(self, state):
        if state in ('index', 'index+newer'):
            self.ok('index', '--direct', LEDGER)
        if state == 'reindex':
            self.ok('reindex', '--direct', LEDGER)
        if state == 'index+newer':
            self.sparql_file('INSERT DATA { <urn:x:later> <urn:x:z> 1 }')

    def admin_ask(self, lang, query):
        if lang == 'sparql':
            r = self.run('query', '--direct', '-l', LEDGER, '--sparql', '--format', 'json', '-e', query)
            try:
                return json.loads(r.stdout).get('boolean')
            except ValueError:
                return 'error: ' + ' '.join(r.stderr.split())[:200]
        r = self.run('query', '--direct', '-l', LEDGER, '--format', 'json', '-e', json.dumps(query))
        text = r.stdout.strip()
        return True if text == 'true' else False if text == 'false' else 'error: ' + ' '.join(
            (r.stderr or text).split())[:200]

    def view_rows(self, identity, policy_class, query):
        r = self.run('query', '--direct', '-l', LEDGER, '--sparql', '--format', 'json', '--as', identity,
                     '--policy-class', policy_class, '-e', query)
        try:
            return [{k: v['value'] for k, v in b.items()} for b in json.loads(r.stdout)['results']['bindings']]
        except (ValueError, KeyError):
            return 'error: ' + ' '.join(r.stderr.split())[:200]

    def write_allowed(self, identity, policy_class, update):
        copy = Ledger(self)
        try:
            r = copy.run('update', '--direct', '-l', LEDGER, '--format', 'sparql', '--as', identity,
                         '--policy-class', policy_class, '-e', update)
            return r.returncode == 0, ' '.join(r.stderr.split())[:160]
        finally:
            copy.close()

    def explain_ops(self, query):
        r = self.run('query', '--direct', '-l', LEDGER, '--sparql', '--explain', '--format', 'json', '-e', query)
        try:
            plan = json.loads(r.stdout[:r.stdout.rindex('}') + 1])['plan']['physical']
        except (ValueError, KeyError):
            return None

        def walk(node):
            names = [node.get('op')]
            for child in node.get('children', []):
                names += walk(child['node'])
            return names
        return walk(plan)

    def close(self):
        shutil.rmtree(self.work, ignore_errors=True)


def policy_node(pid, cls, lang, query, actions, extra=None):
    q = ({'@type': F + 'sparql', '@value': query} if lang == 'sparql' else {'@type': '@json', '@value': query})
    node = {'@id': pid, '@type': ['f:AccessPolicy', cls], 'f:action': [{'@id': a} for a in actions], 'f:query': q}
    node.update(extra or {})
    return node


def substitute(lang, query, identity, this=None):
    """The condition as the administrator's ordinary query: special variables written in as constants."""
    if lang == 'sparql':
        q = query.replace('$identity', '<%s>' % identity)
        return q.replace('$this', '<%s>' % this) if this else q
    # The JSON-LD conditions here use the special variables only in "@id" positions.
    text = json.dumps({'ask': query['where']}).replace('"?$identity"', json.dumps(identity))
    if this:
        text = text.replace('"?$this"', json.dumps(this))
    return json.loads(text)


def explicit_values(query, identity, this=None):
    """The condition as an ordinary query with the VALUES the policy executor injects, for --explain."""
    q = query.replace('$identity', '?identity').replace('$this', '?this')
    seed = 'VALUES ?identity { <%s> } ' % identity + ('VALUES ?this { <%s> } ' % this if this else '')
    return q.replace('ASK {', 'ASK { ' + seed, 1)


record = {'version': subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip(),
          'partA': [], 'partB': [], 'partC': [], 'explain': []}
wrong = 0


def note(part, row):
    global wrong
    record[part].append(row)
    if not row['consistent']:
        wrong += 1


# ---------------------------------------------------------------- Part A: small data
SEED_A = '''INSERT DATA {
  <urn:x:rob> <urn:x:kind> <urn:x:User> ; <urn:x:role> <urn:r:x>, <urn:r:a> ; <urn:x:flagA> false ; <urn:x:flagB> true ;
     <urn:x:delegateRole> <urn:r:d> ; <urn:x:memberOf> <urn:grp:1> .
  <urn:grp:1> <urn:x:memberOf> <urn:grp:2> . <urn:grp:2> <urn:x:memberOf> <urn:grp:admins> .
  <urn:x:rob> <urn:x:memberOf> <urn:x:ws1> ~ <urn:x:m1> {| <urn:x:status> "suspended" |} .
  <urn:x:rob> <urn:x:memberOf> <urn:x:ws2> ~ <urn:x:m2> {| <urn:x:status> "active" |} .
  GRAPH <urn:g:a> { <urn:x:other> <urn:x:may> true . <urn:x:rob> <urn:x:grole> <urn:r:x> }
  GRAPH <urn:g:b> { <urn:x:rob> <urn:x:may> true . <urn:x:rob> <urn:x:grole> <urn:r:a> }
  GRAPH <urn:grp:4> { <urn:x:rob> <urn:x:may> true }
  <urn:x:item> <urn:x:note> "visible" .
}'''
# (id, description, language, condition). rob's memberships: ws1 "suspended" first, ws2 "active" second.
# Named graphs are visited g:a, g:b, grp:4: rob's grole is r:x in g:a and r:a in g:b.
SHAPES_A = [
    ('A1', 'VALUES ?g { second graph } GRAPH ?g { $identity ... } (earlier finding)', 'sparql',
     'ASK { VALUES ?g { <urn:grp:4> } GRAPH ?g { $identity <urn:x:may> true } }'),
    ('A2', 'VALUES on an object variable inside GRAPH ?g (graph variable free)', 'sparql',
     'ASK { VALUES ?role { <urn:r:a> <urn:r:b> } GRAPH ?g { $identity <urn:x:grole> ?role } }'),
    ('A3', 'VALUES on an edge-annotation value (default graph, no GRAPH)', 'sparql',
     'ASK { VALUES ?st { "active" "trial" } $identity <urn:x:memberOf> ?ws {| <urn:x:status> ?st |} }'),
    ('A4', 'one-row literal VALUES on an edge-annotation value', 'sparql',
     'ASK { VALUES ?st { "active" } $identity <urn:x:memberOf> ?ws {| <urn:x:status> ?st |} }'),
    ('A5', 'control: annotation value as a constant', 'sparql',
     'ASK { $identity <urn:x:memberOf> ?ws {| <urn:x:status> "active" |} }'),
    ('A6', 'control: annotation value tested with FILTER IN', 'sparql',
     'ASK { $identity <urn:x:memberOf> ?ws {| <urn:x:status> ?st |} FILTER(?st IN ("active", "trial")) }'),
    ('A7', 'control: GRAPH ?g with FILTER on the inner variable', 'sparql',
     'ASK { GRAPH ?g { $identity <urn:x:grole> ?role } FILTER(?role IN (<urn:r:a>, <urn:r:b>)) }'),
    ('A8', 'VALUES above UNION', 'sparql',
     'ASK { VALUES ?role { <urn:r:d> <urn:r:b> } { $identity <urn:x:role> ?role } UNION '
     '{ $identity <urn:x:delegateRole> ?role } }'),
    ('A9', 'VALUES above a property path', 'sparql',
     'ASK { VALUES ?grp { <urn:grp:admins> } $identity <urn:x:memberOf>+ ?grp }'),
    ('A10', 'VALUES above a two-triple identity star (nested-loop join)', 'sparql',
     'ASK { VALUES ?role { <urn:r:a> <urn:r:b> } $identity <urn:x:role> ?role ; <urn:x:kind> <urn:x:User> }'),
    ('A11', 'VALUES above a sub-SELECT', 'sparql',
     'ASK { VALUES ?role { <urn:r:a> <urn:r:b> } { SELECT ?role WHERE { $identity <urn:x:role> ?role } } }'),
    ('A1-json', 'A1 as a JSON-LD condition', 'jsonld',
     {'where': [['values', ['?g', [{'@id': 'urn:grp:4'}]]],
                ['graph', '?g', {'@id': '?$identity', 'urn:x:may': True}]]}),
    ('A2-json', 'A2 as a JSON-LD condition', 'jsonld',
     {'where': [['values', ['?role', [{'@id': 'urn:r:a'}, {'@id': 'urn:r:b'}]]],
                ['graph', '?g', {'@id': '?$identity', 'urn:x:grole': '?role'}]]}),
    ('A3-json', 'A3 as a JSON-LD condition', 'jsonld',
     {'where': [['values', ['?st', ['active', 'trial']]],
                {'@id': '?$identity', 'urn:x:memberOf': {'@id': '?ws', '@annotation': {'urn:x:status': '?st'}}}]}),
]

print(record['version'])
print('Part A: small data (rob)')
base_policies = [policy_node('urn:x:pa-' + sid, 'urn:x:PA-' + sid, lang, q, ['f:modify', 'f:view'])
                 for sid, _, lang, q in SHAPES_A]
for state in STATES:
    base = Ledger()
    base.create()
    base.sparql_file(SEED_A)
    base.jsonld_file({'@context': {'f': F}, '@graph': base_policies + [
        {'@id': ROB, 'f:policyClass': [{'@id': 'urn:x:PA-' + sid} for sid, _, _, _ in SHAPES_A]}]})
    if state == 'unindexed':
        for sid, desc, lang, q in SHAPES_A:
            if lang == 'sparql':
                ops = base.explain_ops(explicit_values(q, ROB))
                record['explain'].append({'shape': sid, 'query': explicit_values(q, ROB), 'physicalRootFirst': ops})
    base.set_state(state)
    for sid, desc, lang, q in SHAPES_A:
        answer = base.admin_ask(lang, substitute(lang, q, ROB))
        visible = base.view_rows(ROB, 'urn:x:PA-' + sid, 'SELECT ?o WHERE { <urn:x:item> <urn:x:note> ?o }')
        view_allowed = bool(visible) if isinstance(visible, list) else visible
        write_allowed, err = base.write_allowed(ROB, 'urn:x:PA-' + sid,
                                                'INSERT DATA { <urn:x:item> <urn:x:note> "written by rob" }')
        for action, allowed in (('view', view_allowed), ('modify', write_allowed)):
            ok = allowed is answer
            note('partA', {'shape': sid, 'description': desc, 'language': lang, 'condition': q, 'state': state,
                           'action': action, 'queryAnswer': answer, 'policyAllowed': allowed, 'consistent': ok,
                           'writeError': err if action == 'modify' else None})
            print('%-12s %-8s %-6s query=%-5s policy=%-7s %s' % (
                state, sid, action, answer, 'allowed' if allowed is True else 'refused' if allowed is False
                else allowed, '' if ok else 'INCONSISTENT'))
    base.close()

# ---------------------------------------------------------------- Part B: 3,000 users and documents
print('Part B: %d users and %d documents' % (N, N))
SHAPES_B = [
    ('B1', 'identity star of three triples with FILTER (no VALUES written)', 'sparql', 'identity',
     'ASK { $identity <urn:x:kind> <urn:x:User> ; <urn:x:dept> ?d ; <urn:x:level> ?lv FILTER(?lv >= 3) }'),
    ('B1-json', 'B1 as a JSON-LD condition', 'jsonld', 'identity',
     {'where': [{'@id': '?$identity', 'urn:x:kind': {'@id': 'urn:x:User'}, 'urn:x:dept': '?d',
                 'urn:x:level': '?lv'}, ['filter', '(>= ?lv 3)']]}),
    ('B2', 'document ($this) star of three triples with FILTER, owner is the identity', 'sparql', 'this',
     'ASK { $this <urn:x:kind> <urn:x:Doc> ; <urn:x:owner> $identity ; <urn:x:tier> ?t FILTER(?t < 3) }'),
    ('B3', 'identity star of three triples with BIND, no FILTER', 'sparql', 'identity',
     'ASK { $identity <urn:x:kind> <urn:x:User> ; <urn:x:dept> ?d ; <urn:x:level> ?lv BIND(?lv AS ?l) }'),
    ('B4', 'BIND a graph IRI, then GRAPH ?g { $identity ... }', 'sparql', 'identity',
     'ASK { BIND(<urn:g:big> AS ?g) GRAPH ?g { $identity <urn:x:may> true } }'),
    ('B10', 'identity star of three triples, no constant object, range FILTER', 'sparql', 'identity',
     'ASK { $identity <urn:x:dept> ?d ; <urn:x:level> ?lv ; <urn:x:kind> ?k FILTER(?lv >= 3) }'),
    ('B11', 'identity star of three triples with a constant object and a non-range FILTER', 'sparql', 'identity',
     'ASK { $identity <urn:x:kind> <urn:x:User> ; <urn:x:dept> ?d ; <urn:x:level> ?lv FILTER(?d != <urn:d:9>) }'),
    ('B5', 'control: same star without FILTER or BIND', 'sparql', 'identity',
     'ASK { $identity <urn:x:kind> <urn:x:User> ; <urn:x:dept> ?d ; <urn:x:level> ?lv }'),
    ('B6', 'control: two-triple star with FILTER', 'sparql', 'identity',
     'ASK { $identity <urn:x:kind> <urn:x:User> ; <urn:x:level> ?lv FILTER(?lv >= 3) }'),
    ('B7', 'control: constant GRAPH { $identity ... }', 'sparql', 'identity',
     'ASK { GRAPH <urn:g:big> { $identity <urn:x:may> true } }'),
    ('B8', 'control: root FILTER over a 3,000-row scan', 'sparql', 'identity',
     'ASK { ?u <urn:x:level> ?lv FILTER(?u = $identity) }'),
    ('B9', 'control: MINUS leaving only the last row', 'sparql', 'identity',
     'ASK { ?u <urn:x:level> ?lv MINUS { ?u <urn:x:kind> <urn:x:User> } }'),
]
seed_b = ['INSERT DATA {']
for i in range(N):
    seed_b.append('<urn:user:%05d> <urn:x:kind> <urn:x:User> ; <urn:x:dept> <urn:d:1> ; <urn:x:level> 3 .' % i)
for i in range(N):
    seed_b.append('<urn:doc:%05d> <urn:x:kind> <urn:x:Doc> ; <urn:x:owner> <urn:user:00000> ; <urn:x:tier> 1 .' % i)
seed_b.append('GRAPH <urn:g:big> {')
for i in range(N):
    seed_b.append('<urn:user:%05d> <urn:x:may> true .' % i)
seed_b.append('}')
seed_b.append('<urn:zz:last> <urn:x:level> 3 .')
seed_b.append('<urn:x:item> <urn:x:note> "visible" .')
seed_b.append('}')
classes_b = ['urn:x:PB-' + sid for sid, _, _, _, _ in SHAPES_B]
pol_b = [policy_node('urn:x:pb-' + sid, 'urn:x:PB-' + sid, lang, q, ['f:modify', 'f:view'])
         for sid, _, lang, _, q in SHAPES_B]
# B2 view is checked through the document's tier: a policy on that property only.
pol_b.append(policy_node('urn:x:pb-B2-tier', 'urn:x:PB-B2-tier', 'sparql', SHAPES_B[2][4], ['f:view'],
                         {'f:onProperty': [{'@id': 'urn:x:tier'}]}))
classes_b.append('urn:x:PB-B2-tier')
for state in STATES:
    base = Ledger()
    base.create()
    base.sparql_file('\n'.join(seed_b))
    base.jsonld_file({'@context': {'f': F}, '@graph': pol_b + [
        {'@id': 'urn:user:%05d' % i, 'f:policyClass': [{'@id': c} for c in classes_b]} for i in range(N)]})
    if state == 'unindexed':
        for sid, desc, lang, kind, q in SHAPES_B:
            if lang == 'sparql':
                ev = explicit_values(q, 'urn:user:01000', 'urn:doc:01000' if kind == 'this' else None)
                record['explain'].append({'shape': sid, 'query': ev, 'physicalRootFirst': base.explain_ops(ev)})
    base.set_state(state)
    for sid, desc, lang, kind, q in SHAPES_B:
        for pos in POSITIONS:
            if kind == 'identity':
                identity, this = 'urn:user:%05d' % pos, None
                view = base.view_rows(identity, 'urn:x:PB-' + sid, 'SELECT ?o WHERE { <urn:x:item> <urn:x:note> ?o }')
                update = 'INSERT DATA { <urn:x:item> <urn:x:note> "written" }'
            else:
                identity, this = 'urn:user:00000', 'urn:doc:%05d' % pos
                view = base.view_rows(identity, 'urn:x:PB-B2-tier', 'SELECT ?t WHERE { <%s> <urn:x:tier> ?t }' % this)
                update = 'INSERT DATA { <%s> <urn:x:note> "written" }' % this
            answer = base.admin_ask(lang, substitute(lang, q, identity, this))
            view_allowed = bool(view) if isinstance(view, list) else view
            write_allowed, err = base.write_allowed(identity, 'urn:x:PB-' + sid, update)
            for action, allowed in (('view', view_allowed), ('modify', write_allowed)):
                ok = allowed is answer
                note('partB', {'shape': sid, 'description': desc, 'language': lang, 'condition': q, 'state': state,
                               'position': pos, 'identity': identity, 'this': this, 'action': action,
                               'queryAnswer': answer, 'policyAllowed': allowed, 'consistent': ok})
                print('%-12s %-8s pos=%-5d %-6s query=%-5s policy=%-7s %s' % (
                    state, sid, pos, action, answer, 'allowed' if allowed is True else 'refused'
                    if allowed is False else allowed, '' if ok else 'INCONSISTENT'))
    if state in ('unindexed', 'reindex'):
        # Whole-ledger view of B2: how many of the 3,000 tiers user 0 (owner of every document) can read.
        t0 = time.time()
        rows = base.view_rows('urn:user:00000', 'urn:x:PB-B2-tier', 'SELECT ?d WHERE { ?d <urn:x:tier> ?t }')
        count = len(rows) if isinstance(rows, list) else rows
        ok = count == N
        note('partB', {'shape': 'B2', 'description': 'all documents: visible tier count', 'state': state,
                       'action': 'view-count', 'queryAnswer': N, 'policyAllowed': count, 'consistent': ok,
                       'seconds': round(time.time() - t0, 1)})
        print('%-12s B2       all documents: %s of %d tiers visible %s' % (state, count, N,
                                                                         '' if ok else 'INCONSISTENT'))
    base.close()

# ---------------------------------------------------------------- Part C: consequence for a reader
print('Part C: a wrongly refused view policy and the reader\'s own FILTER NOT EXISTS')
SEED_C = '''INSERT DATA {
  <urn:x:rob> <urn:x:memberOf> <urn:x:ws1> ~ <urn:x:m1> {| <urn:x:status> "suspended" |} .
  <urn:x:rob> <urn:x:memberOf> <urn:x:ws2> ~ <urn:x:m2> {| <urn:x:status> "active" |} .
  <urn:doc:open> <urn:x:kind> <urn:x:Doc> .
  <urn:doc:held> <urn:x:kind> <urn:x:Doc> ; <urn:x:hold> true .
}'''
COND_C = 'ASK { VALUES ?st { "active" "trial" } $identity <urn:x:memberOf> ?ws {| <urn:x:status> ?st |} }'
READER_C = ('SELECT ?d WHERE { ?d <urn:x:kind> <urn:x:Doc> FILTER NOT EXISTS { ?d <urn:x:hold> true } }')
for state in ('unindexed', 'reindex'):
    base = Ledger()
    base.create()
    base.sparql_file(SEED_C)
    base.jsonld_file({'@context': {'f': F}, '@graph': [
        policy_node('urn:x:pc-hold', 'urn:x:PC', 'sparql', COND_C, ['f:view'],
                    {'f:onProperty': [{'@id': 'urn:x:hold'}]}),
        {'@id': 'urn:x:pc-rest', '@type': ['f:AccessPolicy', 'urn:x:PC'], 'f:action': [{'@id': 'f:view'}],
         'f:allow': True},
        {'@id': ROB, 'f:policyClass': [{'@id': 'urn:x:PC'}]}]})
    base.set_state(state)
    answer = base.admin_ask('sparql', substitute('sparql', COND_C, ROB))
    holds = base.view_rows(ROB, 'urn:x:PC', 'SELECT ?d WHERE { ?d <urn:x:hold> true }')
    reader = base.view_rows(ROB, 'urn:x:PC', READER_C)
    unheld = sorted(r['d'] for r in reader) if isinstance(reader, list) else reader
    # rob may see the hold flag (answer true), so his own query must leave out the held document.
    ok = unheld == ['urn:doc:open'] if answer is True else None
    note('partC', {'state': state, 'condition': COND_C, 'queryAnswer': answer,
                   'holdFlagsVisibleToRob': holds, 'readerQuery': READER_C, 'readerRows': unheld,
                   'consistent': bool(ok)})
    print('%-12s condition answer=%s; hold flags rob sees=%s; his "documents without a hold" query returns %s %s' % (
        state, answer, holds, unheld, '' if ok else 'WRONG (held document listed)'))
    base.close()

print('cases where the policy decision contradicts the query answer:', wrong)
if output:
    with open(output, 'w') as out:
        json.dump(record, out, indent=1)
sys.exit(1 if wrong else 0)
