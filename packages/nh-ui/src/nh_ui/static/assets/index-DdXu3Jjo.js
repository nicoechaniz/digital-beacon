(function(){let e=document.createElement(`link`).relList;if(e&&e.supports&&e.supports(`modulepreload`))return;for(let e of document.querySelectorAll(`link[rel="modulepreload"]`))n(e);new MutationObserver(e=>{for(let t of e)if(t.type===`childList`)for(let e of t.addedNodes)e.tagName===`LINK`&&e.rel===`modulepreload`&&n(e)}).observe(document,{childList:!0,subtree:!0});function t(e){let t={};return e.integrity&&(t.integrity=e.integrity),e.referrerPolicy&&(t.referrerPolicy=e.referrerPolicy),e.crossOrigin===`use-credentials`?t.credentials=`include`:e.crossOrigin===`anonymous`?t.credentials=`omit`:t.credentials=`same-origin`,t}function n(e){if(e.ep)return;e.ep=!0;let n=t(e);fetch(e.href,n)}})();function e(){let e=document.getElementById(`app-shell`);e&&(e.className=`app-shell`,e.innerHTML=`
    <header class="topbar">
      <div class="brand">
        <span class="sigil">NH</span>
        <div>
          <h1>NaturalHarmony v2</h1>
          <p>Scene instrument · Beacon + Shaper + field recordings</p>
        </div>
      </div>
      <div id="preset-bar" class="preset-bar">
        <select id="preset-select" aria-label="Preset select">
          <option value="">Loading v2 presets…</option>
        </select>
        <button id="refresh-button" type="button">Refresh</button>
        <button id="panic-button" type="button" class="danger">Panic</button>
        <span id="connection-status" class="status disconnected">disconnected</span>
      </div>
    </header>

    <main class="workspace-grid">
      <section id="sources-panel" class="panel sources-panel">
        <div class="panel-heading"><h2>Sources</h2><span id="source-count">0</span></div>
        <div id="sources-list" class="card-list"></div>
      </section>

      <section id="shaper-panel" class="panel shaper-panel">
        <div class="panel-heading"><h2>Shaper / Launchpad</h2><span id="active-voices">0 voices</span></div>
        <div id="launchpad-grid" class="launchpad-grid" aria-label="Launchpad harmonic pad grid"></div>
        <div id="voice-list" class="voice-list"></div>
      </section>

      <section id="spatial-panel" class="panel spatial-panel">
        <div class="panel-heading"><h2>Spatial Field</h2><span>32 bands</span></div>
        <div id="spatial-radar" class="spatial-radar"><canvas id="spatial-canvas" width="320" height="240"></canvas></div>
        <div id="spatial-band-list" class="spatial-band-list"></div>
      </section>

      <section id="processing-panel" class="panel processing-panel">
        <div class="panel-heading"><h2>Processing</h2><span id="processor-count">0</span></div>
        <div id="processor-list" class="card-list empty-note">No processors in scene yet.</div>
      </section>

      <section id="lfo-panel" class="panel lfo-panel">
        <div class="panel-heading"><h2>LFO / Modulation</h2><span id="route-count">0</span></div>
        <div id="lfo-list" class="card-list empty-note">No modulation routes yet.</div>
      </section>

      <section id="analysis-panel" class="panel analysis-panel">
        <div class="panel-heading"><h2>Analysis / Field Recordings</h2><span>upload → analyze → source</span></div>
        <div class="analysis-grid">
          <div id="analysis-f0" class="metric-card"><b>F0</b><span>waiting for analysis</span></div>
          <div id="analysis-phideus" class="metric-card"><b>Phideus</b><span>waiting for descriptors</span></div>
          <div id="analysis-proposed-f1" class="metric-card"><b>Proposed f1</b><span>waiting for recording</span></div>
        </div>
        <div id="samples-panel" class="samples-panel empty-note">Sample workflow will appear here.</div>
      </section>

      <section id="event-log" class="panel event-log">
        <div class="panel-heading"><h2>Event Log</h2><span>live</span></div>
        <div id="event-log-lines"></div>
      </section>
    </main>
  `,c())}function t(e,t){let n=document.getElementById(`connection-status`);n&&(n.className=`status ${e}`,n.textContent=t)}function n(e){let t=document.getElementById(`event-log-lines`);if(!t)return;let n=document.createElement(`div`);for(n.className=`log-line`,n.textContent=`[${new Date().toLocaleTimeString()}] ${e}`,t.prepend(n);t.children.length>80;)t.removeChild(t.lastChild)}function r(e){document.getElementById(`refresh-button`)?.addEventListener(`click`,()=>e.onRefresh()),document.getElementById(`panic-button`)?.addEventListener(`click`,()=>e.onTypedControl(`panic`,!0)),document.getElementById(`launchpad-grid`)?.addEventListener(`click`,t=>{let n=t.target.closest(`.pad-button`);if(!n)return;let r=Number(n.dataset.n);e.onControl(`sources.shaper.voice_${r}_toggle`,1)}),document.getElementById(`sources-panel`)?.addEventListener(`click`,t=>{let n=t.target,r=n.dataset.sourceId;r&&(n.matches(`[data-action="mute"]`)&&e.onMuteSource(r,!0),n.matches(`[data-action="unmute"]`)&&e.onMuteSource(r,!1),n.matches(`[data-action="solo"]`)&&e.onSoloSource(r))}),document.getElementById(`sources-panel`)?.addEventListener(`input`,t=>{let n=t.target,r=n.dataset.path;r&&e.onControl(r,Number(n.value))})}function i(e){a(e),s(e),l(e),u(e),d(e)}function a(e){let t=document.getElementById(`sources-list`),n=document.getElementById(`source-count`);if(!t)return;let r=e.sources||{},i=Object.entries(r);n&&(n.textContent=String(i.length)),t.innerHTML=i.map(([e,t])=>o(e,t)).join(``)}function o(e,t){let n=t.kind||t.type||e,r=t.runtime||{},i=t.f1??t.model?.f1,a=t.master_gain??t.gain??1,o=r.gain_offset??1,s=a*o,c=`sources.${e}.gain`,l=t.bands?Object.keys(t.bands).length:0,u=r.voice_count??Object.keys(r.active_voices||{}).length,d=r.effective_f1??i,f=o===0;return`
    <article id="${m(`source-card-${e}`)}" class="source-card kind-${m(n)} ${f?`muted`:``}">
      <div class="source-title"><b>${m(e)}</b><span>${m(n)}</span></div>
      <dl>
        ${i===void 0?``:`<div><dt>f1</dt><dd>${p(i)} Hz</dd></div>`}
        ${d!==void 0&&d!==i?`<div><dt>effective f1</dt><dd>${p(d)} Hz</dd></div>`:``}
        <div><dt>model gain</dt><dd>${p(a)}</dd></div>
        <div><dt>runtime gain</dt><dd>${p(o)}</dd></div>
        <div><dt>effective gain</dt><dd>${p(s)}</dd></div>
        ${l?`<div><dt>bands</dt><dd>${l}</dd></div>`:``}
        ${u?`<div><dt>voices</dt><dd>${u}</dd></div>`:``}
      </dl>
      <label class="inline-control">runtime gain
        <input id="source-gain-${m(e)}" type="range" min="0" max="1.5" step="0.01" value="${o}" data-path="${m(c)}" />
      </label>
      <div class="source-controls">
        <button type="button" data-action="mute" data-source-id="${m(e)}">Mute</button>
        <button type="button" data-action="unmute" data-source-id="${m(e)}">Unmute</button>
        <button type="button" data-action="solo" data-source-id="${m(e)}">Solo</button>
      </div>
      ${e===`beacon`?`
        <label class="inline-control">f1 offset
          <input type="range" min="-24" max="24" step="0.1" value="${t.f1_offset??r.f1_offset??0}" data-path="sources.beacon.f1_offset" />
        </label>`:``}
    </article>
  `}function s(e){let t=((e.sources?.shaper||Object.values(e.sources||{}).find(e=>e.kind===`shaper`))?.runtime||{}).active_voices||{},n=Object.keys(t),r=document.getElementById(`active-voices`),i=document.getElementById(`voice-list`);r&&(r.textContent=`${n.length} voices`),document.querySelectorAll(`.pad-button`).forEach(e=>{let t=e.dataset.n||``;e.classList.toggle(`active`,n.includes(t))}),i&&(i.innerHTML=n.length?n.map(e=>`<span class="voice-pill">H${m(e)}</span>`).join(``):`<span class="empty-note">No active shaper voices.</span>`)}function c(){let e=document.getElementById(`launchpad-grid`);if(e){e.innerHTML=``;for(let t=1;t<=64;t+=1){let n=document.createElement(`button`);n.type=`button`,n.className=`pad-button`,n.dataset.n=String(t),n.title=`Toggle Shaper harmonic ${t}`,n.textContent=String(t),e.appendChild(n)}}}function l(e){let t=(e.sources?.beacon||Object.values(e.sources||{}).find(e=>e.kind===`beacon`))?.bands||{},n=document.getElementById(`spatial-band-list`);if(n){let e=Object.entries(t).slice(0,32);n.innerHTML=e.length?e.map(([e,t])=>`
        <div class="spatial-row">
          <span>H${m(e)}</span>
          <span>az ${p(t.az??0)}°</span>
          <span>dist ${p(t.dist??1)}</span>
          <span>q ${p(t.q??.5)}</span>
          <span>${t.on===!1?`off`:`on`}</span>
        </div>
      `).join(``):`<span class="empty-note">No beacon bands in scene.</span>`}f(t)}function u(e){let t=e.processing_chain?.processors||[],n=document.getElementById(`processor-count`),r=document.getElementById(`processor-list`);n&&(n.textContent=String(t.length)),r&&(r.classList.toggle(`empty-note`,t.length===0),r.innerHTML=t.length?t.map(e=>`<article class="mini-card"><b>${m(e.processor_id||e.id||`processor`)}</b><span>${m(e.kind||e.type||``)}</span></article>`).join(``):`No processors in scene yet.`)}function d(e){let t=e.modulation_routes||{},n=e.lfos||{},r=document.getElementById(`route-count`),i=document.getElementById(`lfo-list`),a=Object.keys(t).length+Object.keys(n).length;r&&(r.textContent=String(a)),i&&(i.classList.toggle(`empty-note`,a===0),i.innerHTML=a?[...Object.entries(n).map(([e,t])=>`<article class="mini-card"><b>${m(e)}</b><span>${m(t.waveform||`lfo`)}</span></article>`),...Object.entries(t).map(([e,t])=>`<article class="mini-card"><b>${m(e)}</b><span>${m(t.target_path||``)}</span></article>`)].join(``):`No modulation routes yet.`)}function f(e){let t=document.getElementById(`spatial-canvas`);if(!t)return;let n=t.getContext(`2d`);if(!n)return;let r=t.width,i=t.height,a=r/2,o=i/2,s=Math.min(r,i)*.38;n.clearRect(0,0,r,i),n.strokeStyle=`#2f3b52`,n.lineWidth=1;for(let e of[.33,.66,1])n.beginPath(),n.arc(a,o,s*e,0,Math.PI*2),n.stroke();Object.entries(e||{}).slice(0,32).forEach(([e,t])=>{if(t.on===!1)return;let r=(Number(t.az??0)-90)*Math.PI/180,i=Math.max(.1,Math.min(1.2,Number(t.dist??1))),c=a+Math.cos(r)*s*Math.min(i,1),l=o+Math.sin(r)*s*Math.min(i,1);n.fillStyle=`#58a6ff`,n.beginPath(),n.arc(c,l,4,0,Math.PI*2),n.fill(),n.fillStyle=`#c9d1d9`,n.font=`10px system-ui`,n.fillText(e,c+6,l+3)})}function p(e){let t=Number(e);return Number.isFinite(t)?t.toFixed(Math.abs(t)>=100?0:2):`—`}function m(e){return String(e).replaceAll(`&`,`&amp;`).replaceAll(`<`,`&lt;`).replaceAll(`>`,`&gt;`).replaceAll(`"`,`&quot;`).replaceAll(`'`,`&#039;`)}var h=`/nh/v2/scene`,g=`/nh/v2/scene/control`,_=`/nh/v2/presets`,v=null,y=null;async function b(e,t){let n=await fetch(e,t);if(!n.ok){let e=await n.text();throw Error(`${n.status} ${n.statusText}: ${e}`)}return n.json()}async function x(){try{t(`connecting`,`syncing scene`),v=await b(h),i(v),t(`connected`,`scene online`)}catch(e){t(`error`,`scene unavailable`),n(`Scene fetch failed: ${e instanceof Error?e.message:String(e)}`)}}async function S(e,t){await b(g,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify({path:e,value:t})}),n(`control ${e} = ${JSON.stringify(t)}`),await x()}async function C(e,t){await b(g,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify({type:e,value:t})}),n(`control ${e} ${JSON.stringify(t)}`),await x()}async function w(e,t){await b(`/nh/v2/scene/sources/${encodeURIComponent(e)}/mute`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify({mute:t})}),n(`${t?`muted`:`unmuted`} ${e}`),await x()}async function T(e){await b(`/nh/v2/scene/sources/${encodeURIComponent(e)}/solo`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify({solo:!0})}),n(`solo ${e}`),await x()}async function E(){try{let e=await b(_),t=document.getElementById(`preset-select`);if(!t)return;t.innerHTML=`<option value="">Select v2 preset…</option>`,e.forEach(e=>{let n=document.createElement(`option`);n.value=e.id,n.textContent=`${e.name??e.id} · ${e.n_sources??0} sources`,t.appendChild(n)}),n(`loaded ${e.length} v2 presets`)}catch(e){n(`Preset list failed: ${e instanceof Error?e.message:String(e)}`)}}async function D(){e(),r({onRefresh:x,onControl:S,onTypedControl:C,onMuteSource:w,onSoloSource:T}),await Promise.all([x(),E()]),y=window.setInterval(x,1500),window.addEventListener(`beforeunload`,()=>{y!==null&&window.clearInterval(y)})}D().catch(e=>{t(`error`,`boot failed`),n(`Boot failed: ${e instanceof Error?e.message:String(e)}`)});