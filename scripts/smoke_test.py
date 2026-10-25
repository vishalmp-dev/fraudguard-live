import os, subprocess, sys, tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
code=r'''
from fastapi.testclient import TestClient
import app
c=TestClient(app.app)
assert c.get('/healthz').status_code==200
assert c.post('/api/reset').status_code==200
r=c.post('/api/bootstrap',json={'count':30});assert r.status_code==200 and r.json()['events_added']==90
s=c.get('/api/state').json();assert len(s['banks'])==3 and s['transactions_processed']==90 and all(b['model_ready'] for b in s['banks'])
assert c.post('/api/federation/aggregate').status_code==200
for k in ['otp','atm','mule']:assert c.post('/api/scenario/'+k).status_code==200
p={'bank_id':'BANK_A','account_id':'TEST_A1','counterparty_id':'TEST_B1','amount':1000,'direction':'OUT','channel':'UPI'}
assert c.post('/api/ingest',json=p,headers={'X-API-Key':'bad'}).status_code==401
assert c.post('/api/ingest',json={**p,'is_fraud':1},headers={'X-API-Key':'local-demo-key'}).status_code==422
assert c.post('/api/ingest',json=p,headers={'X-API-Key':'local-demo-key'}).status_code==200
assert c.get('/api/export.csv').status_code==200
assert c.get('/').status_code==200
print('PASS: health, bootstrap, 3 nodes, FedAvg, 3 scenarios, auth, label rejection, CSV, UI')
'''
with tempfile.TemporaryDirectory(prefix='fraudguard-test-') as d:
 env=os.environ.copy();env['FRAUDGUARD_RUNTIME']=d;env['FRAUDGUARD_INGEST_API_KEY']='local-demo-key'
 p=subprocess.run([sys.executable,'-c',code],cwd=root,env=env,text=True,capture_output=True)
 print(p.stdout,end='')
 if p.returncode:print(p.stderr,file=sys.stderr);sys.exit(p.returncode)
