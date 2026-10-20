import unittest,json,datetime
from unittest.mock import patch
import test_workflow,test_access
import server as s,business,connectors,engine

class Business(unittest.TestCase):
    setUp=test_workflow.Workflow.setUp
    tearDown=test_workflow.Workflow.tearDown
    def op(self,path,**d):
        self.c.commit();r=s.mutate(self.c,path,{'project':self.p,**d});self.c.commit();return r
    def entity(self,kind,**v):return self.op('/api/entities',kind=kind,value=v)['id']
    def test_simple_start_and_confirmation_keeps_gates(self):
        r=s.mutate(self.c,'/api/projects',{'idea':'从零开始尝试跨境电商','quick_start':True})
        project=self.c.execute('SELECT * FROM projects WHERE id=?',(r['id'],)).fetchone()
        self.assertEqual(project['name'],'从零开始尝试跨境电商')
        self.assertEqual(project['budget'],0)
        self.assertEqual(json.loads(project['facts'])['budget_status'],'待确认')
        zero=s.mutate(self.c,'/api/projects',{'idea':'明确零预算','budget':'0'})
        self.assertEqual(json.loads(self.c.execute('SELECT facts FROM projects WHERE id=?',(zero['id'],)).fetchone()[0])['budget_status'],'已确认')
        self.assertEqual(self.c.execute('SELECT count(*) FROM attempts WHERE project=?',(r['id'],)).fetchone()[0],1)
        with self.assertRaisesRegex(ValueError,'尚未达到'):
            self.op('/api/advance')
        self.c.rollback()
        self.entity('opportunity',name='方向',audience='客户',problem='需求',source='访谈来源',validation='兴趣测试',decision='确认验证')
        self.assertEqual(self.op('/api/advance')['stage'],'验证')
        self.assertIn('不扩大支出',self.c.execute('SELECT reason FROM plans WHERE project=?',(self.p,)).fetchone()[0])

    def test_gates_sample_and_versions(self):
        self.assertIn('至少确认',business.gate(self.c,self.p,'验证')[0])
        self.entity('opportunity',name='方向',audience='目标客户',problem='需求',source='访谈来源',validation='兴趣测试',decision='确认验证')
        r=self.op('/api/advance',reason='人工确认方向');self.assertEqual(r['stage'],'验证')
        gaps=business.gate(self.c,self.p,'销售准备');self.assertTrue(gaps)
        product=self.entity('product',name='商品',sku='SKU1',specification='规格',material='材料',weight='10g',dimensions='1cm',usage='用途',limitations='限制',source='核查记录')
        supplier=self.entity('supplier',name='供应商',identity='主体确认',contact='授权联系',source='主体文件')
        self.entity('quote',product_id=product,supplier_id=supplier,price='10',currency='CNY',moq='1',stock='100',lead_time='2天',shipping='运费记录',returns='责任已确认',valid_until=(datetime.date.today()+datetime.timedelta(days=30)).isoformat(),source='正式报价')
        self.entity('sample',product_id=product,inspector='测试检查人',inspection_date=datetime.date.today().isoformat(),result='通过',findings='实物检查通过',source='检查资料ID')
        self.entity('risk',name='商品风险',market='目标市场',level='低',status='已处理',source='适用标准',action='人工核查证据')
        amounts={k:'0' for k in ['discount','refund','pass_through','purchase','shipping','fees','tax','other','marketing']}
        self.op('/api/records',kind='成本测算',product_id=product,content='完整预测',revenue='100',**amounts)
        self.op('/api/records',kind='成本测算',product_id=product,content='保守预测',scenario='保守',revenue='80',**amounts)
        self.assertEqual(business.gate(self.c,self.p,'销售准备'),[])
        self.assertEqual(self.op('/api/advance',reason='商品与成本已核查')['stage'],'销售准备')
        old=self.c.execute('SELECT * FROM entities WHERE id=?',(product,)).fetchone();value=json.loads(old['data']);value['material']='更正材料'
        self.op('/api/entities',kind='product',id=product,version=1,value=value,reason='核实后更正')
        self.assertEqual(business.flow(self.c,self.p)['status'],'暂停');self.assertEqual(self.c.execute('SELECT count(*) FROM entity_versions WHERE entity=?',(product,)).fetchone()[0],2)
        with self.assertRaisesRegex(ValueError,'刷新'):self.op('/api/entities',kind='product',id=product,version=1,value=value,reason='旧版本')
        self.c.rollback()
    def test_budget_cross_reservations(self):
        a=self.op('/api/approvals',kind='采购',subject='采购样品',amount='600',reason='验证预算')
        self.op('/api/approval/decide',id=a['id'],status='已批准',decision='确认范围')
        with self.assertRaisesRegex(ValueError,'超过'):self.op('/api/campaigns',name='测试',audience='人群',channel='SEO',landing='站点',hypothesis='需求',stop_rule='7天',budget='500')
        self.c.rollback()
    def test_concurrent_approvals_cannot_exceed_budget(self):
        import concurrent.futures
        approvals=[self.op('/api/approvals',kind='采购',subject='采购'+str(i),amount='600',reason='测试预算') for i in range(2)]
        def decide(a):
            try:
                with s.connect() as c:s.mutate(c,'/api/approval/decide',{'project':self.p,'id':a['id'],'status':'已批准','decision':'人工批准'})
                return 'approved'
            except ValueError:return 'blocked'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(decide,approvals))
        self.assertEqual(sorted(results),['approved','blocked'])
    def test_risk_pauses_autopilot_and_ticket_followup(self):
        self.op('/api/flow',status='进行中',autopilot=1)
        self.entity('risk',name='风险',market='US',level='高',status='待核查',source='监管要求',action='需复核')
        with self.assertRaisesRegex(ValueError,'风险'):self.op('/api/flow',status='进行中',autopilot=1)
        self.c.rollback();self.assertEqual(business.flow(self.c,self.p)['status'],'暂停')
        self.entity('ticket',subject='配送问题',customer_ref='customer-1',message='查件',owner='客服',status='待处理')
        self.assertEqual(self.c.execute('SELECT count(*) FROM followups WHERE project=?',(self.p,)).fetchone()[0],2)
    def test_feedback_creates_one_task_and_executes(self):
        exp=self.op('/api/experiments',hypothesis='标题假设',change_text='只改标题',observation='100次访问')
        d=dict(id=exp['id'],status='数据不足',result='只有5次访问',decision='继续观察')
        first=self.op('/api/experiment/update',**d);second=self.op('/api/experiment/update',**d);self.assertEqual(first['task'],second['task'])
        self.op('/api/run',id=first['task']);engine.execute(engine.claim())
        result=json.loads(self.c.execute('SELECT output FROM attempts WHERE task=?',(first['task'],)).fetchone()[0]);self.assertIn('数据不足',result['deliverables'][0])
    def test_finance_refund_and_expense_do_not_double_count(self):
        import finance
        order=self.op('/api/orders',external_id='REAL-reference',revenue='100',cost='30')
        self.op('/api/order/update',id=order['id'],status='已确认支付',tracking='',refund='0',note='支付核查')
        expense=dict(category='系统',basis='实际',amount='10',reference='unique-cost',note='期间费用')
        self.op('/api/expenses',**expense);self.assertTrue(self.op('/api/expenses',**expense)['duplicate'])
        self.assertEqual(finance.statement(self.c,self.p)['operating_result'],6000)
        self.c.commit();self.op('/api/order/update',id=order['id'],status='售后处理中',tracking='',refund='20',note='实际退款记录')
        self.assertEqual(finance.statement(self.c,self.p)['operating_result'],4000)
    def test_file_validation(self):
        import base64
        data=base64.b64encode(('核查资料\n'*10000).encode()).decode()
        uploaded=self.op('/api/files',name='source.txt',mime='text/plain',data=data)
        self.assertGreater(self.c.execute('SELECT length(data) FROM files WHERE id=?',(uploaded['id'],)).fetchone()[0],10000)
        with self.assertRaisesRegex(ValueError,'类型不符'):self.op('/api/files',name='fake.png',mime='image/png',data=data)
        self.c.rollback()
    def test_persistence_and_legacy_migration(self):
        self.op('/api/approvals',kind='方向变更',subject='方向',amount='0',reason='核查')
        self.c.close();self.c=s.connect();self.assertEqual(len(s.snapshot(self.c)['approvals']),1);self.assertEqual(len(s.snapshot(self.c)['projects']),1)

class Isolation(unittest.TestCase):
    setUp=test_access.PublicAccess.setUp
    tearDown=test_access.PublicAccess.tearDown
    request=test_access.PublicAccess.request
    def call(self,path,data=None,cookie=None):
        status,headers,body=self.request(path,data,cookie,'https://venture.example');return status,headers,json.loads(body)
    def test_two_accounts_cannot_read_write_export_or_download_other_projects(self):
        root='venture_access='+s.access_cookie(self.token)
        for name in ['alice','bob']:
            self.assertEqual(self.call('/api/account/create',{'username':name,'password':'test-password-123456'},root)[0],200)
        a=self.call('/api/login',{'username':'alice','password':'test-password-123456'})[1]['Set-Cookie']
        b=self.call('/api/login',{'username':'bob','password':'test-password-123456'})[1]['Set-Cookie']
        p=self.call('/api/projects',{'name':'私有项目','idea':'私人资料','budget':'1'},a)[2]['id']
        self.assertEqual(len(self.call('/api/state',cookie=b)[2]['projects']),0)
        self.assertEqual(len(self.call('/api/state',cookie=root)[2]['projects']),0)
        self.assertEqual(self.call('/api/messages',{'project':p,'content':'越权'},b)[0],404)
        self.assertEqual(self.call('/api/export/'+p,cookie=b)[0],404)
        import base64
        f=self.call('/api/files',{'project':p,'name':'private.txt','mime':'text/plain','data':base64.b64encode(b'private').decode()},a)[2]['id']
        self.assertEqual(self.call('/api/file/'+f,cookie=b)[0],404)
        self.assertEqual(self.call('/api/account/create',{'username':'evil','password':'test-password-123456'},b)[0],403)
        accounts=self.call('/api/state',cookie=root)[2]['accounts'];bid=next(x['id'] for x in accounts if x['username']=='bob')
        self.call('/api/account/disable',{'id':bid},root)
        self.assertEqual(self.call('/api/state',cookie=b)[0],401)

class ExternalActions(unittest.TestCase):
    setUp=test_workflow.Workflow.setUp
    tearDown=test_workflow.Workflow.tearDown
    op=Business.op
    entity=Business.entity
    def prepare(self):
        product=self.entity('product',name='商品',sku='SKU',specification='规格',material='材质',weight='重量',dimensions='尺寸',usage='用途',limitations='限制',source='原始文件')
        content=self.entity('content',name='标题',product_id=product,channel='SEO',body='基于事实的文案',source='商品事实')
        self.op('/api/connector');self.c.execute("UPDATE connectors SET status='可执行' WHERE project=?",(self.p,));self.c.commit()
        approval=self.op('/api/approvals',kind='内容发布',subject=content,amount='0',reason='授权发布这篇文章')
        self.op('/api/approval/decide',id=approval['id'],status='已批准',decision='确认此版本')
        return self.op('/api/action',content=content,approval=approval['id']),approval
    def test_timeout_no_duplicate_and_reconcile(self):
        action,_=self.prepare()
        with patch('connectors.request',side_effect=[[],connectors.RemoteUnknown('超时')]) as call:
            r=self.op('/api/action/execute',id=action['id']);self.assertEqual(r['status'],'结果未知');self.assertEqual(call.call_count,2)
        with patch('connectors.request') as call:
            with self.assertRaisesRegex(ValueError,'禁止重复'):self.op('/api/action/execute',id=action['id'])
            self.c.rollback();call.assert_not_called()
        with patch('connectors.request',return_value=[{'id':123,'link':'https://example.com/published','status':'publish'}]):
            self.assertEqual(self.op('/api/action/reconcile',id=action['id'])['status'],'已确认')
        with patch('connectors.request') as call:
            self.assertEqual(self.op('/api/action/execute',id=action['id'])['status'],'已确认');call.assert_not_called()
    def test_revoke_before_dispatch_blocks_write(self):
        action,approval=self.prepare();self.op('/api/approval/decide',id=approval['id'],status='已撤销',decision='停止发布')
        with patch('connectors.request') as call:
            with self.assertRaisesRegex(ValueError,'禁止重复'):self.op('/api/action/execute',id=action['id'])
            self.c.rollback();call.assert_not_called()
    def test_restart_turns_sending_into_unknown(self):
        action,_=self.prepare();self.c.execute("UPDATE external_actions SET status='发送中' WHERE id=?",(action['id'],));self.c.commit()
        connectors.recover(self.c);self.c.commit();self.assertEqual(self.c.execute('SELECT status FROM external_actions WHERE id=?',(action['id'],)).fetchone()[0],'结果未知')
    def test_successful_publish_has_external_evidence(self):
        action,_=self.prepare()
        with patch('connectors.request',side_effect=[[],{'id':456,'link':'https://example.com/test-post','status':'publish'}]) as call:
            result=self.op('/api/action/execute',id=action['id']);self.assertEqual(result['status'],'已确认');self.assertEqual(call.call_count,2)
        row=self.c.execute('SELECT * FROM external_actions WHERE id=?',(action['id'],)).fetchone();self.assertEqual(row['external_id'],'456')

    def test_paginated_site_import_deduplicates_without_private_customer_data(self):
        self.prepare()
        product={'id':9,'name':'外部商品','sku':'S','status':'publish','price':'9','stock_status':'instock'}
        order={'id':10,'status':'processing','currency':'USD','total':'100','total_tax':'5','billing':{'email':'private@example.com'}}
        with patch('connectors.request',side_effect=[[product],[order],[product],[order]]):
            self.op('/api/connector/import',page=1);self.op('/api/connector/import',page=1)
        imports=self.c.execute('SELECT * FROM external_imports WHERE project=?',(self.p,)).fetchall()
        self.assertEqual(len(imports),2);self.assertNotIn('private@example.com',''.join(r['data'] for r in imports))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM orders WHERE project=?',(self.p,)).fetchone()[0],0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM followups WHERE project=? AND event_key='site-order:10'",(self.p,)).fetchone()[0],1)
