"""Per-role protocols and versioned, source-bound drafts (never invented verified facts)."""
import json,io,hashlib
import server as s,business
PROFILES=[
('经营负责人',['启动卡','阶段计划','协调与复盘'],['项目条件','经营反馈']),
('市场与消费者研究',['来源摘录','竞争比较','少量机会假设'],['研究资料','项目约束']),
('选品与商品运营',['商品规格草稿','SKU建议','取舍依据'],['机会','商品及供应资料']),
('采购与供应商管理',['供应核实问题清单','报价缺口','样品人工交接'],['商品事实','供应商','报价']),
('跨境合规与风险',['权威来源与适用范围','证据缺口','整改与复核事项'],['市场','商品材料','风险来源']),
('财务与经营分析',['逐商品正常及保守测算检查','费用与对账缺口'],['成本','订单','费用']),
('品牌与本地化文案',['事实核对后的页面草稿','术语与承诺核查'],['商品事实','主要语言']),
('电商视觉与视频设计',['真实参考图约束','5张主图及10张9:16详情的生产清单','实际文件验收'],['真实商品图','使用权','品牌']),
('独立站运营与转化',['购买路径检查','单变量调整计划','回滚条件'],['页面','站内漏斗','服务反馈']),
('SEO与内容增长',['搜索意图与页面映射','有用内容草稿','更新与合并计划'],['来源','商品','实际访问']),
('社媒、达人与联盟',['内容日历','合作筛选标准','授权联系草稿'],['渠道','内容','合作预算']),
('广告投放',['追踪就绪检查','有限预算测试卡','暂停规则'],['商品贡献','渠道','素材','预算']),
('订单与跨境物流',['支付/库存/地址待核查清单','履约交接','异常跟进'],['核查后订单','物流状态']),
('客服与客户生命周期',['事实与政策核查','待审批答复','工单协同'],['商品','政策','最少必要服务信息'])]

def spec(role,title):
    profile=PROFILES[s.ROLES.index(role)]
    return {'objective':title,'role_goal':profile[0],'input_requirements':profile[2],'deliverables':profile[1],'acceptance_rules':['结论引用输入资料ID','不把假设/未知变成事实','真实外部动作须有工具台账','重要经营决策交给用户'],'permission_scope':['本项目最少必要资料','生成待核查草稿'],'budget_limit':{'calls':1,'output_tokens':2000},'escalation_rules':['证据缺失或冲突','预算不足','需认证/付款/实物核查'],'artifact_schema':{'optional':'除基础结果字段外可返回 artifacts 数组；最多3份。每项为 kind(opportunity/product/content/supplier/risk)、value(对应档案字段)、evidence_ids(必须是当前资料ID)。不得编造规格、报价、主体或实际检查；未知写待核实。opportunity需name,audience,problem,validation,source；content需name,product_id,channel,body,source；product需name,sku,specification,material,weight,dimensions,usage,limitations,source。'},'constraints':'草稿入库不等于事实确认或外部执行。'}

def artifacts(c,task,result):
    proposals=result.get('artifacts',[])
    if not isinstance(proposals,list) or len(proposals)>3:raise ValueError('专业草稿档案数量或格式无效')
    allowed={'opportunity','product','content','supplier','risk'}
    known={r['id'] for r in s.effective_records(c,task['project'])}|{r['id'] for r in business.rows(c,task['project'])}
    created=[]
    for proposal in proposals:
        if not isinstance(proposal,dict) or proposal.get('kind') not in allowed or not isinstance(proposal.get('value'),dict):raise ValueError('专业草稿档案格式无效')
        refs=proposal.get('evidence_ids',[])
        if not isinstance(refs,list) or not refs or not set(refs).issubset(known):raise ValueError('专业档案必须引用本项目存在的资料')
        kind=proposal['kind'];value=proposal['value']
        allowed_by_role={s.ROLES[1]:{'opportunity'},s.ROLES[2]:{'product'},s.ROLES[3]:{'supplier'},s.ROLES[4]:{'risk'},s.ROLES[6]:{'content'},s.ROLES[9]:{'content'}}
        if kind not in allowed_by_role.get(task['role'],set()):raise ValueError('草稿类型超出岗位权限')
        value={**value,'classification':'待核实','evidence_ids':','.join(refs),'source':'输入资料ID：'+','.join(refs)}
        if kind=='risk':value['status']='待核查'
        if kind=='opportunity':value['decision']='待确认'
        if kind=='product':
            matches=[r for r in business.rows(c,task['project'],'product') if r['value'].get('sku')==value.get('sku')]
            if matches:
                conflict_id=conflict(c,task,matches[0],value,refs)
                if conflict_id:result.setdefault('conflict_ids',[]).append(conflict_id)
                continue
        created.append(business.mutate(c,'/api/entities',{'project':task['project'],'kind':kind,'value':value})['id'])
    return created

def import_file(c,p,file_id):
    f=c.execute('SELECT * FROM files WHERE id=? AND project=?',(file_id,p)).fetchone()
    if not f:raise ValueError('文件不存在')
    raw=bytes(f['data'])
    if f['mime'] in {'text/plain','text/csv','application/json'}:text=raw.decode('utf-8')
    elif f['mime']=='application/pdf':
        from pypdf import PdfReader
        try:
            reader=PdfReader(io.BytesIO(raw))
            if reader.is_encrypted:raise ValueError('加密PDF需先由文件持有人解密')
            text='\n'.join((page.extract_text() or '')[:10000] for page in reader.pages[:20])
        except (ValueError,TypeError,KeyError):raise ValueError('PDF无法提取文本；扫描文件需提供可读文本，不猜测内容') from None
    else:raise ValueError('图片用于素材参考；本入口提取UTF-8文本或非扫描PDF')
    if not text.strip():raise ValueError('文件没有可提取文本，不能冒充已读取')
    old=c.execute('SELECT id FROM records WHERE project=? AND kind=?',(p,'导入文档')).fetchall()
    for r in old:
        body=json.loads(c.execute('SELECT body FROM records WHERE id=?',(r['id'],)).fetchone()[0])
        if body.get('file_id')==file_id:return {'id':r['id'],'duplicate':True}
    i=s.uid();c.execute('INSERT INTO records VALUES(?,?,?,?,?)',(i,p,'导入文档',json.dumps({'content':text[:30000],'source':'文件 '+f['name']+'；ID '+file_id,'file_id':file_id,'classification':'待核实','truncated':len(text)>30000},ensure_ascii=False),s.now()));return {'id':i,'truncated':len(text)>30000}

def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS input_snapshots(attempt TEXT PRIMARY KEY REFERENCES attempts(id),project TEXT REFERENCES projects(id),body TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS fact_conflicts(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),entity TEXT REFERENCES entities(id),risk TEXT,proposal TEXT,status TEXT,decision TEXT,created TEXT,updated TEXT);''')

def inputs(c,task,records,entities):
    role=task['role'];allowed={
        s.ROLES[1]:{'opportunity'},s.ROLES[2]:{'opportunity','product','supplier','quote','sample','risk'},
        s.ROLES[3]:{'product','supplier','quote','sample'},s.ROLES[4]:{'product','risk','sample'},
        s.ROLES[6]:{'product','content','risk'},s.ROLES[7]:{'product','content'},s.ROLES[8]:{'product','content','checklist','risk'},
        s.ROLES[9]:{'opportunity','product','content'},s.ROLES[10]:{'opportunity','product','content'},s.ROLES[11]:{'product','content','risk'},
        s.ROLES[12]:{'product','sample'},s.ROLES[13]:{'product','content'}}.get(role,set(business.KINDS)-{'ticket'})
    es=[e for e in entities if e['kind'] in allowed]
    if role==s.ROLES[13]:
        follow=c.execute("SELECT event_key FROM followups WHERE task=? AND event_key LIKE 'ticket:%'",(task['id'],)).fetchone()
        if follow:
            row=c.execute("SELECT * FROM entities WHERE id=? AND project=? AND kind='ticket'",(follow['event_key'].split(':',1)[1],task['project'])).fetchone()
            if row:
                v=json.loads(row['data'])
                if v.get('ai_consent')=='允许':
                    clean={k:v[k] for k in ['subject','message','status'] if k in v};es.append(dict(row)|{'data':json.dumps(clean,ensure_ascii=False),'value':clean})
    rs=records
    if role in {s.ROLES[1],s.ROLES[9],s.ROLES[10]}:rs=[r for r in records if r['kind'] in {'机会证据','导入文档','商品与供应'}]
    elif role in {s.ROLES[6],s.ROLES[7],s.ROLES[13]}:rs=[r for r in records if r['kind'] in {'商品与供应','风险','导入文档'}]
    return rs[-12:],es[-30:]

def snapshot(c,attempt,task,project,records,entities):
    body={'task_version':task['version'],'project_hash':hashlib.sha256(json.dumps(project,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),'records':[{'id':r['id'],'body_hash':hashlib.sha256(r['body'].encode()).hexdigest()} for r in records],'entities':[{'id':e['id'],'version':e['version']} for e in entities]}
    c.execute('INSERT INTO input_snapshots VALUES(?,?,?,?) ON CONFLICT(attempt) DO NOTHING',(attempt['id'],task['project'],json.dumps(body),s.now()))

def conflict(c,task,old,value,refs):
    existing=json.loads(old['data'])
    fields=business.REQUIRED['product']
    if not any(str(existing.get(k,''))!=str(value.get(k,'')) for k in fields if k!='source'):return None
    i=s.uid();risk=business.mutate(c,'/api/entities',{'project':task['project'],'kind':'risk','value':{'name':'商品事实冲突：'+value['sku'],'market':'当前项目市场','level':'高','status':'待核查','source':'原商品档案 '+old['id']+'；新资料 '+','.join(refs),'action':'由经营者核实原资料与新草稿，确认后再恢复关联工作'}})['id']
    c.execute('INSERT INTO fact_conflicts VALUES(?,?,?,?,?,?,?,?,?)',(i,task['project'],old['id'],risk,json.dumps(value,ensure_ascii=False),'待核实','',s.now(),s.now()))
    s.log(c,task['project'],'关键商品事实出现冲突，保留原事实并暂停待核实');return i
