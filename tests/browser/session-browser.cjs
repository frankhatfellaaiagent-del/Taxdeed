'use strict';
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');
const results=[];
function mockBackend(){
 const clone=x=>JSON.parse(JSON.stringify(x));
 const backend={active:null,callback:null,rows:{},writes:[],channels:[],fail:null,gates:{},profiles:{},blockedRequests:[]};
 backend.hold=key=>{let release;backend.gates[key]={promise:new Promise(r=>release=r),release:()=>{delete backend.gates[key];release()}}};
 backend.emit=user=>{backend.active=user;backend.callback(user?'SIGNED_IN':'SIGNED_OUT',user?{user}:null)};
 backend.release=key=>backend.gates[key]?.release();
 function query(table){
  let filters={},op='select',payload,options={},signal;
  const q={select(){return q},eq(k,v){filters[k]=v;return q},abortSignal(s){signal=s;return q},upsert(p,o){op='upsert';payload=clone(p);options=o||{};return q},maybeSingle(){return execute(true)},then(resolve,reject){return execute(false).then(resolve,reject)}};
  async function execute(single){
   if(op==='upsert'){
    const entry={table,row:clone(payload),options:clone(options),accepted:false};backend.writes.push(entry);
    const key='write:'+payload.key;if(backend.gates[key])await backend.gates[key].promise;
    if(signal?.aborted)return {error:{message:'Aborted'}};
    if(backend.fail===payload.key)return {error:{message:'Synthetic denied/offline'}};
    const id=payload.user_id||payload.team_id,storeKey=table+':'+id+':'+payload.key;
    if(!options.ignoreDuplicates||!backend.rows[storeKey])backend.rows[storeKey]=clone(payload);
    entry.accepted=true;return {data:null,error:null};
   }
   const key='read:'+table;if(backend.gates[key])await backend.gates[key].promise;
   if(signal?.aborted)return {error:{message:'Aborted'}};
   const rows=Object.entries(backend.rows).filter(([k,v])=>k.startsWith(table+':')&&Object.entries(filters).every(([key,value])=>v[key]===value)).map(([,v])=>clone(v));
   return {data:single?(rows[0]||null):rows,error:null};
  }
  return q;
 }
 backend.client={
  from:query,
  async rpc(){const user=clone(backend.active);if(backend.gates['profile:'+user.id])await backend.gates['profile:'+user.id].promise;return {data:[backend.profiles[user.id]||{team_id:'team_'+user.id,plan:'pro'}],error:null}},
  channel(name){const channel={name,on(type,filter,callback){backend.channels.push({name,filter,callback,channel});return channel},subscribe(){return channel}};return channel},
  removeChannel(){},
  auth:{onAuthStateChange(callback){backend.callback=callback;return {data:{subscription:{unsubscribe(){}}}}},getSession:async()=>({data:{session:backend.active?{user:backend.active}:null}}),signOut:async()=>backend.emit(null),signInWithPassword:async({email})=>backend.emit({id:email.split('@')[0],email}),updateUser:async()=>({error:null}),resetPasswordForEmail:async()=>({error:null})},
 };
 window.__backend=backend;
}
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CHROME_EXECUTABLE?{executablePath:process.env.CHROME_EXECUTABLE}:{})});
 const check=async(name,fn)=>{try{await fn();results.push({name,result:'PASS'});console.log('PASS '+name)}catch(e){results.push({name,result:'FAIL',error:e.stack});console.error('FAIL '+name+' '+e.message)}};
 async function start(width=1280,storage){
  const context=await browser.newContext({viewport:{width,height:900}});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(mockBackend);if(storage)await page.addInitScript(v=>{for(const [k,data]of Object.entries(v))localStorage.setItem(k,JSON.stringify(data))},storage);
  await page.clock.setFixedTime(new Date('2026-10-16T12:00:00Z'));
  const feed={generated_at:'2026-10-16T08:00:00Z',default_buybox:{target_counties:['lee'],land_use_keywords:['vacant'],non_land_keywords:['house']},counts:{total:1,scheduled:1,redeemed:0,past_due:0,counties:1,counties_total:1,by_county:{lee:{scheduled:1,total:1}}},counties_registry:[{slug:'lee',name:'Lee',coverage:'online'}],records:[{county:'lee',parcel_id:'SYNTHETIC-PARCEL',case_number:'SYNTHETIC-CASE',property_address:'1 Synthetic Lane',sale_date:'10/19/2026',status:'Scheduled',opening_bid:123,assessed_value:1000,property_use:'Vacant land'}]};
  await page.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(url.hostname==='esm.sh'&&url.pathname.includes('supabase'))return route.fulfill({contentType:'application/javascript',headers:{'access-control-allow-origin':'*'},body:'export const createClient=()=>window.__backend.client;'});
   if(url.hostname!=='taxdeed.test'){await page.evaluate(u=>window.__backend.blockedRequests.push(u),url.hostname).catch(()=>{});return route.abort()}
   if(url.pathname.endsWith('master_list.json'))return route.fulfill({json:feed});
   const local=url.pathname==='/app/'?'dashboard/index.html':url.pathname.startsWith('/app/ds/')?'dashboard/ds/'+path.basename(url.pathname):url.pathname==='/app/state-sync.js'?'dashboard/state-sync.js':null;
   if(!local)return route.abort();
   return route.fulfill({contentType:local.endsWith('.html')?'text/html':local.endsWith('.css')?'text/css':'application/javascript',body:fs.readFileSync(path.join(root,local))});
  });
  await page.goto('http://taxdeed.test/app/',{waitUntil:'networkidle'});await page.waitForFunction(()=>CLOUD&&DATA.length>0);
  return {context,page,errors};
 }
 async function login(page,id){await page.evaluate(id=>window.__backend.emit({id,email:id+'@example.invalid'}),id);await page.waitForFunction(id=>CLOUD.user?.id===id&&SYNC_READY,id)}
 async function detail(page){await page.getByRole('button',{name:'Explore',exact:true}).click();await page.locator('#scr-dashboard [data-open]').first().click();await page.locator('#pos-notes').waitFor()}
 try{
 await check('Real DOM notes and lists stay with A after direct switch to B and back',async()=>{const {context,page,errors}=await start();await login(page,'synthetic_a');await detail(page);await page.locator('#pos-notes').fill('A PRIVATE SYNTHETIC NOTE');await page.evaluate(()=>SYNC_QUEUE.flush());await page.getByRole('button',{name:'Saved',exact:true}).click();await page.getByRole('button',{name:'+ New list',exact:true}).click();await page.locator('#nl-name').fill('A PRIVATE SYNTHETIC LIST');await page.locator('#nl-create').click();await page.evaluate(()=>SYNC_QUEUE.flush());await login(page,'synthetic_b');assert.doesNotMatch(await page.locator('body').innerText(),/A PRIVATE/);assert.doesNotMatch(await page.evaluate(()=>JSON.stringify(__backend.writes.filter(w=>w.row.user_id==='synthetic_b'))),/A PRIVATE/);await login(page,'synthetic_a');await detail(page);assert.equal(await page.locator('#pos-notes').inputValue(),'A PRIVATE SYNTHETIC NOTE');assert.deepEqual(errors,[]);await context.close()});
 await check('A debounced note never becomes a B write and survives A reconnect',async()=>{const {context,page}=await start();await login(page,'synthetic_a');await detail(page);await page.locator('#pos-notes').fill('A QUEUED NOTE');await login(page,'synthetic_b');await page.waitForTimeout(900);assert.doesNotMatch(await page.evaluate(()=>JSON.stringify(__backend.writes.filter(w=>w.row.user_id==='synthetic_b'))),/A QUEUED/);await login(page,'synthetic_a');assert.match(await page.evaluate(()=>JSON.stringify(interest)),/A QUEUED/);await context.close()});
 await check('Delayed A profile and removed realtime callbacks cannot contaminate B',async()=>{const {context,page}=await start();await page.evaluate(()=>{__backend.hold('profile:synthetic_a');__backend.emit({id:'synthetic_a',email:'a@example.invalid'})});await login(page,'synthetic_b');await page.evaluate(()=>__backend.release('profile:synthetic_a'));await page.waitForTimeout(20);assert.equal(await page.evaluate(()=>TEAM_ID),'team_synthetic_b');await login(page,'synthetic_a');await page.evaluate(()=>window.oldChannel=__backend.channels.find(c=>c.name==='user_state:synthetic_a').callback);await login(page,'synthetic_b');await page.evaluate(()=>window.oldChannel({new:{user_id:'synthetic_a',key:'interest',data:{x:{notes:'STALE A'}}}}));assert.doesNotMatch(await page.evaluate(()=>JSON.stringify(interest)),/STALE A/);await context.close()});
 for(const key of ['team','teambuybox'])await check(key+' edit during initial browser writeback is replayed before saved',async()=>{const {context,page}=await start();await page.evaluate(()=>{__backend.rows['team_state:team_synthetic_a:buybox']={team_id:'team_synthetic_a',key:'buybox',data:{target_counties:['lee']}};__backend.hold('write:interest');__backend.emit({id:'synthetic_a',email:'a@example.invalid'})});await page.waitForFunction(()=>__backend.writes.some(w=>w.row.key==='interest'));await page.evaluate(key=>{if(key==='team'){team.lists.push({name:'SHARED DURING BOOT'});saveTeam()}else{buybox.max_opening_bid=456;saveBuybox()}},key);assert.equal(await page.evaluate(()=>SYNC_READY),false);await page.evaluate(()=>__backend.release('write:interest'));await page.waitForFunction(()=>SYNC_READY);assert(await page.evaluate(key=>__backend.writes.some(w=>w.accepted&&w.table==='team_state'&&w.row.key===(key==='team'?'lists':'buybox')&&(key==='team'?JSON.stringify(w.row.data).includes('SHARED DURING BOOT'):w.row.data.max_opening_bid===456)),key));await context.close()});
 await check('Failure remains visible and Retry preserves notes and shared changes',async()=>{const {context,page}=await start();await login(page,'synthetic_a');await page.evaluate(()=>{__backend.fail='lists';team.lists.push({name:'RETAIN SHARED'});saveTeam()});await page.evaluate(()=>SYNC_QUEUE.flush());await page.getByRole('button',{name:'Settings',exact:true}).click();assert.match(await page.locator('#settings-sync-status').innerText(),/could not sync/);await page.evaluate(()=>__backend.fail=null);await page.locator('#sync-retry').click();await page.waitForFunction(()=>SYNC_READY&&!SYNC_ERROR);assert(await page.evaluate(()=>__backend.writes.some(w=>w.accepted&&w.table==='team_state'&&JSON.stringify(w.row.data).includes('RETAIN SHARED'))));await context.close()});
 for(const width of [1280,390])await check('Ownership-confirmed recovery and reflow at '+width+'px',async()=>{const {context,page,errors}=await start(width,{tdd_interest:{'lee|synthetic|case':{notes:'OWNED LEGACY NOTE'}},tdd_lists:[{name:'Legacy list'}]});await login(page,'synthetic_a');await page.getByRole('button',{name:'Settings',exact:true}).click();assert(await page.locator('#recover-import').isDisabled());await page.locator('#recover-confirm').check();await page.locator('#recover-import').click();await page.evaluate(()=>SYNC_QUEUE.flush());assert.match(await page.locator('#recover-message').innerText(),/Recovered/);assert.match(await page.evaluate(()=>JSON.stringify(interest)),/OWNED LEGACY/);assert.match(await page.evaluate(()=>localStorage.getItem('tdd_interest')),/OWNED LEGACY/);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth),width);await login(page,'synthetic_b');assert.doesNotMatch(await page.evaluate(()=>JSON.stringify(interest)),/OWNED LEGACY/);assert.deepEqual(errors,[]);if(process.env.SCREENSHOT_DIR){await login(page,'synthetic_a');await page.getByRole('button',{name:'Settings',exact:true}).click();await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'app-recovery-'+width+'.png'),fullPage:true})}await context.close()});
 }finally{await browser.close()}
 const out={scope:'Real isolated browser; synthetic auth and backend; all external requests intercepted/blocked',results};console.log(JSON.stringify(out,null,2));if(process.env.RESULT_FILE)fs.writeFileSync(process.env.RESULT_FILE,JSON.stringify(out,null,2));process.exitCode=results.some(x=>x.result==='FAIL')?1:0;
})();
