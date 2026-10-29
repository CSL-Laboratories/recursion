"""Two separately written reducers; declarations/encoder alone are shared."""
import wire_domain as d

def inclusive(s,c,mutant=None):
 u,z,qe,qb=c;out=[]
 def add(a,**changes):
  n=dict(s);n.update(changes);m=d.encode(a,s,c)
  if m:n['wire']=s['wire']+(d.canonical(m),)
  out.append((a,n))
 if s['phase']==0:add('gap',phase=1,sent=1,acknowledged=0)
 if s['phase']==1:add('reset',phase=2,pending=(),byte_credit=0,event_credit=0,acknowledged=1)
 if s['phase']==2:
  if not s['installed']:add('install',installed=1,installed_cursor=s['snapshot'][0],view_reserved=s['snapshot'][1],**({'reserved':0,'free':10} if mutant=='install-reservation' else {}))
  if s['grants']<2:add('credit',grants=s['grants']+1,byte_credit=s['byte_credit']+z,event_credit=s['event_credit']+1)
  if (s['installed'] or mutant=='install-guard') and s['source'] and s['byte_credit']>=z and s['event_credit']>=1 and len(s['pending'])<qe and (len(s['pending'])+1)*z<=qb*z:
   add('emit',source=s['source'][1:],pending=s['pending']+(s['source'][0],),byte_credit=s['byte_credit']-z,event_credit=s['event_credit']-1,sent=s['source'][0])
  if s['pending']:add('ack',pending=(),acknowledged=s['sent'])
  if s['pending'] and not s['lost_ack']:add('lose-ack',lost_ack=1,**({'pending':()} if mutant=='lost-ack-release' else {}))
  if not s['restart'] and (not s['installed'] or (s['sent']==2 and s['pending'])):add('restart',restart=1,**({'reserved':0,'free':10} if mutant=='restart-reservation' else {}))
 if not s['control']:add('control',control=1)
 if not s['quota']:add('quota',quota=1,**({'reserved':0,'free':10} if mutant=='quota-reservation' else {}))
 if mutant=='erase-source':
  out=[(a,dict(n,source=()) if a=='install' else n) for a,n in out]
 return out

def separated(state,c):
 p,f,a=state;z=c[1];rows=[]
 def append(label,pp=None,ff=None,aa=None):
  np=dict(p);nf=dict(f);na=dict(a)
  np.update(pp or {});nf.update(ff or {});na.update(aa or {})
  message=d.encode(label,d.flatten(state),c)
  if message is not None:nf['wire']=tuple([*f['wire'],d.canonical(message)])
  rows.append((label,d.Split(np,nf,na)))
 if f['phase']<1:append('gap',ff={'phase':1,'sent':1,'acknowledged':0})
 elif f['phase']==1:append('reset',ff={'phase':2,'pending':(),'byte_credit':0,'event_credit':0,'acknowledged':1})
 else:
  if a['installed']==0:append('install',aa={'installed':1,'installed_cursor':a['snapshot'][0],'view_reserved':a['snapshot'][1]})
  if 2-f['grants']>0:append('credit',ff={'grants':f['grants']+1,'byte_credit':z+f['byte_credit'],'event_credit':1+f['event_credit']})
  room=c[2]>len(f['pending']) and c[3]*z-sum(z for _ in f['pending'])>=z
  funded=f['event_credit']>0 and f['byte_credit']-z>=0
  if a['installed']!=0 and p['source'] and room and funded:
   first,*tail=p['source'];append('emit',pp={'source':tuple(tail)},ff={'pending':(*f['pending'],first),'byte_credit':f['byte_credit']-z,'event_credit':f['event_credit']-1,'sent':first})
  if len(f['pending'])>0:
   append('ack',ff={'pending':(),'acknowledged':f['sent']})
   if a['lost_ack']==0:append('lose-ack',aa={'lost_ack':1})
  if a['restart']==0 and (a['installed']==0 or (f['sent']==2 and len(f['pending'])>0)):append('restart',aa={'restart':1})
 if a['control']==0:append('control',aa={'control':1})
 if a['quota']==0:append('quota',aa={'quota':1})
 return rows
