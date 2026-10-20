import json, os, sqlite3, uuid
import threading
import secrets
import hmac
from http.cookies import SimpleCookie
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from decimal import Decimal, InvalidOperation

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get('APP_DB', str(ROOT / '.data/app.db')))
ROLES = ['经营负责人','市场与消费者研究','选品与商品运营','采购与供应商管理','跨境合规与风险','财务与经营分析','品牌与本地化文案','电商视觉与视频设计','独立站运营与转化','SEO与内容增长','社媒、达人与联盟','广告投放','订单与跨境物流','客服与客户生命周期']

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.executescript('''CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, name TEXT, idea TEXT, budget INTEGER, facts TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), title TEXT, role TEXT, status TEXT, dependency TEXT, version INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), kind TEXT, body TEXT, created TEXT);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, project TEXT REFERENCES projects(id), action TEXT, created TEXT);''')
    c.executescript('''CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), task TEXT REFERENCES tasks(id), version INTEGER, status TEXT, output TEXT, error TEXT, created TEXT, updated TEXT);
    CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt ON attempts(task) WHERE status IN ('排队中','执行中');
    CREATE TABLE IF NOT EXISTS grants(project TEXT PRIMARY KEY REFERENCES projects(id), remaining INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, project TEXT REFERENCES projects(id), role TEXT, content TEXT, created TEXT);''')
    from operations import setup
    setup(c)
    return c

def now(): return datetime.now(timezone.utc).isoformat()
def uid(): return uuid.uuid4().hex
def access_cookie(token):
    # Cookies must be ASCII and must not contain the configured password.
    return hmac.digest(token.encode('utf-8'),b'venture-session-v1','sha256').hex()
def log(c,p,a): c.execute('INSERT INTO events(project,action,created) VALUES(?,?,?)',(p,a,now()))
def required(data,key):
    value = data.get(key)
    if not isinstance(value,str) or not value.strip(): raise ValueError(f'请填写 {key}')
    if len(value)>10000: raise ValueError('文本过长')
    return value.strip()
def money(value):
    try:
        x=Decimal(str(value))
        if not x.is_finite() or x<0 or x>Decimal('1000000000') or x.as_tuple().exponent < -2: raise ValueError('金额须为非负数，最多两位小数')
        return int(x*100)
    except (InvalidOperation,TypeError): raise ValueError('金额无效')

def snapshot(c):
    from engine import model_status
    return {k:[dict(r) for r in c.execute('SELECT * FROM '+k)] for k in ['projects','tasks','records','events','attempts','grants','messages','campaigns','orders','experiments']} | {'roles':ROLES,'ephemeral':os.environ.get('APP_EPHEMERAL')=='true','mode':'单工作区开发版','model':model_status()}

def invalidate(c, project, reason):
    c.execute("UPDATE tasks SET status=CASE WHEN dependency IS NULL THEN '需返工' ELSE '等待依赖' END,version=version+1 WHERE project=?",(project,))
    c.execute("UPDATE attempts SET status='已取消',error=?,updated=? WHERE project=? AND status IN ('排队中','执行中')",(reason,now(),project))
    log(c,project,reason+'；关联任务需重新验证，历史成果保留')

def effective_records(c,project):
    rows=[dict(r) for r in c.execute('SELECT * FROM records WHERE project=? ORDER BY created',(project,))]
    superseded={json.loads(r['body']).get('supersedes') for r in rows}
    return [r for r in rows if r['id'] not in superseded]

def acceptance(c,task):
    records=effective_records(c,task['project'])
    kinds={r['kind'] for r in records}
    if task['role']==ROLES[1] and not any(r['kind']=='机会证据' and json.loads(r['body']).get('source') for r in records): raise ValueError('缺少可追溯的机会证据，请先录入真实来源')
    if task['role']==ROLES[2] and '商品与供应' not in kinds: raise ValueError('缺少商品与供应核查记录')
    if task['role']==ROLES[3] and '样品检查' not in kinds: raise ValueError('必须提交实物样品人工检查记录')
    if task['role']==ROLES[5]:
        costs=[r for r in records if r['kind']=='成本测算']
        if not costs or not json.loads(costs[-1]['body']).get('result'): raise ValueError('最新成本测算不完整，不能通过商品决策')
        if '风险' not in kinds: raise ValueError('缺少风险核查记录')

def mutate(c,path,d):
    if path=='/api/projects':
        p=uid(); name=required(d,'name'); idea=required(d,'idea'); budget=money(d.get('budget'))
        facts={k:d.get(k,'待确定') for k in ['market','language','time','resources','excluded','loss_limit']}
        c.execute('INSERT INTO projects VALUES(?,?,?,?,?,?)',(p,name,idea,budget,json.dumps(facts,ensure_ascii=False),now()))
        previous=None
        for title,role in [('确认经营条件与阶段目标',ROLES[0]),('收集需求证据与候选机会',ROLES[1]),('核实商品与供应条件',ROLES[2]),('提交实物样品检查',ROLES[3]),('成本、风险与商品决策',ROLES[5])]:
            t=uid(); c.execute('INSERT INTO tasks(id,project,title,role,status,dependency) VALUES(?,?,?,?,?,?)',(t,p,title,role,'待处理' if previous is None else '等待依赖',previous)); previous=t
        log(c,p,'项目启动：已创建阶段任务；尚未开展研究或外部操作'); return {'id':p}
    p=required(d,'project')
    if not c.execute('SELECT 1 FROM projects WHERE id=?',(p,)).fetchone(): raise ValueError('项目不存在')
    if path in {'/api/campaigns','/api/campaign/update','/api/orders','/api/order/update','/api/experiments','/api/experiment/update'}:
        from operations import mutate as operate
        return operate(c,path,d)
    if path=='/api/project/update':
        name=required(d,'name'); idea=required(d,'idea'); budget=money(d.get('budget'))
        reserved=c.execute('SELECT COALESCE(SUM(budget),0) FROM campaigns WHERE project=?',(p,)).fetchone()[0]
        if budget<reserved: raise ValueError('项目预算不能低于已分配活动预算；请先核查已有营销计划')
        facts={k:d.get(k,'待确定') for k in ['market','language','time','resources','excluded','loss_limit']}
        c.execute('UPDATE projects SET name=?,idea=?,budget=?,facts=? WHERE id=?',(name,idea,budget,json.dumps(facts,ensure_ascii=False),p))
        invalidate(c,p,'用户更正经营条件'); return {'ok':True}
    if path=='/api/messages':
        content=required(d,'content')
        c.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(uid(),p,'user',content,now()))
        log(c,p,'用户补充项目资料'); return {'ok':True}
    if path=='/api/grants':
        calls=d.get('calls')
        if not isinstance(calls,int) or isinstance(calls,bool) or not 0<=calls<=20: raise ValueError('调用授权必须为 0–20 次；0 表示撤销')
        c.execute('INSERT INTO grants VALUES(?,?) ON CONFLICT(project) DO UPDATE SET remaining=excluded.remaining',(p,calls))
        if calls==0:
            active=c.execute("SELECT task FROM attempts WHERE project=? AND status IN ('排队中','执行中')",(p,)).fetchall()
            c.execute("UPDATE attempts SET status='已取消',error='用户撤销授权',updated=? WHERE project=? AND status IN ('排队中','执行中')",(now(),p))
            for row in active: c.execute("UPDATE tasks SET status='暂停',version=version+1 WHERE id=?",(row['task'],))
        log(c,p,'模型调用授权更新为 '+str(calls)+' 次；不含外部发布、采购或付款权限'); return {'ok':True}
    if path=='/api/run':
        from engine import enqueue
        return enqueue(c,p,d.get('id'))
    if path=='/api/tasks':
        task=c.execute('SELECT * FROM tasks WHERE id=? AND project=?',(d.get('id'),p)).fetchone()
        if not task: raise ValueError('任务不存在')
        if c.execute("SELECT 1 FROM attempts WHERE task=? AND status IN ('排队中','执行中')",(task['id'],)).fetchone():
            if d.get('status') not in {'暂停','等待人工'}: raise ValueError('任务正在执行；请先暂停或人工接管')
            c.execute("UPDATE attempts SET status='已取消',error='人工接管',updated=? WHERE task=? AND status IN ('排队中','执行中')",(now(),task['id']))
        status=d.get('status'); allowed={'待处理','进行中','等待人工','暂停','待验收','已完成','需返工'}
        if status not in allowed: raise ValueError('状态无效')
        if task['dependency'] and status not in {'暂停','等待人工'}:
            dep=c.execute('SELECT status FROM tasks WHERE id=?',(task['dependency'],)).fetchone()
            if dep['status']!='已完成': raise ValueError('前置任务尚未验收通过')
        note=required(d,'note')
        if status=='已完成' and task['status']!='待验收': raise ValueError('请先提交成果进入待验收，再确认通过')
        if status=='已完成': acceptance(c,task)
        if task['status']=='已完成' and status!='已完成':
            queue=[task['id']]
            while queue:
                children=c.execute('SELECT id FROM tasks WHERE dependency=?', (queue.pop(),)).fetchall()
                for child in children:
                    c.execute("UPDATE tasks SET status='等待依赖',version=version+1 WHERE id=?",(child['id'],)); queue.append(child['id'])
        c.execute('UPDATE tasks SET status=?,version=version+1 WHERE id=?',(status,task['id']))
        if status=='已完成': c.execute("UPDATE tasks SET status='待处理' WHERE dependency=? AND status='等待依赖'",(task['id'],))
        log(c,p,task['title']+' → '+status+'：'+note); return {'ok':True}
    if path=='/api/records':
        kind=d.get('kind')
        if kind not in ['机会证据','商品与供应','样品检查','风险','成本测算']: raise ValueError('记录类型无效')
        body={'content':required(d,'content'),'source':d.get('source',''),'classification':d.get('classification','待核实')}
        if body['classification'] not in ['事实','假设','待核实']: raise ValueError('事实分类无效')
        if kind=='机会证据' and not body['source']: raise ValueError('机会证据必须填写来源与采集时间')
        if kind=='成本测算':
            keys=['revenue','discount','refund','pass_through','purchase','shipping','fees','tax','other','marketing']
            amounts={k:money(d[k]) if d.get(k) not in (None,'') else None for k in keys}
            body['amounts']=amounts; body['missing']=[k for k,v in amounts.items() if v is None]
            if body['missing']: body['result']=None
            else:
                net=amounts['revenue']-sum(amounts[k] for k in ['discount','refund','pass_through'])
                pre=net-sum(amounts[k] for k in ['purchase','shipping','fees','tax','other'])
                body['result']={'net':net,'pre_marketing':pre,'post_marketing':pre-amounts['marketing']}
        c.execute('INSERT INTO records VALUES(?,?,?,?,?)',(uid(),p,kind,json.dumps(body,ensure_ascii=False),now())); log(c,p,'新增'+kind+'记录'); return body
    if path=='/api/record/correct':
        old=c.execute('SELECT * FROM records WHERE id=? AND project=?',(d.get('id'),p)).fetchone()
        if not old: raise ValueError('资料不存在')
        if old['kind']=='成本测算': raise ValueError('成本更正请新增一份完整测算，保留原始核算记录')
        body={'content':required(d,'content'),'source':required(d,'source'),'classification':'待核实','supersedes':old['id']}
        c.execute('INSERT INTO records VALUES(?,?,?,?,?)',(uid(),p,old['kind'],json.dumps(body,ensure_ascii=False),now()))
        invalidate(c,p,'商品或研究资料被更正（原始记录保留）');return {'ok':True}
    raise ValueError('未知操作')

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):
        # Request paths and cookies can contain private access information.
        pass
    def send(self,status,body,kind='application/json; charset=utf-8',headers=None):
        b=json.dumps(body,ensure_ascii=False).encode() if kind.startswith('application/json') else body
        self.send_response(status); self.send_header('Content-Type',kind); self.send_header('Content-Length',str(len(b))); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Cache-Control','no-store'); self.send_header('Referrer-Policy','no-referrer')
        for key,value in (headers or {}).items(): self.send_header(key,value)
        self.end_headers(); self.wfile.write(b)
    def authorized(self):
        token=os.environ.get('APP_ACCESS_TOKEN')
        if not token:return True
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except Exception:return False
        value=cookie.get('venture_access')
        return bool(value and secrets.compare_digest(value.value.encode('utf-8'),access_cookie(token).encode('ascii')))
    def same_origin(self):
        origin=self.headers.get('Origin')
        public=os.environ.get('APP_PUBLIC_ORIGIN') or os.environ.get('RENDER_EXTERNAL_URL')
        expected=public.rstrip('/') if public else 'http://'+self.headers.get('Host','')
        return not origin or origin==expected
    def do_GET(self):
        if self.path=='/api/health':return self.send(200,{'status':'ok'})
        if not self.authorized():
            if self.path=='/' or self.path=='/login':return self.send(200,(ROOT/'web/login.html').read_bytes(),'text/html; charset=utf-8')
            return self.send(401,{'error':'请先使用访问密码登录'})
        if self.path=='/api/state':
            with connect() as c: return self.send(200,snapshot(c))
        if self.path.startswith('/api/export/'):
            p=self.path.removeprefix('/api/export/')
            with connect() as c:
                state=snapshot(c)
                if not any(x['id']==p for x in state['projects']): return self.send(404,{'error':'项目不存在'})
                return self.send(200,{k:[r for r in state[k] if r.get('project',r.get('id'))==p] for k in ['projects','tasks','records','events','attempts','messages','campaigns','orders','experiments']})
        files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
        if self.path not in files: return self.send(404,{'error':'不存在'})
        f=ROOT/'web'/files[self.path]; self.send(200,f.read_bytes(),{'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[f.suffix[1:]])
    def do_POST(self):
        if not self.same_origin(): return self.send(403,{'error':'不允许跨站操作'})
        if self.path!='/api/login' and not self.authorized():return self.send(401,{'error':'请先登录'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=100000: raise ValueError('请求大小无效')
            d=json.loads(self.rfile.read(length))
            if not isinstance(d,dict): raise ValueError('请求必须是对象')
            if self.path=='/api/login':
                token=os.environ.get('APP_ACCESS_TOKEN')
                password=d.get('password','')
                if not token or not isinstance(password,str) or not secrets.compare_digest(password.encode('utf-8'),token.encode('utf-8')):return self.send(401,{'error':'访问密码不正确'})
                public=os.environ.get('APP_PUBLIC_ORIGIN') or os.environ.get('RENDER_EXTERNAL_URL','')
                secure='; Secure' if public.startswith('https://') else ''
                return self.send(200,{'ok':True},headers={'Set-Cookie':'venture_access='+access_cookie(token)+'; HttpOnly; SameSite=Lax; Path=/; Max-Age=86400'+secure})
            if self.path=='/api/logout':return self.send(200,{'ok':True},headers={'Set-Cookie':'venture_access=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'})
            with connect() as c: result=mutate(c,self.path,d)
            self.send(200,result)
        except (ValueError,KeyError,TypeError) as e: self.send(400,{'error':str(e)})

if __name__=='__main__':
    # Share this module instance with the worker when invoked as a script.
    import sys
    sys.modules['server']=sys.modules[__name__]
    host=os.environ.get('APP_HOST','127.0.0.1')
    if host not in {'127.0.0.1','localhost','::1'} and len(os.environ.get('APP_ACCESS_TOKEN',''))<24:
        raise SystemExit('公开监听需要至少 24 位随机 APP_ACCESS_TOKEN；服务未启动')
    connect().close()
    from engine import recover, worker
    recover()
    threading.Thread(target=worker,daemon=True).start()
    ThreadingHTTPServer((host,int(os.environ.get('PORT','3000'))),Handler).serve_forever()
