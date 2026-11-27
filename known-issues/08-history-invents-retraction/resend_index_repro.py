"""Does re-sending an unchanged fact add a history row, and does it depend on whether the fact is indexed?

Usage: python3 -I resend_index_repro.py <fluree-binary> <port> <output.json>

Plain Fluree, `fluree server run --memory`, default graph only, no application config. For three write forms, an edge
<urn:x:S> <urn:x:link> <urn:x:b> is inserted and then re-sent unchanged in a later commit:
  novelty  the re-send happens while the original fact is still only in novelty;
  indexed  POST /reindex (synchronous) runs between the insert and the re-send, so the fact is in the index.
After the re-send, the commit itself is decoded with GET /show (?commit=t:N) and the edge's history is queried.
Then /reindex runs again and the history is queried a second time.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

cli, port, output = sys.argv[1], sys.argv[2], sys.argv[3]
base = 'http://127.0.0.1:%s/v1/fluree' % port
work = tempfile.mkdtemp(prefix='fluree-resend-')
env = dict(os.environ, HOME=work, XDG_CONFIG_HOME=work)
log_path = os.path.join(work, 'server.log')
log = open(log_path, 'w')
server = subprocess.Popen([cli, 'server', 'run', '--memory', '--listen-addr', '127.0.0.1:' + port, '--log-level', 'info'],
                          cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT)
L = 'repro:main'
record = {'version': None, 'cases': []}


def http(method, path, body=None, content_type='application/json'):
    data = None if body is None else (body if isinstance(body, str) else json.dumps(body)).encode()
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={'Content-Type': content_type, 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, text = response.status, response.read().decode()
    except urllib.error.HTTPError as failure:
        status, text = failure.code, failure.read().decode()
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def write(body):
    if isinstance(body, str):
        status, result = http('POST', '/update?ledger=' + urllib.parse.quote(L), body, 'application/sparql-update')
    else:
        status, result = http('POST', '/update?ledger=' + urllib.parse.quote(L), body)
    assert status == 200, (status, result)
    return result.get('t') if isinstance(result, dict) else None


def reindex():
    status, result = http('POST', '/reindex', {'ledger': L})
    assert status == 200, (status, result)
    return result.get('index_t')


def show(t):
    status, result = http('GET', '/show/%s?commit=t:%d' % (L, t))
    return {'status': status, 'asserts': result.get('asserts'), 'retracts': result.get('retracts'),
            'flakes': result.get('flakes')} if isinstance(result, dict) else {'status': status, 'body': result}


def history(subject):
    query = ('PREFIX f: <https://ns.flur.ee/db#>\nSELECT ?o ?t ?op FROM <%s@t:1> TO <%s@t:latest>\n'
             'WHERE { << <%s> <urn:x:link> ?o >> f:t ?t ; f:op ?op } ORDER BY ?t ?op' % (L, L, subject))
    status, result = http('POST', '/query', query, 'application/sparql-query')
    if isinstance(result, dict) and 'results' in result:
        return [(b['t']['value'], b['op']['value']) for b in result['results']['bindings']]
    return {'status': status, 'body': result}


def forms(subject):
    return {
        'JSON-LD insert (unguarded)': {'insert': {'@id': subject, 'urn:x:link': {'@id': 'urn:x:b'}}},
        'JSON-LD update guarded by the edge': {'where': [{'@id': subject, 'urn:x:link': {'@id': 'urn:x:b'}}],
                                               'insert': [{'@id': subject, 'urn:x:link': {'@id': 'urn:x:b'}}]},
        'SPARQL INSERT DATA': 'INSERT DATA { <%s> <urn:x:link> <urn:x:b> }' % subject,
    }


try:
    for _ in range(80):
        try:
            urllib.request.urlopen('http://127.0.0.1:%s/health' % port, timeout=1)
            break
        except OSError:
            time.sleep(0.25)
    record['version'] = subprocess.run([cli, '--version'], capture_output=True, text=True).stdout.strip()
    for args in (['init'], ['remote', 'add', 'local', 'http://127.0.0.1:' + port], ['create', L, '--remote', 'local']):
        r = subprocess.run([cli, '--quiet', '--no-color', *args], cwd=work, env=env, capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 0, (args, r.stdout, r.stderr)
    n = 0
    for mode in ('novelty', 'indexed'):
        for name in forms('urn:x:placeholder'):
            n += 1
            subject = 'urn:x:s%d' % n
            first = write({'insert': {'@id': subject, 'urn:x:link': {'@id': 'urn:x:b'}}})
            index_before = reindex() if mode == 'indexed' else None
            resend = write(forms(subject)[name])
            case = {'mode': mode, 'form': name, 'subject': subject, 'insertT': first, 'indexTBeforeResend': index_before,
                    'resendT': resend, 'resendCommit': show(resend) if resend else None,
                    'historyAfterResend': history(subject)}
            case['indexTAfter'] = reindex()
            case['historyAfterReindex'] = history(subject)
            record['cases'].append(case)
finally:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
    log.close()
    record['serverLog'] = open(log_path).read()[-20000:]
    with open(output, 'w') as out:
        json.dump(record, out, indent=1)

print(record['version'])
for case in record['cases']:
    commit = case['resendCommit'] or {}
    print('%-8s %-36s insert t=%s resend t=%s | commit asserts=%s retracts=%s flakes=%s | history %s | after reindex %s'
          % (case['mode'], case['form'], case['insertT'], case['resendT'], commit.get('asserts'), commit.get('retracts'),
             commit.get('flakes'), case['historyAfterResend'], case['historyAfterReindex']))
