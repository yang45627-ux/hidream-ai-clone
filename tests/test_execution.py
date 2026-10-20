import json, unittest
from unittest.mock import patch
import server, engine
import test_workflow

class Execution(unittest.TestCase):
    setUp=test_workflow.Workflow.setUp
    tearDown=test_workflow.Workflow.tearDown
    def task(self,n=0): return server.snapshot(self.c)['tasks'][n]
    def enqueue(self,t):
        self.c.commit(); r=server.mutate(self.c,'/api/run',{'project':self.p,'id':t['id']}); self.c.commit(); return r
    def change(self,t,status):
        server.mutate(self.c,'/api/tasks',{'project':self.p,'id':t['id'],'status':status,'note':'验收证据'});self.c.commit()
    def test_real_local_execution_and_duplicate(self):
        t=self.task(); first=self.enqueue(t); second=self.enqueue(t)
        self.assertEqual(first['id'],second['id']); self.assertTrue(second['duplicate'])
        engine.execute(engine.claim()); state=server.snapshot(self.c)
        self.assertEqual(state['tasks'][0]['status'],'待验收'); output=json.loads(state['attempts'][0]['output']); self.assertIn('项目启动卡',output['summary'])
        self.change(t,'已完成'); self.assertEqual(self.task(1)['status'],'待处理')
    def test_missing_provider_and_missing_evidence(self):
        t=self.task(); self.change(t,'待验收');self.change(t,'已完成'); t=self.task(1)
        with patch.dict('os.environ',{'AI_API_KEY':'','OPENAI_API_KEY':'','AI_MODEL':''}):
            with self.assertRaisesRegex(ValueError,'模型未配置'):self.enqueue(t)
        self.c.rollback();self.change(t,'待验收')
        with self.assertRaisesRegex(ValueError,'机会证据'):self.change(t,'已完成')
        self.c.rollback()
    def test_restart_does_not_repeat_inflight_call(self):
        self.enqueue(self.task()); engine.claim(); engine.recover()
        self.assertIsNone(engine.claim()); state=server.snapshot(self.c)
        self.assertEqual(state['attempts'][0]['status'],'需人工确认'); self.assertEqual(state['tasks'][0]['status'],'等待人工')
    def test_human_takeover_rejects_late_result(self):
        t=self.task();self.enqueue(t); job=engine.claim();self.change(t,'暂停');engine.execute(job)
        self.assertEqual(self.task()['status'],'暂停'); self.assertIsNone(server.snapshot(self.c)['attempts'][0]['output'])
    def test_version_change_invalidates_output(self):
        self.enqueue(self.task()); job=engine.claim()
        server.mutate(self.c,'/api/project/update',{'project':self.p,'name':'更正','idea':'新条件','budget':'200'});self.c.commit();engine.execute(job)
        state=server.snapshot(self.c); self.assertEqual(state['attempts'][0]['status'],'已取消');self.assertEqual(state['tasks'][0]['status'],'需返工')
    def test_model_authorization_consumption_and_revoke(self):
        t=self.task(); self.change(t,'待验收');self.change(t,'已完成');t=self.task(1)
        with patch('engine.model_status',return_value={'configured':True}):
            with self.assertRaisesRegex(ValueError,'授权'):self.enqueue(t)
            self.c.rollback();server.mutate(self.c,'/api/grants',{'project':self.p,'calls':1});self.c.commit()
            self.enqueue(t);self.assertEqual(server.snapshot(self.c)['grants'][0]['remaining'],0)
            server.mutate(self.c,'/api/grants',{'project':self.p,'calls':0});self.c.commit()
            self.assertIsNone(engine.claim());self.assertEqual(self.task(1)['status'],'暂停')
    def test_corrected_evidence_preserves_history_and_invalidates(self):
        self.c.commit()
        server.mutate(self.c,'/api/records',{'project':self.p,'kind':'商品与供应','content':'原始规格','source':'原文件'})
        self.c.commit(); old=server.effective_records(self.c,self.p)[0]
        server.mutate(self.c,'/api/record/correct',{'project':self.p,'id':old['id'],'content':'更正规格','source':'新核查'})
        self.c.commit()
        self.assertEqual(len(server.snapshot(self.c)['records']),2)
        effective=server.effective_records(self.c,self.p)
        self.assertEqual(len(effective),1); self.assertEqual(json.loads(effective[0]['body'])['content'],'更正规格')
        self.assertEqual(self.task()['status'],'需返工')
    def test_provider_protocol_and_invalid_evidence(self):
        result={'summary':'研究草稿','deliverables':['已有资料分析'],'evidence_ids':['invented'],'unknowns':['缺少核实'],'next_steps':['补充证据']}
        with patch('engine.model_config',return_value=('https://example.com/v1','test-only-key','test-model')),patch('net.json_request',side_effect=lambda *a,**k:{'choices':[{'message':{'content':json.dumps(result)}}]}):
            with self.assertRaisesRegex(ValueError,'不存在'):engine.model_call({'records':[]})
            result['evidence_ids']=[];self.assertIn('草稿',engine.model_call({'records':[]})['execution'])

class Operating(unittest.TestCase):
    setUp=test_workflow.Workflow.setUp
    tearDown=test_workflow.Workflow.tearDown
    def op(self,path,**data):
        self.c.commit();r=server.mutate(self.c,path,{'project':self.p,**data});self.c.commit();return r
    def test_order_idempotency_transitions_and_refund(self):
        order=self.op('/api/orders',external_id='TEST-1',revenue='100',cost='30')
        self.assertTrue(self.op('/api/orders',external_id='TEST-1',revenue='100',cost='30')['duplicate'])
        with self.assertRaisesRegex(ValueError,'跳过'):self.op('/api/order/update',id=order['id'],status='已发货',tracking='X',refund='0',note='证据')
        self.c.rollback()
        for status in ['已确认支付','已发货','售后处理中']:
            self.op('/api/order/update',id=order['id'],status=status,tracking='X',refund='0',note='人工原始凭证')
        self.op('/api/order/update',id=order['id'],status='已关闭',tracking='X',refund='20',note='实际退款记录')
        o=server.snapshot(self.c)['orders'][0];self.assertEqual(o['revenue']-o['cost']-o['refund'],5000);self.assertEqual(len(server.snapshot(self.c)['orders']),1)
    def test_budget_cap_and_expenses(self):
        args=dict(name='测试计划',audience='细分人群',channel='自然内容',landing='页面位置',hypothesis='验证需求',stop_rule='7天后复盘',budget='600')
        campaign=self.op('/api/campaigns',**args)
        with self.assertRaisesRegex(ValueError,'超过'):self.op('/api/campaigns',**args)
        self.c.rollback()
        with self.assertRaisesRegex(ValueError,'超出'):self.op('/api/campaign/update',id=campaign['id'],spent='601',status='人工执行中',note='费用记录')
        self.c.rollback();self.op('/api/campaign/update',id=campaign['id'],spent='20',status='人工执行中',note='实际支出凭据')
        self.assertEqual(server.snapshot(self.c)['campaigns'][0]['spent'],2000)
    def test_experiment_insufficient_data(self):
        exp=self.op('/api/experiments',hypothesis='需求假设',change_text='更改标题',observation='至少100次有效访问')
        self.op('/api/experiment/update',id=exp['id'],status='数据不足',result='仅5次访问',decision='继续观察，不判定有效')
        self.assertEqual(server.snapshot(self.c)['experiments'][0]['status'],'数据不足')
