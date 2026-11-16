const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm');
const elements = new Map();
const node = () => ({querySelectorAll(){return []}, close(){}, showModal(){}, value:'', textContent:'', children:[], append(...children){this.children.push(...children)}, replaceChildren(){this.children=[]}});
const el = id => {if(!elements.has(id)) elements.set(id,node()); return elements.get(id)};
let calls = [], fail = false;
const context = {document:{getElementById:el,querySelectorAll:()=>[],createElement:node,createTextNode:text=>({textContent:text}),body:{classList:{add(){},remove(){}}}},localStorage:{getItem(){},removeItem(){}},clearTimeout(){},setTimeout(){},fetch:async(url,opts)=>{
 calls.push([url,JSON.parse(opts.body)]);
 return {ok:!fail,status:400,json:async()=>fail ? {error:'无法获取模型：请检查 Key、地址、上游状态；支持 HTTP/HTTPS 及内网地址。可继续手动输入。'} : {models:['paid/model','free/model','manual']},text:async()=> 'Discovery failed'};
}};
vm.runInNewContext(fs.readFileSync('app.js','utf8'),context);
(async()=>{
 for(const prefix of ['setup','provider']) {
  el(prefix+'-url').value='https://example.com/v1/responses';
  el(prefix+'-models').value='manual\nsource=alias';
  const before = calls.length; await el(prefix+'-discover').onclick(); assert.equal(calls.length, before);
  el(prefix+'-key').value='entered-secret';
  await el(prefix+'-discover').onclick();
  assert.equal(calls.at(-1)[1].api,'entered-secret');
  let options=el(prefix+'-model-options').children;
  assert.equal(options.length,3); assert.equal(options[0].children[0].checked,false);
  options[0].children[0].checked=true; options[0].children[0].onchange();
  el(prefix+'-model-search').value='free'; el(prefix+'-model-search').oninput();
  assert.equal(el(prefix+'-model-options').children.length,1);
  el(prefix+'-model-add').onclick();
  assert.equal(el(prefix+'-models').value,'manual\nsource=alias\npaid/model');
  el(prefix+'-model-add').onclick(); assert.equal(el(prefix+'-models').value.split('paid/model').length,2);
  fail=true; await el(prefix+'-discover').onclick();
  assert.equal(el(prefix+'-model-options').children.length,0);
  assert.equal(el(prefix+'-discovery-status').textContent, '无法获取模型：请检查 Key、地址、上游状态；支持 HTTP/HTTPS 及内网地址。可继续手动输入。');
  assert.ok(!el(prefix+'-discovery-status').textContent.includes('仅允许公共 HTTPS'));
  assert.equal(el(prefix+'-models').value,'manual\nsource=alias\npaid/model'); fail=false;
 }
 el('logout').onclick(); assert.equal(el('provider-key').value,'');
 console.log('Discovery first setup/update, explicit key, search, selection, manual preservation, failure and logout pass');
})().catch(e=>{console.error(e);process.exitCode=1});
