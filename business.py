"""Structured business objects, phase gates, approvals and feedback handoffs."""
import json,datetime
import server as s

KINDS={'opportunity','product','supplier','quote','sample','risk','content','ticket','checklist'}
REQUIRED={
'opportunity':['name','audience','problem','source','validation'],
'product':['name','sku','specification','material','weight','dimensions','usage','limitations','source'],
'supplier':['name','identity','contact','source'],
'quote':['product_id','supplier_id','price','currency','moq','stock','lead_time','shipping','returns','valid_until','source'],
'sample':['product_id','inspector','inspection_date','result','findings','source'],
'risk':['name','market','level','status','source','action'],
'content':['name','product_id','channel','body','source'],
'ticket':['subject','customer_ref','message','owner','status'],
'checklist':['name','evidence','result']}

def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),kind TEXT,data TEXT,version INTEGER,created TEXT,updated TEXT);
    CREATE TABLE IF NOT EXISTS entity_versions(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),entity TEXT,version INTEGER,data TEXT,reason TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS flows(project TEXT PRIMARY KEY REFERENCES projects(id),stage TEXT DEFAULT '探索',status TEXT DEFAULT '进行中',autopilot INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),stage TEXT,reason TEXT,body TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),kind TEXT,subject TEXT,amount INTEGER,status TEXT,reason TEXT,decision TEXT,created TEXT,updated TEXT);
    CREATE TABLE IF NOT EXISTS task_specs(task TEXT PRIMARY KEY REFERENCES tasks(id),project TEXT REFERENCES projects(id),body TEXT);
    CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),name TEXT,mime TEXT,data BLOB,created TEXT);
    CREATE TABLE IF NOT EXISTS followups(project TEXT REFERENCES projects(id),event_key TEXT,task TEXT REFERENCES tasks(id),PRIMARY KEY(project,event_key));''')

def rows(c,p,kind=None):
    return [dict(r)|{'value':json.loads(r['data'])} for r in c.execute('SELECT * FROM entities WHERE project=?'+(' AND kind=?' if kind else ''),(p,kind) if kind else (p,))]

def task(c,p,title,role,dependency=None,key=None):
    if key:
        existing=c.execute('SELECT task FROM followups WHERE project=? AND event_key=?',(p,key)).fetchone()
        if existing:return existing['task']
    i=s.uid();c.execute('INSERT INTO tasks(id,project,title,role,status,dependency) VALUES(?,?,?,?,?,?)',(i,p,title,role,'等待依赖' if dependency else '待处理',dependency))
    from workflows import spec as role_spec
    spec=role_spec(role,title)|{'input_refs':[r['id'] for r in rows(c,p)]}
    c.execute('INSERT INTO task_specs VALUES(?,?,?)',(i,p,json.dumps(spec,ensure_ascii=False)))
    if key:c.execute('INSERT INTO followups VALUES(?,?,?)',(p,key,i))
    return i

def followup(c,p,key,title,role):
    result=task(c,p,title,role,key=key);s.log(c,p,'事件创建跟进任务：'+title);return result

def flow(c,p):
    row=c.execute('SELECT * FROM flows WHERE project=?',(p,)).fetchone()
    return dict(row) if row else {'project':p,'stage':'探索','status':'进行中','autopilot':0}

def put_flow(c,p,stage=None,status=None,autopilot=None):
    old=flow(c,p)
    c.execute('INSERT INTO flows VALUES(?,?,?,?) ON CONFLICT(project) DO UPDATE SET stage=excluded.stage,status=excluded.status,autopilot=excluded.autopilot',(p,stage or old['stage'],status or old['status'],old['autopilot'] if autopilot is None else autopilot))

def gate(c,p,stage):
    es=rows(c,p);by={k:[r['value'] for r in es if r['kind']==k] for k in KINDS};missing=[]
    if stage=='验证':
        if not any(o.get('decision')=='确认验证' for o in by['opportunity']):missing.append('至少确认一个带来源的机会方向')
    if stage in {'销售准备','上线','经营'}:
        if not by['product']:missing.append('完整商品主档')
        for product in [r for r in es if r['kind']=='product']:
            i=product['id']
            if product['value'].get('classification') in {'待核实','假设'}:missing.append(product['value']['name']+'：商品事实仍待用户或可靠来源核查')
            if not any(x['product_id']==i and x['result']=='通过' for x in by['sample']):missing.append(product['value']['name']+'：人工样品检查通过')
            if not any(x['product_id']==i and x['valid_until']>=datetime.date.today().isoformat() for x in by['quote']):missing.append(product['value']['name']+'：有效供应报价')
        costs=[json.loads(r['body']) for r in s.effective_records(c,p) if r['kind']=='成本测算']
        for product in [r for r in es if r['kind']=='product']:
            for scenario in ['正常','保守']:
                scenario_costs=[r for r in costs if r.get('scenario','正常')==scenario and r.get('product_id')==product['id']]
                if not scenario_costs or not scenario_costs[-1].get('result') or scenario_costs[-1]['result']['post_marketing']<=0:
                    missing.append(product['value']['name']+'：'+scenario+'情景须有本商品完整且营销后贡献为正的最新成本测算')
        if not by['risk']:missing.append('风险核查记录')
        if any(x['status']!='已处理' for x in by['risk']):missing.append('所有未解除风险均需处理或专业复核')
    if stage in {'上线','经营'}:
        import media
        language=json.loads(c.execute('SELECT facts FROM projects WHERE id=?',(p,)).fetchone()[0]).get('language','')
        if language.lower() not in {'en','english','英语','zh','中文','汉语','zh-cn'}:missing.append('明确主要站点语言：当前内置商店支持中文或英语')
        for product in [r for r in es if r['kind']=='product']:
            if not media.ready(c,p,product['id']):missing.append(product['value']['name']+'：核查通过的5张主图与10张9:16详情图')
        needed={'商品信息','支付','运费配送','移动端','购物车','结账','测试订单','退款流程','数据追踪','客服与政策'}
        passed={x['name'] for x in by['checklist'] if x['result']=='通过'}
        missing += ['上线检查：'+x for x in sorted(needed-passed)]
    if stage=='经营':
        native=c.execute("SELECT 1 FROM stores WHERE project=? AND mode='正式' AND status='已发布'",(p,)).fetchone()
        if native:
            if not c.execute("SELECT 1 FROM checkouts WHERE project=? AND status='已支付'",(p,)).fetchone():missing.append('至少一次经过签名回调核对的支付链路测试（测试支付不代表获客）')
        else:
            if not c.execute("SELECT 1 FROM connectors WHERE project=? AND status='可执行'",(p,)).fetchone():missing.append('正式内置商店发布或真实外部站点连接验证')
            if not c.execute("SELECT 1 FROM external_actions WHERE project=? AND status='已确认'",(p,)).fetchone():missing.append('至少一次已核对的真实发布操作')
    return missing

def accept_task(c,t):
    attempts=c.execute("SELECT output FROM attempts WHERE task=? AND version=? AND status='已交付' ORDER BY created DESC",(t['id'],t['version'])).fetchall()
    if not attempts:raise ValueError('任务缺少当前版本的实际交付，请先执行并查看成果')
    result=json.loads(attempts[0]['output'])
    if not result.get('deliverables'):raise ValueError('任务没有可验收产物')
    if t['role'] in {s.ROLES[1],s.ROLES[2],s.ROLES[4]} and not result.get('evidence_ids'):raise ValueError('专业结论缺少可追溯证据，不能通过验收')
    if t['role']==s.ROLES[3] and '实物样品检查' in t['title'] and not any(r['value']['result']=='通过' for r in rows(c,t['project'],'sample')):raise ValueError('请在商品档案中提交结构化实物检查及来源')
    if t['role']==s.ROLES[7]:
        import media
        if not media.ready(c,t['project']):raise ValueError('视觉任务须交付经过核查的实际素材，而不是制作方案')
    if t['role']==s.ROLES[8] and '检查' in t['title']:
        missing=gate(c,t['project'],'上线')
        if missing:raise ValueError('上线检查尚有缺口：'+'；'.join(missing))


def mutate(c,path,d):
    p=d['project']
    s.begin_write(c)
    if path=='/api/entities':
        kind=d.get('kind');v=d.get('value')
        if kind not in KINDS or not isinstance(v,dict):raise ValueError('档案类型或内容无效')
        v={k:str(value).strip() for k,value in v.items()}
        for key in REQUIRED[kind]:s.required(v,key)
        for key,target in [('product_id','product'),('supplier_id','supplier')]:
            if key in v and not c.execute('SELECT 1 FROM entities WHERE id=? AND project=? AND kind=?',(v[key],p,target)).fetchone():raise ValueError('关联档案不存在或不属于当前项目')
        if kind=='quote':
            s.money(v['price'])
            try:datetime.date.fromisoformat(v['valid_until'])
            except ValueError:raise ValueError('报价有效期使用 YYYY-MM-DD')
        if kind=='sample':
            if v['result'] not in {'通过','不通过','待核实'}:raise ValueError('样品结果无效')
            try:datetime.date.fromisoformat(v['inspection_date'])
            except ValueError:raise ValueError('检查日期格式无效')
        if kind=='risk' and (v['level'] not in {'高','中','低'} or v['status'] not in {'待核查','处理中','已处理'}):raise ValueError('风险状态无效')
        if kind=='ticket' and v['status'] not in {'待处理','处理中','等待客户','已解决','已关闭'}:raise ValueError('工单状态无效')
        if kind=='checklist' and v['result'] not in {'通过','不通过','待核实'}:raise ValueError('检查结果无效')
        old=c.execute('SELECT * FROM entities WHERE id=? AND project=?',(d.get('id'),p)).fetchone() if d.get('id') else None
        if d.get('id') and (not old or old['kind']!=kind):raise ValueError('档案不存在')
        i=old['id'] if old else s.uid();version=old['version']+1 if old else 1;data=json.dumps(v,ensure_ascii=False);reason=d.get('reason','新建档案')
        if old:
            if d.get('version')!=old['version']:raise ValueError('档案已被更新，请刷新后再修改')
            s.required(d,'reason')
            c.execute('UPDATE entities SET data=?,version=?,updated=? WHERE id=?',(data,version,s.now(),i))
        else:c.execute('INSERT INTO entities VALUES(?,?,?,?,?,?,?)',(i,p,kind,data,version,s.now(),s.now()))
        c.execute('INSERT INTO entity_versions VALUES(?,?,?,?,?,?,?)',(s.uid(),p,i,version,data,reason,s.now()))
        if old and kind=='product':
            for content in rows(c,p,'content'):
                if content['value'].get('product_id')!=i:continue
                body={**content['value'],'classification':'待核实','product_fact_changed':'商品事实已更正，原文案需重新核查'}
                content_version=content['version']+1;c.execute('UPDATE entities SET data=?,version=?,updated=? WHERE id=?',(json.dumps(body,ensure_ascii=False),content_version,s.now(),content['id']))
                c.execute('INSERT INTO entity_versions VALUES(?,?,?,?,?,?,?)',(s.uid(),p,content['id'],content_version,json.dumps(body,ensure_ascii=False),'关联商品事实更正，旧内容不得继续当作已核查',s.now()))
                c.execute("UPDATE publications SET status='待重新核查',updated=? WHERE content_id=? AND status='已发布'",(s.now(),content['id']))
                c.execute("UPDATE external_actions SET error='关联商品事实已更正，请核查外部已发布页面',updated=? WHERE content=? AND status='已确认'",(s.now(),content['id']))
            store=c.execute('SELECT * FROM stores WHERE project=?',(p,)).fetchone()
            if store and any(x['product_id']==i for x in json.loads(store['catalog'])):
                c.execute("UPDATE stores SET status='待重新核查',updated=? WHERE project=?",(s.now(),p))
                followup(c,p,'product-change:'+i+':'+str(old['version']+1),'商品事实变化，核查页面、素材、定价与已发生订单',s.ROLES[8])
        if old and kind=='content':
            c.execute("UPDATE publications SET status='待重新核查',updated=? WHERE content_id=? AND status='已发布'",(s.now(),i))
        decision_only=old and kind=='opportunity' and {k:v for k,v in json.loads(old['data']).items() if k!='decision'}=={k:v for k,v in v.items() if k!='decision'}
        if old and kind in {'product','supplier','quote','sample','risk','opportunity'} and not decision_only:
            s.invalidate(c,p,'业务档案更正：'+kind);put_flow(c,p,status='暂停')
        if kind=='sample' and v['result']=='通过':
            c.execute("UPDATE tasks SET status='待验收' WHERE project=? AND role=? AND status='等待人工'",(p,s.ROLES[3]))
        if kind=='risk' and v['status']!='已处理':
            put_flow(c,p,status='暂停');c.execute("UPDATE attempts SET status='已取消',error='风险核查暂停',updated=? WHERE project=? AND status IN ('排队中','执行中')",(s.now(),p))
            c.execute("UPDATE tasks SET status='暂停' WHERE project=? AND status='进行中'",(p,))
            followup(c,p,'risk:'+i+':'+str(version),'核查风险：'+v['name'],s.ROLES[4])
        if kind=='ticket' and not old:followup(c,p,'ticket:'+i,'处理客户工单：'+v['subject'],s.ROLES[13])
        s.log(c,p,'保存'+kind+'档案，第'+str(version)+'版');return {'id':i,'version':version}
    if path=='/api/flow':
        status=d.get('status');auto=d.get('autopilot')
        if status not in {'进行中','暂停','结束'} or auto not in {0,1}:raise ValueError('项目运行设置无效')
        current=flow(c,p)
        if status=='进行中' and any(r['value']['status']!='已处理' for r in rows(c,p,'risk')):raise ValueError('风险尚未处理，不能恢复项目')
        put_flow(c,p,status=status,autopilot=auto)
        if status!='进行中':
            c.execute("UPDATE jobs SET status='已取消',error='项目暂停或结束',updated=? WHERE project=? AND status IN ('排队中','执行中')",(s.now(),p))
            c.execute("UPDATE attempts SET status='已取消',error='项目暂停或结束',updated=? WHERE project=? AND status IN ('排队中','执行中')",(s.now(),p))
            c.execute("UPDATE tasks SET status='暂停',version=version+1 WHERE project=? AND status='进行中'",(p,))
        s.log(c,p,'项目状态：'+status+'；自动派工：'+str(auto));return flow(c,p)
    if path=='/api/advance':
        stages=['探索','验证','销售准备','经营'];old=flow(c,p);index=stages.index(old['stage'])
        if index==3:raise ValueError('已在经营阶段，请创建复盘与实验')
        stage=stages[index+1];missing=gate(c,p,stage)
        if missing:raise ValueError('尚未达到阶段条件：'+'；'.join(missing))
        if old['status']!='进行中':raise ValueError('请先恢复项目')
        reason=d.get('reason') or '用户确认系统阶段检查通过，进入'+stage+'；不扩大支出或外部操作授权'
        if not isinstance(reason,str) or len(reason)>10000:raise ValueError('阶段确认内容无效')
        put_flow(c,p,stage=stage)
        titles={'验证':[('供应与样品验证计划',s.ROLES[3]),('商品合规风险核查',s.ROLES[4])],'销售准备':[('品牌与商品页面文案',s.ROLES[6]),('商品主图与详情设计方案',s.ROLES[7]),('站点购买与追踪检查',s.ROLES[8]),('客服知识与服务政策',s.ROLES[13])],'经营':[('首轮SEO内容与获客计划',s.ROLES[9]),('渠道合作与内容计划',s.ROLES[10]),('预算内广告测试方案',s.ROLES[11]),('订单履约监测计划',s.ROLES[12])]}
        tasks=[task(c,p,title,role,key='stage:'+stage+':'+role) for title,role in titles[stage]]
        c.execute('INSERT INTO plans VALUES(?,?,?,?,?,?)',(s.uid(),p,stage,reason,json.dumps(tasks),s.now()));s.log(c,p,'用户确认进入'+stage+'阶段：'+reason);return {'stage':stage,'tasks':tasks}
    if path=='/api/approvals':
        kind=d.get('kind')
        if kind not in {'采购','追加预算','调价','退款赔偿','方向变更','内容发布','站点发布','广告执行','客服回复','物流交接','许可触达','供应询价'}:raise ValueError('审批类型无效')
        amount=s.money(d.get('amount','0'));i=s.uid()
        c.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?)',(i,p,kind,s.required(d,'subject'),amount,'待审批',s.required(d,'reason'),'',s.now(),s.now()))
        s.log(c,p,'提交审批：'+kind+'；未执行外部操作');return {'id':i}
    if path=='/api/approval/decide':
        row=c.execute('SELECT * FROM approvals WHERE id=? AND project=?',(d.get('id'),p)).fetchone()
        if not row:raise ValueError('审批不存在')
        status=d.get('status')
        if status=='已撤销' and row['status']=='已批准':
            note=s.required(d,'decision') if d.get('decision') else '用户已核查本次内容并确认：'+status;c.execute("UPDATE approvals SET status='已撤销',decision=?,updated=? WHERE id=?",(note,s.now(),row['id']))
            c.execute("UPDATE external_actions SET status='已取消',error='审批已撤销',updated=? WHERE approval=? AND status='待执行'",(s.now(),row['id']))
            c.execute("UPDATE service_actions SET status='已取消',error='审批已撤销',updated=? WHERE approval=? AND status='待执行'",(s.now(),row['id']))
            s.log(c,p,'审批撤销：'+row['subject']);return {'ok':True}
        if row['status']!='待审批' or status not in {'已批准','已拒绝'}:raise ValueError('审批不存在、已处理或决策无效')
        if status=='已批准' and row['amount'] and row['kind']!='广告执行':
            from finance import committed
            budget=c.execute('SELECT budget FROM projects WHERE id=?',(p,)).fetchone()[0]
            if committed(c,p,exclude_approval=row['id'])+row['amount']>budget:raise ValueError('审批金额超过剩余预算；请先调整预算再审批')
        note=s.required(d,'decision') if d.get('decision') else '用户已核查本次内容并确认：'+status;c.execute('UPDATE approvals SET status=?,decision=?,updated=? WHERE id=?',(status,note,s.now(),row['id']));s.log(c,p,'审批'+status+'：'+row['subject']+'；'+note);return {'ok':True}
    raise ValueError('未知业务操作')
