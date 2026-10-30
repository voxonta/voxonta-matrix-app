'use strict';
// Execute the real widget script; only the host, DOM, clock and HTTP are fakes.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(process.env.WIDGET_SCRIPT_PATH ||
  path.join(__dirname, '../src/voxonta_matrix/web/widget.js'), 'utf8');

function deferred(){
  let resolve, reject;
  const promise = new Promise((ok, no) => {resolve=ok; reject=no;});
  return {promise, resolve, reject};
}
function element(){
  return {hidden:false, disabled:false, checked:false, textContent:'',
    append(){}, replaceChildren(){}, scrollIntoView(){}};
}
const flush = () => new Promise(resolve => setImmediate(resolve));
function harness(){
  const nodes = new Map();
  const get = id => {if(!nodes.has(id))nodes.set(id, element()); return nodes.get(id);};
  const openids=[], intervals=[], timeouts=[], requests=[];
  let now=1000000, fail=null, blocked=null;
  const status={can_control:true, call:null, meetings:[], auto_publish:false, auto_transcribe:false};
  const response = body => ({ok:true, status:200, json:async()=>body});
  class WidgetApi {
    on(name, callback){if(name==='ready')this.ready=callback;}
    start(){this.ready();}
    requestOpenIDConnectToken(){const attempt=deferred();openids.push(attempt);return attempt.promise;}
  }
  const context=vm.createContext({URL, URLSearchParams,
    location:{search:'?widgetId=voxonta&roomId=!test:example.org&parentUrl=https://element.example'},
    document:{getElementById:get, createElement:element, referrer:''},
    mxwidgets:{WidgetApi}, Date:{now:()=>now}, console,
    setInterval(fn){intervals.push(fn);}, setTimeout(fn,ms){timeouts.push({fn,at:now+ms});},
    fetch:async(url)=>{
      const endpoint=url.split('/').pop();requests.push(endpoint);
      if(fail===endpoint){fail=null;throw new Error('network unavailable');}
      if(blocked?.endpoint===endpoint)return blocked.promise;
      return response(endpoint==='session'?{token:'test-ui-token',room_name:'Test'}:status);
    }});
  vm.runInContext(source,context);
  const refresh=()=>get('retry').onclick();
  return {get, openids, requests, refresh, status,
    async approve(index=openids.length-1){openids[index].resolve({matrix_server_name:'example.org',access_token:'test-only'});await flush();},
    async pollFor(ms){
      for(let t=0;t<ms;t+=2000){now+=2000;for(const fn of intervals)fn();
        for(const timer of timeouts){if(timer.at<=now&&!timer.fired){timer.fired=true;timer.fn();}}
        await flush();}
    },
    advance(ms){now+=ms;}, failNext(endpoint){fail=endpoint;},
    block(endpoint){blocked={endpoint,...deferred()};return {
      release(){const pending=blocked;blocked=null;pending.resolve(response(endpoint==='session'?{token:'test-ui-token',room_name:'Test'}:status));}};}
  };
}

test('30 seconds awaiting OpenID share one Promise; approval resumes polling',async()=>{
  const h=harness(), pending=h.refresh();
  await h.pollFor(30000);
  assert.equal(h.openids.length,1,'only one identification dialog');
  assert.strictEqual(h.refresh(),pending,'all callers receive exactly the same Promise');
  assert.equal(h.requests.length,0);
  await h.approve();await pending;
  assert.equal(h.get('state').textContent,'Готово к расшифровке');
  assert.equal(h.get('retry').hidden,true);
  await h.pollFor(4000);
  assert.equal(h.openids.length,1);
  assert.equal(h.requests.filter(p=>p==='status').length,3);
});

for(const endpoint of ['session','status']){
  test(`waiting for backend ${endpoint} also shares the running refresh`,async()=>{
    const h=harness(), block=h.block(endpoint), pending=h.refresh();
    await h.approve();await h.pollFor(30000);
    assert.equal(h.openids.length,1);
    assert.strictEqual(h.refresh(),pending);
    assert.equal(h.requests.filter(p=>p===endpoint).length,1);
    block.release();await pending;
    const next=h.refresh();assert.notStrictEqual(next,pending);await next;
    assert.equal(h.get('error').hidden,true);
  });
}

test('OpenID refusal releases the lock and a subsequent attempt succeeds',async()=>{
  const h=harness(), pending=h.refresh();
  h.openids[0].reject(new Error('identification denied'));await pending;
  assert.equal(h.get('retry').hidden,false);
  const retry=h.refresh();assert.notStrictEqual(retry,pending);
  assert.equal(h.openids.length,2);
  assert.strictEqual(h.refresh(),retry);
  await h.approve();await retry;
  assert.equal(h.get('state').textContent,'Готово к расшифровке');
});

for(const endpoint of ['session','status']){
  test(`network failure at ${endpoint} never leaves refresh locked`,async()=>{
    const h=harness();h.failNext(endpoint);
    const pending=h.refresh();await h.approve();await pending;
    assert.equal(h.get('retry').hidden,false);
    const retry=h.refresh();assert.notStrictEqual(retry,pending);
    if(endpoint==='session')await h.approve();
    await retry;
    assert.equal(h.get('retry').hidden,true);
    assert.equal(h.openids.length,endpoint==='session'?2:1);
  });
}

test('session expiration opens one renewal dialog across thirty seconds of polling',async()=>{
  const h=harness();let pending=h.refresh();await h.approve();await pending;
  h.advance(12*60000+1);pending=h.refresh();
  await h.pollFor(30000);
  assert.equal(h.openids.length,2,'one original login and one renewal');
  assert.strictEqual(h.refresh(),pending);
  await h.approve();await pending;await h.pollFor(2000);
  assert.equal(h.openids.length,2);
  assert.equal(h.get('error').hidden,true);
});

test('denied session renewal can be retried without a stuck lock',async()=>{
  const h=harness();let pending=h.refresh();await h.approve();await pending;
  h.advance(12*60000+1);pending=h.refresh();
  h.openids[1].reject(new Error('renewal denied'));await pending;
  const retry=h.refresh();assert.equal(h.openids.length,3);
  await h.approve();await retry;
  assert.equal(h.get('retry').hidden,true);
});
