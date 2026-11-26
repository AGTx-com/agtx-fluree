"""Does a GRAPH block drop a variable that the row before it left unbound?

Usage: python3 -I query_repro.py <fluree-binary>

Plain Fluree CLI in --direct mode, as the administrator (no policy), on a fresh temporary ledger. Each query runs
before and after `fluree reindex`. The row reaching the GRAPH block leaves ?reader unbound (an OPTIONAL that found
nothing, or a BIND whose expression errors), and the block binds ?reader itself. SPARQL's join keeps the block's
value; the bug returns the block's rows with ?reader missing. The controls bind ?reader with a default-graph
triple instead, or reach the block with ?reader already bound.
"""
import json, os, subprocess, sys, tempfile
cli = sys.argv[1]
SEED = '''INSERT DATA {
  GRAPH <urn:g:shared> { <urn:u:1> <urn:x:person> <urn:p:1> . <urn:u:2> <urn:x:person> <urn:p:2> . }
  <urn:u:1> <urn:x:role> <urn:r:1> . <urn:u:2> <urn:x:role> <urn:r:2> .
  <urn:x:a> <urn:x:b> <urn:x:c> .
}'''
NOT_FOUND = 'OPTIONAL { <urn:x:a> <urn:x:none> ?reader }'
ERRORS = 'VALUES ?v { "x" } BIND(IRI(?v + 1) AS ?reader)'
BLOCK = 'GRAPH <urn:g:shared> { ?reader <urn:x:person> ?o }'
BOTH = 'u:1 p:1; u:2 p:2'
QUERIES = [
    ('OPTIONAL found nothing, then GRAPH <iri>', NOT_FOUND + ' ' + BLOCK, BOTH),
    ('BIND errors, then GRAPH <iri>', ERRORS + ' ' + BLOCK, BOTH),
    ('BIND errors, then GRAPH ?g', ERRORS + ' GRAPH ?g { ?reader <urn:x:person> ?o }', BOTH),
    ('BIND errors, then a UNION with a GRAPH branch',
     ERRORS + ' { ' + BLOCK + ' } UNION { ?reader <urn:x:missing> ?o }', BOTH),
    ('OPTIONAL found nothing, then a default-graph triple (control)',
     NOT_FOUND + ' ?reader <urn:x:role> ?o', 'u:1 r:1; u:2 r:2'),
    ('BIND errors, then a default-graph triple (control)', ERRORS + ' ?reader <urn:x:role> ?o', 'u:1 r:1; u:2 r:2'),
    ('BIND binds ?reader, then GRAPH <iri> (control)',
     'VALUES ?v { "urn:u:1" } BIND(IRI(?v) AS ?reader) ' + BLOCK, 'u:1 p:1'),
]
def short(b, var):
    return b[var]['value'].replace('urn:', '') if var in b else '-'
work = tempfile.mkdtemp(prefix='fluree-graph-unbound-')
env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
def run(*a):
    return subprocess.run([cli, '--quiet', '--no-color', *a], cwd=work, env=env, text=True, capture_output=True,
                          timeout=120)
assert run('init').returncode == 0 and run('create', 'repro:main').returncode == 0
r = run('update', '--direct', '-l', 'repro:main', '--format', 'sparql', '-e', SEED); assert r.returncode == 0, r.stderr
print(subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip())
wrong = 0
for mode in ('novelty', 'indexed'):
    if mode == 'indexed':
        assert run('reindex', '--direct', 'repro:main').returncode == 0
    for name, where, expected in QUERIES:
        q = 'SELECT ?reader ?o WHERE { %s } ORDER BY ?o' % where
        out = run('query', '--direct', '-l', 'repro:main', '--sparql', '--format', 'json', '-e', q)
        try:
            got = '; '.join('%s %s' % (short(b, 'reader'), short(b, 'o'))
                            for b in json.loads(out.stdout)['results']['bindings'])
        except (ValueError, KeyError):
            got = 'error: ' + (out.stderr or out.stdout)[:200]
        ok = got == expected; wrong += not ok
        print('%-8s %-62s expected=[%s] got=[%s] %s' % (mode, name, expected, got, '' if ok else 'WRONG'))
sys.exit(1 if wrong else 0)
