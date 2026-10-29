"""Independent expected action set, complete field changes and invariants."""
import wire_domain as d

def enabled(s,c):
 actions=set()
 if s['phase']==0:actions.add('gap')
 if s['phase']==1:actions.add('reset')
 if s['phase']==2:
  if s['installed']==0:actions.add('install')
  if s['grants'] in (0,1):actions.add('credit')
  if s['installed']==1 and len(s['source'])>0 and min(s['event_credit'],s['byte_credit']//c[1])>0 and len(s['pending'])<min(c[2],c[3]):actions.add('emit')
  if len(s['pending'])>0:
   actions.add('ack')
   if s['lost_ack']==0:actions.add('lose-ack')
  if s['restart']==0 and (s['installed']==0 or (s['sent']==2 and len(s['pending'])>0)):actions.add('restart')
 for flag in ('control','quota'):
  if s[flag]==0:actions.add(flag)
 return actions

def check(s,a,n,c):
 d.require(a in enabled(s,c),'disabled action '+a)
 expected=dict(s);changes={
 'gap':dict(phase=1,sent=1,acknowledged=0),
 'reset':dict(phase=2,pending=(),byte_credit=0,event_credit=0,acknowledged=1),
 'install':dict(installed=1,installed_cursor=d.cursor(1,c),view_reserved=0),
 'credit':dict(grants=s['grants']+1,byte_credit=s['byte_credit']+c[1],event_credit=s['event_credit']+1),
 'ack':dict(pending=(),acknowledged=s['sent']),
 'lose-ack':dict(lost_ack=1),'restart':dict(restart=1),'control':dict(control=1),'quota':dict(quota=1)}
 if a=='emit':changes[a]=dict(source=tuple(x for x in s['source'] if x!=s['source'][0]),pending=tuple([*s['pending'],s['source'][0]]),sent=s['source'][0],byte_credit=s['byte_credit']-c[1],event_credit=s['event_credit']-1)
 expected.update(changes[a]);m=d.encode(a,s,c)
 if m:expected['wire']=s['wire']+(d.canonical(m),)
 d.require(n==expected,'complete postcondition '+a)
 invariant(n,c)

def invariant(s,c):
 d.require(s['reserved']==6 and s['free']==4 and s['owner']==('pool-a','goal-a','operation-a','owner-a',6),'reservation ownership')
 d.require(0<=s['byte_credit'] and 0<=s['event_credit'] and len(s['pending'])<=c[2] and len(s['pending'])*c[1]<=c[3]*c[1],'flow bounds')
 d.require(s['source']==tuple(n for n in (2,3) if n>s['sent']),'source future preservation')
 d.require(s['snapshot']==(d.cursor(1,c),0),'immutable snapshot')
 d.require(s['installed_cursor']==(d.cursor(1,c) if s['installed'] else 0),'installation cursor')
 d.require(not s['installed'] or s['phase']==2,'installation precondition')
 d.require(s['sent']<=1 or s['installed']==1,'application before delivery')

def initialization(s,c):
 invariant(s,c)
 expected={'phase':0,'source':(2,3),'pending':(),'byte_credit':c[1],'event_credit':1,'grants':0,'sent':0,'acknowledged':0,'installed':0,'installed_cursor':0,'snapshot':(d.cursor(1,c),0),'view_reserved':0,'reserved':6,'free':4,'owner':('pool-a','goal-a','operation-a','owner-a',6),'restart':0,'lost_ack':0,'control':0,'quota':0}
 expected['wire']=(d.canonical({'type':'HELLO','protocol_version':'m1b-broker/1','client_id':'client-a','stream_id':'stream-a','daemon_epoch':2,'last_cursor':0,'cursor_unit':c[0],'columns':80,'rows':24,'capabilities':['attach']}),d.canonical({'type':'CREDIT','protocol_version':'m1b-broker/1','client_id':'client-a','stream_id':'stream-a','daemon_epoch':2,'bytes':c[1],'events':1}))
 d.require(s==expected,'complete independent initial conditions')
