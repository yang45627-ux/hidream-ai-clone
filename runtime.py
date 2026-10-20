"""Persisted bounded tool jobs, explicit expiring scope and usage reservations."""
import json,os,hashlib,datetime
import server as s
TOOLS={'search','read','image','video','logistics','email','ads','social','refund','model'}
KINDS={'research','intake','review','media','video','tracking','reply','ad','social','refund'}

def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS tool_grants(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),tool TEXT,target TEXT,remaining INTEGER,budget INTEGER,spent INTEGER,expires TEXT,active INTEGER,created TEXT);
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),kind TEXT,request_key TEXT,payload TEXT,status TEXT,result TEXT,error TEXT,created TEXT,updated TEXT,UNIQUE(project,request_key));
    CREATE TABLE IF NOT EXISTS tool_charges(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),use_id TEXT UNIQUE,actual INTEGER,reference TEXT,note TEXT,created TEXT,UNIQUE(project,reference));
    CREATE TABLE IF NOT EXISTS tool_uses(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),grant_id TEXT,job TEXT,tool TEXT,reserved INTEGER,basis TEXT,created TEXT);''')

def env(c,p,name):
    owner=c.execute('SELECT owner FROM project_owners WHERE project=?',(p,)).fetchone()
    prefix='' if not owner or owner['owner']=='root' else 'WORKSPACE_'+owner['owner'].upper()+'_'
    if name in {'AI_BASE_URL','AI_API_KEY','AI_MODEL','MODEL_CALL_CNY'}:
        from model_connection import saved
        connection=saved(c,owner['owner'] if owner else 'root')
        if connection:
            return connection[{'AI_BASE_URL':'base','AI_API_KEY':'api_key','AI_MODEL':'model','MODEL_CALL_CNY':'quote'}[name]]
    return os.environ.get(prefix+name,os.environ.get(name,'') if name.endswith('_CALL_CNY') else '')

def available(c,p,tool,target='*'):
    return c.execute('SELECT * FROM tool_grants WHERE project=? AND tool=? AND active=1 AND remaining>0 AND expires>? AND (target=? OR target=?) ORDER BY created DESC',(p,tool,s.now(),target,'*')).fetchone()

def consume(c,p,tool,job,target='*'):
    s.begin_write(c)
    row=available(c,p,tool,target)
    if not row:raise ValueError('需要有效的'+tool+'授权（对象、次数和期限）')
    configured=env(c,p,tool.upper()+'_CALL_CNY')
    if tool=='search' and env(c,p,'SEARCH_API_KEY') and not configured:raise ValueError('请配置SEARCH_CALL_CNY；市场检索费用未知时不调用')
    charge=s.money(configured) if configured else 0
    if tool in {'image','video','email','ads','social','logistics','refund','model'} and not configured:raise ValueError('请配置'+tool.upper()+'_CALL_CNY作为每次调用的预留费用；无法确认费用时不执行')
    if row['spent']+charge>row['budget']:raise ValueError('工具费用预留已达到授权上限')
    c.execute('UPDATE tool_grants SET remaining=remaining-1,spent=spent+? WHERE id=?',(charge,row['id']))
    i=s.uid();c.execute('INSERT INTO tool_uses VALUES(?,?,?,?,?,?,?,?)',(i,p,row['id'],job,tool,charge,'预留估计，服务商实际账单待核对',s.now()))
    c.commit();return i

def enqueue(c,p,kind,payload,key=None):
    s.begin_write(c)
    if kind not in KINDS:raise ValueError('任务类型无效')
    if not isinstance(payload,dict) or len(json.dumps(payload))>50000:raise ValueError('任务输入无效')
    import business
    if business.flow(c,p)['status']!='进行中':raise ValueError('请先恢复项目')
    project=c.execute('SELECT name,idea,budget,facts FROM projects WHERE id=?',(p,)).fetchone()
    payload={**payload,'_project_signature':hashlib.sha256(json.dumps(dict(project),sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
    if not key:
        body=json.dumps(payload,sort_keys=True,ensure_ascii=False)
        key=kind+':'+hashlib.sha256(body.encode()).hexdigest()
    old=c.execute('SELECT id,status FROM jobs WHERE project=? AND request_key=?',(p,key)).fetchone()
    if old:return dict(old)|{'duplicate':True}
    i=s.uid();c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',(i,p,kind,key,json.dumps(payload,ensure_ascii=False),'排队中',None,'',s.now(),s.now()))
    s.log(c,p,'中台安排'+kind+'任务；请求有防重复标识');return {'id':i,'status':'排队中'}

def mutate(c,path,d):
    p=d['project'];s.begin_write(c)
    if path=='/api/tools/reconcile-use':
        usage=c.execute('SELECT * FROM tool_uses WHERE id=? AND project=?',(d.get('id'),p)).fetchone()
        if not usage:raise ValueError('调用记录不存在')
        amount=s.money(d.get('actual'));reference=s.required(d,'reference');note=s.required(d,'note')
        old=c.execute('SELECT * FROM tool_charges WHERE use_id=?',(usage['id'],)).fetchone()
        if old:
            if old['actual']!=amount or old['reference']!=reference:raise ValueError('本次调用已核对，不能覆盖原账单；请保留差异并人工复核')
            return {'id':old['id'],'duplicate':True}
        if c.execute('SELECT 1 FROM tool_charges WHERE project=? AND reference=?',(p,reference)).fetchone():raise ValueError('费用凭据行已核对过，不能重复入账')
        i=s.uid();c.execute('INSERT INTO tool_charges VALUES(?,?,?,?,?,?,?)',(i,p,usage['id'],amount,reference,note,s.now()))
        c.execute('UPDATE tool_grants SET spent=spent+? WHERE id=?',(amount-usage['reserved'],usage['grant_id']))
        import finance
        finance.record(c,{'project':p,'category':'AI运行' if usage['tool'] in {'model','image','video'} else '数据工具','basis':'实际','amount':str(amount/100),'reference':reference,'note':'费用凭据 '+reference+'；'+note})
        if amount>usage['reserved']:
            c.execute('UPDATE tool_grants SET active=0 WHERE id=?',(usage['grant_id'],))
            import business
            business.put_flow(c,p,status='暂停');business.followup(c,p,'tool-overrun:'+usage['id'],'工具实际费用超过预留，请核对定价并重新授权',s.ROLES[5])
        s.log(c,p,'工具实际费用已核对，预留差额回流；不执行付款');return {'id':i}
    if path=='/api/tools/grant':
        tool=d.get('tool');target=d.get('target','*');calls=d.get('calls');days=d.get('days',7)
        if tool not in TOOLS or not isinstance(target,str) or not 1<=len(target)<=300:raise ValueError('授权工具或对象无效')
        if not isinstance(calls,int) or isinstance(calls,bool) or not 1<=calls<=100:raise ValueError('授权次数须为1–100')
        if not isinstance(days,int) or isinstance(days,bool) or not 1<=days<=30:raise ValueError('授权期限须为1–30天')
        amount=s.money(d.get('budget','0'))
        project=c.execute('SELECT budget FROM projects WHERE id=?',(p,)).fetchone()
        from finance import committed
        if amount+committed(c,p)>project['budget']:raise ValueError('工具授权与已占用预算合计超过项目预算')
        i=s.uid();expires=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(days=days)).isoformat()
        c.execute('INSERT INTO tool_grants VALUES(?,?,?,?,?,?,0,?,1,?)',(i,p,tool,target,calls,amount,expires,s.now()));s.log(c,p,'工具授权：'+tool+'；次数'+str(calls)+'；范围'+target+'；到期'+expires);return {'id':i}
    if path=='/api/tools/revoke':
        c.execute('UPDATE tool_grants SET active=0 WHERE id=? AND project=?',(d.get('id'),p));s.log(c,p,'工具授权已撤销；未发送的后续调用停止，已发送请求需核对');return {'ok':True}
    if path=='/api/jobs/cancel':
        c.execute("UPDATE jobs SET status='已取消',updated=? WHERE id=? AND project=? AND status IN ('排队中','执行中')",(s.now(),d.get('id'),p));return {'ok':True}
    raise ValueError('未知工具操作')

def check(c,job):
    row=c.execute('SELECT status FROM jobs WHERE id=?',(job['id'],)).fetchone()
    import business
    payload=json.loads(job['payload'])
    if job['kind'] in {'research','intake','media'} and payload.get('_project_signature'):
        project=c.execute('SELECT name,idea,budget,facts FROM projects WHERE id=?',(job['project'],)).fetchone()
        if hashlib.sha256(json.dumps(dict(project),sort_keys=True,ensure_ascii=False).encode()).hexdigest()!=payload['_project_signature']:raise ValueError('项目条件已变化，停止基于旧条件的任务')
    if not row or row['status']!='执行中' or business.flow(c,job['project'])['status']!='进行中':raise ValueError('任务已暂停、取消或项目已停止')

def recover(c):
    c.execute("UPDATE jobs SET status='结果未知',error='运行中断，先核对外部操作；不自动重发',updated=? WHERE status='执行中'",(s.now(),))

def step():
    with s.connect() as c:
        s.begin_write(c)
        row=c.execute("SELECT * FROM jobs WHERE status='排队中' ORDER BY created LIMIT 1").fetchone()
        if not row:return False
        job=dict(row);c.execute("UPDATE jobs SET status='执行中',updated=? WHERE id=?",(s.now(),job['id']))
    try:
        kind=job['kind'];payload=json.loads(job['payload'])
        if kind=='research':
            import research
            result=research.run(job,payload)
        elif kind in {'intake','review'}:
            import director
            result=director.run(job,payload)
        elif kind in {'media','video'}:
            import media
            result=media.run(job,payload)
        else:
            import integrations
            result=integrations.run(job,payload)
        with s.connect() as c:
            s.begin_write(c);check(c,job)
            c.execute("UPDATE jobs SET status='已完成',result=?,error='',updated=? WHERE id=?",(json.dumps(result,ensure_ascii=False),s.now(),job['id']))
            s.log(c,job['project'],'中台任务交付：'+kind+'；实际产物及未知可查看')
    except Exception as e:
        from net import UnknownResult
        error=str(e) if isinstance(e,ValueError) else '工具任务失败（'+type(e).__name__+'），未自动重发'
        with s.connect() as c:
            c.execute("UPDATE jobs SET status=?,error=?,updated=? WHERE id=? AND status='执行中'",('结果未知' if isinstance(e,UnknownResult) else '等待处理',error,s.now(),job['id']))
            s.log(c,job['project'],'中台任务需要处理：'+error)
    return True

def health(c,p):
    rows=c.execute('SELECT status,COUNT(*) AS n FROM jobs WHERE project=? GROUP BY status',(p,)).fetchall()
    return {'jobs':{r['status']:r['n'] for r in rows},'unresolved_external':c.execute("SELECT COUNT(*) FROM service_actions WHERE project=? AND status IN ('结果未知','处理中','已发送')",(p,)).fetchone()[0],'expired_grants':c.execute('SELECT COUNT(*) FROM tool_grants WHERE project=? AND active=1 AND expires<=?',(p,s.now())).fetchone()[0],'fee_reservations':c.execute('SELECT COALESCE(SUM(reserved),0) FROM tool_uses WHERE project=?',(p,)).fetchone()[0],'billing_basis':'工具预留为估计；实际账单以服务商及核对费用凭据为准'}
