// Real HTTP/DB/UI, controlled provider responses. Never connects paid services.
const {chromium}=require('playwright'),{spawn}=require('node:child_process'),crypto=require('node:crypto'),fs=require('node:fs'),os=require('node:os'),path=require('node:path'),assert=require('node:assert/strict');
(async()=>{let tmp=fs.mkdtempSync(path.join(os.tmpdir(),'venture-guided-')),base='http://127.0.0.1:3018',password=crypto.randomBytes(32).toString('hex'),browser;
const program=`
import server as s,engine,research,net,threading,base64,json,io
from PIL import Image
out=io.BytesIO();Image.new('RGB',(320,320),'white').save(out,'PNG');png=out.getvalue()
research.search=lambda c,p,q:[{'url':'https://example.org/TEST-source','title':'TEST 公开研究材料','excerpt':'TEST 假设验证材料，非真实市场结论','type':'TEST fixture'}]
net.public_url=lambda u:u
def model(context):
 refs=[r['id'] for r in context['records']]
 return {'summary':'TEST 研究候选方向','deliverables':['TEST 有来源的方向比较'],'evidence_ids':refs,'unknowns':['市场需求仍待验证'],'next_steps':['TEST 访谈验证'],'opportunities':[{'name':'TEST 旅行收纳方向','audience':'TEST 旅行用户','problem':'TEST 收纳需求','competition':'TEST 竞争资料不足','supply_hypothesis':'TEST 待核实','risk':'TEST 样本不足','validation':'TEST 实际访谈','evidence_ids':refs}]}
engine.model_call=model
def request(url,*args,**kwargs):
 if url.startswith('https://example.org/'):return b'<html><body>TEST public source body</body></html>',{'Content-Type':'text/html'}
 if not url.endswith('/images/edits'):raise ValueError('Unexpected fixture request')
 return json.dumps({'data':[{'b64_json':base64.b64encode(png).decode()}]}).encode(),{}
net.request=request
s.connect().close();engine.recover();threading.Thread(target=engine.worker,daemon=True).start();s.ThreadingHTTPServer(('127.0.0.1',3018),s.Handler).serve_forever()
`;
const child=spawn(process.env.PYTHON_PATH||'python3',['-c',program],{env:{...process.env,DATABASE_URL:'',APP_DB:path.join(tmp,'db.sqlite'),APP_ACCESS_TOKEN:password,APP_HOST:'127.0.0.1',APP_PUBLIC_ORIGIN:base,AI_API_KEY:'TEST-fixture',AI_MODEL:'TEST',MODEL_CALL_CNY:'0',MEDIA_API_KEY:'TEST-fixture',IMAGE_CALL_CNY:'0'},stdio:'pipe'});let stderr='';child.stderr.on('data',x=>stderr+=x.toString());
try{
 for(let i=0;i<80;i++){try{if((await fetch(base+'/api/health')).ok)break}catch{}await new Promise(r=>setTimeout(r,100));if(child.exitCode!==null)throw Error(stderr)}
 browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox']});let page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(base);await page.locator('[name=password]').fill(password);await page.getByRole('button',{name:'进入工作台'}).click();await page.locator('#create').waitFor();
 const save=async values=>{for(let[k,v]of Object.entries(values)){let el=page.locator('#form [name="'+k+'"]');if(await el.evaluate(e=>e.tagName==='SELECT'))await el.selectOption(v);else await el.fill(v)}await page.locator('#form button[type=submit]').click();await page.locator('#dialog').waitFor({state:'hidden'})};
 await page.locator('#create').click();assert.equal(await page.locator('#fields textarea:visible').count(),1);await save({idea:'TEST 从零开始，帮我探索旅行用品方向'});await page.locator('#accept-start').waitFor();await page.locator('#accept-start').click();await page.locator('#accept-start').waitFor({state:'hidden'});
 await page.locator('#manager-next').click();await page.locator('#form [name=allow_model]').check();await save({query:'TEST 旅行用品的需求与竞争'});await page.locator('[data-choose-direction]').waitFor({timeout:20000});await page.screenshot({path:'/tmp/venture-guided-research.png',fullPage:true});await page.locator('[data-choose-direction]').click();await page.waitForFunction(()=>document.querySelector('#view').textContent.includes('已选择验证'));
 await page.locator('[data-page="商品与供应"]').click();await page.locator('[data-new-kind="product"]').click();await save({name:'TEST 旅行用品',sku:'TEST-GUIDED',specification:'TEST 已提供规格',material:'TEST 材料',weight:'10g',dimensions:'10cm',usage:'仅用于受控测试',limitations:'不是实际销售商品',source:'TEST 样品资料',classification:'事实'});
 let state=await page.request.get(base+'/api/state').then(r=>r.json()),p=state.projects[0].id,product=state.entities.find(x=>x.kind==='product').id;
 // Import a valid real PNG fixture without external retrieval.
 let png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAUAAAAFACAIAAABC8jL9AAADt0lEQVR4nO3TQQ0AIBDAMMC/58MDH7KkVbDP9swsoOn8DgDeGRjCDAxhBoYwA0OYgSHMwBBmYAgzMIQZGMIMDGEGhjADQ5iBIczAEGZgCDMwhBkYwgwMYQaGMANDmIEhzMAQZmAIMzCEGRjCDAxhBoYwA0OYgSHMwBBmYAgzMIQZGMIMDGEGhjADQ5iBIczAEGZgCDMwhBkYwgwMYQaGMANDmIEhzMAQZmAIMzCEGRjCDAxhBoYwA0OYgSHMwBBmYAgzMIQZGMIMDGEGhjADQ5iBIczAEGZgCDMwhBkYwgwMYQaGMANDmIEhzMAQZmAIMzCEGRjCDAxhBoYwA0OYgSHMwBBmYAgzMIQZGMIMDGEGhjADQ5iBIczAEGZgCDMwhBkYwgwMYQaGMANDmIEhzMAQZmAIMzCEGRjCDAxhBoYwA0OYgSHMwBBmYAgzMIQZGMIMDGEGhjADQ5iBIczAEGZgCDMwhBkYwgwMYQaGMANDmIEhzMAQZmAIMzCEGRjCDAxhBoYwA0OYgSHMwBBmYAgzMIQZGMIMDGEGhjADQ5iBIczAEGZgCDMwhBkYwgwMYQaGMANDmIEhzMAQZmAIMzCEGRjCDAxhBoYwA0OYgSHMwBBmYAgzMIQZGMIMDGEGhjADQ5iBIczAEGZgCDMwhBkYwgwMYQaGMANDmIEhzMAQZmAIMzCEGRjCDAxhBoYwA0OYgSHMwBBmYAgzMIQZGMIMDGEGhjADQ5iBIczAEGZgCDMwhBkYwgwMYQaGMANDmIEhzMAQZmAIMzCEGRjCDAxhBoYwA0OYgSHMwBBmYAgzMIQZGMIMDGEGhjADQ5iBIczAEGZgCDMwhBkYwgwMYQaGMANDmIEhzMAQZmAIMzCEGRjCDAxhBoYwA0OYgSHMwBBmYAgzMIQZGMIMDGEGhjADQ5iBIczAEGZgCDMwhBkYwgwMYQaGMANDmIEhzMAQZmAIMzCEGRjCDAxhBoYwA0OYgSHMwBBmYAgzMIQZGMIMDGEGhjADQ5iBIczAEGZgCDMwhBkYwgwMYQaGMANDmIEhzMAQZmAIMzCEGRjCDAxhBoYwA0OYgSHMwBBmYAgzMIQZGMIMDGEGhjADQ5iBIczAEGZgCDMwhBkYwgwMYQaGMANDmIEhzMAQZmAIMzCEGRjCDAxhBoYwA0OYgSHMwBBmYAgzMIQZGMIMDGEGhjADQ5iBIczAEGZgCDMwhBkYwgwMYQaGMANDmIEhzMAQZmAIMzCEGRjCDAxhBoYwA0OYgSHMwBB2ARItBX2oMqmxAAAAAElFTkSuQmCC','base64');
 await page.locator('[data-page="知识与连接"]').click();await page.locator('#upload-file').setInputFiles({name:'TEST-reference.png',mimeType:'image/png',buffer:png});await page.getByText('TEST-reference.png',{exact:false}).waitFor();
 await page.locator('#tool-grant').click();await save({tool:'image',target:product,calls:'15',budget:'0',days:'1'});
 state=await page.request.get(base+'/api/state').then(r=>r.json());let reference=state.files.find(x=>x.name==='TEST-reference.png').id;
 await page.locator('[data-page="营销与实验"]').click();await page.locator('#media-set').click();await save({product,reference,rights:'TEST 自有参考资料',prompt:'TEST 保持实物一致'});await page.waitForFunction(()=>document.querySelectorAll('.asset-grid article').length===15,{},{timeout:30000});assert.equal(await page.locator('.asset-grid img').count(),15);
 await page.locator('[data-approve-asset]').first().click();await save({note:'TEST 核查图像、文字和使用权'});await page.screenshot({path:'/tmp/venture-guided-assets.png',fullPage:true});
 // Mobile and per-page navigation regressions.
 for(let name of ['客户与订单','知识与连接','项目工作台'])await page.locator('[data-page="'+name+'"]').click();
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await page.screenshot({path:'/tmp/venture-guided-mobile.png',fullPage:true});assert.deepEqual(errors,[]);
 console.log('引导流程通过：一句话启动、启动卡确认、受控联网研究来源、候选方向选择、自动验证计划、商品事实、实际图像协议15份文件、素材核查、权限及移动端。外部提供商为受控响应。');
}finally{if(browser)await browser.close();child.kill();if(child.exitCode===null)await new Promise(r=>child.once('exit',r));fs.rmSync(tmp,{recursive:true,force:true})}})().catch(e=>{console.error(e);process.exit(1)});
