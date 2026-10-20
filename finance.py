"""CNY operating statements, separating order contribution and period expenses."""
import server as s

def setup(c):
    c.execute('CREATE TABLE IF NOT EXISTS expenses(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),category TEXT,amount INTEGER,basis TEXT,reference TEXT,note TEXT,created TEXT,UNIQUE(project,reference))')
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
