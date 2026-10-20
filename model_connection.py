"""Private workspace model configuration; credentials never enter application state."""
import json
from urllib.parse import urlsplit
import server as s,net

def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS model_connections(owner TEXT PRIMARY KEY,base TEXT,api_key TEXT,model TEXT,quote TEXT,verified TEXT)')

def owner(c,p):
    row=c.execute('SELECT owner FROM project_owners WHERE project=?',(p,)).fetchone()
    return row['owner'] if row else 'root'

def saved(c,account):
    row=c.execute('SELECT * FROM model_connections WHERE owner=?',(account,)).fetchone()
    return dict(row) if row else None

def public(c,account):
    row=saved(c,account)
    return {'base':row['base'],'model':row['model'],'quote':row['quote'],'key_present':bool(row['api_key']),'verified':row['verified']} if row else {'base':'','model':'','quote':'','key_present':False,'verified':''}

def base_url(value):
    if not isinstance(value,str):raise ValueError('请填写平台文档中的API Base URL')
    value=value.strip().rstrip('/');parts=urlsplit(value)
    if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.port not in (None,443):raise ValueError('API地址须为公网HTTPS标准端口，不能包含密钥、查询参数或登录信息')
    if any(part in parts.path.split('/') for part in ['console','keys','chat','completions','models']):raise ValueError('请填写API Base URL，不是控制台、密钥管理页或完整接口地址')
    net.public_url(value)
    return value

def mutate(c,path,d,account):
    if path=='/api/model/save':
        s.begin_write(c);old=saved(c,account);base=base_url(s.required(d,'base'));key=d.get('api_key','')
        if not isinstance(key,str) or len(key)>4096 or any(ch.isspace() for ch in key):raise ValueError('密钥格式无效，请勿包含空格或换行')
        if not key:
            if not old or old['base']!=base:raise ValueError('新地址需要填写对应平台的API密钥')
            key=old['api_key']
        model=d.get('model','').strip();quote=d.get('quote','')
        if len(model)>200 or any(ord(ch)<32 for ch in model):raise ValueError('模型ID无效')
        if quote!='':s.money(quote)
        verified=old['verified'] if old and (old['base'],old['api_key'],old['model'])==(base,key,model) else ''
        c.execute('INSERT INTO model_connections VALUES(?,?,?,?,?,?) ON CONFLICT(owner) DO UPDATE SET base=excluded.base,api_key=excluded.api_key,model=excluded.model,quote=excluded.quote,verified=excluded.verified',(account,base,key,model,str(quote),verified))
        return public(c,account)
    if path=='/api/model/clear':
        s.begin_write(c);c.execute('DELETE FROM model_connections WHERE owner=?',(account,));return {'ok':True}
    row=saved(c,account)
    if not row:raise ValueError('请先保存API连接')
    if path=='/api/model/list':
        result=net.json_request(row['base']+'/models',headers={'Authorization':'Bearer '+row['api_key']})
        if not isinstance(result,dict) or not isinstance(result.get('data'),list):raise ValueError('平台未返回OpenAI兼容模型列表，请核对API文档')
        models=[r['id'] for r in result['data'] if isinstance(r,dict) and isinstance(r.get('id'),str) and 0<len(r['id'])<=200]
        if not models:raise ValueError('当前密钥未返回可用模型，请核对权限和接口地址')
        return {'models':sorted(set(models))[:500],'status':'模型列表可访问；尚未验证对话生成'}
    if path=='/api/model/test':
        if not row['model']:raise ValueError('请先选择并保存模型ID')
        p=s.required(d,'project')
        import identity,runtime,engine
        if not identity.owns(c,account,p):raise ValueError('项目不存在')
        s.begin_write(c);grant=c.execute('SELECT remaining FROM grants WHERE project=?',(p,)).fetchone()
        if not grant or not grant['remaining']:raise ValueError('测试会产生一次模型调用，请先确认项目预算与调用额度')
        c.execute('UPDATE grants SET remaining=remaining-1 WHERE project=?',(p,));runtime.consume(c,p,'model','connection-test:'+s.uid());c.commit()
        result=engine.model_call({'task':{'role':'连接验证','title':'返回结构化连接测试结果，不进行任何外部操作'},'project':{'id':p},'records':[],'entities':[],'messages':[],'previous_outputs':[]})
        s.begin_write(c)
        c.execute('UPDATE model_connections SET verified=? WHERE owner=? AND base=? AND api_key=? AND model=?',(s.now(),account,row['base'],row['api_key'],row['model']))
        s.log(c,p,'模型结构化对话测试通过；已使用一次调用额度')
        return {'status':'结构化对话测试通过','model':row['model']}
    raise ValueError('模型连接操作无效')
