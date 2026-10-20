"""Official search adapters, retrieved evidence and source-bound opportunity drafts."""
import json,hashlib,re,html
from html.parser import HTMLParser
from urllib.parse import urlencode,urlsplit
import server as s,runtime,net

class Text(HTMLParser):
    def __init__(self):super().__init__();self.parts=[];self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in {'script','style','nav','footer'}:self.skip+=1
    def handle_endtag(self,tag):
        if tag in {'script','style','nav','footer'}:self.skip=max(0,self.skip-1)
    def handle_data(self,data):
        if not self.skip:self.parts.append(data)

def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),url TEXT,title TEXT,excerpt TEXT,content_hash TEXT,collected TEXT,source_type TEXT,status TEXT,UNIQUE(project,url,content_hash));''')

def status(c,p):
    return {'provider':'Brave Search' if runtime.env(c,p,'SEARCH_API_KEY') else 'Wikimedia公开百科（有限资料，不代表市场需求）','configured':True,'market_search':bool(runtime.env(c,p,'SEARCH_API_KEY')),'cost_quote':runtime.env(c,p,'SEARCH_CALL_CNY')}

def search(c,p,query):
    key=runtime.env(c,p,'SEARCH_API_KEY')
    if key:
        result=net.json_request('https://api.search.brave.com/res/v1/web/search?'+urlencode({'q':query,'count':5}),headers={'X-Subscription-Token':key})
        return [{'url':x['url'],'title':x.get('title',''),'excerpt':html.unescape(re.sub('<[^>]+>','',x.get('description',''))),'type':'搜索摘要，需交叉核查'} for x in result.get('web',{}).get('results',[]) if str(x.get('url','')).startswith('https://')][:5]
    result=net.json_request('https://en.wikipedia.org/w/api.php?'+urlencode({'action':'query','list':'search','srsearch':query,'srlimit':5,'format':'json'}))
    return [{'url':'https://en.wikipedia.org/?curid='+str(x['pageid']),'title':x['title'],'excerpt':html.unescape(re.sub('<[^>]+>','',x.get('snippet',''))),'type':'百科背景，不是需求或成交证据'} for x in result.get('query',{}).get('search',[])][:5]

def save_source(c,p,item,body=None):
    url=item['url'];net.public_url(url)
    excerpt=(body or item.get('excerpt',''))[:16000]
    if not excerpt.strip():raise ValueError('来源没有可用正文或摘要')
    digest=hashlib.sha256(excerpt.encode()).hexdigest()
    old=c.execute('SELECT id FROM sources WHERE project=? AND url=? AND content_hash=?',(p,url,digest)).fetchone()
    if old:return old['id']
    i=s.uid();c.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?)',(i,p,url,str(item.get('title',url))[:500],excerpt,digest,s.now(),item.get('type','公开页面，待交叉核查'),'待核查'))
    c.execute('INSERT INTO records VALUES(?,?,?,?,?)',(i,p,'机会证据',json.dumps({'content':excerpt,'source':url+'；采集 '+s.now(),'classification':'待核实','source_id':i},ensure_ascii=False),s.now()))
    return i

def import_url(c,p,url,job):
    runtime.check(c,job);runtime.consume(c,p,'read',job['id'])
    raw,headers=net.request(url,limit=1000000)
    content_type=next((v for k,v in headers.items() if k.lower()=='content-type'),'')
    if not any(t in content_type for t in ['text/html','text/plain']):raise ValueError('研究仅提取HTML或UTF-8文本，不执行页面脚本')
    text=raw.decode('utf-8','replace');parser=Text();parser.feed(text)
    cleaned=' '.join(' '.join(parser.parts).split())[:16000]
    return save_source(c,p,{'url':url,'title':url,'type':'网页正文，待交叉核查'},cleaned)

def run(job,payload):
    p=job['project']
    with s.connect() as c:
        runtime.check(c,job);project=dict(c.execute('SELECT * FROM projects WHERE id=?',(p,)).fetchone());query=payload.get('query') or project['idea']
        if not isinstance(query,str) or len(query)>500:raise ValueError('研究问题须在500字符内')
        ids=[];warnings=[]
        if payload.get('analysis_only'):
            ids=[r['id'] for r in c.execute("SELECT id FROM records WHERE project=? AND kind IN ('机会证据','导入文档') ORDER BY created DESC LIMIT 12",(p,))]
        else:
            runtime.consume(c,p,'search',job['id']);items=search(c,p,query)
            if not items:raise ValueError('未找到可用公开来源，请调整问题，不生成无来源机会')
            for item in items:
                runtime.check(c,job)
                try:ids.append(save_source(c,p,item))
                except ValueError:warnings.append('已跳过不可安全读取的来源')
            c.commit()
            to_read=list(dict.fromkeys(payload.get('urls',[])+[x['url'] for x in items[:2]]))[:3]
            for url in to_read:
                try:ids.append(import_url(c,p,url,job));c.commit()
                except ValueError as e:warnings.append(str(e));c.rollback()
            if not status(c,p)['market_search']:warnings.append('当前仅有百科背景资料，市场需求与竞争仍待验证')
        records=[dict(r) for r in c.execute('SELECT * FROM records WHERE project=? AND id IN ('+','.join('?' for _ in ids)+')',(p,*ids))] if ids else []
        if not records:raise ValueError('没有可用证据，不生成方向')
        import engine
        if not engine.model_status(p,c)['configured']:
            return {'summary':'已收集公开来源，模型未配置，尚未生成候选方向','source_ids':ids,'candidates':[],'unknowns':['公开摘要不是需求验证，需接入模型分析和核查',*warnings]}
        s.begin_write(c);grant=c.execute('SELECT remaining FROM grants WHERE project=?',(p,)).fetchone()
        if not grant or grant['remaining']<1:raise ValueError('来源已保存；分析候选方向需要一次模型调用授权')
        c.execute('UPDATE grants SET remaining=remaining-1 WHERE project=?',(p,));runtime.consume(c,p,'model',job['id']);c.commit()
    result=engine.model_call({'task':{'role':s.ROLES[1],'title':'比较少量值得验证的候选机会'},'task_spec':{'objective':'只根据所给来源生成最多三个假设方向，不能把百科或搜索摘要称为已验证市场需求；无适合方向可以返回空数组。除基础结果字段外，返回 opportunities 数组。每项必须含 name,audience,problem,competition,supply_hypothesis,risk,validation,evidence_ids(输入来源ID数组)，所有假设标记待验证。'},'project':project,'records':records,'entities':[],'messages':[],'previous_outputs':[]})
    candidates=result.get('opportunities',[])
    if not isinstance(candidates,list) or len(candidates)>3:raise ValueError('研究候选方向格式无效或超过三个')
    from business import mutate
    saved=[]
    with s.connect() as c:
        s.begin_write(c);runtime.check(c,job)
        for value in candidates:
            keys=['name','audience','problem','competition','supply_hypothesis','risk','validation']
            if not isinstance(value,dict) or any(not isinstance(value.get(k),str) or not value[k].strip() for k in keys):raise ValueError('候选方向缺少完整研究说明')
            refs=value.get('evidence_ids')
            if not isinstance(refs,list) or not refs or not set(refs).issubset(set(ids)):raise ValueError('机会方向缺少本次实际采集的来源引用')
            if len(value['name'])>200:raise ValueError('方向名称过长')
            value={k:value[k][:4000] for k in keys}|{'source':'；'.join(json.loads(r['body'])['source'] for r in records if r['id'] in refs),'evidence_ids':','.join(refs),'decision':'待确认','classification':'假设，尚未验证市场需求'}
            saved.append(mutate(c,'/api/entities',{'project':p,'kind':'opportunity','value':value})['id'])
        s.log(c,p,'研究来源已保存，候选方向待用户选择；不等于市场或供应验证')
    return {'summary':result['summary'],'source_ids':ids,'candidates':saved,'unknowns':result['unknowns']+warnings}
