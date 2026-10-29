"""SC-WIRE-02 exact-candidate gate; historical gates remain immutable."""
import argparse,copy,datetime,hashlib,json,pathlib,platform,re,runpy,subprocess,sys,importlib.metadata
ROOT=pathlib.Path(__file__).resolve().parents[3]
BASE='e3ed18e70ffc86a8827d021335edd3befd3a3832'
PREDECESSOR='f1a68cd8d3d0091dc0a91c32197c32c707620ed4'
PREFIX='docs/v3/construction/wire-correspondence-20260908/'
CODE='experiments/v3/wire-correspondence/'
AREA=ROOT/PREFIX
WR=runpy.run_path(str(ROOT/'experiments/v3/shared-reservations/check.py'))
SC=runpy.run_path(str(ROOT/'experiments/v3/shared-composition/check.py'));FM=SC['FM']
CANONICAL=SC['CANONICAL'];git=SC['git'];sha=SC['sha'];load=SC['load'];canonical=SC['canonical'];require=SC['require']
sys.path.insert(0,str(ROOT/CODE))
import wire_domain as domain,wire_experiment as experiment
ADMIT=runpy.run_path(str(ROOT/CODE/'run_packet.py'))
LIMITS={'states_per_boundary':500000,'seconds_per_experiment':600,'memory_bytes':1610612736,'artifact_bytes':134217728,'max_processes_per_experiment':1,'network':False}

def read(n):return load(AREA/n)
def common():return pathlib.Path(git('rev-parse','--path-format=absolute','--git-common-dir'))
def cache():return common()/'research-attestations/wire-correspondence'
def bound(directory,name):
    p=directory/name;require(p.resolve().is_relative_to(directory.resolve()),'evidence path escape');return p

def goal_check(g):
    require(g['id']=='recursion.v3.wire-correspondence.20260908' and g['campaign']=='SC-WIRE-02' and g['parent_goal']=='goal-recursion-v2-execution-fabric' and g['start_commit']==BASE,'goal identity')
    require(g['branch']=='codex/v3-wire-correspondence-20260908' and g['predecessor']=='recursion.v3.shared-reservations.20260908','lineage')
    require(type(g['new_metered_spend']) is int and g['new_metered_spend']==0 and g['usage_resets_allowed'] is False,'spend/reset')
    require(g['started_at']=='2026-09-08T18:00:37Z' and g['deadline_utc']=='2026-09-08T22:00:37Z' and g['discovery_cutoff_utc']=='2026-09-08T20:30:37Z' and g['synthesis_review_reserve_minutes']==90,'time envelope')
    require(set(g['allowed_paths'])==CANONICAL|{PREFIX+'**',CODE+'**'} and g['max_active_agents_owner']==10 and g['max_concurrent_read_only_workers']==4 and g['worker_packet_minutes']==20,'scope/concurrency')
    require(g['capacity']['no_new_workers_at_remaining_percent']==6 and g['capacity']['checkpoint_at_remaining_percent']==3,'capacity reserve')

def predecessor():
    p=common()/'research-attestations/shared-reservations'/PREDECESSOR;c=load(p/'completion.json')
    require(c['candidate']==PREDECESSOR and c['status']=='published-scoped-campaign-complete','predecessor completion')
    receipts=[]
    for n,h in c['receipt_sha256'].items():
        raw=bound(p,n).read_bytes();require(sha(raw)==h,'predecessor receipt hash');r=json.loads(raw);receipts.append(r)
        require(r['candidate']==PREDECESSOR,'predecessor exact candidate')
        if 'execution_files' in r:
            directory=pathlib.Path(r['execution_directory']);require(directory.resolve().is_relative_to(p.resolve()),'predecessor execution path')
            for f,h in r['execution_files'].items():require(sha(bound(directory,f).read_bytes())==h,'predecessor execution hash')
            for row in r['regressions']+r['frozen_validations']:require(row['exit_code']==0 and sha(bound(p,row['log']).read_bytes())==row['output_sha256'],'predecessor raw log')
    require(sum(r.get('status')=='SHARED_RESERVATIONS_GATE_PASS' for r in receipts)==2 and len({r['reviewer'] for r in receipts if r.get('verdict')=='approve-publication'})==2,'predecessor gates and reviews')
    require(sha((p/'publication-ready.json').read_bytes())==c['publication_ready_sha256'],'predecessor publication readiness')

def preflight():
    g=read('goal.json');goal_check(g);require(git('branch','--show-current')==g['branch'],'branch')
    git('merge-base','--is-ancestor',BASE,'HEAD');git('merge-base','--is-ancestor',PREDECESSOR,BASE)
    rows=[x for x in git('worktree','list','--porcelain').split('\n\n') if 'branch refs/heads/'+g['branch'] in x.splitlines()]
    require(len(rows)==1 and pathlib.Path(rows[0].splitlines()[0][9:]).resolve()==ROOT,'exclusive worktree')
    require(not (ROOT/'state/PAUSE').exists() and not (common().parent/'state/PAUSE').exists(),'PAUSE')
    require('status: active' in (ROOT/'state/goals/goal-recursion-v2-execution-fabric.md').read_text(),'active parent')
    predecessor()

def preservation(changed):
    require(changed and all(p in CANONICAL or p.startswith(PREFIX) or p.startswith(CODE) for p in changed),'allowed paths')
    require(not git('diff','--name-only','--diff-filter=D',BASE),'baseline deletion')
    for path in CANONICAL:
        old=git('show',BASE+':'+path);new=(ROOT/path).read_text(encoding='utf-8')
        if path.endswith('.json'):
            a,b=json.loads(old),json.loads(new);b['ranges']=[r for r in b['ranges'] if r['prefix']!='WIR'];require(a==b,'historical registry')
        else:
            iterator=iter(new.splitlines())
            for line in old.splitlines():require(any(line==x for x in iterator),'historical canonical content '+path)
    FM['HIST']['PRIOR']['document_gate'](changed)
    for gate in (SC,SC['EE'],SC['CM'],SC['CM']['GRA'],SC['CM']['GRA']['RSE'],SC['CM']['GRA']['FRI'],SC['CM']['GRA']['FRI']['RSM']):gate['claims'](gate['read']('evidence.json'))
    FM['claim_bindings'](FM['read']('evidence.json'));FM['IP']['claim_bindings'](FM['IP']['read']('evidence.json'))
    FM['HIST']['bindings'](FM['HIST']['read']('evidence.json'));FM['HIST']['bindings'](FM['HIST']['M']['read']('evidence.json'))
    for row in WR['read']('evidence.json')['claims']:
        require(row['id'] in (ROOT/row['canonical']).read_text(encoding='utf-8'),'preserved SWR claim')

def manifests():
    m=read('manifest.json');prior=WR['read']('manifest.json')
    required={r['path'] for r in prior['inputs']}|set(git('diff','--name-only',prior['start_commit'],BASE).splitlines())
    required|={'docs/v3/foundation-research/mark2-research/mark3-comparison/formal-boundaries.md','docs/v3/foundation-research/mark2-research/mark3-comparison/comparison-contract.json'}
    require(m['start_commit']==BASE and m['parent_goal']=='goal-recursion-v2-execution-fabric' and required<={r['path'] for r in m['inputs']} and len(m['inputs'])==len({r['path'] for r in m['inputs']}),'input closure')
    for r in m['inputs']:require(sha(git('show',BASE+':'+r['path'],raw=True))==r['git_blob_sha256'],'frozen input bytes')
    require(read('plan.json')['limits']==LIMITS,'prospective bounds')

def coverage(c):
    old=FM['read']('coverage.json')
    require(c['preserved_contract_and_prior_coverage']==old and c['predecessor_coverage']==WR['read']('coverage.json'),'complete preserved six-obligation/27-trace history')
    require(c['full_comparison_complete'] is False and c['formal_freeze'] is False and c['coupled_reservation_wire_product_complete'] is False,'truthful coverage boundary')
    require([r['id'] for r in c['slice_obligations']]==[r['id'] for r in old['obligations']] and len(c['slice_obligations'])==6,'six obligations')
    for r in c['slice_obligations']:require(r['status']==('partial-modeled-support' if r['id'] in ('flow-replay','uncertainty') else 'no-new-coverage') and r['residual'],'obligation support limits')
    require([r['id'] for r in c['slice_traces']]==[r['id'] for r in old['traces']] and len(c['slice_traces'])==27,'27 traces')
    for r in c['slice_traces']:
        partial=r['id'][-2:] in ('01','02','12','26')
        require(r['status']==('partial-modeled-support' if partial else 'no-new-coverage') and r['result']==('results.json' if partial else None) and r['residual'],'trace scope')

def result_check(r,expected):
    require(canonical(r)==canonical(expected),'exact semantic results')
    require(r['status']=='wire-correspondence-pass' and r['classification']=='modeled' and r['formal_freeze'] is False and r['full_comparison_complete'] is False and r['runtime_integration_support'] is False and r['live_provider_calls']==0 and r['new_owner_questions']==[],'result claim boundary')
    require(set(r['profiles'])=={str(i) for i in range(len(domain.CONFIGS))},'complete configurations')
    for i,c in enumerate(domain.CONFIGS):require(canonical(r['profiles'][str(i)])==canonical(experiment.explore(c)),'independent complete graph/witness reproduction')
    require(r['states_per_boundary']==sum(p['states'] for p in r['profiles'].values())<=LIMITS['states_per_boundary'],'admitted complete totals')
    require(canonical(r['mutants'])==canonical([experiment.explore(domain.CONFIGS[0],mutant=m) for m in experiment.MUTANTS]),'independent counterexamples')
    require(r['calibrations']==experiment.calibrate() and canonical(r['controls'])==canonical(experiment.controls()),'literal and checker controls')


def admission(a,x,directory,accepted):
    require(a['status']=='admitted-before-execution' and a['new_metered_spend']==0 and a['network_operations_in_model'] is False and a['os_sandbox_claimed'] is False,'admission allowed effects')
    require(set(a['sources'])==set(ADMIT['SOURCES']),'complete admitted sources')
    require(re.fullmatch('[0-9a-f]{40}',a['source_commit']) is not None,'source identity')
    git('merge-base','--is-ancestor',BASE,a['source_commit']);git('merge-base','--is-ancestor',a['source_commit'],'HEAD')
    for path,hashes in a['sources'].items():
        raw=bound(directory,'source/'+path).read_bytes();blob=git('show',a['source_commit']+':'+path,raw=True)
        require(sha(raw)==hashes['working_sha256'] and sha(blob)==hashes['git_blob_sha256'] and raw.replace(b'\r\n',b'\n')==blob.replace(b'\r\n',b'\n'),'executed committed source binding')
        if accepted:require(raw.replace(b'\r\n',b'\n')==(ROOT/path).read_bytes().replace(b'\r\n',b'\n'),'accepted current source identity')
    require(a['limits']==LIMITS and a['command']==[a['host']['executable'],'-X','utf8','-B',CODE+'wire_experiment.py','--output',str(directory/'result.json')],'exact command and limits')
    begin=datetime.datetime.fromisoformat(a['started_at']);end=datetime.datetime.fromisoformat(x['finished_at']);g=read('goal.json')
    require(begin.tzinfo is not None and end.tzinfo is not None and datetime.datetime.fromisoformat(g['started_at'])<=begin<=end<=datetime.datetime.fromisoformat(g['deadline_utc']),'admission time envelope')
    if accepted:
        require(a['host']=={'system':platform.system(),'release':platform.release(),'version':platform.version(),'machine':platform.machine(),'python':sys.version,'executable':sys.executable} and a['dependencies']=={'jsonschema':importlib.metadata.version('jsonschema')} and x['status']=='finished' and x['exit_code']==0,'exact successful host execution')
        e=load(directory/'result.json');require(begin<=datetime.datetime.fromisoformat(e['started_at'])<=datetime.datetime.fromisoformat(e['finished_at'])<=end,'execution ordering')
        require(e['seconds']<=LIMITS['seconds_per_experiment'] and 0<e['peak_bytes']<=LIMITS['memory_bytes'],'observed resource limits')
    require(sum(p.stat().st_size for p in directory.rglob('*') if p.is_file())<=LIMITS['artifact_bytes'],'retained artifact limit')

def evidence(e,expected):
    require(e['start_commit']==BASE and e['formal_freeze'] is False and e['new_owner_questions']==[] and e['semantic_sha256']==sha(canonical(expected).encode()),'evidence scope')
    dirs={d.name for d in (cache()/'discovery').iterdir() if d.is_dir() and (d/'admission.json').exists()};rows=e['retained_runs']
    require(len(rows)==len(dirs) and {r['id'] for r in rows}==dirs,'all discovery attempts retained');accepted_count=0
    for row in rows:
        directory=bound(cache()/'discovery',row['id']);require({p.relative_to(directory).as_posix():sha(p.read_bytes()) for p in directory.rglob('*') if p.is_file()}==row['files'],'complete raw evidence inventory')
        a,x=load(directory/'admission.json'),load(directory/'execution.json');accepted=row['id']==e['accepted_run'];admission(a,x,directory,accepted)
        require(row['source_commit']==a['source_commit'] and row['exit_code']==x['exit_code'] and row['disposition'],'attempt provenance/disposition')
        if accepted:accepted_count+=1;require(e['accepted_source_commit']==a['source_commit'],'accepted commit');result_check(load(directory/'result.json')['result'],expected)
    require(accepted_count==1,'one accepted attempt');result_check(read('results.json'),expected)
    ledger=(ROOT/'docs/v3/CLAIM-LEDGER.md').read_text(encoding='utf-8');FM['HIST']['OLD']['registry_shape'](load(ROOT/'docs/v3/CLAIM-REGISTRY.json'),ledger)
    require({r['id'] for r in e['claims']}=={'WIR-001','WIR-002','WIR-003'} and len(e['claims'])==3,'claim inventory')
    for r in e['claims']:require(r['state']=='modeled' and '| '+r['id']+' | '+r['statement']+' | modeled |' in ledger and r['canonical'] in CANONICAL and r['id'] in (ROOT/r['canonical']).read_text(encoding='utf-8'),'canonical claim binding')
    for row in e['superseded_candidate_validations']:
        directory=bound(cache(),row['candidate'])
        require(row['candidate']!=git('rev-parse','HEAD') and row['disposition']=='superseded-not-a-pass','superseded candidate validation disposition')
        require({p.relative_to(directory).as_posix():sha(p.read_bytes()) for p in directory.rglob('*') if p.is_file()}==row['files'],'retained superseded candidate validation files')

def inventory(inv,changed,candidate):
    prior=WR['read']('validation-inventory.json')
    require(inv['start_commit']==BASE and inv['regression_commands']==prior['regression_commands']==FM['HIST']['OLD']['regression_commands']() and len(inv['regression_commands'])==25,'unchanged25 regressions')
    require(set(inv['artifacts'])==set(changed)-{PREFIX+'validation-inventory.json'},'artifact closure')
    for p,h in inv['artifacts'].items():require(sha(git('show',candidate+':'+p,raw=True))==h,'artifact hash '+p)
    gates=[{'commit':PREDECESSOR,'branch':'codex/v3-shared-reservations-20260908','script':'experiments/v3/shared-reservations/check.py'}]+prior['frozen_gates']
    require(inv['frozen_gates']==gates and len(gates)==11,'eleven frozen gates including shared reservations')
    for row in FM['HIST']['read']('validation-inventory.json')['regression_entrypoints']:
        require(sha(git('show',candidate+':'+row['path'],raw=True))==row['sha256'] and git('show',candidate+':'+row['path'],raw=True)==git('show',BASE+':'+row['path'],raw=True),'unchanged historical validators')
    return gates

def reject(fn,value):
    try:fn(value)
    except (ValueError,KeyError,TypeError,AssertionError,OSError,subprocess.SubprocessError):return
    raise ValueError('gate accepted adversarial mutation')

def adversarial(actual,a,x,directory):
    labels=[]
    for key,value in [('start_commit','0'*40),('usage_resets_allowed',True),('new_metered_spend',1),('deadline_utc','2099-01-01T00:00:00Z')]:
        bad=copy.deepcopy(read('goal.json'));bad[key]=value;reject(goal_check,bad);labels.append('goal/'+key)
    reject(FM['candidate_check'],'0'*40);labels.append('wrong-candidate');reject(preservation,['CONTROL_PLANE.md']);labels.append('protected-path')
    for key,value in [('formal_freeze',True),('classification','measured'),('full_comparison_complete',True),('runtime_integration_support',True),('mutants',[]),('profiles',{})]:
        bad=copy.deepcopy(actual);bad[key]=value;reject(lambda v:result_check(v,actual),bad);labels.append('result/'+key)
    for key,value in [('command',[]),('sources',{}),('host',{}),('started_at','2099-01-01T00:00:00Z')]:
        bad=copy.deepcopy(a);bad[key]=value;reject(lambda v:admission(v,x,directory,True),bad);labels.append('admission/'+key)
    for change in ('erase-run','wrong-hash','wrong-source','promote-claim'):
        bad=copy.deepcopy(read('evidence.json'))
        if change=='erase-run':bad['retained_runs'].pop()
        elif change=='wrong-hash':bad['retained_runs'][0]['files']['admission.json']='0'*64
        elif change=='wrong-source':bad['accepted_source_commit']='0'*40
        else:bad['claims'][0]['state']='measured'
        reject(lambda v:evidence(v,actual),bad);labels.append('evidence/'+change)
    for change in ('erase-trace','promote-trace','promote-obligation','rewrite-prior'):
        bad=copy.deepcopy(read('coverage.json'))
        if change=='erase-trace':bad['slice_traces'].pop()
        elif change=='promote-trace':bad['slice_traces'][2]['status']='complete'
        elif change=='promote-obligation':bad['slice_obligations'][2]['status']='complete'
        else:bad['preserved_contract_and_prior_coverage']['formal_freeze']=True
        reject(coverage,bad);labels.append('coverage/'+change)
    return labels

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--preflight',action='store_true');parser.add_argument('--working',action='store_true');parser.add_argument('--candidate');a=parser.parse_args();preflight()
    if a.preflight:print('WIRE_CORRESPONDENCE_PREFLIGHT_PASS_NOT_FINAL');return
    candidate=a.candidate or git('rev-parse','HEAD');FM['candidate_check'](candidate);changed=git('diff','--name-only',BASE,candidate).splitlines()
    preservation(changed);manifests();coverage(read('coverage.json'));actual=read('results.json');evidence(read('evidence.json'),actual)
    inv=read('validation-inventory.json');gates=inventory(inv,changed,candidate)
    if a.working:
        directory=cache()/'discovery'/read('evidence.json')['accepted_run'];labels=adversarial(actual,load(directory/'admission.json'),load(directory/'execution.json'),directory)
        print('WIRE_CORRESPONDENCE_WORKING_PASS_NOT_FINAL '+str(len(labels)));return
    now=datetime.datetime.now(datetime.timezone.utc);require(now<=datetime.datetime.fromisoformat(read('goal.json')['deadline_utc']),'campaign deadline')
    out=cache()/candidate;out.mkdir(parents=True,exist_ok=True);runid=now.strftime('%Y%m%dT%H%M%S%f');directory=out/runid
    admitted,x=ADMIT['execute'](directory);admission(admitted,x,directory,True);result_check(load(directory/'result.json')['result'],actual);labels=adversarial(actual,admitted,x,directory)
    git('diff','--check',BASE,candidate);frozen=[];regressions=[]
    for row in gates:
        root=FM['frozen_root'](row['commit'],row['branch']);name=runid+'-frozen-'+row['commit'][:7]
        r=FM['run_command']([sys.executable,'-X','utf8','-B',row['script'],'--working'],root,out,name);r.update(log=name+'.log',commit=row['commit']);frozen.append(r)
        require(git('rev-parse','HEAD',root=root)==row['commit'] and not git('status','--porcelain=v1',root=root),'frozen worktree unchanged')
    for i,cmd in enumerate(inv['regression_commands']):
        name=runid+'-regression-'+str(i);r=FM['run_command']([sys.executable,*cmd[1:]],ROOT,out,name);r['log']=name+'.log';regressions.append(r)
    FM['candidate_check'](candidate);require(datetime.datetime.now(datetime.timezone.utc)<=datetime.datetime.fromisoformat(read('goal.json')['deadline_utc']),'gate completion deadline')
    receipt={'status':'WIRE_CORRESPONDENCE_GATE_PASS','candidate':candidate,'base':BASE,'run_id':runid,'semantic_sha256':sha(canonical(actual).encode()),'adversarial_checks':labels,'states_per_boundary':actual['states_per_boundary'],'execution_directory':str(directory),'execution_files':{p.relative_to(directory).as_posix():sha(p.read_bytes()) for p in directory.rglob('*') if p.is_file()},'frozen_validations':frozen,'regressions':regressions,'formal_freeze':False,'artifacts':{p:sha(git('show',candidate+':'+p,raw=True)) for p in changed}}
    raw=(json.dumps(receipt,indent=2)+'\n').encode();(out/(runid+'.json')).write_bytes(raw);print('WIRE_CORRESPONDENCE_GATE_PASS '+json.dumps({'candidate':candidate,'run_id':runid,'sha256':sha(raw)}))
if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,TypeError,OSError,subprocess.SubprocessError) as e:print('WIRE_CORRESPONDENCE_GATE_FAIL '+str(e),file=sys.stderr);raise SystemExit(1)
