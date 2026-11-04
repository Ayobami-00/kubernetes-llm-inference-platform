"""Read-only private telemetry through a bounded operator port-forward."""

import json
from urllib.parse import urlencode

PROGRAM = r"""
import json,select,subprocess,sys,urllib.request
request=json.load(sys.stdin)
target=request['target']
if target not in {'service/klip-thanos-query','service/klip-monitoring-prometheus'}:
 raise ValueError('Unexpected telemetry target')
p=subprocess.Popen(['kubectl','--kubeconfig=/etc/kubernetes/admin.conf','-n','observability',
 'port-forward','--address=127.0.0.1',target,':9090'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
try:
 if not select.select([p.stdout],[],[],15)[0]: raise ValueError('Telemetry tunnel did not open')
 line=p.stdout.readline()
 if not line.startswith('Forwarding from 127.0.0.1:'): raise ValueError('Unexpected tunnel address')
 port=int(line.split('127.0.0.1:')[1].split(' ')[0])
 result={}
 for name,path in request['paths'].items():
  if not path.startswith('/api/v1/') or '\r' in path or '\n' in path:
   raise ValueError('Only read-only telemetry API paths allowed')
  with urllib.request.urlopen('http://127.0.0.1:'+str(port)+path,timeout=30) as r:
   raw=r.read(16*1024*1024+1)
   if len(raw)>16*1024*1024: raise ValueError('Telemetry response exceeds bound')
   result[name]=json.loads(raw)
 print(json.dumps(result))
finally:
 p.terminate()
 try:p.wait(timeout=10)
 except subprocess.TimeoutExpired:p.kill();p.wait()
"""


def read(paths, *, target="service/klip-thanos-query"):
    from cluster import Host, load_hosts

    return json.loads(
        Host(load_hosts()["cp-01"]).run(
            ["sudo", "python3", "-c", PROGRAM],
            data=json.dumps({"target": target, "paths": paths}),
            timeout=120,
        )
    )


def query(expression):
    return read({"query": "/api/v1/query?" + urlencode({"query": expression})})["query"]
