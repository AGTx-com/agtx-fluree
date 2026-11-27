"""The two plans the review of the first bug-11 fix (b13dc10) found wrong, on the CLI.

Usage: python3 -I review_repro.py <fluree-binary>

Plain Fluree CLI in --direct mode, as the administrator (no policy), on fresh temporary ledgers.

C1, rows. A BIND that leaves ?reader unbound (no key has the DID), a lookup of ?reader's role, and a FILTER that
reads ?reader. The expected counts are the stock release's, which are also SPARQL's: the FILTER sees the value the
lookup binds. Run before and after `fluree reindex`.

I1, work. A BIND that always binds (a fallback IRI), a selective lookup (three `rare` triples) and a second
pattern joined to it through ?o (N labels). The answer is one row; the time should not grow with N. Median of
three, including process start-up, after `fluree reindex`.
"""
import json, os, subprocess, sys, tempfile, time
cli = sys.argv[1]

def ledger(seed):
    work = tempfile.mkdtemp(prefix='fluree-bind-review-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
    def run(*a):
        return subprocess.run([cli, '--quiet', '--no-color', *a], cwd=work, env=env, text=True,
                              capture_output=True, timeout=600)
    assert run('init').returncode == 0 and run('create', 'repro:main').returncode == 0
    path = os.path.join(work, 'seed.rq')
    open(path, 'w').write(seed)
    r = run('update', '--direct', '-l', 'repro:main', '--format', 'sparql', '-f', path)
    assert r.returncode == 0, r.stderr[:300]
    return run

def rows(run, q):
    out = run('query', '--direct', '-l', 'repro:main', '--sparql', '--format', 'json', '-e', q)
    try:
        return len(json.loads(out.stdout)['results']['bindings'])
    except (ValueError, KeyError):
        return 'error: ' + (out.stderr or out.stdout)[:200]

print(subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip())
wrong = 0

users = ' '.join('<urn:u:%d> <urn:x:role> "role-%d" .' % (i, i) for i in range(1, 6))
C1_SEED = ('INSERT DATA { %s GRAPH <urn:g:keys> { <urn:k:1> <urn:x:did> "did:example:other" ; '
           '<urn:x:account> <urn:u:9> . } }' % users)
C1 = ('SELECT ?reader ?role WHERE { OPTIONAL { GRAPH <urn:g:keys> { ?key <urn:x:did> "did:example:none" ; '
      '<urn:x:account> ?keyAccount } } BIND(COALESCE(?keyAccount) AS ?reader) ?reader <urn:x:role> ?role %s }')
run = ledger(C1_SEED)
for mode in ('novelty', 'indexed'):
    if mode == 'indexed':
        assert run('reindex', '--direct', 'repro:main').returncode == 0
    for name, extra, expected in (('no FILTER', '', 5), ('FILTER(?reader != <urn:u:1>)', 'FILTER(?reader != <urn:u:1>)', 4),
                                  ('FILTER(BOUND(?reader))', 'FILTER(BOUND(?reader))', 5)):
        got = rows(run, C1 % extra)
        ok = got == expected
        wrong += not ok
        print('C1 %-8s %-30s expected=%s got=%s %s' % (mode, name, expected, got, '' if ok else 'WRONG'))

I1 = ('SELECT ?l WHERE { OPTIONAL { ?key <urn:x:did> "did:example:reader" ; <urn:x:account> ?keyAccount } '
      'BIND(COALESCE(?keyAccount, <urn:u:1>) AS ?r) ?r <urn:x:rare> ?o . ?o <urn:x:label> ?l }')
for n in (1000, 10000):
    labels = ' '.join('<urn:o:%d> <urn:x:label> "label-%d" .' % (i, i) for i in range(1, n + 1))
    run = ledger('INSERT DATA { %s <urn:u:1> <urn:x:rare> <urn:o:1> . <urn:u:2> <urn:x:rare> <urn:o:2> . '
                 '<urn:u:3> <urn:x:rare> <urn:o:3> . }' % labels)
    assert run('reindex', '--direct', 'repro:main').returncode == 0
    times = []
    for _ in range(3):
        t0 = time.perf_counter()
        got = rows(run, I1)
        times.append((time.perf_counter() - t0) * 1000)
    ok = got == 1
    wrong += not ok
    print('I1 N=%6d median %5.0f ms rows=%s %s' % (n, sorted(times)[1], got, '' if ok else 'WRONG'))
sys.exit(1 if wrong else 0)
