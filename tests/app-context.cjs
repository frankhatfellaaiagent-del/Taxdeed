'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.resolve(__dirname, '..', process.env.APP_SOURCE || 'dashboard/index.html'), 'utf8');
function between(start,end){const a=html.indexOf(start), b=html.indexOf(end,a+start.length);assert(a>=0&&b>a,`Source markers: ${start}`);return html.slice(a,b);}
const pieces={
 recovery: between('// Older unscoped and signed-out browser work','// ---------- settings screen:'),
 saves: between('const saveInterest =','// ---------- buy-box'),
 setInt: between('function setInt(id, patch, rec) {','// Bookmark = saved without'),
 retry: between('function retrySave()',html.includes('// Session-bound cache and sync guard.')?'// Session-bound cache and sync guard.':'function cloudPush(name, payload) {'),
 landCheck: between('function runLandCheck(p) {','function parcelTiles() {'),
 undo: between('function pushUndo(label, undoFn) {','// A short informational toast'),
 watch: between('function watchForUpgrade(now) {',html.includes('function clearAuthDeepLink() {')?'function clearAuthDeepLink() {':'function afterSignedIn() {'),
 defaults: between('const DEFAULT_LISTS = [','let interest = store.get'),
 applyRows: between('function applyUserRow(key, data) {','async function onSignedIn(user) {'),
 snap: between('function applySnap(current, next) {','// Union two devices'),
 cache: between('// ---------- persisted team state (browser-local) ----------','// Two generic starter lists'),
 session: html.includes('// Session-bound cache and sync guard.') ? between('// Session-bound cache and sync guard.','function cloudPush(name, payload) {') : '',
 cleanup: html.includes('function clearAuthDeepLink() {') ? between('function clearAuthDeepLink() {','function afterSignedIn() {') : '',
 auth: between('function authSub() {','// After signing up (or in) with a plan in mind:'),
 deep: between('function readDeepLink() {','function wireLogin() {'),
 wire: between('function wireLogin() {','// ---------- app state ----------'),
 after: between('function afterSignedIn() {','// Deep links from the landing page and Stripe:'),
 merge: between('function mergeInterest(localObj, remoteObj) {','// Clear a resolved field conflict'),
 remap: between('const normIdSeg =','const $ = (id)'),
 lists: between('function mergeLists(a, b) {','async function cloudSignIn(email, pass)'),
 onIn: between('async function onSignedIn(user) {','function onSignedOut() {'),
 onOut: between('function onSignedOut() {','async function initCloud() {'),
 push: between('function cloudPush(name, payload) {','// Apply an incoming change only when it truly differs'),
 trial: between('let PLAN_TIER = "free";','// ---------- Florida time ----------')
};
function context(extra='') {
 const c=vm.createContext({URLSearchParams,console,Date,Intl,JSON,Map,Set,Promise,AbortController});
 vm.runInContext(`
 const elements=new Map();
 const $=(id)=>{if(!elements.has(id))elements.set(id,{value:'',hidden:false,disabled:false,textContent:'',innerHTML:'',focus(){}});return elements.get(id);};
 let AUTH_FORM_VERSION=0,_acctMenuOpen=false,AUTH_MODE='signup',PENDING_SIGNUP_EMAIL=null,PENDING_PLAN=null,PENDING_NAME=null,PENDING_CODE=null,JUST_STARTED_TRIAL=false,RETURNED_FROM_CHECKOUT=false;
 const PAYMENTS_LIVE=false,PRICE_MONTHLY='$297',PRICE_YEARLY='$997',FREE_DAYS=5,SIGNUP_FN_URL='https://invalid.test/local-stub-signup';
 const planLabel=(p)=>p==='yearly'?'$997/yr':'$297/mo';
 const calls=[],notices=[];
 let response={ok:true,status:200,body:{trial_days:30}};
 const fetch=async(url,opts)=>{calls.push({type:'signup',url,opts});if(response.throw)throw Error('offline');if(response.gate)await response.gate;return {ok:response.ok,status:response.status,json:async()=>response.body};};
 const cloudSignIn=async(email,password)=>calls.push({type:'signin',email});
 const showNotice=(m)=>notices.push(m);
 const showCheckoutStep=(p)=>calls.push({type:'checkout',plan:p});
 function watchForUpgrade(){};
 let _upgradeWatch=null,_undoTimer=null,_saveIdleTimer=null,SAVE_STATE="idle";
 const esc=(s)=>String(s);const document={visibilityState:"visible",getElementById:$,addEventListener(){},removeEventListener(){}},window={addEventListener(){},removeEventListener(){}};
 let location={search:'',pathname:'/app/',hash:''};
 const history={replaceState(a,b,url){calls.push({type:'replace',url});const u=new URLSearchParams((url.split('?')[1]||'').split('#')[0]);location.search=u.toString()?'?'+u.toString():'';}};
 const storage=new Map();const localStorage={getItem:(k)=>storage.has(k)?storage.get(k):null,setItem:(k,v)=>storage.set(k,String(v)),removeItem:(k)=>storage.delete(k)};
 const EMPTY_BUYBOX={target_counties:[]},FIELD_KEYS=['status','bid','notes','list','type','bookmark','opened'];
 const nowIso=()=>new Date().toISOString();
 let interest={},recent=[],lists=[],prefs={},team={lists:[],membership:{}},buybox={},DATA=[],META=null,TEAM_ID=null,TEAM_NAME='',IS_ADMIN=false,WORKSPACE_ERROR=false,SYNC_ERROR=false;
 const renderAccount=()=>{},rerender=()=>{},recomputeBuybox=()=>{},setSaveState=(s)=>{SAVE_STATE=s;};
 const remote={},writes=[],channels=[],failures={reads:{},writes:false};
 const sb={
  rpc:async()=>({data:[{team_id:'team_'+CLOUD.user.id,plan:'free'}]}),
  from(table){let selection={};const q={select(){return q},eq(k,v){selection[k]=v;return q},maybeSingle:async()=>({data:table==='team_state'?(remote[selection.team_id]||[]).find(r=>r.key===selection.key)||null:null,error:failures.reads[table]||null}),then(resolve,reject){return Promise.resolve({data:remote[selection.user_id||selection.team_id]||[],error:failures.reads[table]||null}).then(resolve,reject)},upsert:async(row,options={})=>{writes.push({table,row:JSON.parse(JSON.stringify(row))});if(!failures.writes){const id=row.user_id||row.team_id;const values=remote[id]||(remote[id]=[]);const i=values.findIndex(r=>r.key===row.key);if(i<0)values.push(JSON.parse(JSON.stringify(row)));else if(!options.ignoreDuplicates)values[i]=JSON.parse(JSON.stringify(row));}return {error:failures.writes?{message:'Synthetic write failure'}:null}}};return q;},
  channel(name){const q={on(event,filter,cb){channels.push({name,filter,cb});return q},subscribe(){return q}};return q;},removeChannel(){},auth:{}
 };
 let CLOUD={sb,user:null,channels:[]};
 const pendingTimers=new Map();let timerId=0;const setTimeout=(f)=>{pendingTimers.set(++timerId,f);return timerId;};const clearTimeout=(id)=>pendingTimers.delete(id);
 const setInterval=setTimeout,clearInterval=clearTimeout;
 const cloudTimers={};const SYNC_MAP={interest:{scope:'user',key:'interest',value:p=>p().data},prefs:{scope:'user',key:'prefs',value:p=>p().data},lists:{scope:'user',key:'lists',value:p=>p().data},team:{scope:'team',key:'lists',value:p=>p()},teambuybox:{scope:'team',key:'buybox',value:p=>p()}};
 `+fs.readFileSync(path.join(__dirname,'../dashboard/state-sync.js'),'utf8')+pieces.trial+pieces.cache+pieces.defaults+pieces.snap+pieces.applyRows+pieces.session+pieces.cleanup+pieces.push+extra,c);
 c.run=(x)=>vm.runInContext(x,c);return c;
}

module.exports={context,pieces,html};
