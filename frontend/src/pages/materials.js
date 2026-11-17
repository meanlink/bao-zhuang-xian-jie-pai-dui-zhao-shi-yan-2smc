import {materials} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {RecallStatus} from '../constants/enums.js';
import {esc,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const canWrite=()=>session.user.role!=='viewer';
const isAdmin=()=>session.user.role==='admin';

function ticketBadge(t){
 const label=RecallStatus[t.status]||t.status;
 const total=t.total_rolls||0, done=t.processed_rolls||0;
 return '<span class="recall-state '+esc(t.status)+'">'+esc(label)+'</span>'+(t.status!=='completed'?'<span class="hint-inline">'+done+'/'+total+' 卷</span>':'');
}

export async function materialsPage(params){
 const id=params?.[0];
 const {rolls}=await materials.list();
 const [list,status,impact]=await Promise.all([materials.recalls(),materials.status(),(id?materials.impact(id):Promise.resolve(null))]);
 workspace.recall=status;
 workspace.rolls=rolls;
 const pending=list.filter(t=>t.status==='pending');
 const running=list.filter(t=>t.status==='running');
 const done=list.filter(t=>t.status==='completed');
 const selected=impact||null;
 return heading('06 / MATERIAL LINEAGE','卷料谱系与召回处置','登记来源卷、派生卷与片段用料，支持多级拆分与合并。召回任一卷料后，全部后代、试验片段、分析结果与分发均不可再使用；既有结论保留并标明召回。')
 +'<div class="recall-banner '+((status.open||[]).length?'warn':'ok')+'"><div><strong>'+((status.open||[]).length?'召回处置进行中':'无待处置召回')+'</strong><span>已封禁卷料 '+(status.recalled_rolls||0)+' 卷 · 数据批次 '+(status.recalled_datasets||0)+' 批 · 分析 '+(status.recalled_jobs||0)+' 个 · 分发计划 '+(status.disabled_reports||0)+' 个</span></div>'+((status.open||[]).length?'<small>扫描分批进行，未结束前已发现的卷料与片段仍持续封禁；未受影响的试验照常使用。</small>':'')+'</div>'
 +'<div class="lineage-grid"><section class="panel"><div class="section-title"><h2>登记来源卷</h2><span>'+rolls.length+' 卷</span></div>'
 +'<form id="roll-form" class="inline-form"><label>卷料编号<input name="code" required minlength="2" placeholder="例如 SRC-202611-01"></label><label>名称<input name="name" placeholder="可选"></label><button class="primary small" type="submit" '+(canWrite()?'':'disabled')+'>登记卷料</button></form>'
 +'<form id="link-form" class="inline-form"><label>来源卷<input name="parent" required placeholder="父卷编号"></label><label>派生卷<input name="child" required placeholder="子卷编号"></label><label>类型<select name="kind"><option value="split">分切拆分</option><option value="merge">拼批合并</option></select></label><button class="button small" type="submit" '+(canWrite()?'':'disabled')+'>挂接谱系</button></form>'
 +'<form id="usage-form" class="inline-form"><label>卷料<input name="roll" required placeholder="卷料编号"></label><label>试验片段<input name="run" required placeholder="片段号 run"></label><button class="button small" type="submit" '+(canWrite()?'':'disabled')+'>挂接用料</button></form>'
 +'<p class="hint">拒绝自关联、重复关联与任何会形成循环的挂接。已召回卷料不可再挂接新派生卷或片段用料。</p>'
 +'<div class="section-title"><h2>卷料清单</h2><span>按编号登记</span></div>'
 +'<div class="roll-list">'+rolls.map(r=>'<div class="roll-item '+(r.recalled?'recalled':'')+'"><div><strong>'+esc(r.code)+'</strong><span>'+esc(r.name||'—')+'</span></div><small>'+r.usage_count+' 段用料'+(r.recalled?' · 已召回':'')+'</small></div>').join('')+(!rolls.length?'<p class="empty">尚无卷料。先登记来源卷，再挂接派生卷与片段用料。</p>':'')+'</div></section>'
 +'<section class="panel"><div class="section-title"><h2>召回处置</h2><span>待处置 '+pending.length+' · 处理中 '+running.length+' · 已完成 '+done.length+'</span></div>'
 +'<form id="recall-form" class="inline-form"><label>召回卷料<input name="roll" required placeholder="卷料编号"></label><label>召回原因<input name="reason" placeholder="例如混批、批次异常"></label><button class="primary small" type="submit" '+(isAdmin()?'':'disabled')+'>发起召回</button></form>'
 +'<p class="hint">重复提交同一卷料不会新增召回单；并发挂接与分发会被闸门拦截。仅业务负责人可发起召回。</p>'
 +'<div class="ticket-columns">'+_ticketColumn('待处置',pending)+_ticketColumn('处理中',running)+_ticketColumn('已完成',done.slice(0,10))+'</div></section></div>'
 +(selected?await _impactPanel(selected):_impactHint());
}

function _impactHint(){
 return '<section class="panel"><div class="section-title"><h2>影响路径</h2><span>选择一张召回单查看</span></div><p class="hint">点击上方召回单，可查看被封禁卷料的来源 → 派生路径、关联试验片段、受影响的数据批次/分析与分发计划。扫描未完成时也会持续封禁已发现的卷料与片段。</p></section>';
}

function _ticketColumn(title,items){
 return '<div class="ticket-column"><h3>'+esc(title)+'</h3>'+items.map(t=>'<a class="ticket '+(t.status==='completed'?'done':'')+'" href="#/materials/'+t.id+'"><div><strong>'+esc(t.roll_code||('#'+t.roll_id))+'</strong><span>'+esc(t.reason||'未填写原因')+'</span><small>'+esc(t.created_by||'')+' · '+date(t.created_at)+'</small></div><div>'+ticketBadge(t)+'<small>片段 '+t.segments.length+' · 批次 '+(t.datasets||0)+' · 分析 '+(t.jobs||0)+'</small></div></a>').join('')+(!items.length?'<p class="empty">暂无记录</p>':'')+'</div>';
}

async function _impactPanel(impact){
 const t=impact.ticket;
 const canScan=session.user.role==='admin';
 const rollCols=[{key:'code',label:'卷料'},{key:'depth',label:'层级'},{key:'path',label:'影响路径'},{key:'state',label:'状态'}];
 const rollRows=impact.rolls.map(r=>({code:r.roll.code,depth:r.depth,path:r.path.join(' → '),state:r.roll.recalled?'已封禁':'待扫描'}));
 const segmentRows=impact.segments.map(s=>({code:s.roll_code,run:s.run,state:'已封禁'}));
 return '<section class="panel recall-impact"><div class="section-title"><h2>召回单 #'+t.id+' · '+esc(impact.rolls[0]?.roll.code||'')+'</h2>'+ticketBadge(t)+'</div>'
 +'<div class="impact-summary"><div><span>原因</span><strong>'+esc(t.reason||'未填写')+'</strong></div><div><span>扫描进度</span><strong>'+(t.scanned_rolls||0)+' / '+(t.total_rolls||0)+'</strong></div><div><span>影响批次</span><strong>'+impact.datasets.length+'</strong></div><div><span>封禁分析</span><strong>'+impact.jobs.length+'</strong></div><div><span>停用分发</span><strong>'+impact.reports.length+'</strong></div></div>'
 +(t.error?'<p class="warning">扫描异常：'+esc(t.error)+'（已保留已处理进度，可继续扫描）</p>':'')
 +'<div class="two-columns"><div><div class="section-title"><h2>影响路径</h2><span>来源 → 派生（分切/拼批）</span></div>'+table(rollCols,rollRows)+'</div><div><div class="section-title"><h2>关联片段</h2><span>'+segmentRows.length+' 段</span></div>'+table([{key:'code',label:'卷料'},{key:'run',label:'试验片段'},{key:'state',label:'状态'}],segmentRows)+'</div></div>'
 +'<div class="two-columns"><section><div class="section-title"><h2>不可再使用的数据批次与分析</h2><span>原结论保留并标明召回</span></div>'+table([{key:'name',label:'数据批次'},{key:'status',label:'状态'}],impact.datasets.map(d=>({name:d.name,status:'已召回封禁'})))+table([{key:'name',label:'分析结果'},{key:'status',label:'状态'}],impact.jobs.map(j=>({name:j.name,status:'已召回封禁'})))+'</section><section><div class="section-title"><h2>停用的分发计划</h2><span>不再产生新归档</span></div>'+table([{key:'name',label:'分发计划'},{key:'state',label:'状态'}],impact.reports.map(r=>({name:r.name,state:r.enabled?'异常启用':'已停用'})))+'</section></div>'
 +'<div class="ticket-actions">'+(t.status!=='completed'&&canScan?'<button class="button" id="scan-ticket" data-ticket="'+t.id+'">继续扫描一批</button>':'')+'<a class="text-link" href="#/materials">返回召回列表</a></div></section>';
}

export function bindMaterials(refresh){
 const bindForm=(sel,fn)=>submit(document.querySelector(sel),fn);
 bindForm('#roll-form',async f=>{await materials.addRoll({code:f.get('code'),name:f.get('name')});notify('卷料已登记');await refresh();});
 bindForm('#link-form',async f=>{await materials.link({parent:f.get('parent'),child:f.get('child'),kind:f.get('kind')});notify('谱系关系已挂接');await refresh();});
 bindForm('#usage-form',async f=>{await materials.usage({roll:f.get('roll'),run:f.get('run')});notify('片段用料已登记');await refresh();});
 bindForm('#recall-form',async f=>{const r=await materials.recall({roll:f.get('roll'),reason:f.get('reason')});notify(r.created?'召回单已建立，正在分批封禁':'该卷料已有召回单，未重复创建');await refresh();});
 const scan=document.querySelector('#scan-ticket');
 if(scan)scan.onclick=async()=>{scan.disabled=true;try{const r=await materials.scan(scan.dataset.ticket);notify('已扫描一批：'+r.processed_rolls+'/'+r.total_rolls+' 卷');await refresh();}catch(e){notify(e.message,true);}finally{scan.disabled=false;}};
}
