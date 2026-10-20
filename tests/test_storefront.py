import json,time,hashlib,hmac,unittest
from unittest.mock import patch
import server as s,storefront,finance,test_workflow

class Storefront(unittest.TestCase):
    setUp=test_workflow.Workflow.setUp
    tearDown=test_workflow.Workflow.tearDown
    def op(self,path,**d):
        self.c.commit();r=s.mutate(self.c,path,{'project':self.p,**d});self.c.commit();return r
    def publish(self,mode='测试'):
        p=self.op('/api/entities',kind='product',value=dict(name='TEST 商品',sku='T',specification='规格',material='材料',weight='1g',dimensions='1cm',usage='仅测试',limitations='非实物承诺',source='TEST 文件'))['id']
        store=self.op('/api/store',name='TEST 商店',mode=mode,shipping='测试不配送',returns='测试无收款',catalog=[{'product_id':p,'price':'100','stock':2}])
        approval=self.op('/api/approvals',kind='站点发布',subject=self.p,amount='0',reason='公开测试界面')['id']
        self.op('/api/approval/decide',id=approval,status='已批准',decision='仅测试')
        self.op('/api/store/publish',approval=approval);return store['id'],p
    def checkout(self,store,product,key='test-checkout-00001',**extra):
        self.c.commit();r=storefront.checkout(self.c,store,{'request_key':key,'items':[{'id':product,'quantity':1,'price':1}],**extra},'https://example.com');self.c.commit();return r
    def signed(self,body):
        raw=json.dumps(body).encode();stamp=int(time.time());sig=hmac.new(b'whsec_test',str(stamp).encode()+b'.'+raw,hashlib.sha256).hexdigest();return raw,'t='+str(stamp)+',v1='+sig
    def test_local_test_checkout_server_price_and_duplicate(self):
        store,product=self.publish();first=self.checkout(store,product);second=self.checkout(store,product)
        self.assertTrue(first['test']);self.assertEqual(first['reference'],second['reference'])
        orders=self.c.execute('SELECT * FROM orders WHERE project=?',(self.p,)).fetchall();self.assertEqual(len(orders),1);self.assertEqual(orders[0]['revenue'],10000);self.assertTrue(orders[0]['external_id'].startswith('TEST-'))
        self.assertEqual(finance.statement(self.c,self.p)['paid_orders'],0)
        self.c.commit();self.op('/api/store/unpublish')
        with self.assertRaisesRegex(ValueError,'下线'):self.checkout(store,product,key='test-checkout-00002')
        self.c.rollback()
    def test_payment_webhook_verified_and_idempotent(self):
        with patch('storefront.credentials',return_value=('sk_test_dummy','whsec_test','STRIPE_')):
            store,product=self.publish('支付测试')
            with patch('storefront.stripe_request',return_value={'id':'cs_test_1','url':'https://checkout.stripe.com/test_session'}):result=self.checkout(store,product)
            self.assertEqual(result['status'],'待支付')
            event={'id':'evt_test_1','livemode':False,'type':'checkout.session.completed','data':{'object':{'id':'cs_test_1','client_reference_id':result['reference'],'payment_status':'paid','amount_total':10000,'currency':'cny'}}}
            raw,sig=self.signed(event)
            self.assertTrue(storefront.webhook(self.c,store,raw,sig)['test']);self.c.commit()
            self.assertTrue(storefront.webhook(self.c,store,raw,sig)['duplicate']);self.c.commit()
            event['id']='evt_test_second';raw,sig=self.signed(event);self.assertTrue(storefront.webhook(self.c,store,raw,sig)['duplicate']);self.c.commit()
            self.assertEqual(self.c.execute('SELECT COUNT(*) FROM orders WHERE project=?',(self.p,)).fetchone()[0],1)
            self.assertEqual(finance.statement(self.c,self.p)['operating_result'],0)
    def test_unknown_payment_creation_only_queries_on_retry(self):
        with patch('storefront.credentials',return_value=('sk_test_dummy','whsec_test','STRIPE_')):
            store,product=self.publish('支付测试')
            with patch('storefront.stripe_request',side_effect=ValueError('超时')):self.assertEqual(self.checkout(store,product)['status'],'结果未知')
            with patch('storefront.stripe_request',return_value={'data':[]}) as call:
                with self.assertRaisesRegex(ValueError,'不重复'):self.checkout(store,product)
                self.c.rollback();self.assertEqual(call.call_args.args[2],'checkout/sessions?limit=100')
    def test_no_publish_without_approval_or_validation(self):
        with self.assertRaisesRegex(ValueError,'草稿'):self.op('/api/store/publish',approval='missing')
        self.c.rollback();store,product=self.publish()
        self.op('/api/store',name='正式店',mode='正式',shipping='配送政策',returns='退换政策',catalog=[{'product_id':product,'price':'100','stock':2}])
        a=self.op('/api/approvals',kind='站点发布',subject=self.p,amount='0',reason='正式销售申请')['id'];self.op('/api/approval/decide',id=a,status='已批准',decision='仍需要程序闸口')
        with self.assertRaisesRegex(ValueError,'条件不满足'):self.op('/api/store/publish',approval=a)
        self.c.rollback()
    def test_invalid_webhook_cannot_create_orders(self):
        store,_=self.publish()
        with patch('storefront.credentials',return_value=('sk_test_dummy','whsec_test','STRIPE_')):
            with self.assertRaisesRegex(ValueError,'签名'):storefront.webhook(self.c,store,b'{}','t=1,v1=wrong')
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM orders').fetchone()[0],0)
