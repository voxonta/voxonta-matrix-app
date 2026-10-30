'use strict';
const $=id=>document.getElementById(id),params=new URLSearchParams(location.search);
const sdk=typeof mxwidgets==='function'?mxwidgets():mxwidgets;
if (!params.get('widgetId')) {
 $('state').textContent='Материалы встречи в Element';
 $('hint').textContent='Откройте комнату и выберите виджет Voxonta. Доступ к материалам проверяется по вашей учётке Matrix.';
 fetch('config').then(r=>r.json()).then(c=>{const a=document.createElement('a');a.textContent='Открыть комнату в Element';a.href=c.parent_origins[0]+'/#/room/'+encodeURIComponent(params.get('roomId')||'');$('hint').append(document.createElement('br'),a);});
} else {
const parentHint=params.get('parentUrl')||document.referrer;
const parentOrigin=parentHint?new URL(parentHint).origin:null;
const widget=new sdk.WidgetApi(params.get('widgetId'),parentOrigin);
let token=null,expires=0,busy=false,current=null,pendingCommand=null;
const phases={idle:'Готово к расшифровке',starting:'Подключаемся к звонку',transcribing:'Идёт расшифровка',stopping:'Останавливаем передачу звука',paused:'Расшифровка приостановлена',unknown:'Проверяем связь с захватом'};
function error(e){$('error').textContent=e?.message||'';$('error').hidden=!e;}
async function api(path,body={}){
 const r=await fetch('api/'+path,{method:'POST',credentials:'omit',headers:{'Content-Type':'application/json',...(token?{'Authorization':'Bearer '+token}:{})},body:JSON.stringify(body)});
 if(!r.ok){if(r.status===401)expires=0;let b=await r.json();throw new Error(b.error||'Сервис временно недоступен');}
 return path==='result'?r.blob():r.json();
}
async function login(){const oid=await widget.requestOpenIDConnectToken();const s=await api('session',{room_id:params.get('roomId'),openid:oid});token=s.token;expires=Date.now()+12*60000;$('room').textContent=s.room_name;}
function render(s){
 current=s;const c=s.call,phase=c?.phase||'idle';$('state').textContent=phases[phase]||phase;
 $('hint').textContent=phase==='transcribing'?'Можно приостановить расшифровку или завершить её и получить результат.':phase==='unknown'?'Состояние захвата не подтверждено. Успешная остановка пока не подтверждена.':'Участникам доступны саммари и расшифровка в этой панели и в архиве.';
 $('start').hidden=!!c;$('start').disabled=busy||!s.can_control;
 $('pause').hidden=!c;$('pause').disabled=busy||!s.can_control||['starting','stopping','unknown'].includes(phase);$('pause').textContent=phase==='paused'?'Продолжить':'Приостановить';
 $('finish').hidden=!c;$('finish').disabled=busy||!s.can_control;
 $('auto-transcribe').disabled=busy||!s.can_control;$('auto-transcribe').checked=s.auto_transcribe;
 $('revoke').hidden=!s.can_revoke_call;$('revoke').disabled=busy;$('auto').disabled=busy||!s.can_control;$('auto').checked=s.auto_publish;
 const host=$('meetings');host.replaceChildren();
 if(!s.meetings.length){const p=document.createElement('p');p.textContent='Готовые саммари и расшифровки появятся здесь.';host.append(p);}
 for(const m of s.meetings){const el=document.createElement('article');el.className='material';const h=document.createElement('h3');h.textContent=m.name;el.append(h);
  for(const kind of m.artifacts){const b=document.createElement('button');b.textContent=kind==='summary'?'Открыть саммари':'Открыть расшифровку';b.onclick=()=>download(m.session_id,kind);el.append(b);}
  if(m.artifacts.length){const b=document.createElement('button');b.textContent=m.delivery_status==='posted'?'Опубликовано':'Поделиться в чате';b.disabled=['posted','pending','sending'].includes(m.delivery_status);b.onclick=()=>command('publish',m.session_id);el.append(b);}
  if(m.can_revoke){const b=document.createElement('button');b.textContent='Отозвать разрешение публикации';b.onclick=()=>command('revoke',m.session_id);el.append(b);}
  if(!m.artifacts.length){const p=document.createElement('p');p.textContent=m.phase==='empty'?'В этой встрече не было звука для расшифровки.':m.phase==='failed'?'Обработка завершилась с ошибкой.':'Готовим материалы встречи';el.append(p);}host.append(el);
 }
}
async function refresh(){try{if(Date.now()>expires)await login();render(await api('status'));error(null);}catch(e){error(e);$('retry').hidden=false;}}
async function command(action,value){if(busy)return;const before=current;
 if(current&&action==='auto_transcribe')current={...current,auto_transcribe:value};
 if(current&&action==='settings')current={...current,auto_publish:value};
 busy=true;error(null);if(current)render(current);
 // Preserve idempotency key after a lost response until this exact command succeeds.
 if(!pendingCommand||pendingCommand.action!==action||pendingCommand.value!==value)pendingCommand={action,value,request_id:crypto.randomUUID()};
 try{await api('command',pendingCommand);pendingCommand=null;await refresh();}catch(e){current=before;error(e);}finally{busy=false;if(current)render(current);}}
async function download(session_id,kind){try{const blob=await api('result',{session_id,kind});$('viewer-title').textContent=kind==='summary'?'Саммари':'Расшифровка';$('viewer-content').textContent=await blob.text();$('viewer').hidden=false;$('viewer').scrollIntoView({block:'start'});}catch(e){error(e);}}
$('close-viewer').onclick=()=>{$('viewer').hidden=true;$('viewer-content').textContent='';};
$('revoke').onclick=()=>command('revoke',current.call.session_id);
$('start').onclick=()=>command('start');$('pause').onclick=()=>command(current.call.phase==='paused'?'resume':'pause');$('finish').onclick=()=>command('finish');$('auto-transcribe').onchange=()=>command('auto_transcribe',$('auto-transcribe').checked);$('auto').onchange=()=>command('settings',$('auto').checked);$('retry').onclick=refresh;
widget.on('ready',()=>{void refresh();setInterval(refresh,2000);});widget.start();setTimeout(()=>{if(!token){error(new Error('Откройте виджет Voxonta из комнаты Element.'));$('retry').hidden=false;}},15000);

}
