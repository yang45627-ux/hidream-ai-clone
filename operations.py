"""Manual operating records. These APIs never publish or move money."""
import json
import server as s

TABLES=['campaigns','orders','experiments']
def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),name TEXT,audience TEXT,channel TEXT,landing TEXT,hypothesis TEXT,stop_rule TEXT,budget INTEGER,spent INTEGER DEFAULT 0,status TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),external_id TEXT,revenue INTEGER,cost INTEGER,refund INTEGER DEFAULT 0,status TEXT,tracking TEXT,created TEXT,UNIQUE(project,external_id));
    CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),hypothesis TEXT,change_text TEXT,observation TEXT,result TEXT,decision TEXT,status TEXT,created TEXT);''')

def mutate(c,path,d):
    p=d['project']
    if path=='/api/campaigns':
        c.execute('BEGIN IMMEDIATE')
        name=s.required(d,'name'); audience=s.required(d,'audience'); channel=s.required(d,'channel'); landing=s.required(d,'landing'); hypothesis=s.required(d,'hypothesis'); stop=s.required(d,'stop_rule'); budget=s.money(d.get('budget'))
        total=c.execute('SELECT COALESCE(SUM(budget),0) FROM campaigns WHERE project=?',(p,)).fetchone()[0]
        cap=c.execute('SELECT budget FROM projects WHERE id=?',(p,)).fetchone()[0]
        if total+budget>cap: raise ValueError('活动预算总额超过项目验证预算，请先调整项目预算或活动范围')
        i=s.uid(); c.execute('INSERT INTO campaigns VALUES(?,?,?,?,?,?,?,?,?,0,?,?)',(i,p,name,audience,channel,landing,hypothesis,stop,budget,'草稿',s.now())); s.log(c,p,'新建营销测试草稿：'+name+'；尚未发布或支出'); return {'id':i}
    if path=='/api/campaign/update':
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM campaigns WHERE id=? AND project=?',(d.get('id'),p)).fetchone()
        if not row: raise ValueError('活动不存在')
        spent=s.money(d.get('spent')); note=s.required(d,'note'); status=d.get('status')
        if spent>row['budget']: raise ValueError('超出活动预算，需先取得追加预算授权；记录未改动')
        if spent<row['spent']: raise ValueError('累计支出不能减少；费用纠正需单独核查')
        if status not in {'草稿','人工执行中','暂停','结束'}: raise ValueError('活动状态无效')
        c.execute('UPDATE campaigns SET spent=?,status=? WHERE id=?',(spent,status,row['id'])); s.log(c,p,'人工更新活动：'+row['name']+' → '+status+'；证据：'+note); return {'ok':True}
    if path=='/api/orders':
        c.execute('BEGIN IMMEDIATE')
        external=s.required(d,'external_id'); revenue=s.money(d.get('revenue')); cost=s.money(d.get('cost'))
        existing=c.execute('SELECT * FROM orders WHERE project=? AND external_id=?',(p,external)).fetchone()
        if existing:
            if existing['revenue']!=revenue or existing['cost']!=cost: raise ValueError('同一订单号的金额冲突，请核查原始记录')
            return {'id':existing['id'],'duplicate':True}
        i=s.uid(); c.execute('INSERT INTO orders VALUES(?,?,?,?,?,0,?,?,?)',(i,p,external,revenue,cost,'待核查','',s.now())); s.log(c,p,'人工导入订单 '+external+'；待核查，不代表系统收到真实支付'); return {'id':i,'duplicate':False}
    if path=='/api/order/update':
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM orders WHERE id=? AND project=?',(d.get('id'),p)).fetchone()
        if not row: raise ValueError('订单不存在')
        status=d.get('status'); note=s.required(d,'note'); tracking=d.get('tracking',''); refund=s.money(d.get('refund'))
        transitions={'待核查':{'待核查','已确认支付','已取消'},'已确认支付':{'已确认支付','已发货','售后处理中'},'已发货':{'已发货','已交付','售后处理中'},'已交付':{'已交付','售后处理中'},'售后处理中':{'售后处理中','已关闭'},'已关闭':{'已关闭'},'已取消':{'已取消'}}
        if status not in transitions[row['status']]: raise ValueError('订单状态不能跳过支付核查或履约步骤')
        if status=='已发货' and not tracking.strip(): raise ValueError('发货必须填写物流追踪信息')
        if refund>row['revenue']: raise ValueError('退款金额不能超过订单收入')
        if refund<row['refund']: raise ValueError('累计退款不能减少')
        if refund and status not in {'售后处理中','已关闭'}: raise ValueError('退款记录需进入售后流程')
        c.execute('UPDATE orders SET status=?,tracking=?,refund=? WHERE id=?',(status,tracking,refund,row['id'])); s.log(c,p,'人工核查订单 '+row['external_id']+' → '+status+'；证据：'+note+'；仅记账，不执行退款或发货'); return {'ok':True}
    if path=='/api/experiments':
        i=s.uid(); c.execute('INSERT INTO experiments VALUES(?,?,?,?,?,?,?,?,?)',(i,p,s.required(d,'hypothesis'),s.required(d,'change_text'),s.required(d,'observation'),'','','观察中',s.now())); s.log(c,p,'建立经营实验；等待达到观察条件'); return {'id':i}
    if path=='/api/experiment/update':
        row=c.execute('SELECT * FROM experiments WHERE id=? AND project=?',(d.get('id'),p)).fetchone()
        if not row: raise ValueError('实验不存在')
        status=d.get('status')
        if status not in {'数据不足','假设获支持','假设未获支持','停止'}: raise ValueError('实验状态无效')
        result=s.required(d,'result'); decision=s.required(d,'decision')
        c.execute('UPDATE experiments SET result=?,decision=?,status=? WHERE id=?',(result,decision,status,row['id'])); s.log(c,p,'人工实验复盘：'+status+'；下一步：'+decision); return {'ok':True}
    raise ValueError('未知操作')
