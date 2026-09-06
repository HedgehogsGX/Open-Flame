from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from test_batch_assets_ui import PageFixtureParser
from video_download_control.uploads.web import UPLOAD_HTML


HARNESS = r"""
const vm=require('node:vm');const ids=new Map();
function contains(root,target){return root===target||root.children.some(item=>contains(item,target));}
class Element{
 constructor(tag,attrs={},text=''){this.tagName=tag;this.children=[];this.parentElement=null;this.listeners=new Map();this._text=text;this._textWriteCount=0;this.value=attrs.value||'';this.hidden=Object.hasOwn(attrs,'hidden');this.disabled=Object.hasOwn(attrs,'disabled');this.checked=false;this.dataset={};this.attributes={...attrs};this.className=attrs.class||'';this.type=attrs.type||'';this.files=[];this.tabIndex=attrs.tabindex===undefined?0:Number(attrs.tabindex);if(attrs.id)ids.set(attrs.id,this);}
 get textContent(){return this._text+this.children.map(item=>item.textContent).join('');}
 set textContent(value){this._textWriteCount+=1;this._text=String(value);for(const child of [...this.children])this.detach(child,false);}
 set innerHTML(value){throw new Error('Unsafe innerHTML assignment');}
 get firstChild(){return this.children[0];}get lastElementChild(){return [...this.children].reverse().find(item=>item.tagName!=='#text')||null;}
 get isConnected(){return this.tagName==='document'||!!this.parentElement?.isConnected;}
 removeAttribute(name){delete this.attributes[name];delete this[name];}setAttribute(name,value){this.attributes[name]=String(value);}
 focus(){if(document.activeElement)document.activeElement.focused=false;document.activeElement=this;this.focused=true;}scrollIntoView(){this.scrolledIntoView=true;}
 detach(item,preserveFocus){const index=this.children.indexOf(item);if(index<0)return;if(!preserveFocus&&document.activeElement&&contains(item,document.activeElement)){document.activeElement.focused=false;document.activeElement=null;}this.children.splice(index,1);item.parentElement=null;}
 append(...items){for(let item of items){if(typeof item==='string')item=new Element('#text',{},item);if(item.parentElement)item.parentElement.detach(item,true);item.parentElement=this;this.children.push(item);}}
 insertBefore(item,reference){if(reference!==null&&!this.children.includes(reference))throw new Error('Reference is not a child');if(item.parentElement)item.parentElement.detach(item,true);const index=reference===null?this.children.length:this.children.indexOf(reference);item.parentElement=this;this.children.splice(index,0,item);return item;}
 remove(){if(this.parentElement)this.parentElement.detach(this,false);}
 replaceChildren(...items){this._text='';for(const child of [...this.children])this.detach(child,false);this.append(...items);}
 querySelectorAll(selector){if(selector==='details[data-job-id]')return all(this).filter(item=>item.tagName==='details'&&item.dataset.jobId!==undefined);throw new Error('Unsupported selector '+selector);}
 addEventListener(name,callback){if(!this.listeners.has(name))this.listeners.set(name,[]);this.listeners.get(name).push(callback);}
 async dispatch(name,event={}){const payload={target:this,currentTarget:this,isComposing:false,key:'',keyCode:0,defaultPrevented:false,...event};payload.preventDefault=()=>{payload.defaultPrevented=true;};for(const callback of this.listeners.get(name)||[])await callback(payload);return payload;}
}
function construct(node){const item=new Element(node.tag,node.attrs,node.text||'');for(const child of node.children||[])item.append(construct(child));if(node.tag==='select')item.value=item.children.find(child=>child.tagName==='option')?.value||'';return item;}
const root=construct(fixture.page);function all(item){return [item,...item.children.flatMap(all)];}
const documentListeners=new Map();
const document={activeElement:null,hidden:false,getElementById:id=>ids.get(id),createElement:tag=>new Element(tag),querySelectorAll(selector){if(selector==='button')return all(root).filter(item=>item.tagName==='button');if(selector==='#account-choices input:checked')return all(ids.get('account-choices')).filter(item=>item.tagName==='input'&&item.checked);throw new Error('Unsupported selector '+selector);},addEventListener(name,callback){if(!documentListeners.has(name))documentListeners.set(name,[]);documentListeners.get(name).push(callback);}};
const state={requests:[],accounts:[{id:'a'.repeat(32),platform:'bilibili',name:'Synthetic Bili',auth_state:'ready',lifecycle_state:'active',disconnected_at:null},{id:'b'.repeat(32),platform:'tencent',name:'Synthetic Tencent',auth_state:'ready',lifecycle_state:'active',disconnected_at:null}],sources:[{id:'c'.repeat(32),name:'synthetic.mp4',size:42,sha256:'d'.repeat(64),media_present:true,media_state:'present',media_deleted_at:null,active_reference_count:0,can_delete:true}],storage:{managed_bytes:42,registered_source_count:1,present_source_count:1,missing_source_count:0,deleted_source_count:0,changed_source_count:0,unsafe_source_count:0,orphan_file_count:0,orphan_bytes:0,unsafe_entry_count:0,free_bytes:1073741824,reserve_bytes:67108864,low_space:false},jobs:[],operations:[],turn:()=>new Promise(resolve=>setImmediate(resolve)),all};
async function fetch(url,options={}){state.requests.push({url,method:options.method||'GET',body:options.body,headers:Object.fromEntries(options.headers.entries())});if(state.requestHook)await state.requestHook(url,options);let payload;
 if(url.endsWith('/session'))payload={csrf_token:'synthetic-session-nonce'};
 else if(url.endsWith('/status'))payload={worker_running:true,backend:{ready:true},platforms:[{id:'bilibili',title_limit:80},{id:'tencent',title_limit:100}]};
 else if(options.method==='POST'&&url.endsWith('/accounts')){const body=JSON.parse(options.body);payload={id:'f'.repeat(32),...body,auth_state:'unchecked',lifecycle_state:'active',disconnected_at:null};state.accounts.push(payload);}
 else if(options.method==='POST'&&/\/accounts\/[0-9a-f]{32}\/disconnect$/.test(url)){const account=state.accounts.find(item=>url.includes(item.id));if(state.disconnectHandler){const response=await state.disconnectHandler(account,options);if(response)return response;}account.lifecycle_state='disconnected';account.auth_state='unchecked';account.code='account_disconnected';account.disconnected_at=account.disconnected_at||'2026-09-07T01:02:03+00:00';for(const job of state.jobs)if(job.account_id===account.id&&job.state==='queued'){job.state='draft';job.code='account_disconnected_confirmation_revoked';job.account_lifecycle_state='disconnected';}payload={account,revoked_confirmation_count:state.jobs.filter(item=>item.account_id===account.id&&item.code==='account_disconnected_confirmation_revoked').length,canceled_operation_count:0,local_login_removed:true};}
 else if(options.method==='POST'&&url.endsWith('/login')){payload={id:'e'.repeat(32),account_id:url.split('/').at(-2),action:'login',state:'running',login_phase:'preparing',qr_revision:0,qr_available:false};state.operations=[payload];}
 else if(options.method==='DELETE'&&/\/sources\/[0-9a-f]{32}\/media$/.test(url)){const source=state.sources.find(item=>url.includes(item.id));source.media_present=false;source.media_state='deleted';source.media_deleted_at='2026-09-07T01:03:04+00:00';source.can_delete=false;state.storage.managed_bytes-=source.size;state.storage.present_source_count--;state.storage.deleted_source_count++;payload=source;}
 else if(options.method==='POST'&&/\/sources\/[0-9a-f]{32}\/media$/.test(url)){const source=state.sources.find(item=>url.includes(item.id));if(state.restoreHandler){const response=await state.restoreHandler(source,options);if(response)return response;}const previous=source.media_state;source.media_present=true;source.media_state='present';source.media_deleted_at=null;source.can_delete=source.active_reference_count===0;state.storage.managed_bytes+=source.size;state.storage.present_source_count++;if(previous==='deleted')state.storage.deleted_source_count--;if(previous==='missing')state.storage.missing_source_count--;for(const job of state.jobs)if(job.source_id===source.id){job.source_media_present=true;job.source_media_state='present';}payload=source;}
 else if(options.method==='POST'&&url.endsWith('/cancel')){const op=state.operations.find(item=>url.includes(item.id));if(op)op.state='canceled';payload=op||{state:'canceled'};}
 else if(options.method==='POST')payload={state:'draft'};
 else if(url.endsWith('/qr')){if(state.qrHandler)return state.qrHandler();return {ok:true,status:200,headers:new Headers({'Content-Type':'image/png'}),blob:async()=>new Blob(['synthetic-png'],{type:'image/png'})};}
 else if(url.includes('/jobs/resolve?')){const wanted=new URL(url,'http://127.0.0.1').searchParams.get('ids').split(',');payload=state.jobs.filter(item=>wanted.includes(item.id));}
 else if(url.includes('/sources/page'))payload=state.sourcePageHandler?state.sourcePageHandler(url):{items:state.sources,next_cursor:null};else if(url.includes('/jobs/page'))payload=state.jobPageHandler?state.jobPageHandler(url):{items:state.jobs,next_cursor:null};
 else if(/\/sources\/[0-9a-f]{32}$/.test(url))payload=state.sources.find(item=>url.endsWith(item.id));else if(/\/jobs\/[0-9a-f]{32}$/.test(url))payload=state.jobs.find(item=>url.endsWith(item.id));
 else if(url.endsWith('/accounts'))payload=state.accounts;else if(url.endsWith('/sources'))payload=state.sources;else if(url.endsWith('/jobs'))payload=state.jobs;else if(url.endsWith('/operations'))payload=state.operations;else if(url.endsWith('/storage'))payload=state.storage;
 else throw new Error('Unexpected fetch '+url);return {ok:true,status:200,json:async()=>payload};}
let timer=0;const timers=new Map(),windowListeners=new Map();state.timers=timers;
state.fireTimer=async id=>{const item=timers.get(id);if(!item)throw new Error('Missing timer');timers.delete(id);await item.callback();};
state.windowEvent=async(name,event={})=>{for(const callback of windowListeners.get(name)||[])await callback(event);};
state.documentEvent=async(name,event={})=>{for(const callback of documentListeners.get(name)||[])await callback(event);};
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


def test_disconnect_is_two_step_preserves_focus_and_keeps_tombstone_out_of_compose():
    result = run_upload_ui(r"""
$('title').value='保留正在编辑的标题';
let card=[...$('accounts').children].find(item=>item.dataset.accountId==='a'.repeat(32));
let open=__test.all(card).find(item=>item.tagName==='button'&&item.textContent==='断开本地账号');
open.focus();await open.dispatch('click');
card=[...$('accounts').children].find(item=>item.dataset.accountId==='a'.repeat(32));
let confirm=__test.all($('accounts')).find(item=>item.tagName==='button'&&item.textContent==='确认断开本地账号');
const afterFirst={posts:__test.requests.filter(item=>item.method==='POST').length,focused:document.activeElement===confirm,copy:card.textContent};
await poll();
confirm=__test.all($('accounts')).find(item=>item.tagName==='button'&&item.textContent==='确认断开本地账号');
const afterPoll={visible:!!confirm,focused:document.activeElement===confirm,title:$('title').value};
await confirm.dispatch('click');for(let i=0;i<5;i++)await __test.turn();
card=[...$('accounts').children].find(item=>item.dataset.accountId==='a'.repeat(32));
return {afterFirst,afterPoll,card:card.textContent,focusedAfter:document.activeElement===card,choiceIds:__test.all($('account-choices')).filter(item=>item.tagName==='input').map(item=>item.value),posts:__test.requests.filter(item=>item.method==='POST').map(item=>({url:item.url,nonce:item.headers['x-upload-csrf']})),message:$('message').textContent,title:$('title').value};
""")
    assert result["afterFirst"]["posts"] == 0
    assert result["afterFirst"]["focused"] is True
    assert "不会撤销平台侧授权" in result["afterFirst"]["copy"]
    assert result["afterPoll"] == {
        "visible": True,
        "focused": True,
        "title": "保留正在编辑的标题",
    }
    assert "已断开本地账号" in result["card"]
    assert result["focusedAfter"] is True
    assert "a" * 32 not in result["choiceIds"]
    assert result["posts"] == [{
        "url": "/api/v1/uploads/accounts/" + "a" * 32 + "/disconnect",
        "nonce": "synthetic-session-nonce",
    }]
    assert "已撤回" in result["message"]
    assert result["title"] == "保留正在编辑的标题"


def test_login_tick_does_not_rewrite_unchanged_live_description():
    result = run_upload_ui(r"""
const accountId='a'.repeat(32);
__test.operations=[{id:'e'.repeat(32),account_id:accountId,action:'login',state:'running',login_phase:'waiting_scan',qr_revision:1,qr_available:false,expires_at:Date.now()/1000+90}];
await refresh();
const description=$('login-description'),before=description._textWriteCount;
renderLogin();renderLogin();
return {text:description.textContent,writesBefore:before,writesAfter:description._textWriteCount};
""")
    assert "扫码" in result["text"]
    assert result["writesAfter"] == result["writesBefore"]


def test_job_validation_marks_and_focuses_the_first_invalid_control():
    result = run_upload_ui(r"""
const form=$('job-form'), error=$('job-form-error');
for(const input of __test.all($('account-choices')).filter(item=>item.tagName==='input'))input.checked=false;
await form.dispatch('submit');
const account={text:error.textContent,invalid:$('account-selection').attributes['aria-invalid'],focused:document.activeElement===$('account-selection')};
const bili=__test.all($('account-choices')).find(item=>item.tagName==='input'&&item.value==='a'.repeat(32));bili.checked=true;updateMetadata();
$('source-id').value='';await form.dispatch('submit');
const source={text:error.textContent,invalid:$('source-id').attributes['aria-invalid'],focused:document.activeElement===$('source-id')};
$('source-id').value='c'.repeat(32);$('title').value='';await form.dispatch('submit');
const title={text:error.textContent,invalid:$('title').attributes['aria-invalid'],focused:document.activeElement===$('title')};
$('title').value='Synthetic';$('category-id').value='';$('copyright').value='';await form.dispatch('submit');
const category={text:error.textContent,invalid:$('category-id').attributes['aria-invalid'],focused:document.activeElement===$('category-id')};
$('category-id').value='21';await form.dispatch('submit');
const copyright={text:error.textContent,invalid:$('copyright').attributes['aria-invalid'],focused:document.activeElement===$('copyright')};
$('copyright').value='2';$('source-credit').value='';await form.dispatch('submit');
const credit={text:error.textContent,invalid:$('source-credit').attributes['aria-invalid'],focused:document.activeElement===$('source-credit')};
return {account,source,title,category,copyright,credit,posts:__test.requests.filter(item=>item.method==='POST').length,describedBy:$('source-id').attributes['aria-describedby']};
""")
    assert result["account"] == {
        "text": "请选择至少一个账号。",
        "invalid": "true",
        "focused": True,
    }
    assert result["source"] == {
        "text": "请先导入并选择视频。",
        "invalid": "true",
        "focused": True,
    }
    assert result["title"] == {
        "text": "请填写标题。",
        "invalid": "true",
        "focused": True,
    }
    assert result["category"] == {
        "text": "请填写 Bilibili 分区 ID。",
        "invalid": "true",
        "focused": True,
    }
    assert result["copyright"] == {
        "text": "请选择 Bilibili 原创或转载。",
        "invalid": "true",
        "focused": True,
    }
    assert result["credit"] == {
        "text": "转载必须填写来源。",
        "invalid": "true",
        "focused": True,
    }
    assert result["posts"] == 0
    assert result["describedBy"] == "source-info job-form-error"


def test_disconnect_cleanup_failure_is_visible_and_retryable():
    result = run_upload_ui(r"""
__test.disconnectHandler=account=>{account.lifecycle_state='disconnected';account.auth_state='unchecked';account.code='account_disconnect_cleanup_failed';account.disconnected_at='2026-09-07T01:02:03+00:00';__test.disconnectHandler=null;return {ok:false,status:409,json:async()=>({detail:'account_disconnect_cleanup_failed'})};};
let card=[...$('accounts').children].find(item=>item.dataset.accountId==='a'.repeat(32));
let open=__test.all(card).find(item=>item.tagName==='button'&&item.textContent==='断开本地账号');await open.dispatch('click');
let confirm=__test.all($('accounts')).find(item=>item.tagName==='button'&&item.textContent==='确认断开本地账号');await confirm.dispatch('click');for(let i=0;i<6;i++)await __test.turn();
card=[...$('accounts').children].find(item=>item.dataset.accountId==='a'.repeat(32));let retry=__test.all(card).find(item=>item.tagName==='button'&&item.textContent==='重试清理本地登录');
const failed={card:card.textContent,retry:!!retry,message:$('message').textContent,choices:__test.all($('account-choices')).filter(item=>item.tagName==='input').map(item=>item.value)};
await retry.dispatch('click');for(let i=0;i<6;i++)await __test.turn();card=[...$('accounts').children].find(item=>item.dataset.accountId==='a'.repeat(32));
return {failed,after:{card:card.textContent,retry:__test.all(card).some(item=>item.tagName==='button'&&item.textContent==='重试清理本地登录'),message:$('message').textContent},posts:__test.requests.filter(item=>item.method==='POST'&&item.url.endsWith('/disconnect')).length};
""")
    assert "清理尚未完成" in result["failed"]["card"]
    assert result["failed"]["retry"] is True
    assert "清理未完成" in result["failed"]["message"]
    assert "a" * 32 not in result["failed"]["choices"]
    assert "本地登录已移除" in result["after"]["card"]
    assert result["after"]["retry"] is False
    assert result["after"]["message"] == "本地登录清理已完成。"
    assert result["posts"] == 2


def test_storage_summary_uses_real_counts_and_shows_low_space_without_mutating():
    result = run_upload_ui(r"""
const initial={summary:$('storage-summary').textContent,warning:$('storage-warning').textContent,hidden:$('storage-warning').hidden};
__test.storage={managed_bytes:1572864,registered_source_count:6,present_source_count:2,missing_source_count:1,deleted_source_count:1,changed_source_count:1,unsafe_source_count:1,orphan_file_count:2,orphan_bytes:4096,unsafe_entry_count:1,free_bytes:33554432,reserve_bytes:67108864,low_space:true};
await poll();
return {initial,after:{summary:$('storage-summary').textContent,warning:$('storage-warning').textContent,hidden:$('storage-warning').hidden},posts:__test.requests.filter(item=>item.method==='POST'||item.method==='DELETE').length};
""")
    assert result["initial"]["hidden"] is True
    assert "42 B" in result["initial"]["summary"]
    assert result["after"]["hidden"] is False
    assert "1.5 MiB" in result["after"]["summary"]
    assert "2 份可用" in result["after"]["summary"]
    assert "1 份缺失" in result["after"]["summary"]
    assert "1 份内容变化" in result["after"]["summary"]
    assert "1 份路径不安全" in result["after"]["summary"]
    assert "2 份未登记文件（4.0 KiB）" in result["after"]["summary"]
    assert "32.0 MiB" in result["after"]["warning"]
    assert "64.0 MiB" in result["after"]["warning"]
    assert "不会自动清理" in result["after"]["warning"]
    assert result["posts"] == 0


def test_source_with_active_references_explains_why_managed_copy_cannot_be_deleted():
    result = run_upload_ui(r"""
__test.sources[0].active_reference_count=2;__test.sources[0].can_delete=false;await refresh();
const card=[...$('source-library').children].find(item=>item.dataset.sourceId==='c'.repeat(32));
const remove=__test.all(card).find(item=>item.tagName==='button'&&item.textContent==='删除受管副本');
return {copy:card.textContent,disabled:remove.disabled,deletes:__test.requests.filter(item=>item.method==='DELETE').length};
""")
    assert result["disabled"] is True
    assert "2 个未完成任务" in result["copy"]
    assert result["deletes"] == 0


def test_media_delete_is_two_step_and_preserves_focus_input_and_selection_through_polling():
    result = run_upload_ui(r"""
$('title').value='删除确认期间继续编辑';const selectedBefore=$('source-id').value;
let card=[...$('source-library').children].find(item=>item.dataset.sourceId==='c'.repeat(32));
let open=__test.all(card).find(item=>item.tagName==='button'&&item.textContent==='删除受管副本');
open.focus();await open.dispatch('click');
card=[...$('source-library').children].find(item=>item.dataset.sourceId==='c'.repeat(32));
let confirm=__test.all(card).find(item=>item.tagName==='button'&&item.textContent==='确认删除受管副本');
const afterFirst={deletes:__test.requests.filter(item=>item.method==='DELETE').length,focused:document.activeElement===confirm,copy:card.textContent};
await poll();
confirm=__test.all($('source-library')).find(item=>item.tagName==='button'&&item.textContent==='确认删除受管副本');
const afterPoll={visible:!!confirm,focused:document.activeElement===confirm,title:$('title').value,selected:$('source-id').value};
await confirm.dispatch('click');for(let i=0;i<5;i++)await __test.turn();
card=[...$('source-library').children].find(item=>item.dataset.sourceId==='c'.repeat(32));
return {selectedBefore,afterFirst,afterPoll,selectedAfter:$('source-id').value,card:card.textContent,message:$('message').textContent,deletes:__test.requests.filter(item=>item.method==='DELETE').map(item=>({url:item.url,nonce:item.headers['x-upload-csrf']})),posts:__test.requests.filter(item=>item.method==='POST').length,title:$('title').value};
""")
    assert result["selectedBefore"] == "c" * 32
    assert result["afterFirst"]["deletes"] == 0
    assert result["afterFirst"]["focused"] is True
    assert "原始文件和下载成品保持不变" in result["afterFirst"]["copy"]
    assert result["afterPoll"] == {
        "visible": True,
        "focused": True,
        "title": "删除确认期间继续编辑",
        "selected": "c" * 32,
    }
    assert result["selectedAfter"] == ""
    assert "已删除" in result["card"]
    assert "受管副本已删除" in result["message"]
    assert result["deletes"] == [{
        "url": "/api/v1/uploads/sources/" + "c" * 32 + "/media",
        "nonce": "synthetic-session-nonce",
    }]
    assert result["posts"] == 0
    assert result["title"] == "删除确认期间继续编辑"


def test_missing_media_blocks_retry_then_exact_restore_recovers_same_source():
    result = run_upload_ui(r"""
const source=__test.sources[0];source.media_present=false;source.media_state='deleted';source.media_deleted_at='2026-09-07T01:03:04+00:00';source.can_delete=false;
__test.storage.managed_bytes=0;__test.storage.present_source_count=0;__test.storage.deleted_source_count=1;
__test.jobs=[{id:'e'.repeat(32),platform:'douyin',account_id:'a'.repeat(32),account_name:'Synthetic Bili',account_lifecycle_state:'active',source_id:source.id,source_media_present:false,source_media_state:'deleted',title:'Synthetic',tags:[],mode:'publish',state:'failed',code:'source_reimport_required'}];
await refresh();let card=[...$('source-library').children].find(item=>item.dataset.sourceId===source.id),job=[...$('jobs').children].find(item=>item.dataset.jobId==='e'.repeat(32));
const before={selected:$('source-id').value,card:card.textContent,job:job.textContent,retry:__test.all(job).some(item=>item.tagName==='button'&&item.textContent==='重新创建本地草稿')};
let restore=__test.all(card).find(item=>item.tagName==='button'&&item.textContent==='重新导入相同视频');await restore.dispatch('click');
$('source-restore-file').files=[new Blob(['x'.repeat(42)],{type:'video/mp4'})];$('source-restore-file').focus();await poll();
const duringPoll={panel:!$('source-restore-panel').hidden,focused:document.activeElement===$('source-restore-file'),files:$('source-restore-file').files.length};
__test.restoreHandler=()=>({ok:false,status:409,json:async()=>({detail:'source_restore_mismatch'})});
await $('source-restore-form').dispatch('submit');for(let i=0;i<4;i++)await __test.turn();
job=[...$('jobs').children].find(item=>item.dataset.jobId==='e'.repeat(32));
const mismatch={state:__test.sources[0].media_state,panel:!$('source-restore-panel').hidden,message:$('message').textContent,retry:__test.all(job).some(item=>item.tagName==='button'&&item.textContent==='重新创建本地草稿')};
__test.restoreHandler=null;await $('source-restore-form').dispatch('submit');for(let i=0;i<5;i++)await __test.turn();
job=[...$('jobs').children].find(item=>item.dataset.jobId==='e'.repeat(32));card=[...$('source-library').children].find(item=>item.dataset.sourceId===source.id);
return {before,duringPoll,mismatch,after:{state:__test.sources[0].media_state,selected:$('source-id').value,panel:$('source-restore-panel').hidden,card:card.textContent,retry:__test.all(job).some(item=>item.tagName==='button'&&item.textContent==='重新创建本地草稿'),message:$('message').textContent},restorePosts:__test.requests.filter(item=>item.method==='POST'&&item.url.endsWith('/media')).map(item=>({url:item.url,contentType:item.headers['content-type'],nonce:item.headers['x-upload-csrf']})),retryPosts:__test.requests.filter(item=>item.method==='POST'&&item.url.endsWith('/retry')).length};
""")
    assert result["before"]["selected"] == ""
    assert "恢复受管副本" in result["before"]["card"]
    assert "先恢复受管副本" in result["before"]["job"]
    assert result["before"]["retry"] is False
    assert result["duringPoll"] == {"panel": True, "focused": True, "files": 1}
    assert result["mismatch"]["state"] == "deleted"
    assert result["mismatch"]["panel"] is True
    assert "历史记录不一致" in result["mismatch"]["message"]
    assert result["mismatch"]["retry"] is False
    assert result["after"]["state"] == "present"
    assert result["after"]["selected"] == "c" * 32
    assert result["after"]["panel"] is True
    assert "副本可用" in result["after"]["card"]
    assert result["after"]["retry"] is True
    assert "受管副本已恢复" in result["after"]["message"]
    assert result["restorePosts"] == [{
        "url": "/api/v1/uploads/sources/" + "c" * 32 + "/media",
        "contentType": "application/octet-stream",
        "nonce": "synthetic-session-nonce",
    }] * 2
    assert result["retryPosts"] == 0


def test_canceling_media_restore_returns_focus_to_the_same_source_action():
    result = run_upload_ui(r"""
const source=__test.sources[0];source.media_present=false;source.media_state='deleted';source.media_deleted_at='2026-09-07T01:03:04+00:00';source.can_delete=false;
await refresh();let card=[...$('source-library').children].find(item=>item.dataset.sourceId===source.id);
let restore=__test.all(card).find(item=>item.tagName==='button'&&item.textContent==='重新导入相同视频');await restore.dispatch('click');
await $('source-restore-cancel').dispatch('click');
card=[...$('source-library').children].find(item=>item.dataset.sourceId===source.id);restore=__test.all(card).find(item=>item.tagName==='button'&&item.textContent==='重新导入相同视频');
return {panelHidden:$('source-restore-panel').hidden,focused:document.activeElement===restore,posts:__test.requests.filter(item=>item.method==='POST').length};
""")
    assert result == {"panelHidden": True, "focused": True, "posts": 0}


def test_disconnected_account_blocks_draft_confirmation_and_retry():
    result = run_upload_ui(r"""
__test.accounts[0].lifecycle_state='disconnected';__test.accounts[0].disconnected_at='2026-09-07T01:02:03+00:00';
const base={platform:'bilibili',account_id:'a'.repeat(32),account_name:'Synthetic Bili',account_lifecycle_state:'disconnected',source_id:'c'.repeat(32),source_media_present:true,source_media_state:'present',title:'Synthetic',tags:['x'],mode:'publish'};
__test.jobs=[{...base,id:'1'.repeat(32),state:'draft'},{...base,id:'2'.repeat(32),state:'failed'}];await refresh();
return {cards:[...$('jobs').children].map(item=>({text:item.textContent,confirm:__test.all(item).some(child=>child.tagName==='button'&&child.textContent==='确认立即上传投稿'),retry:__test.all(item).some(child=>child.tagName==='button'&&child.textContent==='重新创建本地草稿')})),posts:__test.requests.filter(item=>item.method==='POST').length};
""")
    assert all("账号已断开" in card["text"] for card in result["cards"])
    assert not any(card["confirm"] or card["retry"] for card in result["cards"])
    assert result["posts"] == 0


def test_poll_preserves_focus_for_rebuilt_account_and_job_controls():
    result = run_upload_ui(r"""
__test.jobs=[{id:'e'.repeat(32),platform:'douyin',account_id:'a'.repeat(32),account_name:'Synthetic Bili',account_lifecycle_state:'active',source_id:'c'.repeat(32),source_media_present:true,source_media_state:'present',title:'Synthetic',tags:[],mode:'publish',state:'failed'}];await refresh();
let choice=__test.all($('account-choices')).find(item=>item.tagName==='input'&&item.value==='a'.repeat(32));choice.focus();await poll();
choice=__test.all($('account-choices')).find(item=>item.tagName==='input'&&item.value==='a'.repeat(32));const choiceKept=document.activeElement===choice;
let summary=__test.all($('jobs')).find(item=>item.tagName==='summary');summary.focus();await poll();summary=__test.all($('jobs')).find(item=>item.tagName==='summary');const summaryKept=document.activeElement===summary;
let retry=__test.all($('jobs')).find(item=>item.tagName==='button'&&item.textContent==='重新创建本地草稿');retry.focus();await poll();retry=__test.all($('jobs')).find(item=>item.tagName==='button'&&item.textContent==='重新创建本地草稿');
return {choiceKept,summaryKept,retryKept:document.activeElement===retry,posts:__test.requests.filter(item=>item.method==='POST').length};
""")
    assert result == {
        "choiceKept": True,
        "summaryKept": True,
        "retryKept": True,
        "posts": 0,
    }


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


def test_poll_reuses_keyed_account_source_and_job_nodes_with_local_state():
    result = run_upload_ui(r"""
const job={id:'1'.repeat(32),platform:'douyin',account_id:'a'.repeat(32),account_name:'Synthetic Bili',account_lifecycle_state:'active',source_id:'c'.repeat(32),source_media_present:true,source_media_state:'present',title:'Unknown result',description:'Keep this',tags:['one'],mode:'publish',state:'unknown',code:'interrupted_result_unknown'};
__test.jobs=[job];await refresh();
const find=(root,tag,predicate=()=>true)=>__test.all(root).find(item=>item.tagName===tag&&predicate(item));
const before={
 account:[...$('accounts').children].find(item=>item.dataset.accountId==='a'.repeat(32)),
 choice:find($('account-choices'),'input',item=>item.value==='a'.repeat(32)),
 option:find($('source-id'),'option',item=>item.value==='c'.repeat(32)),
 source:[...$('source-library').children].find(item=>item.dataset.sourceId==='c'.repeat(32)),
 job:[...$('jobs').children].find(item=>item.dataset.jobId===job.id)
};
before.details=find(before.job,'details');before.summary=find(before.job,'summary');before.ack=find(before.job,'input');before.retry=find(before.job,'button',item=>item.textContent==='重新创建本地草稿');
before.choice.checked=true;await before.choice.dispatch('change');before.details.open=false;before.ack.checked=true;await before.ack.dispatch('change');before.retry.focus();
__test.accounts.reverse();for(const item of [...__test.accounts,...__test.sources,...__test.jobs])item.updated_at='ignored-'+Math.random();await poll();
const after={
 account:[...$('accounts').children].find(item=>item.dataset.accountId==='a'.repeat(32)),
 choice:find($('account-choices'),'input',item=>item.value==='a'.repeat(32)),
 option:find($('source-id'),'option',item=>item.value==='c'.repeat(32)),
 source:[...$('source-library').children].find(item=>item.dataset.sourceId==='c'.repeat(32)),
 job:[...$('jobs').children].find(item=>item.dataset.jobId===job.id)
};
after.details=find(after.job,'details');after.summary=find(after.job,'summary');after.ack=find(after.job,'input');after.retry=find(after.job,'button',item=>item.textContent==='重新创建本地草稿');
return {same:Object.fromEntries(Object.keys(before).map(key=>[key,before[key]===after[key]])),checked:after.choice.checked,detailsOpen:after.details.open,acknowledged:after.ack.checked,focused:document.activeElement===after.retry,firstAccount:$('accounts').children[0].dataset.accountId,posts:__test.requests.filter(item=>item.method==='POST').length};
""")
    assert all(result["same"].values())
    assert result["checked"] and result["acknowledged"]
    assert result["detailsOpen"] is False
    assert result["focused"] is True
    assert result["firstAccount"] == "b" * 32
    assert result["posts"] == 0


def test_job_card_updates_in_place_when_visible_server_state_changes():
    result = run_upload_ui(r"""
const job={id:'1'.repeat(32),platform:'douyin',account_id:'a'.repeat(32),account_name:'Synthetic Bili',account_lifecycle_state:'active',source_id:'c'.repeat(32),source_media_present:true,source_media_state:'present',title:'Tracked',description:'',tags:[],mode:'publish',state:'running',code:''};
__test.jobs=[job];await refresh();const before=[...$('jobs').children].find(item=>item.dataset.jobId===job.id),details=__test.all(before).find(item=>item.tagName==='details');details.open=false;
job.state='submitted';job.code='upstream_submitted';job.updated_at='2026-09-07T12:34:56Z';await poll();const after=[...$('jobs').children].find(item=>item.dataset.jobId===job.id),afterDetails=__test.all(after).find(item=>item.tagName==='details');
return {sameCard:before===after,copy:after.textContent,detailsOpen:afterDetails.open,hasCancel:__test.all(after).some(item=>item.tagName==='button'&&item.textContent==='取消'),posts:__test.requests.filter(item=>item.method==='POST').length};
""")
    assert result["sameCard"] is True
    assert "上游报告投稿完成" in result["copy"]
    assert result["detailsOpen"] is False
    assert result["hasCancel"] is False
    assert result["posts"] == 0


def test_job_submit_uses_submit_time_snapshot_while_poll_is_pending():
    result = run_upload_ui(r"""
for(const input of __test.all($('account-choices')).filter(item=>item.tagName==='input'))input.checked=input.value==='a'.repeat(32);
updateMetadata();$('title').value='Before poll';$('description').value='Before description';$('tags').value='one，two';$('category-id').value='21';$('copyright').value='2';$('source-credit').value='Before source';
let release,hold=true;__test.requestHook=url=>{if(hold&&url.endsWith('/status')){hold=false;return new Promise(resolve=>release=resolve);}};
const polling=poll();await __test.turn();await $('job-form').dispatch('submit');
for(const input of __test.all($('account-choices')).filter(item=>item.tagName==='input'))input.checked=input.value==='b'.repeat(32);
$('source-id').value='';$('title').value='After poll';$('description').value='After description';$('tags').value='changed';$('category-id').value='99';$('copyright').value='1';$('source-credit').value='After source';$('tencent-mode').value='draft';
release();await polling;for(let index=0;index<8;index++)await __test.turn();
const post=__test.requests.find(item=>item.method==='POST'&&item.url.endsWith('/jobs'));
return {body:JSON.parse(post.body),postCount:__test.requests.filter(item=>item.method==='POST'&&item.url.endsWith('/jobs')).length,currentTitle:$('title').value};
""")
    assert result["postCount"] == 1
    assert result["body"] | {
        "source_id": "c" * 32,
        "title": "Before poll",
        "description": "Before description",
        "tags": ["one", "two"],
        "category_id": 21,
        "copyright": 2,
        "source_credit": "Before source",
        "account_ids": ["a" * 32],
        "mode": "publish",
    } == result["body"]
    assert result["currentTitle"] == "After poll"


def test_ime_composition_blocks_account_and_job_submission_until_finished():
    result = run_upload_ui(r"""
$('account-name').value='Composing account';await $('account-name').dispatch('compositionstart');const accountKey=await $('account-name').dispatch('keydown',{key:'Enter',keyCode:229,isComposing:true});await $('account-form').dispatch('submit',{isComposing:true});
const afterAccount=__test.requests.filter(item=>item.method==='POST').length;await $('account-name').dispatch('compositionend');
for(const input of __test.all($('account-choices')).filter(item=>item.tagName==='input'))input.checked=input.value==='a'.repeat(32);updateMetadata();$('title').value='Composing title';$('category-id').value='21';$('copyright').value='1';await $('title').dispatch('compositionstart');const jobKey=await $('title').dispatch('keydown',{key:'Enter',keyCode:229,isComposing:true});await $('job-form').dispatch('submit',{isComposing:true});const duringJob=__test.requests.filter(item=>item.method==='POST').length;
await $('title').dispatch('compositionend');await $('job-form').dispatch('submit');for(let index=0;index<6;index++)await __test.turn();
return {accountPrevented:accountKey.defaultPrevented,jobPrevented:jobKey.defaultPrevented,afterAccount,duringJob,jobPosts:__test.requests.filter(item=>item.method==='POST'&&item.url.endsWith('/jobs')).length,accountPosts:__test.requests.filter(item=>item.method==='POST'&&item.url.endsWith('/accounts')).length};
""")
    assert result == {
        "accountPrevented": True,
        "jobPrevented": True,
        "afterAccount": 0,
        "duringJob": 0,
        "jobPosts": 1,
        "accountPosts": 0,
    }


def test_visibility_pause_clears_qr_and_resumes_one_poll_and_timer_pair():
    result = run_upload_ui(r"""
__test.operations=[{id:'e'.repeat(32),account_id:'a'.repeat(32),action:'login',state:'running',login_phase:'waiting_scan',qr_available:true,qr_revision:1,expires_at:Math.floor(Date.now()/1000)+120}];await refresh();for(let index=0;index<3;index++)await __test.turn();
const beforeStatus=__test.requests.filter(item=>item.url.endsWith('/status')).length,beforeQR=__test.requests.filter(item=>item.url.endsWith('/qr')).length;
document.hidden=true;await __test.documentEvent('visibilitychange');const paused={timers:__test.timers.size,qrHidden:$('login-qr').hidden,qrRemoved:!$('login-qr').src};await poll();const hiddenStatus=__test.requests.filter(item=>item.url.endsWith('/status')).length;
document.hidden=false;await __test.documentEvent('visibilitychange');for(let index=0;index<3;index++)await __test.turn();await __test.documentEvent('visibilitychange');for(let index=0;index<2;index++)await __test.turn();
return {paused,hiddenPollSkipped:hiddenStatus===beforeStatus,statusIncrease:__test.requests.filter(item=>item.url.endsWith('/status')).length-beforeStatus,qrIncrease:__test.requests.filter(item=>item.url.endsWith('/qr')).length-beforeQR,timers:[...__test.timers.values()].map(item=>item.delay).sort()};
""")
    assert result["paused"] == {
        "timers": 0,
        "qrHidden": True,
        "qrRemoved": True,
    }
    assert result["hiddenPollSkipped"] is True
    assert result["statusIncrease"] == 1
    assert result["qrIncrease"] == 1
    assert result["timers"] == [1000, 1500]
