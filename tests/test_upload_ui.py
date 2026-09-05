from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from test_batch_assets_ui import PageFixtureParser
from video_download_control.uploads.web import UPLOAD_HTML


HARNESS = r"""
const vm=require('node:vm');const ids=new Map();
class Element{
 constructor(tag,attrs={},text=''){this.tagName=tag;this.children=[];this.listeners=new Map();this._text=text;this.value=attrs.value||'';this.hidden=Object.hasOwn(attrs,'hidden');this.disabled=false;this.checked=false;this.dataset={};this.type=attrs.type||'';this.files=[];if(attrs.id)ids.set(attrs.id,this);}
 get textContent(){return this._text+this.children.map(item=>item.textContent).join('');}set textContent(value){this._text=String(value);this.children=[];}
 set innerHTML(value){throw new Error('Unsafe innerHTML assignment');}
 removeAttribute(name){delete this[name];}focus(){this.focused=true;}scrollIntoView(){this.scrolledIntoView=true;}
 append(...items){for(const item of items)this.children.push(typeof item==='string'?new Element('#text',{},item):item);}
 replaceChildren(...items){this._text='';this.children=[];this.append(...items);}get firstChild(){return this.children[0];}
 querySelectorAll(selector){if(selector==='details[data-job-id]')return all(this).filter(item=>item.tagName==='details'&&item.dataset.jobId!==undefined);throw new Error('Unsupported selector '+selector);}
 addEventListener(name,callback){if(!this.listeners.has(name))this.listeners.set(name,[]);this.listeners.get(name).push(callback);}
 async dispatch(name){for(const callback of this.listeners.get(name)||[])await callback({preventDefault(){},target:this});}
}
function construct(node){const item=new Element(node.tag,node.attrs,node.text||'');for(const child of node.children||[])item.append(construct(child));if(node.tag==='select')item.value=item.children.find(child=>child.tagName==='option')?.value||'';return item;}
const root=construct(fixture.page);function all(item){return [item,...item.children.flatMap(all)];}
const document={getElementById:id=>ids.get(id),createElement:tag=>new Element(tag),querySelectorAll(selector){if(selector==='button')return all(root).filter(item=>item.tagName==='button');if(selector==='#account-choices input:checked')return all(ids.get('account-choices')).filter(item=>item.tagName==='input'&&item.checked);throw new Error('Unsupported selector '+selector);}};
const state={requests:[],accounts:[{id:'a'.repeat(32),platform:'bilibili',name:'Synthetic Bili',auth_state:'ready'},{id:'b'.repeat(32),platform:'tencent',name:'Synthetic Tencent',auth_state:'ready'}],sources:[{id:'c'.repeat(32),name:'synthetic.mp4',size:42,sha256:'d'.repeat(64)}],jobs:[],operations:[],turn:()=>new Promise(resolve=>setImmediate(resolve)),all};
async function fetch(url,options={}){state.requests.push({url,method:options.method||'GET',body:options.body,headers:Object.fromEntries(options.headers.entries())});if(state.requestHook)await state.requestHook(url,options);let payload;
 if(url.endsWith('/session'))payload={csrf_token:'synthetic-session-nonce'};
 else if(url.endsWith('/status'))payload={worker_running:true,backend:{ready:true},platforms:[{id:'bilibili',title_limit:80},{id:'tencent',title_limit:100}]};
 else if(options.method==='POST'&&url.endsWith('/accounts')){const body=JSON.parse(options.body);payload={id:'f'.repeat(32),...body,auth_state:'unchecked'};state.accounts.push(payload);}
 else if(options.method==='POST'&&url.endsWith('/login')){payload={id:'e'.repeat(32),account_id:url.split('/').at(-2),action:'login',state:'running',login_phase:'preparing',qr_revision:0,qr_available:false};state.operations=[payload];}
 else if(options.method==='POST'&&url.endsWith('/cancel')){const op=state.operations.find(item=>url.includes(item.id));if(op)op.state='canceled';payload=op||{state:'canceled'};}
 else if(options.method==='POST')payload={state:'draft'};
 else if(url.endsWith('/qr')){if(state.qrHandler)return state.qrHandler();return {ok:true,status:200,headers:new Headers({'Content-Type':'image/png'}),blob:async()=>new Blob(['synthetic-png'],{type:'image/png'})};}
 else if(url.includes('/jobs/resolve?')){const wanted=new URL(url,'http://127.0.0.1').searchParams.get('ids').split(',');payload=state.jobs.filter(item=>wanted.includes(item.id));}
 else if(url.includes('/sources/page'))payload=state.sourcePageHandler?state.sourcePageHandler(url):{items:state.sources,next_cursor:null};else if(url.includes('/jobs/page'))payload=state.jobPageHandler?state.jobPageHandler(url):{items:state.jobs,next_cursor:null};
 else if(/\/sources\/[0-9a-f]{32}$/.test(url))payload=state.sources.find(item=>url.endsWith(item.id));else if(/\/jobs\/[0-9a-f]{32}$/.test(url))payload=state.jobs.find(item=>url.endsWith(item.id));
 else if(url.endsWith('/accounts'))payload=state.accounts;else if(url.endsWith('/sources'))payload=state.sources;else if(url.endsWith('/jobs'))payload=state.jobs;else if(url.endsWith('/operations'))payload=state.operations;
 else throw new Error('Unexpected fetch '+url);return {ok:true,status:200,json:async()=>payload};}
let timer=0;const timers=new Map(),windowListeners=new Map();state.timers=timers;
state.fireTimer=async id=>{const item=timers.get(id);if(!item)throw new Error('Missing timer');timers.delete(id);await item.callback();};
state.windowEvent=async(name,event={})=>{for(const callback of windowListeners.get(name)||[])await callback(event);};
const context=vm.createContext({document,fetch,Headers,AbortController,Blob,URL,crypto:require('node:crypto'),location:{href:'http://127.0.0.1/uploads'},window:{addEventListener(name,callback){if(!windowListeners.has(name))windowListeners.set(name,[]);windowListeners.get(name).push(callback);}},setTimeout:(callback,delay)=>{const id=++timer;timers.set(id,{callback,delay});return id;},clearTimeout:id=>timers.delete(id),__test:state});
(async()=>{vm.runInContext(fixture.script,context);await state.turn();const result=await vm.runInContext('(async()=>{'+fixture.exercise+'})()',context);await state.turn();process.stdout.write(JSON.stringify(result));})().catch(error=>{process.stderr.write(String(error.stack));process.exitCode=1;});
"""


def run_upload_ui(exercise):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for upload UI execution")
    parser = PageFixtureParser()
    parser.feed(UPLOAD_HTML)
    parser.close()
    assert len(parser.scripts) == 1
    fixture = {"page": parser.root, "script": parser.scripts[0], "exercise": exercise}
    result = subprocess.run([node], input="const fixture=" + json.dumps(fixture) + ";\n" + HARNESS,
                            text=True, encoding="utf-8", capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("tencent_mode,expected_modes", [("draft", ["publish", "draft"]), ("publish", ["publish"])])
def test_compose_creates_only_reviewable_drafts_and_groups_platform_modes(tencent_mode, expected_modes):
    result = run_upload_ui("""
for(const input of __test.all($('account-choices')).filter(item=>item.tagName==='input'))input.checked=true;
updateMetadata();$('title').value='Synthetic title';$('tags').value='synthetic';$('category-id').value='21';$('copyright').value='1';
$('tencent-mode').value=""" + json.dumps(tencent_mode) + ";" + """
await $('job-form').dispatch('submit');await __test.turn();await __test.turn();
return {posts:__test.requests.filter(item=>item.method==='POST').map(item=>({url:item.url,body:JSON.parse(item.body),nonce:item.headers['x-upload-csrf']})),message:$('message').textContent};
""")
    assert [post["body"]["mode"] for post in result["posts"]] == expected_modes
    assert all(post["url"] == "/api/v1/uploads/jobs" for post in result["posts"])
    assert all(post["nonce"] == "synthetic-session-nonce" for post in result["posts"])
    assert "本地草稿已创建" in result["message"]


def test_unknown_requires_explicit_acknowledgement_survives_polling_and_never_confirms():
    result = run_upload_ui(r"""
__test.jobs=[{id:'e'.repeat(32),platform:'douyin',account_name:'Synthetic',source_id:'c'.repeat(32),title:'<img src=x onerror=evil()>',tags:[],mode:'publish',state:'unknown',code:'interrupted_result_unknown'}];await refresh();
const maliciousText=$('jobs').textContent;let retry=__test.all($('jobs')).find(item=>item.tagName==='button');await retry.dispatch('click');await __test.turn();
const withoutAck=__test.requests.filter(item=>item.method==='POST').length;
let checkbox=__test.all($('jobs')).find(item=>item.tagName==='input');checkbox.checked=true;await checkbox.dispatch('change');await refresh();
const afterPoll=__test.all($('jobs')).find(item=>item.tagName==='input').checked;
retry=__test.all($('jobs')).find(item=>item.tagName==='button');await retry.dispatch('click');await __test.turn();await __test.turn();
return {withoutAck,afterPoll,maliciousText,posts:__test.requests.filter(item=>item.method==='POST').map(item=>({url:item.url,body:JSON.parse(item.body)}))};
""")
    assert result["withoutAck"] == 0
    assert result["afterPoll"] is True
    assert "<img src=x onerror=evil()>" in result["maliciousText"]
    assert len(result["posts"]) == 1
    assert result["posts"][0]["url"].endswith("/retry")
    assert result["posts"][0]["body"] == {"acknowledge_unknown": True}


def test_platform_upload_requires_pressing_individual_confirmation_button():
    result = run_upload_ui(r"""
__test.jobs=[{id:'e'.repeat(32),platform:'tencent',account_name:'Synthetic',source_id:'c'.repeat(32),title:'Synthetic',tags:[],mode:'draft',state:'draft'}];await refresh();
const before=__test.requests.filter(item=>item.method==='POST').length;
const confirm=__test.all($('jobs')).find(item=>item.tagName==='button'&&item.textContent==='确认上传并保存平台草稿');await confirm.dispatch('click');await __test.turn();await __test.turn();
return {before,posts:__test.requests.filter(item=>item.method==='POST').map(item=>item.url)};
""")
    assert result["before"] == 0
    assert result["posts"] == ["/api/v1/uploads/jobs/" + "e" * 32 + "/confirm"]


def test_scheduler_failure_is_visible_and_recovery_is_explicit():
    result = run_upload_ui(r"""
let uploadStatus={worker_running:false,scheduler_state:'faulted',scheduler_code:'scheduler_database_unavailable',backend:{ready:true},platforms:[{id:'bilibili',title_limit:80},{id:'tencent',title_limit:100}]};
const defaultFetch=fetch;fetch=async(url,options={})=>{
 if(url.endsWith('/status'))return {ok:true,status:200,json:async()=>uploadStatus};
 if(options.method==='POST'&&url.endsWith('/recover')){uploadStatus={...uploadStatus,worker_running:true,scheduler_state:'running',scheduler_code:'scheduler_recovered'};__test.requests.push({url,method:'POST',body:options.body,headers:Object.fromEntries(options.headers.entries())});return {ok:true,status:200,json:async()=>uploadStatus};}
 return defaultFetch(url,options);
};
await refresh();const before={text:$('engine-status').textContent,visible:!$('recover-scheduler').hidden};
await $('recover-scheduler').dispatch('click');for(let i=0;i<4;i++)await __test.turn();
return {before,after:$('engine-status').textContent,hidden:$('recover-scheduler').hidden,posts:__test.requests.filter(item=>item.method==='POST').map(item=>item.url)};
""")
    assert "上传调度故障" in result["before"]["text"]
    assert "数据库" in result["before"]["text"]
    assert result["before"]["visible"] is True
    assert "已恢复" in result["after"]
    assert result["hidden"] is True
    assert result["posts"] == ["/api/v1/uploads/recover"]


def test_scheduler_failure_stays_visible_when_database_lists_are_unavailable():
    result = run_upload_ui(r"""
const accountsBefore=$('accounts').textContent;const defaultFetch=fetch;
fetch=async(url,options={})=>{
 if(url.endsWith('/status'))return {ok:true,status:200,json:async()=>({worker_running:false,scheduler_state:'faulted',scheduler_code:'scheduler_database_unavailable',backend:{ready:true},platforms:[]})};
 if(options.method!=='POST'&&(url.endsWith('/accounts')||url.includes('/sources/page')||url.includes('/jobs/page')||url.endsWith('/operations')))return {ok:false,status:503,json:async()=>({detail:'upload_database_unavailable'})};
 return defaultFetch(url,options);
};
try{await refresh();}catch{}
return {engine:$('engine-status').textContent,recoverVisible:!$('recover-scheduler').hidden,recordsVisible:!$('records-status').hidden,records:$('records-status').textContent,accountsKept:$('accounts').textContent===accountsBefore,posts:__test.requests.filter(item=>item.method==='POST').length};
""")
    assert "上传调度故障" in result["engine"] and "数据库" in result["engine"]
    assert result["recoverVisible"] is True
    assert result["recordsVisible"] is True
    assert "上次成功读取" in result["records"]
    assert result["accountsKept"] is True
    assert result["posts"] == 0


def test_add_account_starts_inline_login_in_one_action_with_optional_name():
    result = run_upload_ui(r"""
$('account-platform').value='bilibili';$('account-name').value='';
await $('account-form').dispatch('submit');for(let i=0;i<8;i++)await __test.turn();
return {posts:__test.requests.filter(item=>item.method==='POST').map(item=>({url:item.url,body:item.body?JSON.parse(item.body):null})),panelVisible:!$('login-panel').hidden,title:$('login-title').textContent};
""")
    assert [item["url"] for item in result["posts"]] == [
        "/api/v1/uploads/accounts", "/api/v1/uploads/accounts/" + "f" * 32 + "/login"]
    assert result["posts"][0]["body"]["name"] == "Bilibili 账号 1"
    assert result["panelVisible"] and result["title"] == "正在获取二维码"


def test_inline_qr_uses_nonce_header_and_hides_after_scan():
    result = run_upload_ui(r"""
__test.operations=[{id:'e'.repeat(32),account_id:'a'.repeat(32),action:'login',state:'running',login_phase:'waiting_scan',qr_available:true,qr_revision:1,expires_at:Math.floor(Date.now()/1000)+120}];
await refresh();for(let i=0;i<3;i++)await __test.turn();
const shown=!$('login-qr').hidden, imageURL=$('login-qr').src;
__test.operations[0].login_phase='scanned';__test.operations[0].qr_available=false;await refresh();
return {shown,imageURL,hidden:$('login-qr').hidden,src:$('login-qr').src||null,title:$('login-title').textContent,qrRequests:__test.requests.filter(item=>item.url.endsWith('/qr')),posts:__test.requests.filter(item=>item.method==='POST').length};
""")
    assert result["shown"] and result["imageURL"].startswith("blob:")
    assert result["hidden"] and result["src"] is None
    assert result["title"] == "已扫码，等待手机确认"
    assert result["posts"] == 0
    assert len(result["qrRequests"]) == 1
    assert result["qrRequests"][0]["headers"]["x-upload-csrf"] == "synthetic-session-nonce"
    assert "?" not in result["qrRequests"][0]["url"]


def test_late_qr_response_cannot_restore_image_after_cancellation():
    result = run_upload_ui(r"""
let complete;__test.qrHandler=()=>new Promise(resolve=>complete=resolve);
__test.operations=[{id:'e'.repeat(32),account_id:'a'.repeat(32),action:'login',state:'running',login_phase:'waiting_scan',qr_available:true,qr_revision:1}];
await refresh();await __test.turn();__test.operations[0].state='canceled';await refresh();
complete({ok:true,headers:new Headers({'Content-Type':'image/png'}),blob:async()=>new Blob(['synthetic-png'],{type:'image/png'})});
for(let i=0;i<3;i++)await __test.turn();return {hidden:$('login-qr').hidden,src:$('login-qr').src||null,title:$('login-title').textContent};
""")
    assert result == {"hidden": True, "src": None, "title": "登录已取消"}


def test_expired_inline_qr_is_not_requested_and_offers_retry():
    result = run_upload_ui(r"""
__test.operations=[{id:'e'.repeat(32),account_id:'a'.repeat(32),action:'login',state:'running',login_phase:'waiting_scan',qr_available:true,qr_revision:1,expires_at:Math.floor(Date.now()/1000)-1}];await refresh();await __test.turn();
return {hidden:$('login-qr').hidden,title:$('login-title').textContent,retryVisible:!$('login-retry').hidden,qrRequests:__test.requests.filter(item=>item.url.endsWith('/qr')).length};
""")
    assert result == {"hidden": True, "title": "二维码已过期", "retryVisible": True, "qrRequests": 0}


def test_bfcache_return_refetches_qr_and_restores_one_poll_and_one_countdown():
    result = run_upload_ui(r"""
__test.operations=[{id:'e'.repeat(32),account_id:'a'.repeat(32),action:'login',state:'running',login_phase:'waiting_scan',qr_available:true,qr_revision:1,expires_at:Math.floor(Date.now()/1000)+120}];
await refresh();for(let i=0;i<3;i++)await __test.turn();
const firstImage=$('login-qr').src,cycles=[];
for(let cycle=0;cycle<2;cycle++){
 await __test.windowEvent('pagehide',{persisted:true});
 const hidden=$('login-qr').hidden,removed=!$('login-qr').src,paused=__test.timers.size;
 await __test.windowEvent('pageshow',{persisted:true});
 for(let i=0;i<3;i++)await __test.turn();
 // Duplicate restore notifications must replace timers instead of multiplying them.
 await __test.windowEvent('pageshow',{persisted:true});
 for(let i=0;i<3;i++)await __test.turn();
 cycles.push({hidden,removed,paused,visible:!$('login-qr').hidden,fresh:$('login-qr').src!==firstImage,timers:[...__test.timers.values()].map(item=>item.delay).sort()});
}
const before=__test.requests.filter(item=>item.url.endsWith('/operations')).length;
__test.operations[0].state='ready';__test.operations[0].qr_available=false;
const pollTimer=[...__test.timers].find(([id,item])=>item.delay===1500)[0];await __test.fireTimer(pollTimer);await __test.turn();
return {cycles,qrRequests:__test.requests.filter(item=>item.url.endsWith('/qr')).length,pollResumed:__test.requests.filter(item=>item.url.endsWith('/operations')).length===before+1,title:$('login-title').textContent,timers:[...__test.timers.values()].map(item=>item.delay).sort()};
""")
    assert result["cycles"] == [{"hidden": True, "removed": True, "paused": 0,
                                 "visible": True, "fresh": True, "timers": [1000, 1500]}] * 2
    assert result["qrRequests"] == 3
    assert result["pollResumed"] and result["title"] == "登录成功"
    assert result["timers"] == [1000, 3000]


def test_poll_finishing_after_pagehide_does_not_restore_qr_or_restart_timers():
    result = run_upload_ui(r"""
__test.operations=[{id:'e'.repeat(32),account_id:'a'.repeat(32),action:'login',state:'running',login_phase:'waiting_scan',qr_available:true,qr_revision:1}];
await refresh();for(let i=0;i<3;i++)await __test.turn();
let release;__test.requestHook=url=>url.endsWith('/status')?new Promise(resolve=>release=resolve):undefined;
const polling=poll();await __test.turn();await __test.windowEvent('pagehide',{persisted:true});
release();await polling;for(let i=0;i<3;i++)await __test.turn();
const paused={hidden:$('login-qr').hidden,timers:__test.timers.size,qrRequests:__test.requests.filter(item=>item.url.endsWith('/qr')).length};
__test.requestHook=null;await __test.windowEvent('pageshow',{persisted:true});for(let i=0;i<3;i++)await __test.turn();
return {paused,restored:!$('login-qr').hidden,timers:[...__test.timers.values()].map(item=>item.delay).sort(),qrRequests:__test.requests.filter(item=>item.url.endsWith('/qr')).length};
""")
    assert result["paused"] == {"hidden": True, "timers": 0, "qrRequests": 1}
    assert result["restored"] and result["timers"] == [1000, 1500]
    assert result["qrRequests"] == 2


def test_job_details_keep_independent_choices_across_polling_and_terminal_changes():
    result = run_upload_ui(r"""
const base={platform:'tencent',account_name:'Synthetic',source_id:'c'.repeat(32),title:'Synthetic',tags:[],mode:'publish'};
__test.jobs=[{...base,id:'1'.repeat(32),state:'draft'},{...base,id:'2'.repeat(32),state:'draft'},{...base,id:'3'.repeat(32),state:'submitted'},{...base,id:'4'.repeat(32),state:'canceled'}];
__test.sources.push({id:'5'.repeat(32),name:'second.mp4',size:99,sha256:'6'.repeat(64)});
__test.operations=[{id:'e'.repeat(32),account_id:'a'.repeat(32),action:'login',state:'running',login_phase:'waiting_scan',qr_available:true,qr_revision:1,expires_at:Math.floor(Date.now()/1000)+120}];
await refresh();for(let i=0;i<3;i++)await __test.turn();
const preview=id=>__test.all($('jobs')).find(item=>item.tagName==='details'&&item.dataset.jobId===id.repeat(32));
const values=()=>Object.fromEntries(['1','2','3','4'].map(id=>[id,preview(id).open]));
const initial=values(),firstQR=$('login-qr').src;
// Changing open directly also covers a poll before the browser's queued toggle event.
preview('1').open=false;preview('3').open=true;
for(const input of __test.all($('account-choices')).filter(item=>item.tagName==='input'))input.checked=input.value==='a'.repeat(32);
$('source-id').value='5'.repeat(32);
__test.jobs.reverse();await poll();await __test.turn();const afterPoll=values();
__test.jobs.find(item=>item.id==='1'.repeat(32)).state='canceled';
__test.jobs.find(item=>item.id==='2'.repeat(32)).state='failed';
await poll();await __test.turn();
return {initial,afterPoll,afterTerminal:values(),selectedAccounts:selectedAccounts(),selectedSource:$('source-id').value,qrUnchanged:$('login-qr').src===firstQR&&!$('login-qr').hidden,qrRequests:__test.requests.filter(item=>item.url.endsWith('/qr')).length,posts:__test.requests.filter(item=>item.method==='POST').length};
""")
    assert result["initial"] == {"1": True, "2": True, "3": False, "4": False}
    expected = {"1": False, "2": True, "3": True, "4": False}
    assert result["afterPoll"] == result["afterTerminal"] == expected
    assert result["selectedAccounts"] == ["a" * 32]
    assert result["selectedSource"] == "5" * 32
    assert result["qrUnchanged"] and result["qrRequests"] == 1
    assert result["posts"] == 0


def test_cancel_and_recreate_keep_old_details_choice_and_open_the_new_draft():
    result = run_upload_ui(r"""
const original={id:'1'.repeat(32),platform:'douyin',account_name:'Synthetic',source_id:'c'.repeat(32),title:'Same title',tags:[],mode:'publish',state:'draft'};
__test.jobs=[original,{...original,id:'2'.repeat(32),state:'failed'}];
await refresh();
const preview=id=>__test.all($('jobs')).find(item=>item.tagName==='details'&&item.dataset.jobId===id.repeat(32));
const jobButton=(id,label)=>{const article=[...$('jobs').children].find(item=>__test.all(item).some(child=>child===preview(id)));return __test.all(article).find(item=>item.tagName==='button'&&item.textContent===label);};
preview('1').open=false;preview('2').open=true;
__test.requestHook=(url,options)=>{
 if(options.method==='POST'&&url.endsWith('/cancel')){original.state='canceled';original.code='canceled';}
 if(options.method==='POST'&&url.endsWith('/retry'))__test.jobs.unshift({...original,id:'3'.repeat(32),state:'draft',code:'',retry_of:original.id});
};
await jobButton('1','取消').dispatch('click');await __test.turn();
const afterCancel={old:preview('1').open,other:preview('2').open};
await jobButton('1','重新创建本地草稿').dispatch('click');await __test.turn();
const afterRetry={old:preview('1').open,other:preview('2').open,newDraft:preview('3').open};
preview('3').open=false;preview('1').open=true;await poll();
return {afterCancel,afterRetry,afterPoll:{old:preview('1').open,other:preview('2').open,newDraft:preview('3').open},posts:__test.requests.filter(item=>item.method==='POST').map(item=>item.url)};
""")
    assert result["afterCancel"] == {"old": False, "other": True}
    assert result["afterRetry"] == {"old": False, "other": True, "newDraft": True}
    assert result["afterPoll"] == {"old": True, "other": True, "newDraft": False}
    assert result["posts"] == ["/api/v1/uploads/jobs/" + "1" * 32 + suffix
                               for suffix in ("/cancel", "/retry")]


@pytest.mark.parametrize("selection_change", ["cleared", "missing"])
def test_source_selection_stays_empty_after_user_clear_or_missing_selected_source(selection_change):
    result = run_upload_ui(r"""
const initial=$('source-id').value;
""" + ("$('source-id').value='';showSource();" if selection_change == "cleared" else
       "__test.sources=[{id:'9'.repeat(32),name:'different.mp4',size:7,sha256:'8'.repeat(64)}];") + r"""
await poll();const afterPoll=$('source-id').value,shaAfterPoll=$('source-info').textContent;
await $('refresh').dispatch('click');await __test.turn();
for(const input of __test.all($('account-choices')).filter(item=>item.tagName==='input'))input.checked=input.value==='b'.repeat(32);
updateMetadata();$('title').value='Synthetic';await $('job-form').dispatch('submit');for(let i=0;i<3;i++)await __test.turn();
return {initial,afterPoll,shaAfterPoll,afterRefresh:$('source-id').value,posts:__test.requests.filter(item=>item.method==='POST').map(item=>item.url),message:$('message').textContent};
""")
    assert result["initial"] == "c" * 32
    assert result["afterPoll"] == result["afterRefresh"] == ""
    assert result["shaAfterPoll"] == ""
    assert result["posts"] == []
    assert result["message"] == "请先导入并选择视频。"


def test_history_pagination_preserves_active_job_details_and_empty_source_choice():
    result = run_upload_ui(r"""
const active={id:'1'.repeat(32),platform:'douyin',account_name:'Synthetic',source_id:'8'.repeat(32),title:'Active',tags:[],mode:'publish',state:'running'};
const history={...active,id:'2'.repeat(32),source_id:'7'.repeat(32),source_name:'old.mp4',title:'History',state:'submitted'};
const currentSource={id:'8'.repeat(32),name:'current.mp4',size:8,sha256:'8'.repeat(64)};
const oldSource={id:'7'.repeat(32),name:'old.mp4',size:7,sha256:'7'.repeat(64)};
__test.sourcePageHandler=url=>url.includes('cursor=')?{items:[oldSource],next_cursor:null}:{items:[currentSource],next_cursor:'1:2'};
__test.jobPageHandler=url=>url.includes('cursor=')?{items:[history],next_cursor:null}:{items:[active],next_cursor:'2:2'};
await refresh();$('source-id').value='';showSource();
let activeDetails=__test.all($('jobs')).find(item=>item.tagName==='details'&&item.dataset.jobId===active.id);activeDetails.open=false;
await $('more-jobs').dispatch('click');for(let i=0;i<3;i++)await __test.turn();
const historyBeforeSourcePage=[...$('jobs').children].find(item=>item.dataset.jobId===history.id).textContent;
await $('more-sources').dispatch('click');for(let i=0;i<3;i++)await __test.turn();
await poll();
activeDetails=__test.all($('jobs')).find(item=>item.tagName==='details'&&item.dataset.jobId===active.id);
return {sourceValue:$('source-id').value,sourceNames:__test.all($('source-id')).filter(item=>item.tagName==='option').map(item=>item.textContent),historyBeforeSourcePage,jobTitles:[...$('jobs').children].map(item=>item.textContent),activeOpen:activeDetails.open,moreSourcesHidden:$('more-sources').hidden,moreJobsHidden:$('more-jobs').hidden,posts:__test.requests.filter(item=>item.method==='POST').length};
""")
    assert result["sourceValue"] == ""
    assert any("old.mp4" in value for value in result["sourceNames"])
    assert "old.mp4" in result["historyBeforeSourcePage"]
    assert any("History" in value for value in result["jobTitles"])
    assert result["activeOpen"] is False
    assert result["moreSourcesHidden"] is True
    assert result["moreJobsHidden"] is True
    assert result["posts"] == 0


def test_poll_resolves_running_job_after_it_moves_out_of_the_first_page():
    result = run_upload_ui(r"""
const running={id:'1'.repeat(32),platform:'douyin',account_name:'Synthetic',source_id:'c'.repeat(32),source_name:'synthetic.mp4',title:'Tracked',tags:[],mode:'publish',state:'running',code:''};
const replacement={...running,id:'2'.repeat(32),title:'Recent draft',state:'draft'};let completed=null;__test.jobs=[running];
__test.jobPageHandler=()=>({items:completed?[replacement]:[running],next_cursor:'2:2'});await refresh();
completed={...running,state:'submitted',code:'upstream_submitted'};__test.jobs=[completed];await poll();
const target=[...$('jobs').children].find(item=>item.dataset.jobId===running.id);
return {found:!!target,text:target?.textContent||'',resolveRequests:__test.requests.filter(item=>item.url.includes('/jobs/resolve?')).length,posts:__test.requests.filter(item=>item.method==='POST').length};
""")
    assert result["found"]
    assert "上游报告投稿完成" in result["text"]
    assert "执行中" not in result["text"]
    assert result["resolveRequests"] == 1
    assert result["posts"] == 0


def test_cancel_response_keeps_old_draft_visible_when_it_leaves_the_first_page():
    result = run_upload_ui(r"""
const original={id:'1'.repeat(32),platform:'douyin',account_name:'Synthetic',source_id:'c'.repeat(32),source_name:'synthetic.mp4',title:'Old draft',tags:[],mode:'publish',state:'draft',code:''};
const replacement={...original,id:'2'.repeat(32),title:'Recent draft'};__test.jobs=[original];
__test.jobPageHandler=()=>({items:original.state==='draft'?[original]:[replacement],next_cursor:'2:2'});await refresh();
const defaultFetch=fetch;fetch=async(url,options={})=>{const response=await defaultFetch(url,options);if(options.method==='POST'&&url.endsWith('/cancel')){original.state='canceled';original.code='canceled';return {ok:true,status:200,json:async()=>original};}return response;};
const article=[...$('jobs').children].find(item=>item.dataset.jobId===original.id);const cancel=__test.all(article).find(item=>item.tagName==='button'&&item.textContent==='取消');await cancel.dispatch('click');for(let i=0;i<4;i++)await __test.turn();
const target=[...$('jobs').children].find(item=>item.dataset.jobId===original.id);
return {found:!!target,text:target?.textContent||'',posts:__test.requests.filter(item=>item.method==='POST').map(item=>item.url)};
""")
    assert result["found"]
    assert "已取消" in result["text"]
    assert "确认立即上传投稿" not in result["text"]
    assert result["posts"] == ["/api/v1/uploads/jobs/" + "1" * 32 + "/cancel"]


def test_importing_source_explicitly_selects_it_after_previous_selection_was_cleared():
    result = run_upload_ui(r"""
$('source-id').value='';showSource();await poll();
const imported={id:'9'.repeat(32),name:'new.mp4',size:13,sha256:'8'.repeat(64)};
const unchangedFirstPage=[...__test.sources];__test.sourcePageHandler=()=>({items:unchangedFirstPage,next_cursor:'2:1'});
const defaultFetch=fetch;fetch=async(url,options={})=>{const response=await defaultFetch(url,options);if(options.method==='POST'&&url.startsWith('/api/v1/uploads/sources?')){__test.sources.unshift(imported);return {ok:true,status:201,json:async()=>imported};}return response;};
const file=new Blob(['synthetic-mp4'],{type:'video/mp4'});file.name='new.mp4';$('source-file').files=[file];
await $('source-form').dispatch('submit');for(let i=0;i<6;i++)await __test.turn();
const selectedAfterImport=$('source-id').value;await poll();
return {selectedAfterImport,selectedAfterPoll:$('source-id').value,sha:$('source-info').textContent,posts:__test.requests.filter(item=>item.method==='POST').map(item=>item.url)};
""")
    assert result["selectedAfterImport"] == result["selectedAfterPoll"] == "9" * 32
    assert result["sha"] == "SHA-256：" + "8" * 64
    assert result["posts"] == ["/api/v1/uploads/sources?name=new.mp4"]


@pytest.mark.parametrize("successor_state,label", [
    ("draft", "本地草稿已就绪"),
    ("canceled", "已取消"),
    ("running", "执行中"),
    ("submitted", "上游报告投稿完成"),
    ("draft_saved", "上游报告草稿已保存"),
])
def test_retry_reports_returned_successor_state_and_focuses_it_without_confirming(successor_state, label):
    result = run_upload_ui(r"""
const original={id:'1'.repeat(32),platform:'tencent',account_name:'Synthetic',source_id:'c'.repeat(32),title:'Original',tags:[],mode:'draft',state:'canceled'};
const successor={...original,id:'2'.repeat(32),state:""" + json.dumps(successor_state) + r"""};
__test.jobs=[successor,original];await refresh();
const defaultFetch=fetch;fetch=async(url,options={})=>{const response=await defaultFetch(url,options);return url.endsWith('/retry')?{ok:true,status:201,json:async()=>successor}:response;};
const articleFor=id=>[...$('jobs').children].find(item=>__test.all(item).some(child=>child.tagName==='details'&&child.dataset.jobId===id));
const retry=__test.all(articleFor(original.id)).find(item=>item.tagName==='button'&&item.textContent==='重新创建本地草稿');await retry.dispatch('click');await __test.turn();
const target=articleFor(successor.id);
return {message:$('message').textContent,focused:!!target.focused,scrolled:!!target.scrolledIntoView,jobCount:__test.jobs.length,posts:__test.requests.filter(item=>item.method==='POST').map(item=>item.url)};
""")
    assert label in result["message"]
    assert "新本地草稿已创建" not in result["message"]
    if successor_state != "draft":
        assert "未创建新草稿" in result["message"]
    assert result["focused"] and result["scrolled"]
    assert result["jobCount"] == 2
    assert result["posts"] == ["/api/v1/uploads/jobs/" + "1" * 32 + "/retry"]


def test_retry_fetches_and_focuses_successor_and_its_source_outside_loaded_history():
    result = run_upload_ui(r"""
const original={id:'1'.repeat(32),platform:'douyin',account_name:'Synthetic',source_id:'c'.repeat(32),title:'Original',tags:[],mode:'publish',state:'canceled'};
const successor={...original,id:'2'.repeat(32),source_id:'7'.repeat(32),title:'Older successor',state:'draft',retry_of:original.id};
const oldSource={id:'7'.repeat(32),name:'older-source.mp4',size:77,sha256:'7'.repeat(64)};
__test.jobs=[successor];__test.sources=[oldSource];
__test.jobPageHandler=()=>({items:[original],next_cursor:null});__test.sourcePageHandler=()=>({items:[],next_cursor:null});await refresh();
const defaultFetch=fetch;fetch=async(url,options={})=>{const response=await defaultFetch(url,options);return options.method==='POST'&&url.endsWith('/retry')?{ok:true,status:201,json:async()=>successor}:response;};
const article=[...$('jobs').children].find(item=>item.dataset.jobId===original.id);const retry=__test.all(article).find(item=>item.tagName==='button'&&item.textContent==='重新创建本地草稿');await retry.dispatch('click');for(let i=0;i<5;i++)await __test.turn();
const target=[...$('jobs').children].find(item=>item.dataset.jobId===successor.id);const focused=!!target?.focused,scrolled=!!target?.scrolledIntoView;await poll();const afterPoll=[...$('jobs').children].find(item=>item.dataset.jobId===successor.id);
return {found:!!target,focused,scrolled,persisted:!!afterPoll,text:afterPoll?.textContent||'',gets:__test.requests.filter(item=>item.method==='GET'&&(item.url.includes('/jobs/')||item.url.includes('/sources/'))).map(item=>item.url),posts:__test.requests.filter(item=>item.method==='POST').map(item=>item.url)};
""")
    assert result["found"] and result["focused"] and result["scrolled"] and result["persisted"]
    assert "older-source.mp4" in result["text"]
    assert "/api/v1/uploads/jobs/" + "2" * 32 in result["gets"]
    assert "/api/v1/uploads/sources/" + "7" * 32 in result["gets"]
    assert result["posts"] == ["/api/v1/uploads/jobs/" + "1" * 32 + "/retry"]
