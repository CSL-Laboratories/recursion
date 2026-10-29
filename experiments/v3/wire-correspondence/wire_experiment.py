"""All declared finite wire histories, independent ownership audit and literal controls."""
import argparse,collections,copy,ctypes,datetime,hashlib,json,pathlib,time
import wire_domain as d,wire_models as model,wire_audit as audit
ROOT=d.ROOT
MUTANTS=('install-guard','install-reservation','restart-reservation','quota-reservation','lost-ack-release','erase-source')
def write(p,x):p.write_text(json.dumps(x,indent=2)+'\n',encoding='utf-8')
def peak_bytes():
    class Counters(ctypes.Structure):
        _fields_=[('cb',ctypes.c_ulong),('faults',ctypes.c_ulong)]+[(n,ctypes.c_size_t) for n in ('peak','working','pp','p','np','n','page','peakpage')]
    c=Counters();c.cb=ctypes.sizeof(c)
    if not ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(-1),ctypes.byref(c),c.cb):raise OSError('cannot measure process memory')
    return c.peak


def explore(c,limit=500000,seconds=600,mutant=None,calibration=None):
 start=time.monotonic();s=d.initial(c)
 if calibration=='initial':s['reserved']=0
 if calibration=='initial-view':s['view_reserved']=99
 audit.initialization(s,c)
 todo=collections.deque([(s,[])]);seen={d.canonical(s)};edges=set();wire_cache={};witnesses={};terminal=0
 while todo:
  s,trace=todo.popleft();aa=model.inclusive(s,c,mutant);bb=model.separated(d.split(s),c)
  if calibration=='missing-edge' and aa:aa=aa[1:]
  if calibration=='no-op' and aa:aa=[(aa[0][0],s)]+aa[1:]
  if mutant and {a for a,n in aa}!=audit.enabled(s,c):
   bad=[(a,n) for a,n in aa if a not in audit.enabled(s,c)];d.require(bool(bad),'mutant removed expected action');a,n=bad[0]
   return {'guard':mutant,'schedule':trace+[a],'before':s,'after':n,'failure':'complete enabledness: forbidden '+a}
  d.require({a for a,n in aa}==audit.enabled(s,c),'complete enabledness')
  d.require(len({a for a,n in aa})==len(aa),'no duplicate action labels')
  if not mutant:d.require({a:d.canonical(n) for a,n in aa}=={a:d.canonical(d.flatten(n)) for a,n in bb},'independent representation transitions')
  if s['wire'] not in wire_cache:wire_cache[s['wire']]=d.evaluate(s['wire'],c)
  decision=wire_cache[s['wire']]
  expected=('accepted' if s['phase']==0 else 'refused' if s['phase']==1 else 'accepted-reset')
  d.require(decision['status']==expected and decision['reasons']==([] if s['phase']==0 else ['replay-gap']),'literal prefix correspondence')
  d.require(decision['final_cursor']==d.cursor(s['sent'],c),'wire/model cursor correspondence')
  if not aa:terminal+=1
  for a,n in aa:
   try:audit.check(s,a,n,c)
   except ValueError as e:
    if mutant:return {'guard':mutant,'schedule':trace+[a],'before':s,'after':n,'failure':str(e)}
    raise
   if not mutant:audit.check(s,a,d.flatten(dict(bb)[a]),c)
   path=trace+[a]
   for name,ok in [('restart-before-install',a=='restart' and not s['installed']),('restart-pending',a=='restart' and bool(s['pending'])),('lost-ack-retains-pending',a=='lose-ack'),('fresh-credit-before-install',a=='credit' and not s['installed']),('installation-preserves-future-and-reservation',a=='install'),('ack-retry-after-loss',a=='ack' and s['lost_ack']),('complete-recovery',n['sent']==3 and n['acknowledged']==3),('control-during-gap',a=='control' and s['phase']==1)]:
    if ok and name not in witnesses:witnesses[name]={'schedule':path,'before':s,'after':n,'wire_decision':d.evaluate(n['wire'],c)}
   edge=d.canonical([s,a,n]);edges.add(edge);key=d.canonical(n)
   if key not in seen:seen.add(key);todo.append((n,path))
  d.require(len(seen)<=limit and time.monotonic()-start<=seconds,'incomplete exploration resource bound')
  if calibration=='truncated':d.require(not todo,'incomplete frontier')
 if mutant:raise ValueError('vacuous mutant '+mutant)
 d.require(terminal>0 and len(witnesses)==8,'nonvacuous required witnesses')
 return {'configuration':c,'states':len(seen),'edges':len(edges),'distinct_wire_histories':len(wire_cache),'terminal_states':terminal,'frontier':0,'exact_graph_equal':True,'graph_sha256':hashlib.sha256('\n'.join(sorted(edges)).encode()).hexdigest(),'witnesses':witnesses}

def calibrate():
 labels=[]
 for k in ('initial','initial-view','missing-edge','no-op','truncated'):
  try:explore(d.CONFIGS[0],calibration=k)
  except ValueError as e:labels.append({'calibration':k,'failure':str(e)})
  else:raise ValueError('checker missed '+k)
 return labels

def controls():
 c=d.CONFIGS[0];s=d.initial(c);wire=list(s['wire'])
 def step(a):
  nonlocal s
  s=dict(model.inclusive(s,c))[a]
 for a in ('gap','reset','install','credit','emit'):step(a)
 prefix=list(s['wire']);gap=prefix[:3];reset=prefix[3];credit=prefix[4];event=prefix[5]
 ack=d.canonical(d.msg('ACK',client_id='client-a',cursor=2,cursor_unit='event'))
 e3=json.loads(event);e3['cursor']=3;e3=d.canonical(e3)
 cases={
 'positive-recovery':(prefix,'accepted-reset','replay-gap'),
 'unexpected-reset':(wire+[d.canonical(d.msg('RESET',client_id='client-a',reason='gap',snapshot_cursor=0,cursor_unit='event'))],'refused','unexpected-reset'),
 'event-before-reset':(gap+[event,reset],'refused','replay-gap-continuation'),
 'old-credit-after-reset':(gap+[reset,event],'refused','credit-missing'),
 'reused-credit':(prefix+[ack,e3],'refused','credit-exhausted'),
 'credit-without-ack':(prefix+[credit,e3],'refused','credit-exhausted'),
 'ack-ahead':(prefix+[d.canonical(d.msg('ACK',client_id='client-a',cursor=3,cursor_unit='event'))],'refused','ack-ahead'),
 }
 for name,field,value,reason in [('stale-epoch','daemon_epoch',1,'stale-daemon-epoch'),('wrong-stream','stream_id','stream-b','stream-mismatch'),('wrong-unit','cursor_unit','utf8-byte','cursor-unit-mismatch'),('byte-credit-exhaustion','payload_base64','eHg=','credit-exhausted')]:
  e=json.loads(event);e[field]=value;cases[name]=(prefix[:-1]+[d.canonical(e)],'refused',reason)
 rows={}
 for name,(messages,status,reason) in cases.items():
  decision=d.evaluate(tuple(messages),c);d.require(decision['status']==status and reason in decision['reasons'],'literal control '+name);rows[name]={'messages':[json.loads(x) for x in messages],'decision':decision}
 # The exact accepted bytes contain no application/reservation state.
 byte_config=('utf8-byte',2,1,1);byte_state=d.initial(byte_config)
 for action in ('gap','reset','install','credit','emit'):byte_state=dict(model.inclusive(byte_state,byte_config))[action]
 byte_wire=list(byte_state['wire']);bad_event=json.loads(byte_wire[-1]);bad_event['cursor']=3;byte_wire[-1]=d.canonical(bad_event)
 byte_decision=d.evaluate(tuple(byte_wire),byte_config);d.require(byte_decision['status']=='refused' and 'utf8-byte-cursor-mismatch' in byte_decision['reasons'],'wrong byte cursor')
 rows['wrong-byte-cursor']={'configuration':byte_config,'messages':[json.loads(x) for x in byte_wire],'decision':byte_decision}
 rows['wire-cannot-prove-install-or-reservation']={'messages':[json.loads(x) for x in prefix],'decision':d.evaluate(tuple(prefix),c),'finding':'Identical accepted wire after omitting installation or corrupting hidden reservation; independent local obligations reject those mutants.'}
 malformed={'duplicate-key':b'{"type":0,"type":1}','invalid-utf8':bytes([255]),'bom':bytes([239,187,191])+b'{}','unterminated':b'{','oversized':b'"'+b'x'*262143+b'"','float':b'{"x":1.2}','negative-zero':b'{"x":-0}','nan':b'{"x":NaN}','depth':b'['*65+b'0'+b']'*65}
 framing={}
 for name,raw in malformed.items():
  if name=='oversized':d.require(len(json.loads(raw))==262143,'valid JSON oversized control')
  try:d.SCHEMA['strict_loads'](raw)
  except (ValueError,UnicodeError) as e:
   if name=='oversized':d.require(str(e)=='object exceeds byte ceiling','byte limit distinguished from parse error')
   framing[name]={'input_sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'disposition':'typed-invalid-input','failure_type':type(e).__name__}
  else:raise ValueError('decoder accepted '+name)
 bad=json.loads(credit);bad['bytes']=True
 try:d.SCHEMA['validate'](bad,'BrokerMessage')
 except d.SCHEMA['jsonschema'].ValidationError as e:framing['boolean-credit']={'disposition':'typed-invalid-input','failure_type':type(e).__name__}
 else:raise ValueError('boolean credit accepted')
 return {'literal_cases':rows,'strict_decoder_controls':framing,'framing_limit':'Complete immutable-object decoder controls; streaming pre-ingress progress remains separately frozen flow model, not imported here.'}

def run(directory,limits):
 profiles={};start=time.monotonic()
 for i,c in enumerate(d.CONFIGS):
  profiles[str(i)]=explore(c,limits['states_per_boundary'],limits['seconds_per_experiment']);print('profile',i,profiles[str(i)]['states'],flush=True)
  d.require(sum(p['states'] for p in profiles.values())<=limits['states_per_boundary'] and peak_bytes()<=limits['memory_bytes'] and time.monotonic()-start<=limits['seconds_per_experiment'],'admitted total resources')
  write(directory/'progress.json',profiles)
 return {'status':'wire-correspondence-pass','classification':'modeled','profiles':profiles,'states_per_boundary':sum(p['states'] for p in profiles.values()),'mutants':[explore(d.CONFIGS[0],mutant=m) for m in MUTANTS],'calibrations':calibrate(),'controls':controls(),'formal_freeze':False,'full_comparison_complete':False,'runtime_integration_support':False,'live_provider_calls':0,'new_owner_questions':[]}
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True,type=pathlib.Path);a=parser.parse_args()
    if a.output.exists():raise ValueError('fresh output required')
    limits=json.loads((ROOT/'docs/v3/construction/wire-correspondence-20260908/plan.json').read_text())['limits']
    start=datetime.datetime.now(datetime.timezone.utc).isoformat();t=time.monotonic();code=0
    try:result=run(a.output.parent,limits)
    except Exception as e:result={'status':'incomplete-or-checker-failure','detail':type(e).__name__+': '+str(e),'formal_freeze':False};code=1
    write(a.output,{'started_at':start,'finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'seconds':time.monotonic()-t,'peak_bytes':peak_bytes(),'result':result})
    print(json.dumps({'status':result['status'],'detail':result.get('detail'),'seconds':time.monotonic()-t}),flush=True);return code
if __name__=='__main__':raise SystemExit(main())
