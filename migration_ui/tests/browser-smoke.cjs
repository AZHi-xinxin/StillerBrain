// Optional development smoke: installed Playwright + a Python with repo deps.
// No browser profile, remote services, real source files, or real keys.
const {spawn} = require('node:child_process');
const {once} = require('node:events');
const {createInterface} = require('node:readline');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

(async()=>{
  const python=process.argv[2];
  if(!python)throw new Error('Pass the test Python executable.');
  const child=spawn(python,['-B','-m','migration_ui.tests.ui_fixture'],{windowsHide:true,stdio:['pipe','pipe','pipe']});
  const lines=createInterface({input:child.stdout});
  let browser;
  const errors=[];
  child.stderr.on('data',data=>errors.push(data.toString()));
  try{
    const [line]=await Promise.race([once(lines,'line'),once(child,'exit').then(()=>{throw new Error('Synthetic fixture exited.');})]);
    const {url}=JSON.parse(line), origin=new URL(url).origin;
    browser=await chromium.launch({headless:true,...(process.env.MIGRATION_BROWSER_CHANNEL?{channel:process.env.MIGRATION_BROWSER_CHANNEL}:{})});
    const context=await browser.newContext({viewport:{width:1150,height:1100}});
    await context.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
    const page=await context.newPage();
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(url);
    assert.equal(await page.title(),'ST · 记忆迁移');
    for(const id of ['source-http','model-http','target-http','consent'])assert.equal(await page.locator('#'+id).isChecked(),false);
    assert.equal(await page.locator('#start').isDisabled(),true);
    await page.locator('#source-format').selectOption('markdown');
    await page.locator('#source-file').setInputFiles({name:'synthetic.md',mimeType:'text/markdown',buffer:Buffer.from('Synthetic source <img src=x onerror="window.injected=true">\nKept verbatim.')});
    await page.locator('#prepare').click();
    await page.locator('#preview').waitFor({state:'visible'});
    assert.match(await page.locator('#preview').innerText(),/Synthetic source/);
    assert.equal(await page.locator('#preview img').count(),0);
    await page.locator('#model-url').fill('https://synthetic-model.invalid/v1/chat/completions');
    await page.locator('#model-name').fill('synthetic');
    await page.locator('#model-key').fill('synthetic-model-key');
    await page.locator('#target-url').fill('https://synthetic-st.invalid/mcp');
    await page.locator('#target-key').fill('synthetic-st-key');
    await page.locator('#target-namespace').fill('Synthetic target');
    await page.locator('#consent').check();
    await page.locator('#start').click();
    await page.waitForFunction(()=>document.getElementById('status').textContent==='本轮完成');
    assert.match(await page.locator('#rows').innerText(),/已保存/);
    assert.equal(await page.locator('#model-key').inputValue(),'');
    assert.equal(await page.locator('#target-key').inputValue(),'');
    assert.equal(await page.locator('#error').innerText(),'');
    assert.equal(await page.evaluate(()=>window.injected),undefined);
    if(process.argv[3])await page.screenshot({path:process.argv[3],fullPage:true});
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({passed:true,syntheticOnly:true,remoteRequestsAllowed:false,checks:14}));
  }finally{
    if(browser)await browser.close();
    child.stdin.end('stop\n');
    await once(child,'exit');
    lines.close();
  }
})().catch(error=>{console.error(error.message);process.exitCode=1;});
