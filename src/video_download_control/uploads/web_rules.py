"""Pure upload form rules shared by both existing inline page scripts."""

UPLOAD_FORM_RULES_JS = r'''
function uploadScheduleLead(platform,configured){const value=Number(configured);return Number.isSafeInteger(value)&&value>=60&&value<=604800?value:(platform==='bilibili'?21900:14700);}
function textLength(value){return [...value].length;}
function splitTags(value){return value.split(/[,，]/).map(item=>item.trim()).filter(Boolean);}
function uploadTagsValid(tags,required=false){return (!required||tags.length>0)&&tags.length<=10&&new Set(tags).size===tags.length&&tags.every(tag=>!!tag&&textLength(tag)<=20&&!/[#＃\n\r\t]/.test(tag));}
function leadText(seconds){const hours=Math.floor(seconds/3600),minutes=Math.floor(seconds%3600/60);return hours+' 小时'+(minutes?' '+minutes+' 分钟':'');}
function publishTimeError(platform,timestampMs,nowMs,localMinute,leadSeconds){
  if(timestampMs<=nowMs+leadSeconds*1000)return 'lead';
  if(platform==='tencent'){
    if(localMinute!==null&&localMinute!==0)return 'hour';
    if(timestampMs>nowMs+28*24*3600000)return 'horizon';
  }
  return null;
}
function localPublishSchedule(platform,raw,nowMs,required,leadSeconds){
  if(!raw)return required?{error:'required'}:{schedule:{publish_at_unix:null,publish_timezone_offset_minutes:null}};
  const parsed=new Date(raw),key=raw.slice(0,16);
  if(Number.isNaN(parsed.getTime())||localDateTimeKey(parsed)!==key)return {error:'invalid'};
  for(let minutes=-180;minutes<=180;minutes+=30){
    if(!minutes)continue;
    const alternate=new Date(parsed.getTime()+minutes*60000);
    if(localDateTimeKey(alternate)===key&&alternate.getTimezoneOffset()!==parsed.getTimezoneOffset())return {error:'ambiguous'};
  }
  const error=publishTimeError(platform,parsed.getTime(),nowMs,parsed.getMinutes(),leadSeconds);
  return error?{error}:{schedule:{publish_at_unix:Math.floor(parsed.getTime()/60000)*60,publish_timezone_offset_minutes:-parsed.getTimezoneOffset()}};
}
function localDateTimeKey(value){const pad=number=>String(number).padStart(2,'0');return value.getFullYear()+'-'+pad(value.getMonth()+1)+'-'+pad(value.getDate())+'T'+pad(value.getHours())+':'+pad(value.getMinutes());}
'''
