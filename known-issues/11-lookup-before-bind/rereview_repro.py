"""The plans the re-review of the corrected bug-11 fix (738a4cc) found, on the CLI.

Usage: python3 -I rereview_repro.py <fluree-binary>

Plain Fluree CLI in --direct mode, as the administrator (no policy), on fresh temporary ledgers. The expected
answers are the stock release's.

I1b, work. A selective lookup (three `rare` triples) joined to N labelled documents through a BIND that reads the
lookup's ?o: BIND(STR(?o)) to a label, and BIND(IRI(...)) to a title. One row; the time should not grow with N.
Median of three, including process start-up, after `fluree reindex`.

I1b, rows. The same join through BIND(STRLEN(STR(?o))) to integer sizes 1 to 50 and one "7"^^xsd:long. The
stock release returns both size-7 documents, before and after `fluree reindex`.

N1, work. A UNION lookup over two absent predicates behind a BIND fed by N items. No row; the time should not grow
with N, after `fluree reindex`.

N2, strict updates. A BIND after the lookup that fails on one reader's row: the stock release fails the update
when that reader is another user's, and succeeds (writing nothing) when it is the fallback, which has no role.
"""
import json, os, subprocess, sys, tempfile, time
cli = sys.argv[1]

def ledger(seed):
    work = tempfile.mkdtemp(prefix='fluree-bind-rereview-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
    def run(*a):
        return subprocess.run([cli, '--quiet', '--no-color', *a], cwd=work, env=env, text=True,
                              capture_output=True, timeout=600)
    assert run('init').returncode == 0 and run('create', 'repro:main').returncode == 0
    update(run, seed, work)
    return run, work

def update(run, sparql, work):
    path = os.path.join(work, 'update.rq')
    open(path, 'w').write(sparql)
    r = run('update', '--direct', '-l', 'repro:main', '--format', 'sparql', '-f', path)
    return r.returncode == 0, (r.stderr or r.stdout)

def values(run, q):
    out = run('query', '--direct', '-l', 'repro:main', '--sparql', '--format', 'json', '-e', q)
    try:
        return sorted(v['value'] for b in json.loads(out.stdout)['results']['bindings'] for v in b.values())
    except (ValueError, KeyError):
        return 'error: ' + (out.stderr or out.stdout)[:200]

def timed(run, q):
    times, got = [], None
    for _ in range(3):
        t0 = time.perf_counter()
        got = values(run, q)
        times.append((time.perf_counter() - t0) * 1000)
    return sorted(times)[1], got

print(subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip())
wrong = 0
def check(label, got, expected, extra=''):
    global wrong
    ok = got == expected
    wrong += not ok
    print('%-44s %s got=%s %s' % (label, extra, got, '' if ok else 'WRONG (expected %s)' % (expected,)))

KEY = ('OPTIONAL { ?key <urn:x:did> "did:example:reader" ; <urn:x:account> ?keyAccount } '
       'BIND(COALESCE(?keyAccount, <urn:u:1>) AS ?r) ?r <urn:x:rare> ?o . ')
RARE = '<urn:u:1> <urn:x:rare> <urn:o:1> . <urn:u:2> <urn:x:rare> <urn:o:2> . <urn:u:3> <urn:x:rare> <urn:o:3> . '
I1B = (('STR', 'SELECT ?doc WHERE { ' + KEY + 'BIND(STR(?o) AS ?name) ?doc <urn:x:label> ?name . }', ['urn:doc:1']),
       ('IRI', 'SELECT ?t WHERE { ' + KEY + 'BIND(IRI(CONCAT("urn:doc:", STRAFTER(STR(?o), "urn:o:"))) AS ?d) '
               '?d <urn:x:title> ?t . }', ['title-1']))
for n in (1000, 10000):
    docs = ' '.join('<urn:doc:%d> <urn:x:label> "urn:o:%d" ; <urn:x:title> "title-%d" .' % (i, i, i)
                    for i in range(1, n + 1))
    run, _ = ledger('INSERT DATA { %s %s }' % (docs, RARE))
    assert run('reindex', '--direct', 'repro:main').returncode == 0
    for name, q, expected in I1B:
        ms, got = timed(run, q)
        check('I1b work %s N=%d' % (name, n), got, expected, 'median %5.0f ms' % ms)

sizes = ' '.join('<urn:doc:%d> <urn:x:size> %d .' % (i, i) for i in range(1, 51))
run, _ = ledger('INSERT DATA { %s <urn:d:x> <urn:x:size> "7"^^<http://www.w3.org/2001/XMLSchema#long> . %s }'
                % (sizes, RARE))
NUM = 'SELECT ?doc WHERE { ' + KEY + 'BIND(STRLEN(STR(?o)) AS ?len) ?doc <urn:x:size> ?len . }'
for mode in ('novelty', 'indexed'):
    if mode == 'indexed':
        assert run('reindex', '--direct', 'repro:main').returncode == 0
    check('I1b rows STRLEN %s' % mode, values(run, NUM), ['urn:d:x', 'urn:doc:7'])

UNION = ('SELECT ?o WHERE { ?item <urn:x:code> ?id BIND(COALESCE(IRI(CONCAT("urn:u:", ?id)), <urn:u:0>) AS ?x) '
         '{ ?x <urn:x:rare> ?o } UNION { ?x <urn:x:rarer> ?o } }')
for n in (1000, 10000):
    items = ' '.join('<urn:i:%06d> <urn:x:code> "%06d" .' % (i, i) for i in range(n))
    run, _ = ledger('INSERT DATA { %s <urn:x:a> <urn:x:b> <urn:x:c> . }' % items)
    assert run('reindex', '--direct', 'repro:main').returncode == 0
    ms, got = timed(run, UNION)
    check('N1 UNION N=%d' % n, got, [], 'median %5.0f ms' % ms)

roles = ' '.join('<urn:u:%d> <urn:x:role> "role-%d" .' % (i, i) for i in range(1, 6))
run, work = ledger('INSERT DATA { %s GRAPH <urn:g:keys> { <urn:k:1> <urn:x:did> "did:example:other" . } }' % roles)
STRICT = ('INSERT { GRAPH <urn:g:log> { <urn:x:log> <urn:x:n> ?n } } WHERE { OPTIONAL { GRAPH <urn:g:keys> { '
          '?key <urn:x:did> "did:example:none" ; <urn:x:account> ?keyAccount } } '
          'BIND(COALESCE(?keyAccount, <%s>) AS ?reader) ?reader <urn:x:role> ?role '
          'BIND(IF(?reader = <%s>, ?reader + 1, 0) AS ?n) }')
ok, msg = update(run, STRICT % ('urn:u:1', 'urn:u:2'), work)
check('N2 strict, fails on urn:u:2', 'fails' if not ok and 'arithmetic' in msg else 'succeeds', 'fails')
ok, msg = update(run, STRICT % ('urn:x:nobody', 'urn:x:nobody'), work)
check('N2 strict, fallback without a role', 'succeeds' if ok else 'fails: ' + msg[:120], 'succeeds')
check('N2 strict, nothing written',
      values(run, 'SELECT ?n WHERE { GRAPH <urn:g:log> { <urn:x:log> <urn:x:n> ?n } }'), [])
sys.exit(1 if wrong else 0)
