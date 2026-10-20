import unittest,json,io,base64,datetime
from unittest.mock import patch
from PIL import Image
import server as s,business,engine,runtime,research,media,growth,integrations,director,workflows,net,finance
import test_workflow,test_storefront

class Helpers(unittest.TestCase):
    setUp=test_workflow.Workflow.setUp
    tearDown=test_workflow.Workflow.tearDown
    def op(self,path,**data):
        self.c.commit();result=s.mutate(self.c,path,{'project':self.p,**data});self.c.commit();return result
    def entity(self,kind,**v):return self.op('/api/entities',kind=kind,value=v)['id']
    def product(self,name='TEST 商品'):
        return self.entity('product',name=name,sku=name,specification='真实规格',material='材料',weight='10g',dimensions='1cm',usage='测试',limitations='非真实商业证据',source='TEST 文件')
    def grant(self,tool,calls=10,budget='0',target='*'):
        return self.op('/api/tools/grant',tool=tool,calls=calls,budget=budget,target=target,days=1)
    def file(self):
        out=io.BytesIO();Image.new('RGB',(320,320),'white').save(out,'PNG')
        return self.op('/api/files',name='TEST-reference.png',mime='image/png',data=base64.b64encode(out.getvalue()).decode())['id'],out.getvalue()
    def job(self,i):return dict(self.c.execute('SELECT * FROM jobs WHERE id=?',(i,)).fetchone())
    def approve(self,a):self.op('/api/approval/decide',id=a,status='已批准',decision='TEST 核查确切内容与授权')

class Extended(Helpers):
    def test_automatic_start_needs_no_startup_acceptance_or_dispatch_click(self):
        p=s.mutate(self.c,'/api/projects',{'idea':'TEST 一句话自动启动','quick_start':True})['id'];self.c.commit()
        self.assertEqual(business.flow(self.c,p)['autopilot'],1)
        attempt=engine.claim();self.assertIsNotNone(attempt);engine.execute(attempt)
        self.assertEqual(self.c.execute('SELECT status FROM tasks WHERE project=? AND dependency IS NULL',(p,)).fetchone()[0],'已完成')
        director.automate();director.dispatch_research()
        self.assertEqual(self.c.execute('SELECT status FROM jobs WHERE project=?',(p,)).fetchone()[0],'排队中')
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM tool_grants WHERE project=?',(p,)).fetchone()[0],2)
        director.automate();director.dispatch_research()
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM jobs WHERE project=?',(p,)).fetchone()[0],1)
    def test_automation_does_not_invent_model_or_reauthorize_revoked_calls(self):
        p=s.mutate(self.c,'/api/projects',{'idea':'TEST 没有模型','quick_start':True})['id'];self.c.commit()
        with patch('engine.model_status',return_value={'configured':False}):director.automate()
        self.assertFalse(self.c.execute('SELECT 1 FROM grants WHERE project=?',(p,)).fetchone())
        self.c.execute('INSERT INTO grants VALUES(?,0)',(p,));self.c.commit()
        with patch('engine.model_status',return_value={'configured':True}):director.automate()
        self.assertEqual(self.c.execute('SELECT remaining FROM grants WHERE project=?',(p,)).fetchone()[0],0)
    def test_private_sources_and_redirects_not_permitted(self):
        for url in ['http://example.com','https://user:pass@example.com','https://example.com:8080']:
            with self.assertRaises(ValueError):net.public_url(url)
        with patch('net.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaisesRegex(ValueError,'私有'):net.public_url('https://example.com')
    def test_expiring_grants_and_shared_budget_reservation(self):
        self.grant('image',calls=2,budget='800')
        with self.assertRaisesRegex(ValueError,'超过'):self.op('/api/campaigns',name='活动',audience='人群',channel='内容',landing='站点',hypothesis='假设',stop_rule='条件',budget='300')
        self.c.rollback()
        with patch.dict('os.environ',{'IMAGE_CALL_CNY':'450'}):
            runtime.consume(self.c,self.p,'image','TEST-job');self.c.commit()
            with self.assertRaisesRegex(ValueError,'上限'):runtime.consume(self.c,self.p,'image','TEST-job')
        self.c.rollback()
        g=self.c.execute('SELECT id FROM tool_grants').fetchone()[0];self.op('/api/tools/revoke',id=g)
        with self.assertRaisesRegex(ValueError,'授权'):runtime.consume(self.c,self.p,'image','TEST-job')
    def test_research_collects_sources_and_creates_three_or_fewer_real_drafts(self):
        self.grant('search');self.op('/api/grants',calls=1)
        task=self.op('/api/director/research',query='TEST 旅行用品')
        def output(context):
            ids=[r['id'] for r in context['records']]
            return {'summary':'TEST 研究假设','deliverables':['比较'],'evidence_ids':ids,'unknowns':['需求待验证'],'next_steps':['访谈'], 'opportunities':[dict(name='TEST 方向',audience='旅行用户',problem='收纳问题',competition='同类产品待比较',supply_hypothesis='供应待核实',risk='样本不足',validation='访谈验证',evidence_ids=ids)]}
        with patch('research.search',return_value=[{'url':'https://example.com/research','title':'TEST 访谈摘要','excerpt':'TEST 需求材料','type':'TEST'}]),patch('net.public_url',side_effect=lambda u:u),patch('engine.model_status',return_value={'configured':True}),patch('engine.model_call',side_effect=output):
            runtime.step()
        j=self.job(task['id']);self.assertEqual(j['status'],'已完成',j['error']);result=json.loads(j['result']);self.assertEqual(len(result['candidates']),1)
        value=json.loads(self.c.execute('SELECT data FROM entities WHERE id=?',(result['candidates'][0],)).fetchone()[0]);self.assertEqual(value['decision'],'待确认');self.assertIn('example.com',value['source'])
        root=self.c.execute('SELECT * FROM tasks WHERE project=? AND dependency IS NULL',(self.p,)).fetchone();self.op('/api/tasks',id=root['id'],status='待验收',note='TEST 启动卡');self.op('/api/tasks',id=root['id'],status='已完成',note='TEST 确认启动卡')
        plan=self.op('/api/director/choose',id=result['candidates'][0]);self.assertEqual(len(plan['tasks']),5)
    def test_hallucinated_candidate_source_is_rejected(self):
        self.grant('search');self.op('/api/grants',calls=1);job=self.op('/api/director/research',query='TEST')
        candidate=dict(name='方向',audience='客户',problem='需求',competition='竞争',supply_hypothesis='假设',risk='未知',validation='验证',evidence_ids=['invented'])
        with patch('research.search',return_value=[dict(url='https://example.com',title='来源',excerpt='测试')]),patch('net.public_url',side_effect=lambda u:u),patch('engine.model_status',return_value={'configured':True}),patch('engine.model_call',return_value={'summary':'测试','opportunities':[candidate],'unknowns':[]}):runtime.step()
        self.assertEqual(self.job(job['id'])['status'],'等待处理');self.assertEqual(len(business.rows(self.c,self.p,'opportunity')),0)
    def test_intake_proposals_need_explicit_user_confirmation(self):
        self.op('/api/grants',calls=1);job=self.op('/api/director/message',content='每周10小时，做美国市场，预算200',analyze=True)
        result={'summary':'条件建议','proposal':{'market':'美国','time':'每周10小时','budget':'200'},'questions':['经营主体待确认？'],'unknowns':['经营主体未知']}
        with patch('engine.model_status',return_value={'configured':True}),patch('engine.model_call',return_value=result):runtime.step()
        facts=json.loads(self.c.execute('SELECT facts FROM projects WHERE id=?',(self.p,)).fetchone()[0]);self.assertNotEqual(facts['market'],'美国')
        self.op('/api/director/apply',id=job['id']);row=self.c.execute('SELECT * FROM projects WHERE id=?',(self.p,)).fetchone();self.assertEqual(row['budget'],20000);self.assertEqual(json.loads(row['facts'])['market'],'美国')
    def test_per_product_costs_cannot_be_reused_for_other_products(self):
        a=self.product('商品A');b=self.product('商品B');amounts={k:'0' for k in ['discount','refund','pass_through','purchase','shipping','fees','tax','other','marketing']}
        for scenario in ['正常','保守']:self.op('/api/records',kind='成本测算',product_id=a,content='TEST 测算',scenario=scenario,revenue='100',**amounts)
        gaps=business.gate(self.c,self.p,'销售准备');self.assertTrue(any('商品B' in x and '成本' in x for x in gaps));self.assertFalse(any('商品A' in x and '成本' in x for x in gaps))
        foreign=self.op('/api/projects',idea='另一个项目')['id']
        with self.assertRaisesRegex(ValueError,'不属于'):s.mutate(self.c,'/api/records',dict(project=foreign,kind='成本测算',product_id=a,content='测试'))
    def test_formal_sale_requires_complete_launch_checks_and_assets(self):
        p=self.product();gaps=business.gate(self.c,self.p,'上线');self.assertTrue(any('退款流程' in x for x in gaps));self.assertTrue(any('主图' in x for x in gaps))
    def test_actual_image_generation_preserves_reference_and_exact_detail_ratio(self):
        p=self.product();f,png=self.file();self.grant('image',calls=1,budget='1',target=p)
        with patch.dict('os.environ',{'MEDIA_API_KEY':'TEST-not-real','IMAGE_CALL_CNY':'1'}),patch('net.request',return_value=(json.dumps({'data':[{'b64_json':base64.b64encode(png).decode()}]}).encode(),{})) as call:
            job=self.op('/api/media/generate',product=p,reference=f,rights='TEST 自有素材',role='详情',count=1,prompt='测试排版');runtime.step();self.assertEqual(self.job(job['id'])['status'],'已完成',self.job(job['id'])['error']);self.assertIn(b'name="image"',call.call_args.args[1])
        asset=self.c.execute('SELECT * FROM assets WHERE project=?',(self.p,)).fetchone();self.assertEqual((asset['width'],asset['height']),(864,1536));self.assertEqual(asset['status'],'待核查')
        self.op('/api/media/approve',id=asset['id'],note='TEST 已核查一致性和权利');self.assertEqual(self.c.execute('SELECT status FROM assets WHERE id=?',(asset['id'],)).fetchone()[0],'已核查')
    def test_image_set_preflight_stops_before_any_charge_when_authorization_short(self):
        p=self.product();f,_=self.file();self.grant('image',calls=1,budget='1',target=p)
        with patch.dict('os.environ',{'MEDIA_API_KEY':'TEST','IMAGE_CALL_CNY':'1'}),patch('net.request') as request:
            job=self.op('/api/media/generate',product=p,reference=f,rights='TEST',role='整套');runtime.step();request.assert_not_called();self.assertIn('不足',self.job(job['id'])['error'])
    def test_uploaded_text_is_really_imported_and_deduplicated(self):
        f=self.op('/api/files',name='TEST.txt',mime='text/plain',data=base64.b64encode('实际上传的测试材料'.encode()).decode())['id']
        a=self.op('/api/director/import',file=f);b=self.op('/api/director/import',file=f);self.assertEqual(a['id'],b['id']);record=json.loads(self.c.execute('SELECT body FROM records WHERE id=?',(a['id'],)).fetchone()[0]);self.assertEqual(record['content'],'实际上传的测试材料')
    def test_specialist_acceptance_requires_real_delivery_not_only_status_text(self):
        task=business.task(self.c,self.p,'TEST 风险核查',s.ROLES[4]);self.c.commit()
        self.op('/api/tasks',id=task,status='待验收',note='只有文字状态')
        with self.assertRaisesRegex(ValueError,'实际交付'):self.op('/api/tasks',id=task,status='已完成',note='尝试跳过成果')
    def test_tool_jobs_restart_and_cancel_do_not_repeat_unknown_calls(self):
        job=self.op('/api/director/review');self.c.execute("UPDATE jobs SET status='执行中' WHERE id=?",(job['id'],));runtime.recover(self.c);self.c.commit();self.assertEqual(self.job(job['id'])['status'],'结果未知')
        self.assertFalse(runtime.step());self.assertEqual(self.c.execute('SELECT COUNT(*) FROM tasks WHERE project=?',(self.p,)).fetchone()[0],5)

class Growth(unittest.TestCase):
    setUp=test_workflow.Workflow.setUp
    tearDown=test_workflow.Workflow.tearDown
    op=test_storefront.Storefront.op
    publish=test_storefront.Storefront.publish
    checkout=test_storefront.Storefront.checkout
    signed=test_storefront.Storefront.signed
    def test_native_publication_approval_and_escaping(self):
        store,product=self.publish();content=self.op('/api/entities',kind='content',value={'name':'TEST <script>','product_id':product,'channel':'SEO','body':'<script>alert(1)</script>','source':'TEST 商品事实'})['id']
        a=self.op('/api/approvals',kind='内容发布',subject=content,amount='0',reason='核查本文真实内容')['id'];self.op('/api/approval/decide',id=a,status='已批准',decision='TEST 核查')
        result=self.op('/api/growth/publish',content=content,approval=a);duplicate=self.op('/api/growth/publish',content=content,approval=a);self.assertEqual(result['id'],duplicate['id']);body=growth.article(self.c,store,result['id']);self.assertNotIn(b'<script>',body);self.assertIn(b'&lt;script&gt;',body)
    def test_analytics_requires_consent_and_cannot_fake_purchase(self):
        store,product=self.publish();self.assertTrue(growth.track(self.c,store,{'consent':False})['ignored'])
        data={'consent':True,'session':'a'*32,'id':'b'*32,'event':'visit','source':'campaign'};growth.track(self.c,store,data);growth.track(self.c,store,data);self.c.commit();self.assertEqual(self.c.execute('SELECT COUNT(*) FROM visits').fetchone()[0],1)
        self.assertEqual(growth.metrics(self.c,self.p)['sessions'],0) # TEST shop traffic excluded
        with self.assertRaisesRegex(ValueError,'付款'):growth.track(self.c,store,{**data,'event':'purchase'})
    def test_first_touch_and_unattributed_orders_not_assumed_organic(self):
        store,product=self.publish();self.c.execute("UPDATE stores SET mode='正式' WHERE id=?",(store,));self.c.commit()
        data={'consent':True,'session':'a'*32,'id':'b'*32,'event':'visit','source':'newsletter','medium':'email'};growth.track(self.c,store,data);self.c.commit()
        data['source']='different';data['id']='c'*32;data['event']='add_to_cart';growth.track(self.c,store,data);self.c.commit()
        metrics=growth.metrics(self.c,self.p);self.assertEqual(metrics['sessions'],1);self.assertEqual(metrics['cart_sessions'],1);self.assertEqual(self.c.execute("SELECT source FROM visits WHERE event='add_to_cart'").fetchone()[0],'newsletter')

class Services(Helpers):
    # Keep helper fixtures, avoid calling real services.
    def tracking_action(self):
        order=self.op('/api/orders',external_id='REAL-TEST-fixture',revenue='100',cost='20')['id'];self.op('/api/order/update',id=order,status='已确认支付',refund='0',note='TEST fixture 支付证据')
        result=self.op('/api/services/prepare',kind='tracking',target=order,payload={'tracking':'TEST12345','carrier':'test-carrier'},request_key='TEST-tracking-key');return order,result
    def test_logistics_exact_approval_grant_and_exception_followup(self):
        order,result=self.tracking_action()
        with self.assertRaisesRegex(ValueError,'审批'):self.op('/api/services/execute',id=result['id'])
        self.c.rollback();self.approve(result['approval']);self.grant('logistics',target=order)
        with patch.dict('os.environ',{'AFTERSHIP_API_KEY':'TEST','LOGISTICS_CALL_CNY':'0'}),patch('integrations.send',return_value={'id':'TEST-external','tag':'Exception'}):
            job=self.op('/api/services/execute',id=result['id']);runtime.step();self.assertEqual(self.job(job['id'])['status'],'已完成',self.job(job['id'])['error'])
        self.assertTrue(self.c.execute("SELECT 1 FROM tasks WHERE project=? AND title LIKE '物流异常%'",(self.p,)).fetchone())
        with self.assertRaisesRegex(ValueError,'待执行'):self.op('/api/services/execute',id=result['id'])
    def test_external_timeout_only_reconciles_never_resends(self):
        order,result=self.tracking_action();self.approve(result['approval']);self.grant('logistics',target=order)
        with patch.dict('os.environ',{'AFTERSHIP_API_KEY':'TEST','LOGISTICS_CALL_CNY':'0'}),patch('integrations.send',side_effect=net.UnknownResult('TEST timeout')) as send:
            self.op('/api/services/execute',id=result['id']);runtime.step();self.assertEqual(send.call_count,1)
            self.assertEqual(self.c.execute('SELECT status FROM service_actions WHERE id=?',(result['id'],)).fetchone()[0],'结果未知')
            with self.assertRaisesRegex(ValueError,'待执行'):self.op('/api/services/execute',id=result['id'])
            self.c.rollback()
        with patch('integrations.reconcile',return_value={'id':'TEST-external','tag':'Delivered'}),patch('integrations.send') as send:
            self.op('/api/services/reconcile',id=result['id']);runtime.step();send.assert_not_called()
    def test_customer_reply_needs_matching_contact_and_permission(self):
        contact=self.op('/api/contacts',email='test@example.com',purpose='服务',consent='TEST 服务请求依据')['id']
        ticket=self.entity('ticket',subject='TEST 咨询',customer_ref=contact,message='问题',owner='客服',status='待处理')
        action=self.op('/api/services/prepare',kind='reply',target=ticket,payload={'contact':contact,'subject':'TEST 回复','body':'TEST 准确内容'},request_key='TEST-email')
        self.approve(action['approval']);self.op('/api/contacts/revoke',id=contact)
        with self.assertRaisesRegex(ValueError,'联系人'):self.op('/api/services/execute',id=action['id'])
    def test_review_creates_followup_tasks_without_claiming_demand_failure(self):
        job=self.op('/api/director/review');runtime.step();result=json.loads(self.job(job['id'])['result']);self.assertTrue(result['tasks']);self.assertTrue(any('不认定商品无需求' in x for x in result['decisions']))

class Reliability(Helpers):
    def refund_fixture(self):
        product=self.product();store=self.op('/api/store',name='TEST 模拟正式支付来源',mode='测试',shipping='TEST',returns='TEST',catalog=[{'product_id':product,'price':'100','stock':1}])['id'];checkout=s.uid()
        self.c.execute('INSERT INTO checkouts VALUES(?,?,?,?,?,?,?,?,?,?,?)',(checkout,self.p,store,'TEST-refund-key','[]',10000,'已支付','cs_TEST_fixture','','',s.now()));self.c.commit()
        order=self.op('/api/orders',external_id='STRIPE-'+checkout,revenue='100',cost='20')['id'];self.op('/api/order/update',id=order,status='已确认支付',refund='0',note='TEST 受控支付记录，非真实交易');return order
    def test_confirmed_refund_is_recorded_once_even_after_repeated_reconciliation(self):
        order=self.refund_fixture();action=self.op('/api/services/prepare',kind='refund',target=order,payload={'amount':'20'},request_key='TEST-refund')
        self.approve(action['approval']);self.grant('refund',target=order)
        value={'id':'re_TEST','status':'succeeded','amount':2000}
        with patch.dict('os.environ',{'REFUND_CALL_CNY':'0'}),patch('integrations.preflight'),patch('integrations.send',return_value=value):
            job=self.op('/api/services/execute',id=action['id']);runtime.step();self.assertEqual(self.job(job['id'])['status'],'已完成',self.job(job['id'])['error'])
        with patch('integrations.reconcile',return_value=value):
            self.op('/api/services/reconcile',id=action['id']);runtime.step()
        self.assertEqual(self.c.execute('SELECT refund FROM orders WHERE id=?',(order,)).fetchone()[0],2000)
    def test_unknown_refund_reserves_balance_against_a_second_request(self):
        order=self.refund_fixture();self.op('/api/services/prepare',kind='refund',target=order,payload={'amount':'70'},request_key='TEST-refund-70')
        with self.assertRaisesRegex(ValueError,'待确认退款'):self.op('/api/services/prepare',kind='refund',target=order,payload={'amount':'40'},request_key='TEST-refund-40')
    def test_media_rejects_duplicate_approved_files_as_distinct_deliverables(self):
        product=self.product();f,_=self.file();a=self.op('/api/media/attach',product=product,file=f,role='主图',rights='TEST');b=self.op('/api/media/attach',product=product,file=f,role='主图',rights='TEST')
        self.op('/api/media/approve',id=a['id'],note='TEST 核查实际文件')
        with self.assertRaisesRegex(ValueError,'重复素材'):self.op('/api/media/approve',id=b['id'],note='TEST 不得重复计数')
        self.assertFalse(media.ready(self.c,self.p))
    def test_video_create_then_poll_never_creates_a_second_paid_job(self):
        product=self.product();ref,_=self.file();self.grant('video',target=product)
        with patch.dict('os.environ',{'MEDIA_API_KEY':'TEST','VIDEO_CALL_CNY':'0'}),patch('net.request',return_value=(b'{"id":"video_TEST","status":"queued"}',{})) as request:
            task=self.op('/api/media/video',product=product,reference=ref,rights='TEST',prompt='TEST 4秒展示');runtime.step();self.assertEqual(self.job(task['id'])['status'],'已完成',self.job(task['id'])['error']);self.assertEqual(request.call_count,1)
        row=self.c.execute('SELECT * FROM video_requests').fetchone()
        with patch.dict('os.environ',{'MEDIA_API_KEY':'TEST'}),patch('net.json_request',return_value={'status':'in_progress'}),patch('net.request') as create:
            self.op('/api/media/video/poll',id=row['id']);runtime.step();create.assert_not_called()
    def test_meta_uses_official_versioned_endpoint_and_scoped_object(self):
        with patch.dict('os.environ',{'META_ACCESS_TOKEN':'TEST'}),patch('net.json_request',return_value={'id':'12345'}) as call:
            self.assertEqual(integrations.graph(self.c,self.p,'12345?fields=id')['id'],'12345');self.assertEqual(call.call_args.args[0],'https://graph.facebook.com/v24.0/12345?fields=id')
    def test_ad_cannot_pause_an_unrelated_external_object(self):
        campaign=self.op('/api/campaigns',name='TEST 活动',audience='TEST',channel='Meta',landing='TEST',hypothesis='TEST',stop_rule='TEST',budget='100')['id']
        with self.assertRaisesRegex(ValueError,'关联对象'):self.op('/api/services/prepare',kind='ad',target=campaign,payload={'operation':'pause_campaign','campaign_id':'9999999'},request_key='TEST-wrong-object')
    def test_permissioned_marketing_has_unsubscribe_and_revocation(self):
        product=self.product();content=self.entity('content',name='TEST 客户内容',product_id=product,channel='邮件',body='TEST 已核查内容',source='TEST')
        contact=self.op('/api/contacts',email='test@example.com',purpose='营销',consent='TEST 已有明确许可')['id']
        action=self.op('/api/services/prepare',kind='lifecycle',target=contact,payload={'content':content},request_key='TEST-permission-email');self.approve(action['approval']);self.grant('email',target=contact)
        with patch.dict('os.environ',{'RESEND_API_KEY':'TEST','EMAIL_FROM':'test@example.com','APP_PUBLIC_ORIGIN':'https://example.com','EMAIL_CALL_CNY':'0'}),patch('net.json_request',return_value={'id':'mail_TEST'}) as send:
            self.op('/api/services/execute',id=action['id']);runtime.step();body=send.call_args.args[1]['text'];token=body.rsplit('/unsubscribe/',1)[1];self.assertIn('确认退订'.encode(),integrations.unsubscribe(self.c,token));integrations.unsubscribe(self.c,token,True);self.c.commit()
        with self.assertRaisesRegex(ValueError,'许可'):self.op('/api/services/prepare',kind='lifecycle',target=contact,payload={'content':content},request_key='TEST-after-revoke')
    def test_sqlite_backup_restore_and_refuse_overwrite(self):
        if self.pg_schema:self.skipTest('此用例验证SQLite恢复，PostgreSQL由独立演练验证')
        from tools.maintenance import backup,restore_sqlite
        from pathlib import Path
        self.c.commit();target=Path(self.temp.name)/'backup.db';restore=Path(self.temp.name)/'restored.db'
        result=backup(target);self.assertGreater(result['bytes'],0);self.assertEqual(restore_sqlite(target,restore)['counts']['projects'],1)
        with self.assertRaisesRegex(ValueError,'已存在'):restore_sqlite(target,restore)
    def test_model_cost_quote_required_and_reservations_share_project_budget(self):
        with patch.dict('os.environ',{'MODEL_CALL_CNY':''}):
            with self.assertRaisesRegex(ValueError,'MODEL_CALL_CNY'):self.op('/api/grants',calls=1)
        self.c.rollback()
        with patch.dict('os.environ',{'MODEL_CALL_CNY':'600'}):
            with self.assertRaisesRegex(ValueError,'超过'):self.op('/api/grants',calls=2)
        self.c.rollback()
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM grants WHERE project=?',(self.p,)).fetchone()[0],0)
    def test_missing_provider_stops_before_reserving_or_sending_paid_operation(self):
        order=self.op('/api/orders',external_id='REAL-TEST-failure',revenue='100',cost='20')['id'];self.op('/api/order/update',id=order,status='已确认支付',refund='0',note='TEST')
        action=self.op('/api/services/prepare',kind='tracking',target=order,payload={'tracking':'TEST1234','carrier':'test'},request_key='TEST-missing-provider');self.approve(action['approval']);self.grant('logistics',target=order)
        with patch.dict('os.environ',{'AFTERSHIP_API_KEY':''}),patch('integrations.send') as send:
            self.op('/api/services/execute',id=action['id']);runtime.step();send.assert_not_called()
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM tool_uses WHERE tool=?',('logistics',)).fetchone()[0],0)
    def test_portable_checkpoint_restores_files_and_refuses_existing_data(self):
        from tools.checkpoint import backup,restore
        from pathlib import Path
        import support
        file_id,png=self.file();self.c.commit();archive=Path(self.temp.name)/'portable.zip';result=backup(archive);self.assertEqual(result['tables']['projects'],1)
        previous=s.DB;target=None
        if self.pg_schema:
            target=type('Target',(),{})();support.start_database(target)
        else:s.DB=Path(self.temp.name)/'portable-restored.db'
        try:
            restored=restore(archive);self.assertEqual(restored['counts']['projects'],1)
            with s.connect() as c:self.assertEqual(bytes(c.execute('SELECT data FROM files WHERE id=?',(file_id,)).fetchone()[0]),png)
            with self.assertRaisesRegex(ValueError,'非空'):restore(archive)
        finally:
            s.DB=previous
            if target:support.stop_database(target)
    def test_customer_data_is_not_default_research_context_or_unrelated_service_context(self):
        ticket=self.entity('ticket',subject='TEST 私人咨询',customer_ref='TEST-CONTACT',message='TEST 联系地址不能默认进入研究',owner='客服',status='待处理',ai_consent='允许')
        rows=business.rows(self.c,self.p);research_task={'id':'TEST-research-task','project':self.p,'role':s.ROLES[1]}
        _,selected=workflows.inputs(self.c,research_task,[],rows);self.assertFalse(any(r['kind']=='ticket' for r in selected))
        follow=self.c.execute('SELECT task FROM followups WHERE project=? AND event_key=?',(self.p,'ticket:'+ticket)).fetchone()[0]
        _,selected=workflows.inputs(self.c,{'id':follow,'project':self.p,'role':s.ROLES[13]},[],rows)
        own=[r for r in selected if r['kind']=='ticket'];self.assertEqual(len(own),1);self.assertNotIn('customer_ref',own[0]['value'])
    def test_conflicting_generated_product_cannot_overwrite_confirmed_facts(self):
        product=self.product('TEST 冲突SKU');entity=self.c.execute('SELECT * FROM entities WHERE id=?',(product,)).fetchone();value=json.loads(entity['data']);value['weight']='999g'
        result={'artifacts':[{'kind':'product','value':value,'evidence_ids':[product]}]}
        workflows.artifacts(self.c,{'id':'TEST-artifact','project':self.p,'role':s.ROLES[2]},result);self.c.commit()
        self.assertEqual(json.loads(self.c.execute('SELECT data FROM entities WHERE id=?',(product,)).fetchone()[0])['weight'],'10g');self.assertEqual(business.flow(self.c,self.p)['status'],'暂停');self.assertEqual(self.c.execute('SELECT COUNT(*) FROM fact_conflicts').fetchone()[0],1)
    def test_old_intake_cannot_replace_conditions_changed_after_analysis(self):
        self.op('/api/grants',calls=1);job=self.op('/api/director/message',content='TEST 新条件',analyze=True)
        with patch('engine.model_status',return_value={'configured':True}),patch('engine.model_call',return_value={'summary':'TEST','proposal':{'market':'美国'},'questions':[],'unknowns':[]}):runtime.step()
        row=self.c.execute('SELECT * FROM projects WHERE id=?',(self.p,)).fetchone();self.op('/api/project/update',name=row['name'],idea=row['idea'],budget='900',market='英国')
        with self.assertRaisesRegex(ValueError,'已改变'):self.op('/api/director/apply',id=job['id'])
