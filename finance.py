"""CNY operating statements, separating order contribution and period expenses."""
import server as s

def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS expenses(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),category TEXT,amount INTEGER,basis TEXT,reference TEXT,note TEXT,created TEXT,UNIQUE(project,reference))')
    c.execute('CREATE TABLE IF NOT EXISTS cash_entries(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),direction TEXT,category TEXT,amount INTEGER,basis TEXT,reference TEXT,note TEXT,occurred TEXT,created TEXT,UNIQUE(project,reference))')
    c.commit()

def record(c,d):
    p=d['project'];category=d.get('category');basis=d.get('basis')
    if category not in {'AI运行','数据工具','系统','固定费用','其他期间费用'} or basis not in {'实际','估计'}:raise ValueError('费用类别或核算口径无效')
    amount=s.money(d.get('amount'));ref=s.required(d,'reference');note=s.required(d,'note')
    old=c.execute('SELECT * FROM expenses WHERE project=? AND reference=?',(p,ref)).fetchone()
    if old:
        if (old['amount'],old['category'],old['basis'])!=(amount,category,basis):raise ValueError('同一费用凭据的数据冲突，请先核查，不重复入账')
        return {'id':old['id'],'duplicate':True}
    i=s.uid();c.execute('INSERT INTO expenses VALUES(?,?,?,?,?,?,?,?)',(i,p,category,amount,basis,ref,note,s.now()));s.log(c,p,'登记'+basis+category+'费用；不执行付款');return {'id':i,'duplicate':False}

def statement(c,p):
    orders=c.execute("SELECT * FROM orders WHERE project=? AND status NOT IN ('待核查','已取消') AND external_id NOT LIKE 'TEST-%'",(p,)).fetchall()
    net=sum(o['revenue']-o['refund'] for o in orders);variable=sum(o['cost'] for o in orders)
    marketing=c.execute('SELECT COALESCE(SUM(spent),0) FROM campaigns WHERE project=?',(p,)).fetchone()[0]
    period=c.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project=? AND basis='实际'",(p,)).fetchone()[0]
    estimates=c.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project=? AND basis='估计'",(p,)).fetchone()[0]
    budget=c.execute('SELECT budget FROM projects WHERE id=?',(p,)).fetchone()[0]
    return {'currency':'CNY','paid_orders':len(orders),'net_revenue':net,'variable_cost':variable,'pre_marketing':net-variable,'marketing':marketing,'post_marketing':net-variable-marketing,'period_actual':period,'period_estimated':estimates,'operating_result':net-variable-marketing-period,'provisional':True,'budget_exceeded':marketing+period>budget,'basis':'仅已人工核查支付的非TEST订单；订单变量成本来自人工录入，营销费用取活动累计支出，期间费用另扣。估计费用单独列示。外部导入未核对订单不计入。'}

def committed(c,p,exclude_approval=None,exclude_campaign=None):
    campaigns=c.execute('SELECT COALESCE(SUM(budget),0) FROM campaigns WHERE project=? AND id!=?',(p,exclude_campaign or '')).fetchone()[0]
    approvals=c.execute("SELECT COALESCE(SUM(amount),0) FROM approvals WHERE project=? AND status='已批准' AND kind!='广告执行' AND id!=?",(p,exclude_approval or '')).fetchone()[0]
    tools=c.execute('SELECT COALESCE(SUM(CASE WHEN budget>spent THEN budget-spent ELSE 0 END),0) FROM tool_grants WHERE project=? AND active=1 AND expires>?',(p,s.now())).fetchone()[0]
    consumed=c.execute('SELECT COALESCE(SUM(COALESCE(c.actual,u.reserved)),0) FROM tool_uses u LEFT JOIN tool_charges c ON c.use_id=u.id WHERE u.project=?',(p,)).fetchone()[0]
    period=c.execute("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE project=? AND basis='实际' AND reference NOT IN (SELECT reference FROM tool_charges WHERE project=?)",(p,p)).fetchone()[0]
    return campaigns+approvals+tools+consumed+period

def cash_record(c,d):
    p=d['project'];direction=d.get('direction');basis=d.get('basis');category=d.get('category')
    if direction not in {'流入','流出'} or basis not in {'实际','预测'} or category not in {'期初资金','销售结算','工具与系统','样品','采购','物流','营销','退款','税费','其他'}:raise ValueError('现金记录类别或口径无效')
    if category=='期初资金' and (direction!='流入' or basis!='实际'):raise ValueError('期初资金须为已核实的实际流入')
    amount=s.money(d.get('amount'));reference=s.required(d,'reference');note=s.required(d,'note');occurred=s.required(d,'occurred')
    try:__import__('datetime').date.fromisoformat(occurred)
    except ValueError:raise ValueError('资金日期使用YYYY-MM-DD')
    old=c.execute('SELECT * FROM cash_entries WHERE project=? AND reference=?',(p,reference)).fetchone()
    if old:
        if (old['amount'],old['direction'],old['category'],old['basis'])!=(amount,direction,category,basis):raise ValueError('同一现金凭据记录冲突，请先核对')
        return {'id':old['id'],'duplicate':True}
    if category=='期初资金' and c.execute("SELECT 1 FROM cash_entries WHERE project=? AND category='期初资金'",(p,)).fetchone():raise ValueError('期初资金已有记录；更正需保留独立调整凭据，不重复设置期初')
    i=s.uid();c.execute('INSERT INTO cash_entries VALUES(?,?,?,?,?,?,?,?,?,?)',(i,p,direction,category,amount,basis,reference,note,occurred,s.now()));s.log(c,p,'记录'+basis+'现金'+direction+'；不执行付款或收款');return {'id':i}

def planning(c,p):
    import json,business
    costs=[json.loads(r['body']) for r in s.effective_records(c,p) if r['kind']=='成本测算']
    products=[]
    for row in business.rows(c,p,'product'):
        scenarios={}
        for scenario in ['正常','保守']:
            matches=[v for v in costs if v.get('product_id')==row['id'] and v.get('scenario','正常')==scenario]
            latest=matches[-1] if matches else None;result=latest.get('result') if latest else None
            scenarios[scenario]={'net':result['net'] if result else None,'pre_marketing':result['pre_marketing'] if result else None,'post_marketing':result['post_marketing'] if result else None,'break_even_acquisition_cost':max(0,result['pre_marketing']) if result else None,'basis':latest.get('basis','预测') if latest else '未知','missing':latest.get('missing',[]) if latest else ['尚未提供本商品测算']}
        products.append({'id':row['id'],'name':row['value']['name'],'scenarios':scenarios})
    entries=[dict(r) for r in c.execute('SELECT * FROM cash_entries WHERE project=?',(p,))]
    actual=sum(v['amount']*(1 if v['direction']=='流入' else -1) for v in entries if v['basis']=='实际');forecast=sum(v['amount']*(1 if v['direction']=='流入' else -1) for v in entries if v['basis']=='预测')
    return {'currency':'CNY','products':products,'cash_balance':actual if any(v['category']=='期初资金' for v in entries) else None,'forecast_net_flow':forecast if any(v['basis']=='预测' for v in entries) else None,'cash_basis':'仅已录入并核对的现金凭据；未设期初时不猜余额。到账不等于收入或利润，预测不等于实际。','budget_committed':committed(c,p)}
