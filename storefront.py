"""Built-in storefront with test checkout and optional Stripe-hosted payment."""
import os,json,re,hashlib,hmac,time,urllib.request,urllib.error
from urllib.parse import urlencode
import server as s

def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS stores(id TEXT PRIMARY KEY,project TEXT UNIQUE REFERENCES projects(id),name TEXT,mode TEXT,status TEXT,shipping TEXT,returns TEXT,catalog TEXT,updated TEXT);
    CREATE TABLE IF NOT EXISTS checkouts(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),store TEXT REFERENCES stores(id),request_key TEXT,items TEXT,amount INTEGER,status TEXT,external_id TEXT,url TEXT,error TEXT,created TEXT,UNIQUE(store,request_key));
    CREATE TABLE IF NOT EXISTS payment_events(project TEXT REFERENCES projects(id),event_id TEXT,created TEXT,PRIMARY KEY(project,event_id));
    CREATE TABLE IF NOT EXISTS shop_messages(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),store TEXT,ticket TEXT,contact TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS order_checks(order_id TEXT PRIMARY KEY REFERENCES orders(id),verified INTEGER,source TEXT);''')

def credentials(c,p):
    owner=c.execute('SELECT owner FROM project_owners WHERE project=?',(p,)).fetchone();prefix='STRIPE_' if not owner or owner['owner']=='root' else 'STRIPE_'+owner['owner'].upper()+'_'
    return os.environ.get(prefix+'SECRET_KEY',''),os.environ.get(prefix+'WEBHOOK_SECRET',''),prefix

def shipping_country(c,p):
    project=c.execute('SELECT facts FROM projects WHERE id=?',(p,)).fetchone()
    market=json.loads(project['facts']).get('market','').strip()
    country={'美国':'US','英国':'GB','加拿大':'CA','澳大利亚':'AU','中国':'CN','德国':'DE','法国':'FR'}.get(market,market.upper())
    return country if re.fullmatch('[A-Z]{2}',country) else None

def public_store(c,store_id):
    row=c.execute("SELECT * FROM stores WHERE id=? AND status='已发布'",(store_id,)).fetchone()
    if not row:return None
    catalog=json.loads(row['catalog']);products=[]
    reserved={}
    if row['mode']!='测试':
        for checkout in c.execute("SELECT items FROM checkouts WHERE store=? AND status IN ('发送中','结果未知','待支付','已支付')",(store_id,)):
            for item in json.loads(checkout['items']):
                if item.get('catalog_version')==row['updated']:reserved[item['id']]=reserved.get(item['id'],0)+item['quantity']
    for entry in catalog:
        r=c.execute("SELECT * FROM entities WHERE id=? AND project=? AND kind='product'",(entry['product_id'],row['project'])).fetchone()
        if not r or r['version']!=entry['version']:continue
        v=json.loads(r['data']);products.append({'id':r['id'],'name':v['name'],'sku':v['sku'],'specification':v['specification'],'material':v['material'],'usage':v['usage'],'limitations':v['limitations'],'price':entry['price'],'stock':max(0,entry['stock']-reserved.get(r['id'],0))})
    import media
    for product in products:
        product['videos']=[a['id'] for a in c.execute("SELECT a.id FROM assets a JOIN entities e ON e.id=a.product WHERE a.project=? AND a.product=? AND a.product_version=e.version AND a.status='已核查' AND a.role='视频'",(row['project'],product['id']))]
        product['images']=[{'id':a['id'],'role':a['role']} for a in c.execute("SELECT a.id,a.role FROM assets a JOIN entities e ON e.id=a.product WHERE a.project=? AND a.product=? AND a.product_version=e.version AND a.status='已核查' AND a.role IN ('主图','详情') ORDER BY a.created",(row['project'],product['id']))]
    articles=[{'id':r['id'],'title':r['title']} for r in c.execute("SELECT r.id,p.title FROM article_routes r JOIN publications p ON p.id=r.current_publication WHERE r.store=? AND r.status='已发布' AND p.status='已发布'",(row['id'],))]
    facts=json.loads(c.execute('SELECT facts FROM projects WHERE id=?',(row['project'],)).fetchone()[0]);language='en' if facts.get('language','').lower() in {'en','english','英语'} else 'zh'
    return {'language':language,'articles':articles,'id':row['id'],'name':row['name'],'mode':row['mode'],'shipping':row['shipping'],'returns':row['returns'],'products':products}

def mutate(c,path,d):
    p=d['project'];s.begin_write(c)
    if path=='/api/store':
        mode=d.get('mode')
        if mode not in {'测试','支付测试','正式'}:raise ValueError('商店模式无效')
        entries=d.get('catalog')
        if not isinstance(entries,list) or not entries or len(entries)>50:raise ValueError('请选择1–50个已建立主档的商品')
        catalog=[];seen=set()
        for entry in entries:
            i=entry.get('product_id');product=c.execute("SELECT * FROM entities WHERE id=? AND project=? AND kind='product'",(i,p)).fetchone()
            if not product or i in seen:raise ValueError('商品不存在或重复')
            seen.add(i);price=s.money(entry.get('price'));stock=entry.get('stock')
            if price<=0 or not isinstance(stock,int) or not 0<=stock<=1000000:raise ValueError('售价必须大于0，库存必须是非负整数')
            catalog.append({'product_id':i,'version':product['version'],'price':price,'stock':stock})
        old=c.execute('SELECT id FROM stores WHERE project=?',(p,)).fetchone();i=old['id'] if old else s.uid()
        c.execute('INSERT INTO stores VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(project) DO UPDATE SET name=excluded.name,mode=excluded.mode,status=excluded.status,shipping=excluded.shipping,returns=excluded.returns,catalog=excluded.catalog,updated=excluded.updated',(i,p,s.required(d,'name'),mode,'草稿',s.required(d,'shipping'),s.required(d,'returns'),json.dumps(catalog),s.now()))
        s.log(c,p,'保存内置商店草稿；尚未公开销售');return {'id':i}
    store=c.execute('SELECT * FROM stores WHERE project=?',(p,)).fetchone()
    if not store:raise ValueError('请先保存商店草稿')
    if path=='/api/store/prepare':
        import business
        return business.mutate(c,'/api/approvals',{'project':p,'kind':'站点发布','subject':p,'amount':'0','reason':'核查商店模式、商品、售价库存、配送与退换政策：'+json.dumps({k:store[k] for k in ['name','mode','shipping','returns','catalog']},ensure_ascii=False)})
    if path=='/api/store/unpublish':
        c.execute("UPDATE stores SET status='已下线',updated=? WHERE project=?",(s.now(),p));s.log(c,p,'内置商店已下线，禁止新结账；已发生支付仍接收核对');return {'ok':True}
    if path=='/api/store/publish':
        approval=c.execute("SELECT * FROM approvals WHERE id=? AND project=? AND kind='站点发布' AND subject=? AND status='已批准'",(d.get('approval'),p,p)).fetchone()
        if not approval or approval['updated']<store['updated']:raise ValueError('需要在草稿保存后批准对应项目ID的站点发布审批')
        if store['mode']=='正式':
            import business
            missing=business.gate(c,p,'上线')
            if missing:raise ValueError('正式销售条件不满足：'+'；'.join(missing))
            if not shipping_country(c,p):raise ValueError('请先将项目市场设为明确的两位发货国家码，例如US；不能用未知市场开启实物销售')
            key,secret,_=credentials(c,p)
            if not key.startswith('sk_live_') or not secret:raise ValueError('正式销售需要Stripe正式密钥和Webhook签名密钥')
            account=stripe_request(c,p,'account')
            if not account.get('id') or not account.get('charges_enabled'):raise ValueError('Stripe经营主体尚未启用真实收款')
        if store['mode']=='支付测试':
            key,secret,_=credentials(c,p)
            if not key.startswith('sk_test_') or not secret:raise ValueError('支付测试需要Stripe测试密钥和Webhook签名密钥')
        c.execute("UPDATE stores SET status='已发布',updated=? WHERE project=?",(s.now(),p));s.log(c,p,'公开内置商店，模式：'+store['mode']);return {'url':'/shop/'+store['id'],'mode':store['mode']}
    raise ValueError('未知商店操作')

def stripe_request(c,p,endpoint,data=None,idempotency=None):
    key,_,_=credentials(c,p)
    if not key:raise ValueError('Stripe未配置')
    headers={'Authorization':'Bearer '+key}
    if idempotency:headers['Idempotency-Key']=idempotency
    body=urlencode(data).encode() if data is not None else None
    if body is not None:headers['Content-Type']='application/x-www-form-urlencoded'
    req=urllib.request.Request('https://api.stripe.com/v1/'+endpoint,data=body,headers=headers)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    try:
        with urllib.request.build_opener(NoRedirect).open(req,timeout=25) as r:return json.loads(r.read(1000000))
    except (urllib.error.URLError,TimeoutError,ValueError):raise ValueError('支付服务结果未确认，请查询操作状态；未自动重复提交') from None

def checkout(c,store_id,d,origin):
    s.begin_write(c);store=public_store(c,store_id)
    if not store:raise ValueError('商店不存在或已下线')
    raw=c.execute('SELECT * FROM stores WHERE id=?',(store_id,)).fetchone();p=raw['project']
    import business
    if business.flow(c,p)['status']!='进行中':raise ValueError('项目暂停，新结账已停止')
    request_key=s.required(d,'request_key')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{16,100}',request_key):raise ValueError('结账操作标识无效')
    old=c.execute('SELECT * FROM checkouts WHERE store=? AND request_key=?',(store_id,request_key)).fetchone()
    if old:
        if old['status'] in {'测试完成','已支付'}:return {'status':old['status'],'reference':old['id'],'url':old['url']}
        if old['status']=='待支付' and old['url']:return {'status':'待支付','url':old['url'],'reference':old['id']}
        if old['status'] in {'发送中','结果未知'}:
            c.commit();result=stripe_request(c,p,'checkout/sessions?limit=100')
            found=next((x for x in result.get('data',[]) if x.get('client_reference_id')==old['id']),None)
            if not found:raise ValueError('上次结账结果未知，暂不重复创建，请联系商店负责人核查')
            c.execute("UPDATE checkouts SET status='待支付',external_id=?,url=?,error='' WHERE id=?",(found['id'],found.get('url',''),old['id']));return {'status':'待支付','url':found.get('url',''),'reference':old['id']}
        raise ValueError('上次操作未确认，请联系商店负责人')
    items=d.get('items')
    if not isinstance(items,list) or not 1<=len(items)<=50:raise ValueError('购物车不能为空')
    products={r['id']:r for r in store['products']};priced=[];seen=set()
    for item in items:
        product=products.get(item.get('id'));qty=item.get('quantity')
        if not product or product['id'] in seen or not isinstance(qty,int) or isinstance(qty,bool) or not 1<=qty<=min(product['stock'],99):raise ValueError('商品、库存或数量已变化，请刷新购物车')
        seen.add(product['id']);priced.append({'id':product['id'],'name':product['name'],'quantity':qty,'price':product['price'],'catalog_version':raw['updated'],'mode':store['mode']})
    total=sum(x['quantity']*x['price'] for x in priced)
    if total>100000000:raise ValueError('订单金额超过单笔上限')
    i=s.uid();c.execute('INSERT INTO checkouts VALUES(?,?,?,?,?,?,?,?,?,?,?)',(i,p,store_id,request_key,json.dumps(priced,ensure_ascii=False),total,'发送中','','','',s.now()))
    import growth
    growth.bind_checkout(c,store_id,i,d.get('attribution',{}))
    if store['mode']=='测试':
        order=s.uid();c.execute('INSERT INTO orders VALUES(?,?,?,?,?,0,?,?,?)',(order,p,'TEST-SHOP-'+i,total,0,'待核查','',s.now()))
        c.execute('INSERT INTO order_checks VALUES(?,0,?)',(order,'本地测试订单，不是支付'))
        c.execute("UPDATE checkouts SET status='测试完成' WHERE id=?",(i,));business.followup(c,p,'shop-order:'+i,'测试订单核查：'+i,s.ROLES[12]);s.log(c,p,'测试下单完成，未收款、未扣真实库存、未计入经营收入');return {'status':'测试完成','reference':i,'test':True}
    key,secret,_=credentials(c,p)
    if not secret or not key.startswith('sk_live_' if store['mode']=='正式' else 'sk_test_'):raise ValueError('支付配置与商店模式不符')
    if not origin.startswith('https://'):raise ValueError('真实支付回跳地址需要配置HTTPS公网来源')
    c.commit()
    payload={'mode':'payment','client_reference_id':i,'success_url':origin+'/shop/'+store_id+'?payment=returned','cancel_url':origin+'/shop/'+store_id+'?payment=cancelled','metadata[checkout_id]':i}
    country=shipping_country(c,p)
    if country:payload['shipping_address_collection[allowed_countries][0]']=country
    for index,item in enumerate(priced):
        pre='line_items['+str(index)+']';payload[pre+'[price_data][currency]']='cny';payload[pre+'[price_data][unit_amount]']=item['price'];payload[pre+'[price_data][product_data][name]']=item['name'];payload[pre+'[quantity]']=item['quantity']
    try:
        result=stripe_request(c,p,'checkout/sessions',payload,'venture-'+i)
        if not result.get('id') or not str(result.get('url','')).startswith('https://checkout.stripe.com/'):raise ValueError('支付服务未返回可验证的结账地址')
        c.execute("UPDATE checkouts SET status='待支付',external_id=?,url=? WHERE id=?",(result['id'],result['url'],i));return {'status':'待支付','url':result['url'],'reference':i}
    except ValueError as e:
        c.execute("UPDATE checkouts SET status='结果未知',error=? WHERE id=?",(str(e),i));return {'status':'结果未知','error':str(e),'reference':i}

def webhook(c,store_id,body,signature):
    store=c.execute('SELECT * FROM stores WHERE id=?',(store_id,)).fetchone()
    if not store:raise ValueError('商店不存在')
    _,secret,_=credentials(c,store['project'])
    if not secret:raise ValueError('Webhook尚未配置')
    parts={}
    for x in signature.split(','):
        k,_,v=x.partition('=');parts.setdefault(k,[]).append(v)
    try:timestamp=int(parts['t'][0])
    except (KeyError,ValueError,IndexError):raise ValueError('签名无效')
    _,_,prefix=credentials(c,store['project']);secrets_to_check=[secret,os.environ.get(prefix+'PREVIOUS_WEBHOOK_SECRET','')]
    expected=[hmac.new(value.encode(),str(timestamp).encode()+b'.'+body,hashlib.sha256).hexdigest().encode() for value in secrets_to_check if value]
    if abs(time.time()-timestamp)>300 or not any(hmac.compare_digest(digest,v.encode()) for digest in expected for v in parts.get('v1',[])):raise ValueError('签名无效或已过期')
    event=json.loads(body)
    if event.get('type') not in {'checkout.session.completed','checkout.session.async_payment_succeeded'}:return {'ignored':True}
    session=event.get('data',{}).get('object',{})
    if session.get('payment_status')!='paid':return {'ignored':True}
    s.begin_write(c)
    if c.execute('SELECT 1 FROM payment_events WHERE project=? AND event_id=?',(store['project'],event['id'])).fetchone():return {'duplicate':True}
    checkout=c.execute('SELECT * FROM checkouts WHERE id=? AND store=?',(session.get('client_reference_id'),store_id)).fetchone()
    if not checkout or session.get('amount_total')!=checkout['amount'] or session.get('currency')!='cny':raise ValueError('支付金额、币种或订单归属不一致')
    if checkout['external_id'] and checkout['external_id']!=session.get('id'):raise ValueError('支付会话与原始操作不一致')
    if checkout['status']=='已支付':
        c.execute('INSERT INTO payment_events VALUES(?,?,?)',(store['project'],event['id'],s.now()));return {'duplicate':True}
    if not isinstance(event.get('livemode'),bool):raise ValueError('支付事件缺少明确的测试/正式模式')
    is_test=not event['livemode'];original_modes={x.get('mode') for x in json.loads(checkout['items'])}
    if original_modes not in ({'正式'},{'支付测试'}):raise ValueError('原始结账模式不可核对')
    if is_test!=(original_modes=={'支付测试'}):raise ValueError('支付事件与原始结账测试/正式模式不一致')
    order=s.uid();reference=('TEST-STRIPE-' if is_test else 'STRIPE-')+checkout['id']
    c.execute('INSERT INTO orders VALUES(?,?,?,?,?,0,?,?,?)',(order,store['project'],reference,checkout['amount'],0,'待核查','',s.now()))
    c.execute('INSERT INTO order_checks VALUES(?,0,?)',(order,'Stripe签名验证通过；税费与商品成本仍待核对'))
    c.execute("UPDATE checkouts SET status='已支付',external_id=?,error='' WHERE id=?",(session.get('id',''),checkout['id']))
    c.execute('INSERT INTO payment_events VALUES(?,?,?)',(store['project'],event['id'],s.now()))
    import business
    import growth
    growth.purchase(c,checkout,order,is_test)
    business.followup(c,store['project'],'shop-order:'+checkout['id'],'核查支付与履约：'+reference,s.ROLES[12]);s.log(c,store['project'],'接收已验证支付事件；'+('测试支付，不计真实经营' if is_test else '真实支付，成本和税费待核查'));return {'ok':True,'test':is_test}

def fulfillment(c,p,order_id):
    order=c.execute('SELECT * FROM orders WHERE id=? AND project=?',(order_id,p)).fetchone()
    if not order or order['status'] in {'待核查','已取消'}:raise ValueError('请先核查支付与订单，再读取履约所需资料')
    if order['external_id'].startswith('STRIPE-'):
        checkout=c.execute("SELECT * FROM checkouts WHERE id=? AND project=? AND status='已支付'",(order['external_id'].removeprefix('STRIPE-'),p)).fetchone()
        if not checkout:raise ValueError('没有可核对的原始支付会话')
        session=stripe_request(c,p,'checkout/sessions/'+checkout['external_id'])
        shipping=session.get('shipping_details') or session.get('collected_information',{}).get('shipping_details') or {}
        address=shipping.get('address') or {}
        return {'name':shipping.get('name'),'address':{k:address.get(k) for k in ['country','state','city','postal_code','line1','line2']},'items':json.loads(checkout['items']),'basis':'按本次明确请求读取履约所需信息；不保存到模型资料或普通日志，地址缺失时需联系客户确认'}
    if order['external_id'].startswith('WOOCOMMERCE-'):
        import connectors
        external=order['external_id'].removeprefix('WOOCOMMERCE-')
        if not external.isdigit():raise ValueError('来源订单标识无效')
        connector=c.execute('SELECT status FROM connectors WHERE project=?',(p,)).fetchone()
        if not connector or connector['status'] not in {'可读取','可执行','部分可用'}:raise ValueError('站点读取授权不可用')
        result=connectors.request(c,p,'/wp-json/wc/v3/orders/'+external);address=result.get('shipping',{})
        return {'name':str(address.get('first_name',''))+' '+str(address.get('last_name','')),'address':{k:address.get(k) for k in ['country','state','city','postcode','address_1','address_2']},'items':[{'name':r.get('name'),'sku':r.get('sku'),'quantity':r.get('quantity')} for r in result.get('line_items',[])],'basis':'只为本次履约核对读取，不默认发送给模型'}
    raise ValueError('人工来源订单请根据原始订单凭据核对地址，不猜测客户信息')

def contact(c,store_id,d):
    s.begin_write(c);store=c.execute("SELECT * FROM stores WHERE id=? AND status IN ('已发布','已下线','待重新核查')",(store_id,)).fetchone()
    if not store:raise ValueError('商店服务入口暂不可用')
    key=s.required(d,'request_key')
    if not re.fullmatch('[a-f0-9-]{16,64}',key) or d.get('website'):raise ValueError('服务请求标识无效')
    unique=store_id+':'+key;old=c.execute('SELECT * FROM shop_messages WHERE id=?',(unique,)).fetchone()
    if old:return {'reference':old['ticket'],'duplicate':True}
    subject=s.required(d,'subject');message=s.required(d,'message')
    if len(message)<5 or len(message)>3000 or len(subject)>150:raise ValueError('问题主题最多150字，内容为5–3000字')
    import integrations,business
    customer=integrations.mutate(c,'/api/contacts',{'project':store['project'],'email':s.required(d,'email'),'purpose':'服务','consent':'客户主动提交此次商店服务请求；仅用于本次回复，不授权营销'})['id']
    ticket=business.mutate(c,'/api/entities',{'project':store['project'],'kind':'ticket','value':{'subject':subject,'customer_ref':customer,'message':message,'owner':'客服与客户生命周期','status':'待处理','ai_consent':'允许' if d.get('ai_consent') is True else '不允许'}})['id']
    c.execute('INSERT INTO shop_messages VALUES(?,?,?,?,?,?)',(unique,store['project'],store_id,ticket,customer,s.now()));s.log(c,store['project'],'商店收到真实渠道服务请求，已建工单；不代表问题已经解决');return {'reference':ticket}
