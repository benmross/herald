'use strict';
const $ = id => document.getElementById(id);
const state = {sessions:[],sid:null,offset:0,total:0,source:0,tz:'America/New_York',generation:0,detail:null,analysis:null,turn:"",anchor:null};
const num = n => n == null ? 'Unknown' : Number(n).toLocaleString();
const duration = ms => ms == null ? 'Unknown' : ms < 1000 ? `${Math.round(ms)} ms` : ms < 60000 ? `${(ms/1000).toFixed(1)} s` : ms < 3600000 ? `${(ms/60000).toFixed(1)} min` : `${(ms/3600000).toFixed(1)} hr`;
const stamp = t => {if(!t)return 'Timestamp not recorded';const d=new Date(t);return Number.isNaN(+d)?t:new Intl.DateTimeFormat('en-US',{timeZone:state.tz,year:'numeric',month:'short',day:'2-digit',hour:'numeric',minute:'2-digit',second:'2-digit',fractionalSecondDigits:3,hour12:true,timeZoneName:'short'}).format(d).replace(/\.(\d{3})/,(_,ms)=>'.'+ms+(t.match(/\.\d{3}(\d+)/)?.[1]||''));};
const el = (tag,cls,text) => {const x=document.createElement(tag);if(cls)x.className=cls;if(text!=null)x.textContent=text;return x;};
async function api(path){const r=await fetch(path,{cache:'no-store'});if(!r.ok)throw new Error((await r.json()).error||`HTTP ${r.status}`);return r.json();}
function error(e){$('error').textContent=e.message||String(e);$('error').hidden=false;$('status').textContent='Refresh failed';}
function metrics(target,values){const parent=$(target);parent.replaceChildren();for(const [label,value,note] of values){const x=el('div','metric');x.append(el('div','label',label),el('div','value',value));if(note)x.append(el('small','',note));parent.append(x);}}
function tab(which){$('overview').hidden=which!=='overview';$('explorer').hidden=which!=='session';$('overview-tab').classList.toggle('active',which==='overview');$('session-tab').classList.toggle('active',which==='session');}
function sessionList(){const q=$('session-search').value.toLowerCase(),engine=$('engine').value,errors=$('session-state').value;const ss=state.sessions.filter(s=>(!engine||s.engine===engine)&&(!errors||s.errors)&&(!q||JSON.stringify([s.id,s.label,s.title,s.model]).toLowerCase().includes(q)));$('session-count').textContent=`${ss.length}`;$('sessions').replaceChildren();for(const s of ss){const b=el('button','session-row'+(s.id===state.sid?' selected':''));b.append(el('strong','',s.title||s.label),el('small','',`${s.engine||'Unknown engine'} · ${s.model||'model unrecorded'}`));const row=el('div','row-bottom');row.append(el('span','',`${s.id.slice(0,8)} · ${s.turns} turns`),el('span',s.errors?'error-badge':'',s.errors?`${s.errors} errors`:duration(s.turns?s.duration_ms:null)));b.append(row);b.title=`${s.id}\n${stamp(s.updated)}`;b.onclick=()=>selectSession(s.id);$('sessions').append(b);}if(!ss.length)$('sessions').append(el('p','muted','No sessions match.'));}
const NS='http://www.w3.org/2000/svg';function svgEl(tag,attrs,text){const e=document.createElementNS(NS,tag);for(const [k,v]of Object.entries(attrs||{}))e.setAttribute(k,v);if(text!=null)e.textContent=text;return e;}
function tooltip(target, title, lines) {
    document.querySelector('.hovered')?.classList.remove('hovered');
    if(target.matches('g'))target.classList.add('hovered');
    const box = $('chart-tooltip');
    box.replaceChildren(el('strong', '', title), ...lines.map(line => el('div', '', line)));
    box.hidden = false;
    const rect = target.getBoundingClientRect();
    box.style.left = Math.max(8, Math.min(innerWidth - 330, rect.left)) + 'px';
    box.style.top = Math.max(8, Math.min(innerHeight - box.offsetHeight - 8, rect.bottom + 8)) + 'px';
}
function hideTooltip() { $('chart-tooltip').hidden = true; document.querySelector('.hovered')?.classList.remove('hovered'); }
function attachTooltip(target, title, lines, click) {
    target.setAttribute('tabindex', '0');
    target.setAttribute('aria-label', [title, ...lines].join('. '));
    target.onpointerenter = () => tooltip(target, title, lines);
    target.onpointerleave = hideTooltip;
    target.onfocus = () => tooltip(target, title, lines);
    target.onblur = hideTooltip;
    if (click) {
        target.setAttribute('role', 'button');
        target.onclick = () => { hideTooltip(); click(); };
        target.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); target.onclick(); } };
    }
}
function chart(target, points, {bar=false, format=num}={}) {
    const parent = $(target); parent.replaceChildren();
    if (!points.length) { parent.append(el('p','muted','No measurements recorded for this source.')); return; }
    const groups = [], hoverMarkers = [];
    for (const point of points) {
        const id = point.turn || 'unassigned';
        let group = groups.find(g => g.id === id);
        if (!group) { group = {id, points:[]}; groups.push(group); }
        group.points.push(point);
    }
    const segmented = !bar && target === 'context-chart';
    const w = Math.max(700, segmented ? groups.length * 130 : 700), h=240, l=55, r=14, t=35, b=38;
    const max = Math.max(1, ...points.map(p => p.y)), y = n => h-b-(n/max)*(h-t-b);
    const svg = svgEl('svg',{viewBox:`0 0 ${w} ${h}`,role:'img','aria-label':segmented?'Context by turn; idle gaps compressed':'Recorded turns per day'});
    if (w > 700) { svg.style.width = w+'px'; }
    for(let i=0;i<4;i++) {
        const value=max*i/3, yy=y(value);
        svg.append(svgEl('line',{x1:l,y1:yy,x2:w-r,y2:yy,class:'grid'}),svgEl('text',{x:l-8,y:yy+4,'text-anchor':'end'},value>999?`${(value/1000).toFixed(0)}k`:Math.round(value)));
    }
    if (bar) {
        const bw=(w-l-r)/points.length;
        points.forEach((p,i)=>{
            const rect=svgEl('rect',{x:l+i*bw+1,y:y(p.y),width:Math.max(1,bw-2),height:h-b-y(p.y),fill:'#90dfc1',rx:2});
            attachTooltip(rect,p.label,[`${format(p.y)} recorded turns`]); svg.append(rect);
        });
        svg.append(svgEl('text',{x:l,y:h-8},points[0].short||''),svgEl('text',{x:w-r,y:h-8,'text-anchor':'end'},points.at(-1).short||''));
    } else {
        for (const group of groups) {
            const turn = state.analysis?.timing.turns.find(t => t.id === group.id);
            const times=group.points.map(p=>Date.parse(p.ts)).filter(Number.isFinite);
            group.hasTime = times.length === group.points.length;
            group.start = group.hasTime ? (turn?.start_ms ?? Math.min(...times)) : 0;
            group.end = group.hasTime ? (turn?.end_ms ?? Math.max(...times)) : group.points.length-1;
            if (!Number.isFinite(group.start)) {group.start=0; group.end=group.points.length-1;}
            group.duration = Math.max(1,group.end-group.start);
            group.title = turn ? `Turn ${turn.number}` : 'Measurements';
        }
        const gap=30, room=w-l-r-gap*(groups.length-1), minimum=Math.min(70,room/groups.length), total=groups.reduce((n,g)=>n+g.duration,0);
        let left=l, previous=null;
        for (const group of groups) {
            const width=minimum+(room-minimum*groups.length)*group.duration/total;
            const x=(p,i)=>left+(group.hasTime?(Date.parse(p.ts)-group.start)/group.duration:i/Math.max(1,group.points.length-1))*width;
            const coords=group.points.map((p,i)=>({p,x:x(p,i),y:y(p.y)}));
            if (previous) {
                const first=coords[0], last=previous.coords.at(-1), idle=Math.max(0,group.start-previous.end);
                const join=svgEl('path',{d:`M ${last.x} ${last.y} L ${first.x} ${first.y}`,class:'idle-join'});
                attachTooltip(join,'Gap between turns compressed',[`${duration(idle)} between observed turns`, 'No continuous context measurements in this gap.']); svg.append(join);
                svg.append(svgEl('text',{x:left-gap/2,y:h-b+17,'text-anchor':'middle'},'//'));
            }
            const heading=svgEl('text',{x:left,y:16,class:'turn-heading'},`${group.title} · ${group.hasTime?duration(group.duration):'event order'}`);
            attachTooltip(heading,group.title,[group.id==='unassigned'?'Turn boundary not recorded':'Click to show just this turn'],state.analysis?.timing.turns.some(t=>t.id===group.id)?()=>chooseTurn(group.id):null); svg.append(heading);
            svg.append(svgEl('path',{d:coords.map((p,i)=>`${i?'L':'M'} ${p.x} ${p.y}`).join(' '),class:'line'}));
            for (const {p,x:xx,y:yy} of coords) {
                const dot=svgEl('g',{});
                dot.append(svgEl('circle',{cx:xx,cy:yy,r:10,class:'hit'}),svgEl('circle',{cx:xx,cy:yy,r:3.5,class:'visible-dot'}));
                const lines=[stamp(p.ts),`${num(p.y)} prompt tokens${p.delta==null?'':` · ${p.delta>=0?'+':''}${num(p.delta)} since previous measurement`}`,
                    p.window?`${(100*p.y/p.window).toFixed(1)}% of ${num(p.window)} token window`:'Context window not recorded',
                    `Reported near: ${p.nearby||'completed turn'}`,p.tools==null?'Step activity unavailable':`${p.tools} tool call${p.tools===1?'':'s'} ${p.delta==null?'since recording began':'since previous measurement'}`, p.anchor_seq?'Click to inspect nearby activity; proximity does not prove causation.':'No linked event was recorded.'];
                attachTooltip(dot,'Context measurement',lines,p.anchor_seq?()=>jumpToActivity(p.anchor_seq,p.turn):null); hoverMarkers.push({dot,lines,x:xx,y:yy,point:p}); svg.append(dot);
            }
            svg.append(svgEl('text',{x:left,y:h-8},group.hasTime?new Intl.DateTimeFormat('en-US',{timeZone:state.tz,hour:'numeric',minute:'2-digit',hour12:true}).format(new Date(group.start)):'Timestamps incomplete'));
            previous={coords,end:group.end}; left+=width+gap;
        }
    }
    if (!bar && hoverMarkers.length) {
        // Use the nearest visible point, rather than overlapping invisible hit
        // circles that let the last record steal its neighbours' pointer events.
        const surface=svgEl('rect',{x:l,y:t,width:w-l-r,height:h-t-b,fill:'transparent',class:'hover-surface'});
        let nearest=null;
        const findPoint=event=>{
            const inverse=svg.getScreenCTM().inverse();
            const here=new DOMPoint(event.clientX,event.clientY).matrixTransform(inverse);
            let distance=Infinity;nearest=null;
            for(const marker of hoverMarkers) {
                const d=Math.hypot(marker.x-here.x,marker.y-here.y);
                if(d<distance){distance=d;nearest=marker;}
            }
            if(distance>25){nearest=null;hideTooltip();return;}
            tooltip(nearest.dot,'Context measurement',nearest.lines);
        };
        surface.onpointermove=findPoint;
        surface.onpointerleave=hideTooltip;
        surface.onclick=event=>{findPoint(event);if(nearest?.point.anchor_seq){hideTooltip();jumpToActivity(nearest.point.anchor_seq,nearest.point.turn);}};
        svg.append(surface);
    }
    parent.append(svg);
}
function bars(target,data){const parent=$(target);parent.replaceChildren();const max=Math.max(1,...data.map(x=>x.value));for(const d of data){const row=el('div','bar-row');row.append(el('div','',d.label));const track=el('div','bar-track'),fill=el('div','bar-fill');fill.style.width=`${d.value/max*100}%`;track.append(fill);row.append(track,el('div','bar-number',d.display));row.title=d.title||'';parent.append(row);}if(!data.length)parent.append(el('p','muted','No timing data recorded.'));}
function renderOverview(d){metrics('metrics',[['Sessions',num(d.sessions),`${num(d.runs)} recorded turns`],['Turn time',duration(d.duration_ms),`Median ${duration(d.median_ms)} · p95 ${duration(d.p95_ms)}`],['Tokens generated',num(d.output_tokens),`${num(d.cache_read)} cached input tokens`],['Errors',num(d.errors),`${d.runs?(100*d.errors/d.runs).toFixed(1):0}% of recorded turns`]]);chart('activity-chart',d.days.map(x=>({y:x.runs,label:x.date,short:x.date})),{bar:true});bars('time-chart',[{label:'Model spans',value:d.model_ms,display:duration(d.model_ms)},{label:'Tool spans',value:d.tool_ms,display:duration(d.tool_ms)},{label:'Startup',value:d.startup_ms||0,display:duration(d.startup_ms)}]);bars('tools',d.tools.map(t=>({label:t.name||'Unnamed tool',value:t.ms,display:duration(t.ms),title:`${num(t.calls)} calls · average ${duration(t.avg_ms)}`})));}
async function refresh(updateSelected=false){try{$('error').hidden=true;const [data,summary]=await Promise.all([api('/api/sessions'),api('/api/overview')]);state.sessions=data.sessions;state.tz=data.timezone;sessionList();renderOverview(summary);$('status').textContent='Connected · '+new Intl.DateTimeFormat('en-US',{hour:'numeric',minute:'2-digit',hour12:true,timeZone:state.tz}).format(new Date());if(updateSelected&&state.sid&&!$('explorer').hidden)await selectSession(state.sid,false);}catch(e){error(e);}}
function base(){return `/api/session/${encodeURIComponent(state.sid)}`;}
async function selectSession(sid, reset=true) {
    const generation=++state.generation; state.sid=sid;
    if(reset) { state.offset=0; state.source=0; state.turn=''; state.anchor=null; history.replaceState(null,'',`#session=${encodeURIComponent(sid)}`); }
    tab('session'); sessionList();
    try {
        const d=await api(`${base()}/info`); if(generation!==state.generation)return;
        state.detail=d; $('empty').hidden=true; $('detail').hidden=false;
        const s=d.session;
        $('session-engine').textContent=`${s.engine||'Engine unknown'} / ${s.model||'native model'}`;
        $('session-title').textContent=s.title||s.label; $('session-id').textContent=s.id;
        $('session-meta').textContent=`${s.turns} completed runs · Last activity ${stamp(s.updated)}`;
        $('session-metrics').replaceChildren(el('span','',`Latest completed-run context: ${num(s.context_tokens)} tokens`),el('span','',`${s.errors} recorded errors`),el('span','',`${(s.bytes/1048576).toFixed(1)} MB · ${d.sources.length} source(s); raw records preserved`));
        $('source').replaceChildren();
        for (const src of d.sources) { const op=el('option','',`${src.kind} · ${src.name}`);op.value=src.index;$('source').append(op); }
        if(state.source>=d.sources.length)state.source=0;
        $('source').value=state.source; renderTurns(d); await loadAnalysis(generation); await loadEvents(generation);
    } catch(e) { if(generation===state.generation)error(e); }
}
async function loadAnalysis(generation=state.generation) {
    if (!state.detail?.sources.length) { state.analysis=null; return; }
    const source=state.source;
    const data=await api(`${base()}/analysis?source=${source}`);
    if(generation!==state.generation||source!==state.source)return;
    state.analysis=data;
    if(state.turn&&!data.timing.turns.some(t=>t.id===state.turn))state.turn='';
    $('turn-filter').replaceChildren(el('option','','All turns'));
    $('turn-filter').firstChild.value='';
    for(const t of data.timing.turns) { const option=el('option','',`Turn ${t.number} · ${duration(t.duration_ms)}${t.complete?'':' · observed / partial'}`); option.value=t.id;$('turn-filter').append(option); }
    $('turn-filter').value=state.turn; renderTiming(); contextChart(data.context);
}
function renderTiming() {
    const turns=state.analysis?.timing.turns.filter(t=>!state.turn||t.id===state.turn)||[];
    const sum=key=>turns.reduce((n,t)=>n+(t[key]||0),0);
    metrics('timing-metrics', [['Observed turn time',turns.length?duration(sum('duration_ms')):'Unknown',`${turns.length} turns; inter-turn gaps omitted`],['Waiting for tools',turns.length?duration(sum('tool_ms')):'Unknown','Overlapping waits counted once'],['Between tool responses',turns.length?duration(sum('between_ms')):'Unknown','Model / transport / background work'],['Unconfirmed waits',turns.length?duration(sum('unconfirmed_ms')):'Unknown','Request recorded; result missing']]);
    const parent=$('time-lanes');parent.replaceChildren();
    for(const turn of turns) {
        const row=el('div','turn-lane'),label=el('button','turn-label',`Turn ${turn.number} · ${duration(turn.duration_ms)}${turn.complete?'':' · partial'}`);
        label.onclick=()=>chooseTurn(state.turn?'':turn.id); row.append(label);
        const lane=el('div','time-lane');
        for(const segment of turn.segments) {
            const part=el('button',`time-segment ${segment.kind}`);
            part.style.flexGrow=String(segment.ms);part.style.flexBasis='0';
            const name={tool:'Waiting for a tool response',between:'Between tool responses',unconfirmed:'Tool response not recorded'}[segment.kind];
            attachTooltip(part,`${name} · ${duration(segment.ms)}`,[stamp(new Date(segment.start_ms).toISOString()),segment.kind==='between'?'Includes model generation, transport, background work and overhead.':'Click to inspect the tool call.'],segment.seqs.length?()=>jumpToActivity(segment.seqs[0],turn.id):null);
            lane.append(part);
        }
        row.append(lane);parent.append(row);
    }
}
function chooseTurn(id) { state.turn=id; state.offset=0; $('turn-filter').value=id; renderTiming(); contextChart(state.analysis?.context||[]); loadEvents(); }
function jumpToActivity(seq,turn) {
    state.anchor=seq; state.turn=turn||''; state.offset=0;
    $('turn-filter').value=state.turn; $('log-view').value='activity';$('kind').value='';$('event-search').value='';
    renderTiming(); contextChart(state.analysis?.context||[]);loadEvents();
}
function renderTurns(d){const parent=$('turns');parent.replaceChildren();const table=el('table');const head=el('tr');for(const t of ['Recorded at','Duration','Context','Input','Cached input','Output','Model spans (overlap)','Tool spans (overlap)','Error'])head.append(el('th','',t));table.append(head);for(const r of d.session.runs){const row=el('tr');for(const val of [stamp(r.ts),duration(r.duration_ms),num(r.context_tokens),num(r.input_tokens),num(r.cache_read),num(r.output_tokens),duration(r.model_ms),duration(r.tool_ms),r.error||''])row.append(el('td','',val));table.append(row);}parent.append(table);const phases=d.phases.filter(p=>p.kind==='tool').sort((a,b)=>b.ms-a.ms).slice(0,12);if(phases.length){parent.append(el('h3','','Slowest recorded tool phases'));for(const p of phases)parent.append(el('p','footnote',`${duration(p.ms)} · ${p.name} · ${p.detail||''}`));}}
let eventRequest=0;
async function loadEvents(generation=state.generation) {
    const request=++eventRequest;
    if(!state.detail?.sources.length) { $('events').replaceChildren(el('p','muted','No provider transcript is available.'));return; }
    const activity=$('log-view').value==='activity', anchor=state.anchor;
    const q=new URLSearchParams({source:String(state.source),offset:String(state.offset),kind:$('kind').value,q:$('event-search').value,view:activity?'activity':'raw',turn:activity?state.turn:''});
    if(anchor)q.set('anchor',anchor);
    $('download').href=`${base()}/download?source=${state.source}`;
    try {
        const d=await api(`${base()}/events?${q}`);if(request!==eventRequest||generation!==state.generation)return;
        state.offset=d.offset;state.total=d.total;
        $('event-count').textContent=`${num(d.total)} ${activity?'activities':'records'} matching · ${num(state.analysis?.raw_count||d.all)} raw records in source`;
        $('page-label').textContent=d.total?`${num(state.offset+1)}–${num(Math.min(state.offset+100,d.total))} of ${num(d.total)}`:'No matching activity';
        $('previous').disabled=state.offset===0;$('next').disabled=state.offset+100>=d.total;
        $('events').replaceChildren();for(const ev of d.events)renderEvent(ev,activity);
        if(!d.events.length)$('events').append(el('p','muted','No matching activity in this selection. Try All turns or Raw records.'));
        if(anchor) {const target=document.getElementById('event-'+anchor);target?.scrollIntoView({behavior:'smooth',block:'center'});target?.classList.add('highlight');state.anchor=null;}
    }catch(e){if(request===eventRequest)error(e);}
}
function contextChart(points) {
    let fallback=false;
    if(!points.length&&state.detail) {points=state.detail.session.runs.filter(r=>r.context_tokens!=null).map((r,i)=>({seq:i+1,ts:r.ts,context:r.context_tokens,turn:'recorded-'+r.id}));fallback=points.length>0;}
    if(state.turn)points=points.filter(p=>p.turn===state.turn);
    $('context-label').textContent=`${num(points.length)} measurements${fallback?' per completed run':' · idle gaps compressed'}`;
    chart('context-chart',points.map(p=>({...p,y:p.context})));
}
function renderEvent(ev, activity=false) {
    const item=el('article','event');item.dataset.kind=ev.kind;item.id='event-'+ev.seq;
    const head=el('div','event-head');
    head.append(el('span','pill',`#${ev.seq}`),el('span','event-kind',ev.kind),el('span','event-name',ev.name));
    if(ev.duration_ms!=null)head.append(el('span','duration-badge',duration(ev.duration_ms)+' wait'));
    const ts=el('time','',stamp(ev.ts));ts.title=ev.ts||'Not recorded';head.append(ts);item.append(head);
    if(ev.explanation)item.append(el('p','event-explanation',ev.explanation));
    if(ev.repeats)item.append(el('p','footnote',`${ev.repeats} later record(s) contain exactly the same instruction text. See Raw records for each occurrence.`));
    const content=el('div','event-content');
    const input=activity&&ev.kind==='tool'?ev.input:ev.text;
    const pre=el('pre','',input||'');
    if(activity&&ev.kind==='context') {
        const details=el('details','instruction-block'),summary=el('summary','',`Inspect instructions · ${num(ev.characters||ev.text.length)} characters`);
        details.append(summary,pre);content.append(details);
    } else if(input) {if(activity&&ev.kind==='tool')content.append(el('div','content-label','REQUEST'));content.append(pre);}
    if(activity&&ev.kind==='tool'&&ev.output!=null) {content.append(el('div','content-label','RESULT'),el('pre','',ev.output));}
    if(ev.children?.length) {
        const details=el('details','nested-work');details.append(el('summary','',`${ev.children.length} underlying command / tool records`));
        for(const child of ev.children) {details.append(el('p','footnote',`${child.name} · ${stamp(child.ts)}`),el('pre','',child.text));}
        content.append(details);
    }
    item.append(content);
    const actions=el('div','event-actions'),expand=el('button','','Expand output');
    expand.onclick=()=>{item.classList.toggle('expanded');expand.textContent=item.classList.contains('expanded')?'Constrain height':'Expand output';};
    const raw=el('details','raw-record');raw.append(el('summary','',activity?'Original record(s)':'Original record'));
    // Create large raw payloads only when explicitly expanded.
    raw.ontoggle=()=>{if(raw.open&&!raw.querySelector('pre'))raw.append(el('pre','',JSON.stringify(ev.records||ev.raw,null,2)));};
    const copy=el('button','','Copy');copy.onclick=async()=>{try{await navigator.clipboard.writeText(ev.text);copy.textContent='Copied';}catch{copy.textContent='Select text to copy';}};
    actions.append(expand,copy);item.append(actions,raw);
    if(ev.call_id)item.append(el('div','footnote','Call '+ev.call_id+(ev.end_ts?' · result '+stamp(ev.end_ts):'')));
    $('events').append(item);
}
let debounce;function filterEvents(){clearTimeout(debounce);debounce=setTimeout(()=>{state.offset=0;loadEvents();},250);}
$('session-search').oninput=sessionList;$('engine').onchange=sessionList;$('session-state').onchange=sessionList;$('refresh').onclick=()=>refresh(true);$('overview-tab').onclick=()=>{tab('overview');history.replaceState(null,'','#');};$('session-tab').onclick=()=>tab('session');$('source').onchange=async()=>{state.source=Number($('source').value);state.offset=0;state.turn='';try{await loadAnalysis();await loadEvents();}catch(e){error(e);}};$('kind').onchange=filterEvents;$('event-search').oninput=filterEvents;$('previous').onclick=()=>{state.offset=Math.max(0,state.offset-100);loadEvents();};$('next').onclick=()=>{state.offset+=100;loadEvents();};$('top').onclick=()=>document.querySelector('.timeline-panel').scrollIntoView({behavior:'smooth'});
refresh().then(()=>{const match=location.hash.match(/^#session=(.+)$/);if(match)selectSession(decodeURIComponent(match[1]));});
// Keep in-flight work inspectable without disturbing a selected event page.
setInterval(()=>{if(!document.hidden)refresh();},30000);

$('turn-filter').onchange=()=>chooseTurn($('turn-filter').value);
$('log-view').onchange=()=>{state.offset=0;loadEvents();};
window.addEventListener('scroll',hideTooltip,{passive:true});
