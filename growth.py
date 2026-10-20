"""Approved native SEO publishing, consented first-touch attribution and experiments."""
import json,re,hashlib,html,datetime
from urllib.parse import urlencode
import server as s,business

def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS publications(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),store TEXT REFERENCES stores(id),content_id TEXT,content_version INTEGER,title TEXT,body TEXT,status TEXT,created TEXT,updated TEXT);
    CREATE TABLE IF NOT EXISTS article_routes(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),store TEXT,content_id TEXT,current_publication TEXT,status TEXT,updated TEXT,UNIQUE(project,content_id));
    CREATE TABLE IF NOT EXISTS visits(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),store TEXT,session TEXT,event TEXT,product TEXT,campaign TEXT,source TEXT,medium TEXT,is_test INTEGER,created TEXT,UNIQUE(store,id));
    CREATE TABLE IF NOT EXISTS checkout_attribution(checkout TEXT PRIMARY KEY REFERENCES checkouts(id),project TEXT REFERENCES projects(id),session TEXT,campaign TEXT,source TEXT,medium TEXT);
    CREATE TABLE IF NOT EXISTS purchase_attribution(order_id TEXT PRIMARY KEY REFERENCES orders(id),project TEXT REFERENCES projects(id),checkout TEXT,session TEXT,campaign TEXT,source TEXT,medium TEXT,is_test INTEGER,created TEXT);''')

def mutate(c,path,d):
    p=d['project'];s.begin_write(c)
    if path=='/api/growth/prepare':
        content=c.execute("SELECT * FROM entities WHERE id=? AND project=? AND kind='content'",(d.get('content'),p)).fetchone()
        if not content:raise ValueError('文案不存在')
        value=json.loads(content['data'])
        return business.mutate(c,'/api/approvals',{'project':p,'kind':'内容发布','subject':content['id'],'amount':'0','reason':'核查这份文章的事实、承诺和来源后发布：'+value['name']+'\n'+value['body']})
    if path=='/api/growth/publish':
        content=c.execute("SELECT * FROM entities WHERE id=? AND project=? AND kind='content'",(d.get('content'),p)).fetchone()
        approval=c.execute("SELECT * FROM approvals WHERE id=? AND project=? AND kind='内容发布' AND status='已批准'",(d.get('approval'),p)).fetchone()
        store=c.execute("SELECT * FROM stores WHERE project=? AND status='已发布'",(p,)).fetchone()
        if not store or not content or not approval:raise ValueError('需要已发布商店、文案档案与对应内容发布审批')
        if approval['subject']!=content['id'] or approval['updated']<content['updated']:raise ValueError('审批须对应当前文案版本')
        value=json.loads(content['data']);old=c.execute('SELECT * FROM publications WHERE content_id=? AND content_version=?',(content['id'],content['version'])).fetchone()
        route=c.execute('SELECT * FROM article_routes WHERE project=? AND content_id=?',(p,content['id'])).fetchone()
        if old:
            if old['status']!='已发布' and approval['updated']<old['updated']:raise ValueError('已撤回或失效的文章需要重新审批后发布')
            i=old['id'];c.execute("UPDATE publications SET status='已发布',updated=? WHERE id=?",(s.now(),i))
        else:
            i=s.uid();c.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?)',(i,p,store['id'],content['id'],content['version'],value['name'],value['body'],'已发布',s.now(),s.now()))
        permalink=route['id'] if route else i
        c.execute("UPDATE publications SET status='已替代',updated=? WHERE content_id=? AND id!=? AND status='已发布'",(s.now(),content['id'],i))
        c.execute("INSERT INTO article_routes VALUES(?,?,?,?,?,?,?) ON CONFLICT(project,content_id) DO UPDATE SET current_publication=excluded.current_publication,status=excluded.status,updated=excluded.updated",(permalink,p,store['id'],content['id'],i,'已发布',s.now()))
        s.log(c,p,'站内SEO文章已发布/更新，保留永久链接与版本：'+permalink+'；收录和效果待观察')
        return {'id':i,'url':'/shop/'+store['id']+'/articles/'+permalink,'duplicate':bool(old)}
    if path=='/api/growth/unpublish':
        c.execute("UPDATE publications SET status='已撤回',updated=? WHERE id=? AND project=?",(s.now(),d.get('id'),p));c.execute("UPDATE article_routes SET status='已撤回',updated=? WHERE current_publication=? AND project=?",(s.now(),d.get('id'),p));return {'ok':True}
    if path=='/api/growth/experiment/check':
        exp=c.execute('SELECT * FROM experiments WHERE id=? AND project=?',(d.get('id'),p)).fetchone()
        if not exp:raise ValueError('实验不存在')
        minimum=d.get('minimum_sessions',100)
        if not isinstance(minimum,int) or not 10<=minimum<=1000000:raise ValueError('观察样本须为10–1000000')
        report=metrics(c,p)
        if report['sessions']<minimum:
            c.execute("UPDATE experiments SET status='数据不足',result=? WHERE id=?",('观察窗口内同意追踪的非测试会话 '+str(report['sessions'])+'，少于预设 '+str(minimum),exp['id']))
            return {'ready':False,'metrics':report}
        task=business.followup(c,p,'observation:'+exp['id'],'观察条件达到，核查实验并决策：'+exp['hypothesis'],s.ROLES[0]);return {'ready':True,'task':task,'metrics':report}
    raise ValueError('未知增长操作')

def clean_tag(value):
    value=str(value or '')
    return value if re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',value) else ''

def attribution(c,store,d):
    if not isinstance(d,dict):raise ValueError('访问归因格式无效')
    if d.get('consent') is not True:return {'session':'','campaign':'','source':'未归因','medium':''}
    session=d.get('session','')
    if not isinstance(session,str) or not re.fullmatch('[a-f0-9-]{16,64}',session):raise ValueError('匿名会话标识无效')
    session=hashlib.sha256((store['id']+session).encode()).hexdigest()
    first=c.execute('SELECT * FROM visits WHERE store=? AND session=? ORDER BY created LIMIT 1',(store['id'],session)).fetchone()
    if first:return {k:first[k] for k in ['session','campaign','source','medium']}
    campaign=clean_tag(d.get('campaign'))
    if campaign and not c.execute('SELECT 1 FROM campaigns WHERE id=? AND project=?',(campaign,store['project'])).fetchone():campaign=''
    return {'session':session,'campaign':campaign,'source':clean_tag(d.get('source')) or '未归因','medium':clean_tag(d.get('medium'))}

def track(c,store_id,d):
    store=c.execute("SELECT * FROM stores WHERE id=? AND status='已发布'",(store_id,)).fetchone()
    if not store:raise ValueError('商店未发布')
    if d.get('consent') is not True:return {'ignored':True}
    name=d.get('event');key=d.get('id')
    if name not in {'visit','view_product','add_to_cart','begin_checkout'}:raise ValueError('事件类型无效，付款事件只能由验签回调写入')
    if not isinstance(key,str) or not re.fullmatch('[a-f0-9-]{16,64}',key):raise ValueError('事件标识无效')
    product=d.get('product','')
    if product and not c.execute("SELECT 1 FROM entities WHERE id=? AND project=? AND kind='product'",(product,store['project'])).fetchone():raise ValueError('商品不属于本商店')
    a=attribution(c,store,d)
    # Bound spoofed event floods per anonymous session; this is not anti-fraud or platform attribution.
    if c.execute('SELECT COUNT(*) FROM visits WHERE store=? AND session=?',(store_id,a['session'])).fetchone()[0]>=200:return {'ignored':True}
    c.execute('INSERT INTO visits VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',(store_id+':'+key,store['project'],store_id,a['session'],name,product,a['campaign'],a['source'],a['medium'],int(store['mode']!='正式'),s.now()))
    return {'ok':True}

def bind_checkout(c,store_id,checkout,d):
    store=c.execute('SELECT * FROM stores WHERE id=?',(store_id,)).fetchone();a=attribution(c,store,d)
    c.execute('INSERT INTO checkout_attribution VALUES(?,?,?,?,?,?) ON CONFLICT(checkout) DO NOTHING',(checkout,store['project'],a['session'],a['campaign'],a['source'],a['medium']))

def purchase(c,checkout,order,is_test):
    a=c.execute('SELECT * FROM checkout_attribution WHERE checkout=?',(checkout['id'],)).fetchone()
    fields=[a[k] if a else '' for k in ['session','campaign','source','medium']]
    if not fields[2]:fields[2]='未归因'
    c.execute('INSERT INTO purchase_attribution VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(order_id) DO NOTHING',(order,checkout['project'],checkout['id'],*fields,int(is_test),s.now()))

def metrics(c,p):
    start=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=30)).isoformat()
    events=c.execute('SELECT * FROM visits WHERE project=? AND is_test=0 AND created>=?',(p,start)).fetchall()
    sessions={r['session'] for r in events if r['event']=='visit'}
    orders=[dict(r) for r in c.execute("SELECT o.id AS order_id,a.session,a.campaign,a.source,a.medium FROM orders o LEFT JOIN purchase_attribution a ON a.order_id=o.id WHERE o.project=? AND o.created>=? AND o.status NOT IN ('待核查','已取消') AND o.external_id NOT LIKE 'TEST-%'",(p,start))]
    for order in orders:
        order['source']=order['source'] or '未归因'
    matched={o['session'] for o in orders if o['session'] in sessions}
    campaigns=[]
    for row in c.execute('SELECT * FROM campaigns WHERE project=?',(p,)):
        campaign_orders=[o for o in orders if o['campaign']==row['id']]
        campaigns.append({'id':row['id'],'name':row['name'],'sessions':len({e['session'] for e in events if e['campaign']==row['id'] and e['event']=='visit'}),'paid_orders':len(campaign_orders),'spent':row['spent'],'cost_per_order':row['spent']//len(campaign_orders) if campaign_orders else None,'new_customer_cac':None,'basis':'站内首次触点，不等于广告平台归因或新客获客成本'})
    return {'window':'最近30天','sessions':len(sessions),'product_sessions':len({e['session'] for e in events if e['event']=='view_product'}),'cart_sessions':len({e['session'] for e in events if e['event']=='add_to_cart'}),'checkout_sessions':len({e['session'] for e in events if e['event']=='begin_checkout'}),'paid_orders':len(orders),'unattributed_orders':sum(o['source']=='未归因' for o in orders),'consented_session_conversion':len(matched)/len(sessions) if sessions else None,'campaigns':campaigns,'basis':'仅同意匿名追踪的非测试会话；过滤已识别机器人。未同意或无触点订单保留未归因；不能代表全站转化率、全部有效访问或新客数。'}

def article(c,store_id,publication):
    route=c.execute("SELECT * FROM article_routes WHERE store=? AND (id=? OR current_publication=?) AND status='已发布'",(store_id,publication,publication)).fetchone()
    if not route:return None
    publication=route['current_publication']
    row=c.execute("SELECT p.*,s.mode AS store_mode FROM publications p JOIN stores s ON s.id=p.store WHERE p.id=? AND p.store=? AND p.status='已发布' AND s.status='已发布'",(publication,store_id)).fetchone()
    if not row:return None
    # Never execute generated HTML. Plain content is escaped on the public origin.
    origin=(__import__('os').environ.get('APP_PUBLIC_ORIGIN') or __import__('os').environ.get('RENDER_EXTERNAL_URL') or '').rstrip('/')
    import media
    content=c.execute('SELECT data FROM entities WHERE id=?',(row['content_id'],)).fetchone();product=json.loads(content['data']).get('product_id') if content else None
    asset=c.execute("SELECT id FROM assets WHERE product=? AND project=? AND role='主图' AND status='已核查' ORDER BY created LIMIT 1",(product,row['project'])).fetchone() if product else None
    image_tag=('<meta property="og:image" content="'+html.escape(origin+'/shop/'+store_id+'/asset/'+asset['id'],quote=True)+'">') if asset and media.public_asset(c,store_id,asset['id']) else ''
    language=json.loads(c.execute('SELECT facts FROM projects WHERE id=?',(row['project'],)).fetchone()[0]).get('language','');language='en' if language.lower() in {'en','english','英语'} else 'zh'
    robots='<meta name="robots" content="noindex,nofollow">' if row['store_mode']!='正式' else ''
    title=html.escape(row['title']);body=html.escape(row['body']).replace('\n','<br>');canonical=html.escape(origin+'/shop/'+store_id+'/articles/'+route['id'],quote=True)
    return ('<!doctype html><html lang="'+language+'"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+title+'</title>'+robots+image_tag+'<meta property="og:title" content="'+title+'"><meta name="description" content="'+html.escape(row['body'][:150],quote=True)+'"><link rel="canonical" href="'+canonical+'"></head><body style="max-width:760px;margin:40px auto;padding:20px;font:18px/1.8 system-ui"><a href="/shop/'+store_id+'">返回商店</a><article><h1>'+title+'</h1><p>'+body+'</p></article></body></html>').encode()
