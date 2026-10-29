"""Finite data and literal encoding; no shared transition logic."""
import json,base64,copy,runpy,pathlib,itertools
from typing import NamedTuple
ROOT=pathlib.Path(__file__).resolve().parents[3]
SCHEMA=runpy.run_path(str(ROOT/'scripts/tests/v3/execution/schema.test.py'))
ORACLE=runpy.run_path(str(ROOT/'scripts/tests/v3/execution/reference_oracle.py'))['replay_broker']
FIXTURE=json.loads((ROOT/'scripts/tests/v3/execution/fixtures/F-M1B-020.json').read_text())['input']
CONFIGS=tuple(itertools.product(('event','utf8-byte'),(1,2),(1,2),(1,2)))
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'))
def require(v,m):
 if not v:raise ValueError(m)
def msg(k,**x):return dict(type=k,protocol_version='m1b-broker/1',stream_id='stream-a',daemon_epoch=2,**x)
def cursor(n,c):return n*(c[1] if c[0]=='utf8-byte' else 1)
def encode(action,s,c):
 u,z,qe,qb=c
 if action=='gap':return msg('WELCOME',client_id='client-a',replay_floor=cursor(1,c),snapshot_cursor=cursor(1,c),cursor_unit=u,input_lease=None,resize_lease=None,capabilities=['attach'])
 if action=='reset':return msg('RESET',client_id='client-a',reason='gap',snapshot_cursor=cursor(1,c),cursor_unit=u)
 if action=='credit':return msg('CREDIT',client_id='client-a',bytes=z,events=1)
 if action=='emit':return msg('EVENT',cursor=cursor(s['source'][0],c),cursor_unit=u,event_class='output',process_id='process-a',payload_base64=base64.b64encode(b'x'*z).decode(),payload_untrusted=True)
 if action=='ack':return msg('ACK',client_id='client-a',cursor=cursor(s['sent'],c),cursor_unit=u)
 return None
def initial(c):
 h=copy.deepcopy(FIXTURE['messages'][0]);h['cursor_unit']=c[0]
 return dict(phase=0,source=(2,3),pending=(),byte_credit=c[1],event_credit=1,grants=0,sent=0,acknowledged=0,installed=0,installed_cursor=0,snapshot=(cursor(1,c),0),view_reserved=0,reserved=6,free=4,owner=('pool-a','goal-a','operation-a','owner-a',6),restart=0,lost_ack=0,control=0,quota=0,wire=(canonical(h),canonical(encode('credit',{},c))))
PRODUCER=('source',)
RECEIVER=('phase','pending','byte_credit','event_credit','grants','sent','acknowledged','wire')
APP=('installed','installed_cursor','snapshot','view_reserved','reserved','free','owner','restart','lost_ack','control','quota')
class Split(NamedTuple):
 producer:dict
 receiver:dict
 application:dict
def split(s):return Split(*({k:s[k] for k in fields} for fields in (PRODUCER,RECEIVER,APP)))
def flatten(s):return dict(**s.producer,**s.receiver,**s.application) if isinstance(s,Split) else dict(s)
def evaluate(wire,c):
 v=copy.deepcopy(FIXTURE);v.update(messages=[json.loads(m) for m in wire],queue_limit_events=c[2],queue_limit_bytes=c[3]*c[1])
 # Strict decoding is exercised on every distinct literal history; schema on all messages.
 v=SCHEMA['strict_loads'](canonical(v).encode());SCHEMA['validate'](v,'BrokerTraceInput');return ORACLE(v)
