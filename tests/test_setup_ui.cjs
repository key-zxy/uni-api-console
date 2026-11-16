const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const el = id => { if (!elements.has(id)) elements.set(id, {querySelectorAll(){return []}, close(){}, showModal(){}, value:'', textContent:'', hidden:false, disabled:false, replaceChildren(){}}); return elements.get(id); };
let captured;
const saved = new Map();
const context = {document:{getElementById:el, querySelectorAll:()=>[], body:{classList:{add(){},remove(){}}}},localStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},clearTimeout(){},setTimeout(){}, fetch:async (url, opts) => {
 if(opts.body) {captured=JSON.parse(opts.body);assert.equal(el('setup-key').value,'');}
 return {ok:true,json:async()=>url.endsWith('manage')?{providers:[],pools:{},clients:[]}:{provider:'custom',client_api_key:'one-time-client'}};
}};
vm.runInNewContext(fs.readFileSync('app.js','utf8'), context);
const html=fs.readFileSync('index.html','utf8');
for(const id of ['setup-provider','setup-url','setup-models','setup-key']) assert.ok(html.includes('id="'+id+'"'));
assert.ok(!/id="setup-(?:provider|url|models|key)"[^>]*readonly/.test(html));
assert.ok(html.includes('id="api-base-url"'));
el('setup-provider').value='custom';el('setup-url').value='http://localhost:1234/v1/chat/completions';el('setup-models').value='vendor/model:free\nsource=alias';el('setup-key').value='upstream';
(async()=>{
 await el('setup-form').onsubmit({preventDefault(){}});
 assert.deepEqual(captured,{api:'upstream',provider:'custom',base_url:'http://localhost:1234/v1/chat/completions',models:['vendor/model:free',{source:'alias'}]});
 assert.equal(el('setup-client-key').textContent,'one-time-client');
 el('logout').onclick(); assert.equal(el('setup-client-key').textContent,'');assert.equal(saved.size,0);
 context.fetch=async()=>({ok:true,json:async()=>({retention_days:30})});
 await vm.runInNewContext("token='console-secret'; start()",context).catch(()=>{});
 assert.equal(saved.get('uni-api.audit.login.v1'),'console-secret');
 context.fetch=async()=>({ok:false,status:401});
 await vm.runInNewContext("api('settings')",context).catch(()=>{});
 assert.equal(saved.size,0);
 assert.equal(el('console').hidden,true);
 console.log('Generic onboarding, persistent login, failed auth and logout pass');
})().catch(e=>{console.error(e);process.exitCode=1;});
