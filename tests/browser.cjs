// Run against an isolated empty database: BASE_URL=http://127.0.0.1:3012 node tests/browser.cjs
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||'/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(process.env.BASE_URL||'http://127.0.0.1:3012');
  await page.getByRole('button',{name:'＋ 新建项目',exact:true}).click();
  await page.locator('#fields details').evaluate(e=>e.open=true);
  await page.locator('[name=name]').fill('TEST-浏览器经营项目');
  await page.locator('[name=idea]').fill('验证宠物旅行配件的真实需求');
  await page.locator('[name=budget]').fill('1000');
  await page.locator('[name=market]').fill('美国');
  await page.locator('#form button[type=submit]').click();
  await page.getByRole('heading',{name:'项目启动卡：TEST-浏览器经营项目',exact:true}).waitFor({timeout:15000});
  await page.locator('#advanced-tasks').evaluate(e=>e.open=true);
  await page.locator('[data-task]').first().click();
  await page.locator('[name=status]').selectOption('已完成');
  await page.locator('[name=note]').fill('浏览器验收项目启动卡');
  await page.getByRole('button',{name:'保存并继续',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('已保存'));
  let state=await page.request.get((process.env.BASE_URL||'http://127.0.0.1:3012')+'/api/state').then(r=>r.json());
  assert.equal(state.tasks[0].status,'已完成'); assert.equal(state.tasks[1].status,'待处理');
  await page.getByRole('button',{name:'商品与供应',exact:false}).first().click();
  await page.getByRole('button',{name:'＋ 机会证据',exact:true}).click();
  await page.locator('[name=content]').fill('TEST 浏览器录入材料，不是真实研究');
  await page.locator('[name=source]').fill('本地测试记录 / 2026-10-20');
  await page.getByRole('button',{name:'保存并继续',exact:true}).click();
  await page.getByText('TEST 浏览器录入材料，不是真实研究',{exact:true}).waitFor();
  await page.locator('[data-page="营销与实验"]').click();
  await page.getByRole('button',{name:'＋ 营销计划',exact:true}).click();
  for(const [name,value] of Object.entries({name:'TEST 内容实验',audience:'宠物旅行人群',channel:'自然内容',landing:'测试承接页',hypothesis:'测试假设',stop_rule:'100次有效访问',budget:'200'}))await page.locator(`[name=${name}]`).fill(value);
  await page.getByRole('button',{name:'保存并继续',exact:true}).click();
  await page.getByRole('heading',{name:'TEST 内容实验',exact:true}).waitFor();
  await page.locator('[data-page="客户与订单"]').click();
  await page.getByRole('button',{name:'＋ 导入订单',exact:true}).click();
  for(const [name,value] of Object.entries({external_id:'TEST-browser-1',revenue:'100',cost:'30'}))await page.locator(`[name=${name}]`).fill(value);
  await page.getByRole('button',{name:'保存并继续',exact:true}).click();
  await page.getByRole('heading',{name:'TEST-browser-1',exact:true}).waitFor();
  for(const name of ['AI 员工','知识与连接','经营首页'])await page.locator(`[data-page="${name}"]`).click();
  await page.screenshot({path:'/tmp/venture-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  await page.screenshot({path:'/tmp/venture-mobile.png',fullPage:true});
  assert.deepEqual(errors,[]);console.log('浏览器通过：创建、真实本地任务执行、成果、验收、证据录入、活动、订单、七页面导航、手机宽度，无 JS 异常');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
