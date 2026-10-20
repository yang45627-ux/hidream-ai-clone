"""Real reference-image generation, complete image sets and asynchronous video assets."""
import base64,io,json,secrets,hashlib
import server as s,runtime,net

def setup(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS assets(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),product TEXT REFERENCES entities(id),product_version INTEGER,role TEXT,file_id TEXT REFERENCES files(id),width INTEGER,height INTEGER,status TEXT,rights TEXT,created TEXT);
    CREATE TABLE IF NOT EXISTS asset_fingerprints(asset TEXT PRIMARY KEY REFERENCES assets(id),project TEXT REFERENCES projects(id),digest TEXT);
    CREATE TABLE IF NOT EXISTS video_requests(id TEXT PRIMARY KEY,project TEXT REFERENCES projects(id),product TEXT REFERENCES entities(id),product_version INTEGER,external_id TEXT,status TEXT,job TEXT,updated TEXT);''')

def ready(c,p,product=None):
    products=[product] if product else [r['id'] for r in c.execute("SELECT id FROM entities WHERE project=? AND kind='product'",(p,))]
    if not products:return False
    for i in products:
        row=c.execute('SELECT version FROM entities WHERE id=? AND project=?',(i,p)).fetchone()
        if not row:return False
        counts={role:c.execute("SELECT COUNT(DISTINCT f.digest) FROM assets a JOIN asset_fingerprints f ON f.asset=a.id WHERE a.project=? AND a.product=? AND a.product_version=? AND a.role=? AND a.status='已核查'",(p,i,row['version'],role)).fetchone()[0] for role in ['主图','详情']}
        if counts['主图']<5 or counts['详情']<10:return False
    return True

def multipart(fields,files):
    boundary='venture'+secrets.token_hex(16);chunks=[]
    for name,value in fields.items():chunks.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+name+'"\r\n\r\n'+str(value)+'\r\n').encode())
    for name,mime,raw in files:
        chunks.extend([('--'+boundary+'\r\nContent-Disposition: form-data; name="'+name+'"; filename="reference.png"\r\nContent-Type: '+mime+'\r\n\r\n').encode(),raw,b'\r\n'])
    chunks.append(('--'+boundary+'--\r\n').encode());return b''.join(chunks),'multipart/form-data; boundary='+boundary

def normalize(raw,role):
    from PIL import Image,ImageOps
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.width<256 or im.height<256:raise ValueError('生成图片分辨率不足，不能冒充合格商品素材')
            if im.width*im.height>25000000:raise ValueError('图片尺寸超过上限')
            im.load();im=im.convert('RGB');size=(1024,1024) if role=='主图' else (864,1536)
            # Pad rather than crop product identity or text. Details are exactly 9:16.
            im=ImageOps.contain(im,size)
            canvas=Image.new('RGB',size,'white');canvas.paste(im,((size[0]-im.width)//2,(size[1]-im.height)//2));out=io.BytesIO();canvas.save(out,format='PNG');return out.getvalue(),size
    except (OSError,SyntaxError,Image.DecompressionBombError):raise ValueError('生成结果不是可读取的安全图片') from None

def reference(c,p,d):
    product=c.execute("SELECT * FROM entities WHERE id=? AND project=? AND kind='product'",(d.get('product'),p)).fetchone()
    f=c.execute('SELECT * FROM files WHERE id=? AND project=?',(d.get('reference'),p)).fetchone()
    if not product or not f or f['mime'] not in {'image/png','image/jpeg','image/webp'}:raise ValueError('请选择本项目商品和真实商品参考图')
    from PIL import Image
    try:
        with Image.open(io.BytesIO(bytes(f['data']))) as image:
            if image.width<256 or image.height<256 or image.width*image.height>25000000:raise ValueError('参考图至少256×256，且总像素不超过2500万')
            image.verify()
    except (OSError,SyntaxError,Image.DecompressionBombError):raise ValueError('参考图不是可安全解码的有效图片') from None
    return product,f

def mutate(c,path,d):
    p=d['project']
    if path in {'/api/media/generate','/api/media/video'}:
        product,f=reference(c,p,d);rights=s.required(d,'rights')
        if path.endswith('video'):
            return runtime.enqueue(c,p,'video',{'product':product['id'],'version':product['version'],'reference':f['id'],'rights':rights,'prompt':d.get('prompt','商品展示，保持商品身份一致')},key='video:'+s.uid())
        role=d.get('role','整套');count=d.get('count',1)
        if role not in {'主图','详情','整套'} or not isinstance(count,int) or isinstance(count,bool) or not 1<=count<=15:raise ValueError('素材规格或数量无效')
        roles=['主图']*5+['详情']*10 if role=='整套' else [role]*count
        return runtime.enqueue(c,p,'media',{'product':product['id'],'version':product['version'],'reference':f['id'],'rights':rights,'roles':roles,'prompt':d.get('prompt','真实商品展示，不编造功能或认证')},key='media:'+s.uid())
    if path=='/api/media/attach':
        product,f=reference(c,p,{'product':d.get('product'),'reference':d.get('file')});role=d.get('role');rights=s.required(d,'rights')
        if role not in {'主图','详情'}:raise ValueError('素材用途无效')
        raw,size=normalize(bytes(f['data']),role)
        return store_asset(c,p,product['id'],product['version'],role,raw,size,rights)
    if path=='/api/media/approve':
        row=c.execute('SELECT * FROM assets WHERE id=? AND project=?',(d.get('id'),p)).fetchone()
        if not row:raise ValueError('素材不存在')
        product=c.execute('SELECT version FROM entities WHERE id=?',(row['product'],)).fetchone()
        if product['version']!=row['product_version']:raise ValueError('商品已更正，请重新核查素材')
        s.required(d,'note')
        own=c.execute('SELECT digest FROM asset_fingerprints WHERE asset=?',(row['id'],)).fetchone()
        if own and c.execute("SELECT 1 FROM assets a JOIN asset_fingerprints f ON f.asset=a.id WHERE a.project=? AND a.product=? AND a.product_version=? AND a.role=? AND a.status='已核查' AND a.id!=? AND f.digest=?",(p,row['product'],row['product_version'],row['role'],row['id'],own['digest'])).fetchone():raise ValueError('重复素材不能作为独立图片交付，请选择不同的有效文件')
        c.execute("UPDATE assets SET status='已核查' WHERE id=?",(row['id'],));s.log(c,p,'用户核查素材身份、文字、使用权与尺寸：'+d['note']);return {'ok':True}
    if path=='/api/media/video/poll':
        row=c.execute('SELECT * FROM video_requests WHERE id=? AND project=?',(d.get('id'),p)).fetchone()
        if not row:raise ValueError('视频请求不存在')
        return runtime.enqueue(c,p,'video',{'poll':row['id']},key='video-poll:'+row['id']+':'+s.uid())
    raise ValueError('未知素材操作')

def store_asset(c,p,product,version,role,raw,size,rights,mime='image/png'):
    i=s.uid();f=s.uid();c.execute('INSERT INTO files VALUES(?,?,?,?,?,?)',(f,p,role+'-'+i+('.mp4' if mime=='video/mp4' else '.png'),mime,raw,s.now()))
    c.execute('INSERT INTO assets VALUES(?,?,?,?,?,?,?,?,?,?,?)',(i,p,product,version,role,f,size[0],size[1],'待核查',rights,s.now()));c.execute('INSERT INTO asset_fingerprints VALUES(?,?,?)',(i,p,hashlib.sha256(raw).hexdigest()));return {'id':i,'file':f}

def run(job,payload):
    p=job['project'];created=[]
    with s.connect() as c:
        runtime.check(c,job);key=runtime.env(c,p,'MEDIA_API_KEY') or runtime.env(c,p,'AI_API_KEY') or runtime.env(c,p,'OPENAI_API_KEY')
        if not key:raise ValueError('请在服务端配置 MEDIA_API_KEY；不会把制作方案当作生成文件')
        base=(runtime.env(c,p,'MEDIA_BASE_URL') or ('https://api.openai.com/v1' if runtime.env(c,p,'MEDIA_API_KEY') else runtime.env(c,p,'AI_BASE_URL') or 'https://api.openai.com/v1')).rstrip('/')
        if job['kind']=='video' and payload.get('poll'):
            row=c.execute('SELECT * FROM video_requests WHERE id=? AND project=?',(payload['poll'],p)).fetchone()
            if not row:raise ValueError('视频请求不存在')
            if row['status']=='已取回':return {'summary':'视频文件已取回，不重复下载'}
            value=net.json_request(base+'/videos/'+row['external_id'],headers={'Authorization':'Bearer '+key})
            state=value.get('status')
            if state!='completed':
                c.execute('UPDATE video_requests SET status=?,updated=? WHERE id=?',(str(state),s.now(),row['id']));return {'summary':'视频状态：'+str(state),'request':row['id']}
            raw,_=net.request(base+'/videos/'+row['external_id']+'/content',headers={'Authorization':'Bearer '+key},limit=32*1024*1024)
            if raw[4:8]!=b'ftyp':raise ValueError('视频结果不是可识别MP4')
            s.begin_write(c);runtime.check(c,job)
            if c.execute('SELECT status FROM video_requests WHERE id=?',(row['id'],)).fetchone()[0]=='已取回':return {'summary':'视频已经取回，不重复存档'}
            product=c.execute('SELECT version FROM entities WHERE id=?',(row['product'],)).fetchone()
            if product['version']!=row['product_version']:raise ValueError('商品版本变化，视频需要重新核查，未发布')
            asset=store_asset(c,p,row['product'],row['product_version'],'视频',raw,video_dimensions(raw),'生成请求的授权材料见任务记录',mime='video/mp4')
            c.execute("UPDATE video_requests SET status='已取回',updated=? WHERE id=?",(s.now(),row['id']));return {'summary':'已取回实际视频，待人工核查','assets':[asset['id']]}
        product,f=reference(c,p,payload)
        if job['kind']=='media':
            grant=runtime.available(c,p,'image',product['id'])
            charge=runtime.env(c,p,'IMAGE_CALL_CNY')
            if not grant or not charge or grant['remaining']<len(payload['roles']) or grant['budget']-grant['spent']<s.money(charge)*len(payload['roles']):raise ValueError('本次整套素材的调用次数或费用预留不足，尚未发送生成请求')
        if product['version']!=payload['version']:raise ValueError('商品已更正，停止基于旧事实生成')
        facts=json.loads(product['data']);reference_bytes=bytes(f['data']);mime=f['mime']
        if job['kind']=='video':
            runtime.consume(c,p,'video',job['id'],product['id'])
            from PIL import Image,ImageOps
            with Image.open(io.BytesIO(reference_bytes)) as im:
                im=ImageOps.contain(im.convert('RGB'),(720,1280));canvas=Image.new('RGB',(720,1280),'white');canvas.paste(im,((720-im.width)//2,(1280-im.height)//2));out=io.BytesIO();canvas.save(out,'PNG')
            body,content_type=multipart({'model':runtime.env(c,p,'VIDEO_MODEL') or 'sora-2','prompt':str(payload['prompt'])[:3000]+'。只展示参考商品，不改变结构、品牌或虚构功能。','size':'720x1280','seconds':'4'},[('input_reference','image/png',out.getvalue())])
            raw,_=net.request(base+'/videos',body,{'Authorization':'Bearer '+key,'Content-Type':content_type},timeout=60)
            try:value=json.loads(raw);external=value['id']
            except (ValueError,KeyError):raise net.UnknownResult('视频创建结果未知，请在服务商核查，不重复生成') from None
            i=s.uid();runtime.check(c,job);c.execute('INSERT INTO video_requests VALUES(?,?,?,?,?,?,?,?)',(i,p,product['id'],product['version'],external,str(value.get('status','queued')),job['id'],s.now()))
            return {'summary':'视频生成请求已登记，稍后查询并取回文件','request':i,'external_id':external}
    for index,role in enumerate(payload['roles']):
        with s.connect() as c:
            runtime.check(c,job)
            current=c.execute('SELECT version FROM entities WHERE id=?',(product['id'],)).fetchone()
            if current['version']!=payload['version']:raise ValueError('商品版本已变化，停止剩余素材调用')
            runtime.consume(c,p,'image',job['id'],product['id'])
            model=runtime.env(c,p,'IMAGE_MODEL') or 'gpt-image-1'
        prompt='为参考实物制作'+role+'，第'+str(index+1)+'张。保持商品形状、结构、颜色、标识一致，不捏造认证或功能。禁止加入没有事实依据的宣传。商品事实：'+json.dumps(facts,ensure_ascii=False)+'。设计要求：'+str(payload.get('prompt',''))[:3000]
        body,content_type=multipart({'model':model,'prompt':prompt,'n':1,'size':'1024x1024' if role=='主图' else '1024x1536'},[('image',mime,reference_bytes)])
        raw,_=net.request(base+'/images/edits',body,{'Authorization':'Bearer '+key,'Content-Type':content_type},limit=15*1024*1024,timeout=60)
        try:result=json.loads(raw);image=base64.b64decode(result['data'][0]['b64_json'],validate=True)
        except (ValueError,KeyError,IndexError,TypeError):raise net.UnknownResult('图像服务结果无法核验；已请求部分需核对，不自动重发') from None
        normalized,size=normalize(image,role)
        with s.connect() as c:
            s.begin_write(c);runtime.check(c,job)
            if c.execute('SELECT version FROM entities WHERE id=?',(product['id'],)).fetchone()[0]!=payload['version']:raise ValueError('商品已更正，本次旧事实生成结果不用于页面')
            created.append(store_asset(c,p,product['id'],payload['version'],role,normalized,size,payload['rights'])['id'])
            c.execute('UPDATE jobs SET result=?,updated=? WHERE id=?',(json.dumps({'summary':'正在生成素材，已保存部分文件','assets':created},ensure_ascii=False),s.now(),job['id']))
    return {'summary':'实际素材已生成，待核查商品一致性、文字与权利','assets':created,'unknowns':['生成不能代替实物核验；通过人工核查后才可用于正式页面']}

def public_asset(c,store_id,asset_id):
    store=c.execute("SELECT * FROM stores WHERE id=? AND status='已发布'",(store_id,)).fetchone()
    if not store:return None
    row=c.execute("SELECT a.*,f.data,f.mime FROM assets a JOIN files f ON f.id=a.file_id JOIN entities e ON e.id=a.product WHERE a.id=? AND a.project=? AND a.status='已核查' AND a.product_version=e.version",(asset_id,store['project'])).fetchone()
    if not row or not any(x['product_id']==row['product'] and x['version']==row['product_version'] for x in json.loads(store['catalog'])):return None
    return row

def video_dimensions(raw):
    def boxes(start,end,depth=0):
        found=[];pos=start
        while pos+8<=end:
            length=int.from_bytes(raw[pos:pos+4],'big');kind=raw[pos+4:pos+8];header=8
            if length==1:
                if pos+16>end:break
                length=int.from_bytes(raw[pos+8:pos+16],'big');header=16
            if length==0:length=end-pos
            if length<header or pos+length>end:break
            if kind==b'tkhd' and length>=header+8:
                w=int.from_bytes(raw[pos+length-8:pos+length-4],'big')>>16;h=int.from_bytes(raw[pos+length-4:pos+length],'big')>>16
                if 0<w<=8192 and 0<h<=8192:found.append((w,h))
            if kind in {b'moov',b'trak'} and depth<3:found+=boxes(pos+header,pos+length,depth+1)
            pos+=length
        return found
    values=boxes(0,len(raw));return max(values,key=lambda v:v[0]*v[1]) if values else (0,0)
