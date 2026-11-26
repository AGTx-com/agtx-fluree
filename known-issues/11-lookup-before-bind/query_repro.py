"""Does Fluree run a lookup that reads a BIND-defined variable before that BIND, scanning every record?

Usage: python3 -I query_repro.py <fluree-binary>

Plain Fluree CLI in --direct mode, as the administrator (no policy), on fresh temporary ledgers of N users.
The query mirrors the shape found in a read policy: an OPTIONAL that may find a key's account, a BIND that fills
in the account (from the key, else a fixed fallback), then a GRAPH lookup of that account's user record. If the
engine evaluates the lookup before the BIND, it scans every user and the BIND's clobber check discards all but one,
so time grows with N even though exactly one row comes back. The control writes the BIND inside the GRAPH block.
"""
import os
import subprocess
import sys
import tempfile
import time

cli = sys.argv[1]
LEDGER = 'repro:main'

def seed(n, target):
    rows = ' '.join('<urn:u:%06d> a <urn:x:User> ; <urn:x:person> <urn:p:%06d> .' % (i, i) for i in range(n))
    return ('INSERT DATA { GRAPH <urn:g:shared> { %s } '
            'GRAPH <urn:g:keys> { <urn:k:none> <urn:x:did> "did:example:other" . } }' % rows)

LIFTED = '''SELECT ?person WHERE {
  OPTIONAL { GRAPH <urn:g:keys> { ?key <urn:x:did> "did:example:reader" ; <urn:x:account> ?keyAccount } }
  BIND(COALESCE(?keyAccount, <urn:u:TARGET>) AS ?reader)
  GRAPH <urn:g:shared> { ?reader a <urn:x:User> ; <urn:x:person> ?person }
}'''
CONTROL = '''SELECT ?person WHERE {
  OPTIONAL { GRAPH <urn:g:keys> { ?key <urn:x:did> "did:example:reader" ; <urn:x:account> ?keyAccount } }
  GRAPH <urn:g:shared> { BIND(COALESCE(?keyAccount, <urn:u:TARGET>) AS ?reader) ?reader a <urn:x:User> ; <urn:x:person> ?person }
}'''

print(subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip())
for n in (1000, 10000, 40000):
    work = tempfile.mkdtemp(prefix='fluree-bindorder-')
    env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
    def run(*a):
        return subprocess.run([cli, '--quiet', '--no-color', *a], cwd=work, env=env, text=True, capture_output=True,
                              timeout=600)
    assert run('init').returncode == 0 and run('create', LEDGER).returncode == 0
    seed_file = os.path.join(work, 'seed.rq')
    open(seed_file, 'w').write(seed(n, n - 1))
    r = run('update', '--direct', '-l', LEDGER, '--format', 'sparql', '-f', seed_file)
    assert r.returncode == 0, r.stderr[:300]
    assert run('reindex', '--direct', LEDGER).returncode == 0
    target = '%06d' % (n - 1)  # sorts last, so a scan visits every user before it
    line = 'N=%6d' % n
    for name, q in (('lifted', LIFTED), ('control', CONTROL)):
        q = q.replace('TARGET', target)
        times = []
        out = None
        for _ in range(3):
            t0 = time.perf_counter()
            res = run('query', '--direct', '-l', LEDGER, '--sparql', '--format', 'json', '-e', q)
            times.append((time.perf_counter() - t0) * 1000)
            out = res.stdout.strip()
        rows = out.count('"person"')
        line += ' | %-7s median %7.0f ms rows=%s' % (name, sorted(times)[1], rows)
    print(line)
