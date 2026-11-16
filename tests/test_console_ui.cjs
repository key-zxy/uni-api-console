const fs=require('node:fs'),assert=require('node:assert/strict');
const html=fs.readFileSync('index.html','utf8'),js=fs.readFileSync('app.js','utf8');
assert.match(html, /lang="zh-CN"/);
for(const id of ['provider-form','pool-form','client-form','management-output','rollback']) assert.ok(html.includes('id="'+id+'"'));
assert.match(html,/授权列表/);
assert.match(js,/api\('manage'/);
assert.ok(!js.includes('sessionStorage.setItem'));
assert.match(html,/console.css/);
console.log('Chinese console management UI contracts pass');
assert.match(js,/仅限本地单写入者/);
assert.match(js,/明文保存/);
assert.match(js,/DNS 重绑定/);

for(const page of ['dashboard','keys','records','upstream']) assert.ok(html.includes('data-page="'+page+'"'));
assert.match(js,/el.hidden = el.dataset.view !== currentPage/);
assert.ok(!html.includes('href="#requests"'));

assert.ok(!/HTTPS 公网|公网 443|仅支持 HTTPS/.test(html));
assert.match(html, /HTTP\/HTTPS/);
assert.match(html, /1–65535/);
