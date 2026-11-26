"""Same EXISTS shape in an ordinary query (no policy), run by the administrator.

Usage: python3 -I query_repro.py <fluree-binary>

Plain Fluree CLI in --direct mode on a fresh temporary ledger; no server, no policy. Runs each query before and
after `fluree reindex`. Same data as policy_repro.py.
"""
import json, os, subprocess, sys, tempfile
cli = sys.argv[1]
SEED = '''PREFIX ex: <http://example.org/ns/>
INSERT DATA {
  ex:doc1 ex:workspace ex:ws1 .
  ex:doc2 ex:workspace ex:ws2 .
  GRAPH <http://example.org/members/ws1> { ex:bob ex:memberOf ex:ws1 . ex:alice ex:memberOf ex:ws2 . }
  GRAPH <http://example.org/members/ws2> { ex:alice ex:memberOf ex:ws2 . ex:alice ex:memberOf ex:ws1 . }
}'''
P = 'PREFIX ex: <http://example.org/ns/> '
B = 'BIND(IRI(CONCAT("http://example.org/members/", STRAFTER(STR(?w), "http://example.org/ns/"))) AS ?g)'
QUERIES = [
    ('EXISTS, BIND computes the GRAPH name', P + 'SELECT ?u ?d WHERE { VALUES ?u { ex:bob ex:alice } ?d ex:workspace ?w '
     'FILTER EXISTS { ' + B + ' GRAPH ?g { ?u ex:memberOf ?w } } } ORDER BY ?u ?d', 'alice doc2; bob doc1'),
    ('NOT EXISTS, BIND computes the GRAPH name', P + 'SELECT ?u ?d WHERE { VALUES ?u { ex:bob ex:alice } ?d ex:workspace ?w '
     'FILTER NOT EXISTS { ' + B + ' GRAPH ?g { ?u ex:memberOf ?w } } } ORDER BY ?u ?d', 'alice doc1; bob doc2'),
    ('EXISTS, compare after GRAPH ?g (control)', P + 'SELECT ?u ?d WHERE { VALUES ?u { ex:bob ex:alice } ?d ex:workspace ?w '
     'FILTER EXISTS { BIND(CONCAT("http://example.org/members/", STRAFTER(STR(?w), "http://example.org/ns/")) AS ?e) '
     'GRAPH ?g { ?u ex:memberOf ?w } FILTER(STR(?g) = ?e) } } ORDER BY ?u ?d', 'alice doc2; bob doc1'),
]
work = tempfile.mkdtemp(prefix='fluree-boundgraph-plain-')
env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
def run(*a):
    return subprocess.run([cli, '--quiet', '--no-color', *a], cwd=work, env=env, text=True, capture_output=True, timeout=120)
assert run('init').returncode == 0 and run('create', 'repro:main').returncode == 0
r = run('update', '--direct', '-l', 'repro:main', '--format', 'sparql', '-e', SEED); assert r.returncode == 0, r.stderr
print(subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip())
wrong = 0
for mode in ('novelty', 'indexed'):
    if mode == 'indexed':
        assert run('reindex', '--direct', 'repro:main').returncode == 0
    for name, q, expected in QUERIES:
        out = run('query', '--direct', '-l', 'repro:main', '--sparql', '--format', 'json', '-e', q)
        try:
            got = '; '.join('%s %s' % (b['u']['value'].rsplit(':', 1)[-1].rsplit('/', 1)[-1], b['d']['value'].rsplit(':', 1)[-1].rsplit('/', 1)[-1])
                            for b in json.loads(out.stdout)['results']['bindings'])
        except (ValueError, KeyError):
            got = 'error: ' + (out.stderr or out.stdout)[:200]
        ok = got == expected; wrong += not ok
        print('%-8s %-42s expected=[%s] got=[%s] %s' % (mode, name, expected, got, '' if ok else 'WRONG'))
sys.exit(1 if wrong else 0)
