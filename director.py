"""Manager intake, scoped proposals, dynamic plans and evidence-based review."""
import json,hashlib
import server as s,business,runtime

def guidance(c,p):
    project=dict(c.execute('SELECT * FROM projects WHERE id=?',(p,)).fetchone());facts=json.loads(project['facts']);f=business.flow(c,p)
    unknown=[name for key,name in [('budget_status','验证预算'),('market','主要市场'),('time','每周可投入时间')] if facts.get(key) in {None,'','待确定','待确认'}]
    opportunities=business.rows(c,p,'opportunity');products=business.rows(c,p,'product')
    if f['status']!='进行中':return {'title':'项目已'+f['status'],'action':'resume','explanation':'先处理风险或恢复项目，再继续派工','questions':[]}
    root=c.execute("SELECT * FROM tasks WHERE project=? AND role=? AND dependency IS NULL AND title LIKE '确认%' ORDER BY id LIMIT 1",(p,s.ROLES[0])).fetchone()
    if root and root['status']=='进行中':return {'title':'正在整理你的项目启动卡','action':'wait','explanation':'先明确本次目标与权限，不要求现在补齐所有条件','questions':[]}
    if root and root['status']=='待验收':return {'title':'启动卡已整理好，请先查看并确认','action':'startup','task':root['id'],'explanation':'确认记录正确后，中台继续探索；未知条件仍保留待确认','questions':[]}
    if root and root['status'] in {'待处理','需返工'}:return {'title':'先整理项目启动卡','action':'start_task','task':root['id'],'explanation':'中台先整理你的表达与未知条件，再开展探索','questions':[]}
    running=c.execute("SELECT 1 FROM jobs WHERE project=? AND status IN ('排队中','执行中')",(p,)).fetchone()
    if running:return {'title':'中台正在执行，请稍等','action':'wait','explanation':'执行结果、来源和缺口会保存在工作记录中','questions':[]}
    if facts.get('entry')=='operate' and not c.execute("SELECT 1 FROM connectors WHERE project=? AND status IN ('可读取','可执行','部分可用')",(p,)).fetchone() and not c.execute('SELECT 1 FROM stores WHERE project=?',(p,)).fetchone():return {'title':'先读取你的现有站点','action':'connect','explanation':'在知识与连接验证站点权限，读取商品与订单后再诊断；也可以使用内置商店','questions':[]}
    if f['stage']=='探索' and not any(x['value'].get('decision')=='确认验证' for x in opportunities):
        return {'title':'先寻找值得验证的方向','action':'choose' if opportunities else 'analyze' if c.execute("SELECT 1 FROM records WHERE project=? AND kind IN ('机会证据','导入文档')",(p,)).fetchone() else 'research','explanation':'先给你少量有来源的候选方向，再由你选择；不需要先有商品或站点','questions':unknown[:3]}
    if not products:return {'title':'把选定方向变成商品验证计划','action':'plan','explanation':'中台安排商品、供应、样品与成本核查；未知规格不会自动编成事实','questions':[]}
    next_stage={'探索':'验证','验证':'销售准备','销售准备':'经营','经营':'经营'}[f['stage']]
    gaps=business.gate(c,p,next_stage)
    return {'title':'完成本阶段验证' if gaps else '本阶段资料已齐，等待你的决策','action':'review' if gaps else 'advance','explanation':'；'.join(gaps[:3]) if gaps else '进入下一阶段不会自动增加支出权限','questions':[]}

def mutate(c,path,d):
    p=d['project']
    if path=='/api/director/resolve':
        row=c.execute("SELECT * FROM fact_conflicts WHERE id=? AND project=? AND status='待核实'",(d.get('id'),p)).fetchone()
        if not row or d.get('decision') not in {'保持原事实','核实后采用新资料'}:raise ValueError('请选择待核实冲突及明确决策')
        if d['decision']=='核实后采用新资料':
            old=c.execute('SELECT * FROM entities WHERE id=?',(row['entity'],)).fetchone();value=json.loads(row['proposal']);value['classification']='事实'
            business.mutate(c,'/api/entities',{'project':p,'kind':old['kind'],'id':old['id'],'version':old['version'],'value':value,'reason':'用户已核实冲突并采用新资料'})
        risk=c.execute('SELECT * FROM entities WHERE id=?',(row['risk'],)).fetchone();value=json.loads(risk['data']);value['status']='已处理';value['action']=d['decision']
        business.mutate(c,'/api/entities',{'project':p,'kind':'risk','id':risk['id'],'version':risk['version'],'value':value,'reason':'经营者完成资料冲突核查'})
        c.execute("UPDATE fact_conflicts SET status='已处理',decision=?,updated=? WHERE id=?",(d['decision'],s.now(),row['id']));return {'ok':True}
    if path=='/api/director/import':
        from workflows import import_file
        return import_file(c,p,s.required(d,'file'))
    if path=='/api/director/message':
        text=s.required(d,'content');c.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(s.uid(),p,'user',text,s.now()))
        if d.get('analyze'):
            return runtime.enqueue(c,p,'intake',{'message':text},key='intake:'+s.uid())
        guide=guidance(c,p);reply='我已记下。'+guide['explanation']+('。接下来只需确认：'+'、'.join(guide['questions'])+'；不确定可暂不回答。' if guide['questions'] else '')
        c.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(s.uid(),p,'assistant',reply,s.now()));return guide
    if path=='/api/director/analyze-sources':return runtime.enqueue(c,p,'research',{'analysis_only':True},key='source-analysis:'+s.uid())
    if path=='/api/director/research':
        query=d.get('query') or c.execute('SELECT idea FROM projects WHERE id=?',(p,)).fetchone()[0]
        if not isinstance(query,str) or not query.strip() or len(query)>500:raise ValueError('请用500字内描述本次研究问题')
        urls=d.get('urls',[])
        if not isinstance(urls,list) or len(urls)>3 or not all(isinstance(x,str) for x in urls):raise ValueError('最多提供三个公开来源链接')
        return runtime.enqueue(c,p,'research',{'query':query,'urls':urls},key='research:'+s.uid())
    if path=='/api/director/choose':
        row=c.execute("SELECT * FROM entities WHERE id=? AND project=? AND kind='opportunity'",(d.get('id'),p)).fetchone()
        if not row:raise ValueError('方向不存在')
        v=json.loads(row['data']);v['decision']='确认验证'
        business.mutate(c,'/api/entities',{'project':p,'kind':'opportunity','id':row['id'],'version':row['version'],'value':v,'reason':'用户选择本轮验证方向'})
        # A direction decision changes the draft, but it must not silently approve stage or spend.
        if any(x['value'].get('status')!='已处理' for x in business.rows(c,p,'risk')):raise ValueError('请先处理现有风险，不能通过选择方向恢复项目')
        business.put_flow(c,p,status='进行中')
        initial=c.execute("SELECT * FROM tasks WHERE project=? AND role=? AND dependency IS NOT NULL ORDER BY id LIMIT 1",(p,s.ROLES[1])).fetchone()
        if initial and initial['status']!='已完成':
            dep=c.execute('SELECT status FROM tasks WHERE id=?',(initial['dependency'],)).fetchone()
            if dep and dep['status']!='已完成':raise ValueError('请先查看并确认项目启动卡')
            s.acceptance(c,initial)
            c.execute("UPDATE tasks SET status='已完成',version=version+1 WHERE id=?",(initial['id'],))
            s.log(c,p,'用户确认有来源的研究方向，研究交接完成；继续方向专属验证计划')
        return make_plan(c,p)
    if path=='/api/director/plan':return make_plan(c,p)
    if path=='/api/director/review':return runtime.enqueue(c,p,'review',{},key='review:'+s.uid())
    if path=='/api/director/apply':
        row=c.execute("SELECT * FROM jobs WHERE id=? AND project=? AND kind='intake' AND status='已完成'",(d.get('id'),p)).fetchone()
        if not row:raise ValueError('没有可确认的条件建议')
        result=json.loads(row['result'])
        if result.get('applied'):raise ValueError('这份建议已经确认，不能重复覆盖新条件')
        expected=json.loads(row['payload']).get('_project_signature');current=c.execute('SELECT name,idea,budget,facts FROM projects WHERE id=?',(p,)).fetchone()
        if expected and hashlib.sha256(json.dumps(dict(current),sort_keys=True,ensure_ascii=False).encode()).hexdigest()!=expected:raise ValueError('项目条件已改变，请重新分析后确认')
        proposal=result['proposal'];project=dict(c.execute('SELECT * FROM projects WHERE id=?',(p,)).fetchone());facts=json.loads(project['facts'])
        for k in ['market','language','time','resources','excluded','loss_limit','site_status','supply_preference','risk_preference']:
            if proposal.get(k):facts[k]=proposal[k]
        budget=project['budget']
        if proposal.get('budget') is not None:budget=s.money(proposal['budget']);facts['budget_status']='已确认'
        # Reuse validation of reserved budget before replacing facts, do not drop optional preferences.
        s.mutate(c,'/api/project/update',{'project':p,'name':project['name'],'idea':proposal.get('objective') or project['idea'],'budget':str(budget/100) if facts.get('budget_status')=='已确认' else '',**facts})
        c.execute('UPDATE projects SET facts=? WHERE id=?',(json.dumps(facts,ensure_ascii=False),p))
        result['applied']=True;c.execute('UPDATE jobs SET result=? WHERE id=?',(json.dumps(result,ensure_ascii=False),row['id']))
        s.log(c,p,'用户确认经营条件建议，关联任务重新核验');return {'ok':True}
    raise ValueError('未知经营负责人操作')

def make_plan(c,p):
    opportunities=business.rows(c,p,'opportunity');confirmed=[r for r in opportunities if r['value'].get('decision')=='确认验证']
    if not confirmed:raise ValueError('先选择愿意验证的方向，再安排商品验证')
    name=confirmed[-1]['value']['name'];last=None;tasks=[]
    for title,role in [('整理商品规格、供应线索与待核实条件',s.ROLES[2]),('供应商身份、有效报价与备选核查',s.ROLES[3]),('提交实物样品检查与人工交接',s.ROLES[3]),('商品成本与保守情景核验',s.ROLES[5]),('合规来源、适用范围与风险整改',s.ROLES[4])]:
        last=business.task(c,p,name+'：'+title,role,dependency=last,key='direction:'+confirmed[-1]['id']+':'+title);tasks.append(last)
    i=s.uid();c.execute('INSERT INTO plans VALUES(?,?,?,?,?,?)',(i,p,business.flow(c,p)['stage'],'按用户选择方向生成验证计划；未确认事实保持未知',json.dumps(tasks),s.now()))
    s.log(c,p,'经营负责人安排方向验证计划，人工事项仅样品、认证、资金和关键决策');return {'plan':i,'tasks':tasks}

def run(job,payload):
    p=job['project']
    with s.connect() as c:
        runtime.check(c,job);project=dict(c.execute('SELECT * FROM projects WHERE id=?',(p,)).fetchone());facts=json.loads(project['facts'])
        if job['kind']=='review':
            financial=__import__('finance').statement(c,p);experiments=[dict(r) for r in c.execute('SELECT * FROM experiments WHERE project=?',(p,))]
            issues=[]
            if financial['budget_exceeded']:issues.append(('实际费用超预算，暂停新增投入',s.ROLES[5]))
            if financial['paid_orders']==0:issues.append(('没有已核对订单，核查渠道与追踪，不认定商品无需求',s.ROLES[9]))
            for exp in experiments:
                if exp['status']=='数据不足':issues.append(('实验样本不足，先补证据：'+exp['hypothesis'],s.ROLES[0]))
                elif exp['status']=='假设未获支持':issues.append(('停止无效改动并设计单变量替代实验：'+exp['hypothesis'],s.ROLES[8]))
            gaps=business.gate(c,p,{'探索':'验证','验证':'销售准备','销售准备':'经营','经营':'经营'}[business.flow(c,p)['stage']])
            issues.extend((x,s.ROLES[0]) for x in gaps[:8])
            tasks=[business.task(c,p,title,role,key='review:'+title) for title,role in issues]
            return {'summary':'经营复盘与后续执行计划','financials':financial,'decisions':[x[0] for x in issues],'tasks':tasks,'unknowns':['原因仍需证据验证；本次没有自行投放或改变交易条件']}
        import engine
        if not engine.model_status(p,c)['configured']:raise ValueError('已保存你的表达；接通模型后可自动提取条件，目前不会猜测预算或市场')
        grant=c.execute('SELECT remaining FROM grants WHERE project=?',(p,)).fetchone()
        if not grant or grant['remaining']<1:raise ValueError('条件分析需要一次模型调用授权')
        c.execute('UPDATE grants SET remaining=remaining-1 WHERE project=?',(p,));runtime.consume(c,p,'model',job['id']);c.commit()
    result=engine.model_call({'task':{'role':s.ROLES[0],'title':'从表达中提取经营条件并只问关键问题'},'task_spec':{'objective':'根据用户原话提取条件，未知用null；不得推断预算、主体或站点已存在。输出基础结果外增加 proposal 对象，字段 objective,market,language,time,resources,excluded,loss_limit,site_status,supply_preference,risk_preference,budget(人民币字符串或null)，questions 数组最多三个；只提影响路线的问题。条件建议必须由用户确认后才生效。'},'project':project,'records':[],'entities':[],'messages':[{'role':'user','content':payload['message']}],'previous_outputs':[]})
    proposal=result.get('proposal');questions=result.get('questions',[])
    if not isinstance(proposal,dict) or not isinstance(questions,list) or len(questions)>3 or not all(isinstance(q,str) for q in questions):raise ValueError('经营条件建议格式不符合要求')
    allowed={'objective','market','language','time','resources','excluded','loss_limit','site_status','supply_preference','risk_preference','budget'}
    if set(proposal)-allowed or any(v is not None and (not isinstance(v,str) or len(v)>4000) for v in proposal.values()):raise ValueError('经营条件含无效字段')
    if proposal.get('budget') is not None:s.money(proposal['budget'])
    with s.connect() as c:
        runtime.check(c,job);c.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(s.uid(),p,'assistant',result['summary']+'\n'+'\n'.join(questions),s.now()))
    return {'summary':result['summary'],'proposal':proposal,'questions':questions,'applied':False,'unknowns':result['unknowns']}

def dispatch_research():
    with s.connect() as c:
        projects=[dict(r) for r in c.execute("SELECT p.* FROM projects p JOIN flows f ON f.project=p.id WHERE f.autopilot=1 AND f.status='进行中' AND f.stage='探索'")]
    for project in projects:
        with s.connect() as c:
            p=project['id'];g=guidance(c,p)
            if g['action'] not in {'research','analyze'}:continue
            grant=c.execute('SELECT remaining FROM grants WHERE project=?',(p,)).fetchone()
            if not grant or not grant['remaining']:continue
            import engine
            if not engine.model_status(p,c)['configured']:continue
            if g['action']=='research' and not runtime.available(c,p,'search'):continue
            payload={'analysis_only':True} if g['action']=='analyze' else {'query':project['idea'][:500],'urls':[]}
            signature=hashlib.sha256((project['idea']+project['facts']+g['action']).encode()).hexdigest()
            runtime.enqueue(c,p,'research',payload,key='manager-research:'+signature)
