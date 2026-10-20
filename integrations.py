"""Explicitly approved service adapters with immutable payloads and no blind retry."""
import json,re,datetime,secrets,hashlib,html
from urllib.parse import urlencode,quote
import server as s,runtime,net,business

def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS contacts(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),email TEXT,purpose TEXT,consent TEXT,status TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS shipments(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),order_id TEXT REFERENCES orders(id),tracking TEXT,carrier TEXT,external_id TEXT,status TEXT,details TEXT,updated TEXT,UNIQUE(project,order_id,tracking));
    CREATE TABLE IF NOT EXISTS service_actions(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),kind TEXT,target TEXT,approval TEXT,payload TEXT,request_key TEXT,status TEXT,external_id TEXT,result TEXT,error TEXT,created TEXT,updated TEXT,UNIQUE(project,request_key));
    CREATE TABLE IF NOT EXISTS consent_tokens(token_hash TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),contact TEXT REFERENCES contacts(id),created TEXT);
    CREATE TABLE IF NOT EXISTS channel_metrics(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),channel TEXT,object_id TEXT,data TEXT,collected TEXT);''')

def status(c,p):
    requirements={'research':['SEARCH_API_KEY'],'images':['MEDIA_API_KEY'],'video':['MEDIA_API_KEY'],'logistics':['AFTERSHIP_API_KEY'],'email':['RESEND_API_KEY','EMAIL_FROM'],'ads':['META_ACCESS_TOKEN','META_AD_ACCOUNT_ID'],'social':['META_PAGE_TOKEN','META_PAGE_ID'],'refund':['STRIPE_SECRET_KEY']}
    result={}
    for name,keys in requirements.items():
        configured=all(runtime.env(c,p,k) for k in keys)
        if name in {'images','video'}:configured=bool(runtime.env(c,p,'MEDIA_API_KEY') or runtime.env(c,p,'AI_API_KEY') or runtime.env(c,p,'OPENAI_API_KEY'))
        if name=='refund':
            import storefront
            configured=bool(storefront.credentials(c,p)[0])
        result[name]={'configured':configured,'requirements':keys,'status':'凭据已配置，实际权限待调用核查' if configured else '未配置'}
    return result

def checked_approval(c,p,kind,target,approval,updated=None,amount=None):
    row=c.execute("SELECT * FROM approvals WHERE id=? AND project=? AND kind=? AND subject=? AND status='已批准'",(approval,p,kind,target)).fetchone()
    if not row or (updated and row['updated']<updated) or (amount is not None and row['amount']!=amount):raise ValueError('需要对应对象、当前版本和金额的有效审批')
    return row

def mutate(c,path,d):
    p=d['project'];s.begin_write(c)
    if path=='/api/contacts':
        email=s.required(d,'email');purpose=d.get('purpose');consent=s.required(d,'consent')
        if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+',email) or len(email)>254:raise ValueError('联系邮箱格式无效')
        if purpose not in {'服务','营销','供应联系'}:raise ValueError('联系用途无效')
        i=s.uid();c.execute('INSERT INTO contacts VALUES(?,?,?,?,?,?,?)',(i,p,email,purpose,consent,'有效',s.now()));return {'id':i}
    if path=='/api/contacts/revoke':
        c.execute("UPDATE contacts SET status='已撤销' WHERE id=? AND project=?",(d.get('id'),p));return {'ok':True}
    if path=='/api/services/insights':
        if not c.execute('SELECT 1 FROM campaigns WHERE id=? AND project=?',(d.get('target'),p)).fetchone():raise ValueError('营销计划不存在')
        return runtime.enqueue(c,p,'ad',{'insights':d['target']},key='insights:'+s.uid())
    if path=='/api/services/reconcile':
        row=c.execute('SELECT * FROM service_actions WHERE id=? AND project=?',(d.get('id'),p)).fetchone()
        if not row or row['status'] not in {'结果未知','已发送','处理中','已确认'}:raise ValueError('该操作当前不需要查询')
        return runtime.enqueue(c,p,job_kind(row['kind']),{'action':row['id'],'reconcile':True},key='service-query:'+row['id']+':'+s.uid())
    if path=='/api/services/prepare':
        kind=d.get('kind');target=s.required(d,'target');payload=d.get('payload',{})
        if kind not in {'tracking','reply','inquiry','lifecycle','ad','social','refund'} or not isinstance(payload,dict):raise ValueError('外部操作内容无效')
        # Validate and freeze exact scope. Revalidated immediately before sending.
        validate(c,p,kind,target,None,payload,preparing=True)
        key=s.required(d,'request_key')
        old=c.execute('SELECT * FROM service_actions WHERE project=? AND request_key=?',(p,key)).fetchone()
        if old:return {'id':old['id'],'status':old['status'],'duplicate':True}
        approval=business.mutate(c,'/api/approvals',{'project':p,'kind':{'tracking':'物流交接','reply':'客服回复','inquiry':'供应询价','lifecycle':'许可触达','ad':'广告执行','social':'内容发布','refund':'退款赔偿'}[kind],'subject':target,'amount':payload.get('amount','0') if kind=='refund' else str(c.execute('SELECT budget FROM campaigns WHERE id=?',(target,)).fetchone()[0]/100) if kind=='ad' else '0','reason':'请核查本次唯一外部操作及具体内容，批准后不可替换：'+json.dumps({'kind':kind,'target':target,'payload':payload},ensure_ascii=False)})['id']
        i=s.uid();c.execute('INSERT INTO service_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(i,p,kind,target,approval,json.dumps(payload,ensure_ascii=False),key,'待执行','',None,'',s.now(),s.now()))
        return {'id':i,'approval':approval,'status':'待执行，等待审批'}
    if path=='/api/services/execute':
        row=c.execute('SELECT * FROM service_actions WHERE id=? AND project=?',(d.get('id'),p)).fetchone()
        if not row or row['status']!='待执行':raise ValueError('操作不在待执行状态；结果未知时只能查询')
        validate(c,p,row['kind'],row['target'],row['approval'],json.loads(row['payload']),action_id=row['id'])
        key='service:'+row['id']
        previous=c.execute('SELECT * FROM jobs WHERE project=? AND request_key=?',(p,key)).fetchone()
        if previous and previous['status']=='等待处理' and not c.execute('SELECT 1 FROM tool_uses WHERE job=?',(previous['id'],)).fetchone():key+=':retry:'+s.uid()
        return runtime.enqueue(c,p,job_kind(row['kind']),{'action':row['id']},key=key)
    raise ValueError('未知外部服务操作')

def job_kind(kind):
    if kind in {'lifecycle','inquiry'}:return 'reply'
    return 'tracking' if kind=='tracking' else 'reply' if kind=='reply' else kind

def validate(c,p,kind,target,approval,payload,preparing=False,action_id=None):
    def check(*args,**kwargs):
        if not preparing:return checked_approval(*args,**kwargs)
    if kind=='tracking':
        order=c.execute('SELECT * FROM orders WHERE id=? AND project=?',(target,p)).fetchone()
        if not order or order['external_id'].startswith('TEST-') or order['status'] not in {'已确认支付','已发货','售后处理中'}:raise ValueError('物流交接须对应已核查支付的真实订单')
        for key in ['tracking','carrier']:
            if not re.fullmatch(r'[A-Za-z0-9_-]{2,100}',str(payload.get(key,''))):raise ValueError('物流单号或承运商标识无效')
        check(c,p,'物流交接',target,approval)
    elif kind=='reply':
        ticket=c.execute("SELECT * FROM entities WHERE id=? AND project=? AND kind='ticket'",(target,p)).fetchone()
        contact=c.execute("SELECT * FROM contacts WHERE id=? AND project=? AND status='有效'",(payload.get('contact'),p)).fetchone()
        if not ticket or not contact:raise ValueError('需要本项目有效联系人和工单')
        if contact['purpose']!='服务':raise ValueError('本入口仅允许有依据的服务回复，不发送营销邮件')
        if json.loads(ticket['data']).get('customer_ref')!=contact['id']:raise ValueError('工单客户引用必须匹配该联系人ID')
        s.required(payload,'subject');s.required(payload,'body')
        check(c,p,'客服回复',target,approval,ticket['updated'])
    elif kind=='inquiry':
        supplier=c.execute("SELECT * FROM entities WHERE id=? AND project=? AND kind='supplier'",(target,p)).fetchone()
        contact=c.execute("SELECT * FROM contacts WHERE id=? AND project=? AND purpose='供应联系' AND status='有效'",(payload.get('contact'),p)).fetchone()
        if not supplier or not contact or json.loads(supplier['data']).get('contact')!=contact['id']:raise ValueError('需要已核实供应商及其匹配的许可业务联系人ID')
        if json.loads(supplier['data']).get('classification')=='待核实':raise ValueError('先核实供应主体及联系对象，再授权询价')
        s.required(payload,'subject');s.required(payload,'body');check(c,p,'供应询价',target,approval,supplier['updated'])
    elif kind=='lifecycle':
        contact=c.execute("SELECT * FROM contacts WHERE id=? AND project=? AND purpose='营销' AND status='有效'",(target,p)).fetchone()
        content=c.execute("SELECT * FROM entities WHERE id=? AND project=? AND kind='content'",(payload.get('content'),p)).fetchone()
        if not contact or not content:raise ValueError('需要本项目有效营销许可与准确内容档案')
        check(c,p,'许可触达',target,approval,content['updated'])
    elif kind=='social':
        content=c.execute("SELECT * FROM entities WHERE id=? AND project=? AND kind='content'",(target,p)).fetchone()
        if not content:raise ValueError('没有可发布的文案')
        check(c,p,'内容发布',target,approval,content['updated'])
        if payload:raise ValueError('社媒发布使用已审批的文案，不接受替换内容')
    elif kind=='ad':
        campaign=c.execute('SELECT * FROM campaigns WHERE id=? AND project=?',(target,p)).fetchone()
        if not campaign:raise ValueError('营销计划不存在')
        check(c,p,'广告执行',target,approval,amount=campaign['budget'])
        op=payload.get('operation','create_campaign')
        if op not in {'create_campaign','pause_campaign','create_adset','create_creative','create_ad','activate_ad'}:raise ValueError('不支持的广告动作')
        # Creation is paused; activation is a separate immutable, approved operation.
        if op=='activate_ad' and campaign['spent']>=campaign['budget']:raise ValueError('计划费用已达到预算，不得再次启用')
        if op=='create_adset':
            amount=s.money(payload.get('lifetime_budget'))
            if amount<=0 or amount>campaign['budget']:raise ValueError('广告生命周期预算超出计划')
            for k in ['campaign_id','pixel_id']:
                if not re.fullmatch('[0-9]{3,40}',str(payload.get(k,''))):raise ValueError('广告对象标识无效')
            if not isinstance(payload.get('targeting'),dict):raise ValueError('需要明确广告定向')
            for k in ['start_time','end_time']:
                try:datetime.datetime.fromisoformat(str(payload.get(k,'')))
                except ValueError:raise ValueError('广告时间范围无效')
        if op in {'create_ad','pause_campaign','activate_ad'}:

            for k in (['adset_id','creative_id'] if op=='create_ad' else ['ad_id'] if op=='activate_ad' else ['campaign_id']):
                if not re.fullmatch('[0-9]{3,40}',str(payload.get(k,''))):raise ValueError('广告对象标识无效')
        for key,operation in [('campaign_id','create_campaign'),('adset_id','create_adset'),('creative_id','create_creative'),('ad_id','create_ad')]:
            if key in payload:
                rows=c.execute("SELECT payload FROM service_actions WHERE project=? AND target=? AND kind='ad' AND external_id=? AND status='已确认'",(p,target,payload[key])).fetchall()
                if not any(json.loads(r['payload']).get('operation','create_campaign')==operation for r in rows):raise ValueError('广告关联对象未在本项目和计划内核对，不能借用其他对象')
        if op=='create_adset':
            reserved=c.execute("SELECT id,payload FROM service_actions WHERE project=? AND target=? AND kind='ad' AND status NOT IN ('已取消','失败') AND id!=?",(p,target,action_id or '')).fetchall()
            allocated=sum(s.money(json.loads(r['payload']).get('lifetime_budget','0')) for r in reserved if json.loads(r['payload']).get('operation')=='create_adset')
            if allocated+s.money(payload['lifetime_budget'])>campaign['budget']:raise ValueError('所有广告组的生命周期预算合计超过计划')
        if op=='create_creative':
            pub=c.execute("SELECT 1 FROM publications WHERE id=? AND project=? AND status='已发布'",(payload.get('publication'),p)).fetchone()
            if not pub:raise ValueError('广告素材须对应已发布的本项目页面')
    elif kind=='refund':
        order=c.execute('SELECT * FROM orders WHERE id=? AND project=?',(target,p)).fetchone()
        if not order or not order['external_id'].startswith('STRIPE-') or order['status'] in {'待核查','已取消','已关闭'}:raise ValueError('退款执行仅支持已核查的本平台真实Stripe订单')
        amount=s.money(payload.get('amount'))
        if amount<=0 or amount+order['refund']>order['revenue']:raise ValueError('退款超过可退余额')
        check(c,p,'退款赔偿',target,approval,amount=amount)
        pending=c.execute("SELECT id,payload FROM service_actions WHERE project=? AND target=? AND kind='refund' AND id!=? AND status NOT IN ('已取消','失败','已确认')",(p,target,action_id or '')).fetchall()
        if sum(s.money(json.loads(x['payload'])['amount']) for x in pending)+amount+order['refund']>order['revenue']:raise ValueError('已有待确认退款占用余额，请先核对，避免超额退款')

def graph(c,p,path,data=None,page=False):
    token=runtime.env(c,p,'META_PAGE_TOKEN' if page else 'META_ACCESS_TOKEN')
    if not token:raise ValueError('Meta账号尚未配置')
    version=runtime.env(c,p,'META_API_VERSION') or 'v24.0'
    if not re.fullmatch(r'v[0-9]{1,2}\.[0-9]',version):raise ValueError('Meta接口版本配置无效')
    return net.json_request('https://graph.facebook.com/'+version+'/'+path,data,{'Authorization':'Bearer '+token})

def send(c,row,payload):
    p=row['project'];kind=row['kind'];target=row['target'];marker='venture-'+row['id']
    if kind=='tracking':
        key=runtime.env(c,p,'AFTERSHIP_API_KEY')
        if not key:raise ValueError('物流服务尚未配置 AFTERSHIP_API_KEY')
        result=net.json_request('https://api.aftership.com/v4/trackings',{'tracking':{'tracking_number':payload['tracking'],'slug':payload['carrier'],'order_id':marker}},headers={'aftership-api-key':key})
        return result.get('data',{}).get('tracking',{})
    if kind in {'reply','inquiry','lifecycle'}:
        key=runtime.env(c,p,'RESEND_API_KEY');sender=runtime.env(c,p,'EMAIL_FROM');contact=c.execute('SELECT * FROM contacts WHERE id=?',(payload['contact'] if kind in {'reply','inquiry'} else target,)).fetchone()
        if not key or not sender:raise ValueError('邮件服务或已验证发件人未配置')
        if kind=='lifecycle':
            origin=(__import__('os').environ.get('APP_PUBLIC_ORIGIN') or __import__('os').environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')
            if not origin.startswith('https://'):raise ValueError('许可触达需要可用的HTTPS退订入口')
            token=secrets.token_hex(32);c.execute('INSERT INTO consent_tokens VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),p,target,s.now()));c.commit()
            content=json.loads(c.execute('SELECT data FROM entities WHERE id=?',(payload['content'],)).fetchone()[0]);subject=content['name'];body=content['body']+'\n\n停止接收此类内容：'+origin+'/unsubscribe/'+token
        else:subject=payload['subject'];body=payload['body']
        return net.json_request('https://api.resend.com/emails',{'from':sender,'to':[contact['email']],'subject':subject,'text':body},headers={'Authorization':'Bearer '+key,'Idempotency-Key':marker})
    if kind=='social':
        page=runtime.env(c,p,'META_PAGE_ID')
        if not re.fullmatch('[0-9]{3,40}',page):raise ValueError('请配置本工作区Meta主页ID')
        content=json.loads(c.execute('SELECT data FROM entities WHERE id=?',(target,)).fetchone()[0])
        return graph(c,p,page+'/feed',{'message':content['body'],'published':True},page=True)
    if kind=='ad':
        account=runtime.env(c,p,'META_AD_ACCOUNT_ID').removeprefix('act_')
        if not re.fullmatch('[0-9]{3,40}',account):raise ValueError('广告账号ID未配置')
        campaign=c.execute('SELECT * FROM campaigns WHERE id=?',(target,)).fetchone();op=payload.get('operation','create_campaign');name=campaign['name']+' '+marker
        if op=='create_campaign':return graph(c,p,'act_'+account+'/campaigns',{'name':name,'objective':'OUTCOME_SALES','status':'PAUSED','special_ad_categories':[]})
        if op=='activate_ad':
            ad=graph(c,p,payload['ad_id']+'?fields=id,adset_id,campaign_id')
            adset=graph(c,p,str(ad['adset_id'])+'?fields=id,lifetime_budget,daily_budget,end_time')
            if adset.get('daily_budget') or int(adset.get('lifetime_budget',0))>campaign['budget'] or not adset.get('end_time'):raise ValueError('外部广告预算/截止时间与有限测试要求不符，不启用')
            graph(c,p,str(ad['campaign_id']),{'status':'ACTIVE'});graph(c,p,str(ad['adset_id']),{'status':'ACTIVE'})
            return graph(c,p,payload['ad_id'],{'status':'ACTIVE'})|{'id':payload['ad_id']}
        if op=='pause_campaign':return graph(c,p,payload['campaign_id'],{'status':'PAUSED'})|{'id':payload['campaign_id']}
        if op=='create_adset':return graph(c,p,'act_'+account+'/adsets',{'name':name,'campaign_id':payload['campaign_id'],'lifetime_budget':s.money(payload['lifetime_budget']),'billing_event':'IMPRESSIONS','optimization_goal':'OFFSITE_CONVERSIONS','promoted_object':{'pixel_id':payload['pixel_id'],'custom_event_type':'PURCHASE'},'targeting':payload['targeting'],'start_time':payload['start_time'],'end_time':payload['end_time'],'status':'PAUSED'})
        if op=='create_creative':
            pub=c.execute('SELECT * FROM publications WHERE id=?',(payload['publication'],)).fetchone();origin=(__import__('os').environ.get('APP_PUBLIC_ORIGIN') or __import__('os').environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')
            if not origin.startswith('https://'):raise ValueError('真实广告需配置本商店HTTPS公开地址')
            return graph(c,p,'act_'+account+'/adcreatives',{'name':name,'object_story_spec':{'page_id':runtime.env(c,p,'META_PAGE_ID'),'link_data':{'link':origin+'/shop/'+pub['store']+'/articles/'+pub['id'],'message':pub['title']}}})
        return graph(c,p,'act_'+account+'/ads',{'name':name,'adset_id':payload['adset_id'],'creative':{'creative_id':payload['creative_id']},'status':'PAUSED'})
    if kind=='refund':
        import storefront
        checkout_id=c.execute('SELECT external_id FROM orders WHERE id=?',(target,)).fetchone()[0].removeprefix('STRIPE-')
        checkout=c.execute('SELECT * FROM checkouts WHERE id=? AND project=?',(checkout_id,p)).fetchone()
        if not checkout:raise ValueError('找不到原始支付会话')
        session=storefront.stripe_request(c,p,'checkout/sessions/'+checkout['external_id'])
        pi=session.get('payment_intent')
        if not isinstance(pi,str) or not pi.startswith('pi_'):raise ValueError('原始支付没有可核对的PaymentIntent')
        payload['payment_intent']=pi
        c.execute('UPDATE service_actions SET payload=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),row['id']));c.commit()
        return storefront.stripe_request(c,p,'refunds',{'payment_intent':pi,'amount':s.money(payload['amount']),'metadata[venture_action]':row['id']},marker)
    raise ValueError('未知服务动作')

def reconcile(c,row,payload):
    p=row['project'];kind=row['kind']
    if kind=='tracking':
        key=runtime.env(c,p,'AFTERSHIP_API_KEY')
        result=net.json_request('https://api.aftership.com/v4/trackings/'+quote(payload['carrier'],safe='')+'/'+quote(payload['tracking'],safe=''),headers={'aftership-api-key':key})
        return result.get('data',{}).get('tracking',{})
    if kind in {'reply','inquiry','lifecycle'} and row['external_id']:
        return net.json_request('https://api.resend.com/emails/'+row['external_id'],headers={'Authorization':'Bearer '+runtime.env(c,p,'RESEND_API_KEY')})
    if kind=='refund':
        import storefront
        if row['external_id']:return storefront.stripe_request(c,p,'refunds/'+row['external_id'])
        if payload.get('payment_intent'):
            result=storefront.stripe_request(c,p,'refunds?'+urlencode({'payment_intent':payload['payment_intent'],'limit':100}))
            matches=[v for v in result.get('data',[]) if v.get('metadata',{}).get('venture_action')==row['id']]
            if len(matches)==1:return matches[0]
    if kind in {'ad','social'} and row['external_id']:return graph(c,p,row['external_id']+('?fields=id,status,effective_status' if kind=='ad' and payload.get('operation')!='create_creative' else '?fields=id'),page=kind=='social')
    if kind=='ad':
        account=runtime.env(c,p,'META_AD_ACCOUNT_ID').removeprefix('act_');operation=payload.get('operation','create_campaign')
        edge={'create_campaign':'campaigns','create_adset':'adsets','create_creative':'adcreatives','create_ad':'ads'}.get(operation)
        if edge:
            result=graph(c,p,'act_'+account+'/'+edge+'?fields=id,name&limit=100')
            matches=[x for x in result.get('data',[]) if str(x.get('name','')).endswith('venture-'+row['id'])]
            if len(matches)==1:return matches[0]
    raise ValueError('服务商未返回足够操作标识，请在服务商后台核查；系统不会重复发送')

def record(c,row,payload,result):
    s.begin_write(c)
    kind=row['kind'];p=row['project'];external=result.get('id')
    if not external:raise net.UnknownResult('外部服务未返回可追溯标识；先核查，不重复执行')
    state='已确认'
    if kind=='tracking':
        status=str(result.get('tag','Pending'));old=c.execute('SELECT id FROM shipments WHERE project=? AND order_id=? AND tracking=?',(p,row['target'],payload['tracking'])).fetchone();i=old['id'] if old else s.uid()
        c.execute('INSERT INTO shipments VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(project,order_id,tracking) DO UPDATE SET status=excluded.status,details=excluded.details,updated=excluded.updated',(i,p,row['target'],payload['tracking'],payload['carrier'],str(external),status,json.dumps({'tag':status,'last_checkpoint':result.get('last_checkpoint',{}).get('message','')},ensure_ascii=False),s.now()))
        if status in {'Exception','Expired','AttemptFail'}:business.followup(c,p,'shipment:'+i+':'+status,'物流异常核查：'+payload['tracking'],s.ROLES[13])
        if status=='Delivered':c.execute("UPDATE orders SET status='已交付' WHERE id=? AND status='已发货'",(row['target'],))
    if kind=='ad' and payload.get('operation')=='activate_ad' and result.get('status') not in {None,'ACTIVE'}:state='处理中'
    if kind=='reply':
        ticket=c.execute('SELECT * FROM entities WHERE id=?',(row['target'],)).fetchone()
        if ticket and json.loads(ticket['data']).get('last_reply_external_id')!=str(external):
            value=json.loads(ticket['data']);value['status']='等待客户';value['last_reply_external_id']=str(external)
            business.mutate(c,'/api/entities',{'project':p,'kind':'ticket','id':ticket['id'],'version':ticket['version'],'value':value,'reason':'实际服务邮件已发送，等待客户反馈，不自动结单'})
    if kind=='refund':
        if result.get('status')=='succeeded':
            amount=s.money(payload['amount'])
            if result.get('amount')!=amount:raise ValueError('服务商退款金额与授权不一致')
            before=c.execute('SELECT status FROM service_actions WHERE id=?',(row['id'],)).fetchone()[0]
            if before!='已确认':
                c.execute("UPDATE orders SET refund=refund+?,status='售后处理中' WHERE id=?",(amount,row['target']))
                business.followup(c,p,'refund:'+row['id'],'实际退款回流复盘',s.ROLES[5])
        elif result.get('status') in {'failed','canceled'}:state='失败'
        else:state='处理中'
    c.execute('UPDATE service_actions SET status=?,external_id=?,result=?,error=?,updated=? WHERE id=?',(state,str(external),json.dumps({'id':external,'status':result.get('status')},ensure_ascii=False),'',s.now(),row['id']))
    s.log(c,p,'外部'+kind+'结果已核对：'+state+'；标识'+str(external));return {'summary':'外部操作状态：'+state,'external_id':external}

def preflight(c,row):
    p=row['project'];kind=row['kind']
    requirements={'tracking':['AFTERSHIP_API_KEY'],'reply':['RESEND_API_KEY','EMAIL_FROM'],'inquiry':['RESEND_API_KEY','EMAIL_FROM'],'lifecycle':['RESEND_API_KEY','EMAIL_FROM'],'social':['META_PAGE_TOKEN','META_PAGE_ID'],'ad':['META_ACCESS_TOKEN','META_AD_ACCOUNT_ID']}
    if any(not runtime.env(c,p,key) for key in requirements.get(kind,[])):raise ValueError('外部服务凭据尚未完整配置，未发送请求')
    if kind=='refund':
        import storefront
        key,secret,_=storefront.credentials(c,p)
        if not key.startswith('sk_live_'):raise ValueError('真实退款需本工作区Stripe正式凭据')
    if kind=='ad':
        account=runtime.env(c,p,'META_AD_ACCOUNT_ID').removeprefix('act_')
        if not re.fullmatch('[0-9]{3,40}',account):raise ValueError('广告账号ID无效')
        result=graph(c,p,'act_'+account+'?fields=id,currency')
        if result.get('currency')!='CNY':raise ValueError('当前预算口径为人民币，不能将其他币种广告账户当作人民币执行')

def insights(c,job,target):
    p=job['project'];runtime.check(c,job);runtime.consume(c,p,'ads',job['id'],target)
    rows=c.execute("SELECT * FROM service_actions WHERE project=? AND target=? AND kind='ad' AND status='已确认'",(p,target)).fetchall()
    ids={r['external_id'] for r in rows if json.loads(r['payload']).get('operation','create_campaign')=='create_campaign'}
    if not ids:raise ValueError('没有本项目已核对的广告活动，不能读取其他账号的对象')
    spend=0;data=[]
    for external in ids:
        value=graph(c,p,external+'/'+'insights?'+urlencode({'fields':'spend,impressions,clicks,actions,date_start,date_stop','date_preset':'maximum'}))
        for row in value.get('data',[]):
            spend+=s.money(row.get('spend','0'));data.append({k:row.get(k) for k in ['spend','impressions','clicks','actions','date_start','date_stop']})
    runtime.check(c,job)
    i=s.uid();c.execute('INSERT INTO channel_metrics VALUES(?,?,?,?,?,?)',(i,p,'Meta',target,json.dumps(data),s.now()))
    campaign=c.execute('SELECT * FROM campaigns WHERE id=?',(target,)).fetchone()
    c.execute('UPDATE campaigns SET spent=? WHERE id=?',(max(spend,campaign['spent']),target))
    if spend>=campaign['budget']:business.followup(c,p,'ad-budget:'+target,'广告费用达到上限，核对并暂停活动',s.ROLES[11])
    business.followup(c,p,'ad-feedback:'+job['id'],'读取真实广告反馈后复盘：'+campaign['name'],s.ROLES[0])
    return {'summary':'已读取广告平台反馈，平台归因与站内归因分开展示','metric_id':i,'reported_spend':spend,'unknowns':['广告平台报告不等于店铺全部销售，也不是新客获客成本']}

def run(job,payload):
    with s.connect() as c:
        runtime.check(c,job)
        if payload.get('insights'):return insights(c,job,payload['insights'])
        row=c.execute('SELECT * FROM service_actions WHERE id=? AND project=?',(payload.get('action'),job['project'])).fetchone()
        if not row:raise ValueError('外部操作不存在')
        row=dict(row);data=json.loads(row['payload'])
        if payload.get('reconcile'):
            result=reconcile(c,row,data);runtime.check(c,job);return record(c,row,data,result)
        if row['status']!='待执行':raise ValueError('外部操作已经发送，禁止重复提交')
        validate(c,row['project'],row['kind'],row['target'],row['approval'],data,action_id=row['id'])
        preflight(c,row)
        tool={'tracking':'logistics','reply':'email','inquiry':'email','lifecycle':'email','ad':'ads'}.get(row['kind'],row['kind']);runtime.consume(c,row['project'],tool,job['id'],row['target'])
        c.execute("UPDATE service_actions SET status='已发送',updated=? WHERE id=?",(s.now(),row['id']));c.commit()
        try:
            result=send(c,row,data)
            # Once sent, always preserve external result even if cancellation happened in flight.
            return record(c,row,data,result)
        except (ValueError,net.UnknownResult):
            c.execute("UPDATE service_actions SET status='结果未知',error='请求未确认，请只查询外部状态',updated=? WHERE id=?",(s.now(),row['id']));c.commit();raise

def recover(c):
    c.execute("UPDATE service_actions SET status='结果未知',error='服务在发送期间重启，先查询结果',updated=? WHERE status='已发送'",(s.now(),))

def unsubscribe(c,token,confirm=False):
    digest=hashlib.sha256(token.encode()).hexdigest();row=c.execute('SELECT * FROM consent_tokens WHERE token_hash=?',(digest,)).fetchone()
    if not row:return None
    if confirm:
        c.execute("UPDATE contacts SET status='已撤销' WHERE id=? AND project=?",(row['contact'],row['project']))
        s.log(c,row['project'],'联系人主动退订，后续许可触达停止')
        return '<!doctype html><meta charset="utf-8"><p>已停止后续营销内容。必要的订单服务不受影响。</p>'.encode()
    return ('<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>管理接收许可</title><body><h1>停止接收营销内容</h1><p>点击确认后，将停止后续营销触达。</p><form method="post" action="/unsubscribe/'+token+'"><button>确认退订</button></form></body></html>').encode()
