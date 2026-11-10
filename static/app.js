'use strict';
const $ = id => document.getElementById(id);
let session = null, pending = false, generation = 0, reviewerKey = '';
const welcome = $('welcome').cloneNode(true);

async function request(path, data, headers = {}) {
  const response = await fetch(path, {method: data ? 'POST' : 'GET', headers: {...(data ? {'Content-Type':'application/json'} : {}), ...headers}, ...(data ? {body:JSON.stringify(data)} : {})});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Сервис недоступен');
  return result;
}
function error(id, message) { $(id).textContent = message; $(id).hidden = !message; }
function bubble(role, text, meta, target = $('messages')) {
  const div = document.createElement('div'); div.className = 'bubble ' + role; div.textContent = text;
  if (meta) { const small = document.createElement('span'); small.className = 'meta'; small.textContent = meta; div.append(small); }
  target.append(div); target.scrollTop = target.scrollHeight; return div;
}
function controls() { $('send').disabled = pending || !session; $('message').disabled = !session; $('open-lead').disabled = pending || !session; $('header-lead').disabled = pending || !session; }
async function closeSession() {
  const old = session; session = null; generation++; pending = false; controls();
  if (old) await request('/api/close', {session_id:old.id}, {'X-Session-Token':old.token}).catch(()=>{});
}
async function newSession() {
  $('new-chat').disabled = true;
  await closeSession();
  const turn = generation;
  try {
    const next = await request('/api/sessions', {});
    if (turn !== generation) return;
    session = next; $('messages').replaceChildren(welcome.cloneNode(true)); $('reopen').hidden = true;
    $('message').value = ''; error('chat-error',''); $('lead-form').reset(); controls(); $('message').focus();
  } catch(e) { error('chat-error',e.message); }
  finally { $('new-chat').disabled = false; }
}
async function send(text) {
  if (pending || !session || !text.trim()) return;
  text = text.trim();
  const old = session, turn = generation;
  $('welcome')?.remove(); bubble('user',text); $('message').value = ''; error('chat-error','');
  pending = true; controls(); const waiting = bubble('pending','Ищем ответ в базе знаний…');
  try {
    const result = await request('/api/chat',{session_id:old.id,message:text},{'X-Session-Token':old.token});
    if (turn !== generation) return;
    waiting.remove(); bubble('assistant',result.answer,`${result.mode === 'preview' ? 'Фрагмент источника · ' : result.mode === 'unavailable' ? 'Сервис недоступен · ' : ''}${(result.elapsed_ms/1000).toFixed(1)} с`);
  } catch(e) {
    if (turn === generation) { waiting.remove(); error('chat-error',e.message); $('message').value = text; }
  } finally { if (turn === generation) { pending = false; controls(); $('message').focus(); } }
}
$('chat-form').addEventListener('submit',e=>{e.preventDefault();send($('message').value);});
$('message').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send($('message').value);}});
$('messages').addEventListener('click',e=>{const button=e.target.closest('[data-prompt]');if(button)send(button.dataset.prompt);});
$('new-chat').onclick = newSession;
$('close-chat').onclick = async()=>{await closeSession();bubble('assistant','Диалог закрыт. История сохранена для проверки. Новый разговор начнётся без прежнего контекста.');$('reopen').hidden=false;};
$('reopen').onclick = newSession;
$('open-lead').onclick=()=>{error('lead-error','');$('lead-dialog').showModal();};
$('header-lead').onclick=$('open-lead').onclick;
$('cancel-lead').onclick=()=>$('lead-dialog').close();
$('lead-form').onsubmit=async e=>{
  e.preventDefault(); if(!session)return;
  const active=session, turn=generation, fields=Object.fromEntries(new FormData(e.target));
  fields.test_data_confirmed=e.target.elements.test_data_confirmed.checked;
  $('save-lead').disabled=true;error('lead-error','');
  try {const result=await request('/api/leads',{session_id:active.id,...fields},{'X-Session-Token':active.token});
    if(turn!==generation)return;
    $('lead-dialog').close();bubble('assistant',`Тестовая заявка ${result.lead.id} сохранена внутри прототипа. Она содержит контакты и резюме разговора. В МТС ничего не отправлено.`);
  }catch(e){error('lead-error',e.message);}finally{$('save-lead').disabled=false;}
};
function tab(review) {
  $('chat-view').hidden=review;$('review-view').hidden=!review;
  $('chat-tab').classList.toggle('active',!review);$('review-tab').classList.toggle('active',review);
  $('page-title').textContent=review?'Проверка диалогов':'Консультант';
  if(review&&reviewerKey)loadSessions();
}
$('chat-tab').onclick=()=>tab(false);$('review-tab').onclick=()=>tab(true);
$('review-login').onsubmit=e=>{e.preventDefault();reviewerKey=$('review-key').value;loadSessions();};
async function loadSessions(){
  try{const result=await request('/api/review/sessions',null,{'X-Reviewer-Token':reviewerKey});error('review-error','');$('session-list').replaceChildren();
    if(!result.sessions.length)$('session-list').textContent='Диалогов пока нет.';
    for(const session of result.sessions){const button=document.createElement('button');button.className='session-button';button.textContent=(session.title||'Новый диалог').slice(0,110);const small=document.createElement('small');small.textContent=`${new Date(session.created).toLocaleString('ru-RU')} · ${session.message_count} сообщений${session.lead_id?' · Заявка':''}`;button.append(small);button.onclick=()=>{document.querySelectorAll('.session-button').forEach(x=>x.classList.remove('selected'));button.classList.add('selected');loadDetail(session.id);};$('session-list').append(button);}
  }catch(e){reviewerKey='';$('session-list').replaceChildren();$('review-detail').replaceChildren();error('review-error',e.message);}
}
function detail(label,text,parent){const node=document.createElement('details'),summary=document.createElement('summary'),body=document.createElement('div');summary.textContent=label;body.className='source';body.textContent=text;node.append(summary,body);parent.append(node);}
async function loadDetail(id){
  try{const result=await request('/api/review/sessions/'+encodeURIComponent(id),null,{'X-Reviewer-Token':reviewerKey});const target=$('review-detail');target.replaceChildren();
    if(result.lead){const lead=result.lead,s=JSON.parse(lead.summary),card=document.createElement('div');card.className='lead-card';card.textContent=`ЗАЯВКА ${lead.id}\n${lead.name} · ${lead.phone}\n${lead.company}\n\nЗадача: ${s.customer_request}\nУточнения: ${s.customer_details.join('; ')||'Не указаны'}\n\n${s.next_step}\n${s.note}`;target.append(card);detail('Последняя консультация в резюме',s.last_consultation,target);}
    for (const message of result.messages) {
      bubble(message.role, message.content, null, target);
      if (!message.debug) continue;
      const {debug, mode} = message.debug;
      const box = document.createElement('div');
      const note = mode === 'live' ? 'Фрагменты в контексте запроса к модели.' : 'Фрагменты, отобранные поиском.';
      target.append(box);
      detail(`Отладка · ${debug.elapsed_ms} мс · ${debug.sources.length} фрагментов`,
        `Режим: ${mode}\nПоиск: ${debug.retrieval_ms ?? 0} мс\n${debug.warnings.join('\n') || 'Без технических предупреждений'}\n${note}`, box);
      for (const source of debug.sources) detail(source.source + ' → ' + source.heading, source.text, box);
    }
  }catch(e){error('review-error',e.message);}
}
window.addEventListener('pagehide',()=>{if(session)fetch('/api/close',{method:'POST',headers:{'Content-Type':'application/json','X-Session-Token':session.token},body:JSON.stringify({session_id:session.id}),keepalive:true}).catch(()=>{});});
window.addEventListener('pageshow',e=>{if(e.persisted)newSession();});
(async()=>{try{const s=await request('/api/status');$('mode').textContent=s.mode==='live'?'Модель подключена':'Предпросмотр без LLM';$('mode').classList.toggle('live',s.mode==='live');$('preview-notice').hidden=s.mode==='live';await newSession();}catch(e){error('chat-error',e.message);}})();
