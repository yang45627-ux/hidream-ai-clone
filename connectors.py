"""WordPress/WooCommerce official REST adapter with approval and operation ledger."""
import os,json,base64,urllib.request,urllib.error,socket,ipaddress
from urllib.parse import urlsplit,urlencode
import server as s

class RemoteUnknown(ValueError):pass

def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS connectors(id TEXT PRIMARY KEY,project TEXT UNIQUE REFERENCES projects(id),kind TEXT,status TEXT,site TEXT,error TEXT,checked TEXT);
    CREATE TABLE IF NOT EXISTS external_actions(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),connector TEXT REFERENCES connectors(id),approval TEXT REFERENCES approvals(id),content TEXT REFERENCES entities(id),content_version INTEGER,status TEXT,external_id TEXT,url TEXT,error TEXT,created TEXT,updated TEXT,UNIQUE(approval));''')
    c.execute('CREATE TABLE IF NOT EXISTS external_imports(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),kind TEXT,external_id TEXT,data TEXT,version INTEGER,updated TEXT,UNIQUE(project,kind,external_id))')
    c.commit()

def config(c,p):
    row=c.execute('SELECT owner FROM project_owners WHERE project=?',(p,)).fetchone();owner=row['owner'] if row else 'root'
    prefix='WP_' if owner=='root' else 'WP_'+owner.upper()+'_'
    return os.environ.get(prefix+'SITE_URL','').rstrip('/'),os.environ.get(prefix+'USERNAME'),os.environ.get(prefix+'APP_PASSWORD'),prefix

def request(c,p,endpoint,data=None):
    site,user,password,_=config(c,p);parsed=urlsplit(site)
    if not (site and user and password):raise ValueError('站点凭据未配置，请在服务端环境变量设置WordPress地址、用户名和应用密码')
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in {None,443}:raise ValueError('站点必须是有效的HTTPS公网地址')
    try:
        addresses=socket.getaddrinfo(parsed.hostname,443,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):raise ValueError('不允许连接私有网络或本机地址')
    except socket.gaierror:raise ValueError('站点域名解析失败，未发送请求') from None
    if not endpoint.startswith('/wp-json/'):raise ValueError('不支持的站点接口')
    auth=base64.b64encode((user+':'+password).encode()).decode()
    req=urllib.request.Request(site+endpoint,data=json.dumps(data,ensure_ascii=False).encode() if data is not None else None,headers={'Authorization':'Basic '+auth,'Content-Type':'application/json','User-Agent':'VentureWorkspace/1'})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    try:
        with urllib.request.build_opener(NoRedirect).open(req,timeout=25) as r:result=json.loads(r.read(2000000))
        return result
    except urllib.error.HTTPError as e:raise ValueError('站点返回HTTP '+str(e.code)+'，请核查授权与官方接口') from None
    except (urllib.error.URLError,TimeoutError,OSError):raise RemoteUnknown('外部请求超时或连接中断，结果未知；请核对后查询，系统不会自动重复写入') from None
    except (ValueError,TypeError):raise RemoteUnknown('站点响应无法解析，结果未知，请核对外部状态') from None

def recover(c):
    c.execute("UPDATE external_actions SET status='结果未知',error='服务在发送期间重启；先查询，禁止自动重发',updated=? WHERE status='发送中'",(s.now(),))

def reconcile(c,row):
    connector=c.execute('SELECT status FROM connectors WHERE id=?',(row['connector'],)).fetchone()
    if not connector or connector['status']=='已撤销':raise ValueError('连接已撤销，不能查询外部资料')
    results=request(c,row['project'],'/wp-json/wp/v2/posts?'+urlencode({'slug':'venture-'+row['id'],'context':'edit','status':'any'}))
    if not isinstance(results,list):raise ValueError('发布结果查询格式无效')
    if results:
        post=results[0]
        if post.get('status')!='publish':raise ValueError('外部文章尚未正式发布，请人工核查')
        confirm(c,row,post);return {'status':'已确认','url':post.get('link','')}
    return {'status':row['status'],'message':'外部暂未查到文章；保持原状态，不重复发布，请人工核查'}

def confirm(c,row,post):
    if not post.get('id') or not post.get('link'):raise RemoteUnknown('外部缺少发布标识，需人工核查')
    c.execute("UPDATE external_actions SET status='已确认',external_id=?,url=?,error='',updated=? WHERE id=?",(str(post['id']),post['link'],s.now(),row['id']))
    s.log(c,row['project'],'已核对真实内容发布：'+post['link'])

def mutate(c,path,d):
    p=d['project'];site,user,password,prefix=config(c,p)
    if path=='/api/connector':
        i=s.uid();c.execute("INSERT INTO connectors VALUES(?,?,?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET status='未配置',site=excluded.site,error='',checked=''",(i,p,'WordPress/WooCommerce','待授权' if user and password and site else '未配置',site,'',''))
        s.log(c,p,'登记站点连接；需要真实检查，尚未标记可执行');return {'prefix':prefix,'requirements':[prefix+'SITE_URL',prefix+'USERNAME',prefix+'APP_PASSWORD']}
    connector=c.execute('SELECT * FROM connectors WHERE project=?',(p,)).fetchone()
    if not connector:raise ValueError('请先登记站点连接')
    if path=='/api/connector/revoke':
        c.execute("UPDATE connectors SET status='已撤销' WHERE project=?",(p,));c.execute("UPDATE external_actions SET status='已取消',updated=? WHERE project=? AND status='待执行'",(s.now(),p));s.log(c,p,'站点授权撤销，未执行操作已取消');return {'ok':True}
    if path=='/api/connector/check':
        if connector['status']=='已撤销':raise ValueError('站点已撤销，请先重新登记再验证权限')
        c.commit()
        try:
            result=request(c,p,'/wp-json/wp/v2/users/me?context=edit')
            if not isinstance(result,dict):raise ValueError('站点用户接口返回格式无效')
            caps=result.get('capabilities',{})
            status='可执行' if caps.get('publish_posts') else '可读取'
            updated=c.execute("UPDATE connectors SET status=?,site=?,error=?,checked=? WHERE project=? AND status!='已撤销'",(status,site,'',s.now(),p));return {'status':status if updated.rowcount else '已撤销'}
        except ValueError as e:
            c.execute("UPDATE connectors SET status='异常',error=?,checked=? WHERE project=? AND status!='已撤销'",(str(e),s.now(),p));return {'status':'异常','error':str(e)}
    if path=='/api/connector/import':
        if connector['status'] not in {'可读取','可执行','部分可用'}:raise ValueError('请先验证站点读取权限')
        page=d.get('page',1)
        if not isinstance(page,int) or not 1<=page<=1000:raise ValueError('页码必须为1–1000')
        reports=[]
        for kind in ['products','orders']:
            try:
                c.commit()
                items=request(c,p,'/wp-json/wc/v3/'+kind+'?'+urlencode({'per_page':20,'page':page}))
                if not isinstance(items,list):raise ValueError('WooCommerce接口未返回列表')
                s.begin_write(c)
                if c.execute('SELECT status FROM connectors WHERE project=?',(p,)).fetchone()[0]=='已撤销':raise ValueError('读取期间授权已撤销，后续导入停止')
                for item in items:
                    keys=['id','name','sku','status','price','stock_status'] if kind=='products' else ['id','status','currency','total','total_tax','date_paid','date_created']
                    value={key:item.get(key) for key in keys};data=json.dumps(value,ensure_ascii=False);external=str(item['id'])
                    old=c.execute('SELECT * FROM external_imports WHERE project=? AND kind=? AND external_id=?',(p,kind,external)).fetchone()
                    if old and old['data']==data:continue
                    if old:c.execute('UPDATE external_imports SET data=?,version=version+1,updated=? WHERE id=?',(data,s.now(),old['id']))
                    else:c.execute('INSERT INTO external_imports VALUES(?,?,?,?,?,1,?)',(s.uid(),p,kind,external,data,s.now()))
                    if kind=='orders':
                        import business
                        business.followup(c,p,'site-order:'+external,'核查真实站点订单（成本及税费待核对）：'+external,s.ROLES[12])
                c.commit()
                reports.append({'kind':kind,'read':len(items),'page':page,'next_page':page+1 if len(items)==20 else None})
            except ValueError as e:
                c.rollback();reports.append({'kind':kind,'error':str(e)})
        if any('error' in r for r in reports):c.execute("UPDATE connectors SET status='部分可用',error='部分WooCommerce读取未成功，请检查接口权限' WHERE project=? AND status!='已撤销'",(p,))
        s.log(c,p,'读取WooCommerce第'+str(page)+'页；外部订单金额未作为已核对净收入或利润')
        return {'reports':reports,'note':'每页最多20条；订单仅作为待核对来源，不推定支付和成本，不获取客户地址或联系方式'}
    if connector['status']!='可执行':raise ValueError('站点尚未验证可执行或已撤销授权')
    if path=='/api/action':
        s.begin_write(c)
        content=c.execute("SELECT * FROM entities WHERE id=? AND project=? AND kind='content'",(d.get('content'),p)).fetchone()
        approval=c.execute("SELECT * FROM approvals WHERE id=? AND project=? AND kind='内容发布' AND status='已批准'",(d.get('approval'),p)).fetchone()
        if not content or not approval:raise ValueError('请选择当前项目的文案及已批准的内容发布审批')
        if approval['subject']!=content['id']:raise ValueError('审批对象必须是该文案的档案ID，不能借用其他审批')
        if approval['updated']<content['updated']:raise ValueError('文案已在审批后变更，需要重新审批')
        existing=c.execute('SELECT id FROM external_actions WHERE approval=?',(approval['id'],)).fetchone()
        if existing:return {'id':existing['id'],'duplicate':True}
        i=s.uid();c.execute('INSERT INTO external_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(i,p,connector['id'],approval['id'],content['id'],content['version'],'待执行','','','',s.now(),s.now()))
        return {'id':i}
    row=c.execute('SELECT * FROM external_actions WHERE id=? AND project=?',(d.get('id'),p)).fetchone()
    if not row:raise ValueError('操作不存在')
    if path=='/api/action/reconcile':return reconcile(c,row)
    if path=='/api/action/execute':
        s.begin_write(c)
        if c.execute('SELECT status FROM connectors WHERE id=?',(connector['id'],)).fetchone()['status']!='可执行':raise ValueError('站点授权已变化，操作未发送')
        row=c.execute('SELECT * FROM external_actions WHERE id=? AND project=?',(row['id'],p)).fetchone()
        if row['status']=='已确认':return {'status':'已确认','url':row['url']}
        if row['status']!='待执行':raise ValueError('操作已发送或结果未知，请查询结果，禁止重复提交')
        content=c.execute('SELECT * FROM entities WHERE id=?',(row['content'],)).fetchone()
        if content['version']!=row['content_version']:raise ValueError('文案版本变化，操作失效，请重新审批')
        import business
        if business.flow(c,p)['status']!='进行中':raise ValueError('项目已暂停或结束')
        approval=c.execute('SELECT status FROM approvals WHERE id=?',(row['approval'],)).fetchone()
        if not approval or approval['status']!='已批准':raise ValueError('发布审批无效')
        c.execute("UPDATE external_actions SET status='发送中',updated=? WHERE id=?",(s.now(),row['id']));c.commit()
        try:
            previous=request(c,p,'/wp-json/wp/v2/posts?'+urlencode({'slug':'venture-'+row['id'],'context':'edit','status':'any'}))
            if previous:
                if not isinstance(previous,list) or previous[0].get('status')!='publish':raise RemoteUnknown('已存在同标识内容，需要人工核查')
                post=previous[0]
            else:
                value=json.loads(content['data']);post=request(c,p,'/wp-json/wp/v2/posts',{'title':value['name'],'content':value['body'],'slug':'venture-'+row['id'],'status':'publish'})
            confirm(c,row,post);return {'status':'已确认','url':post['link']}
        except ValueError as e:
            # Conservatively treat any failure after dispatch as unknown. Never auto resend.
            c.execute("UPDATE external_actions SET status='结果未知',error=?,updated=? WHERE id=?",(str(e),s.now(),row['id']));return {'status':'结果未知','error':str(e)}
    raise ValueError('不支持的连接操作')
