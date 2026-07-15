const state={token:localStorage.getItem('aps_token')||'',user:null,page:'dashboard',masterType:'order',masterOrderType:'machining',masterCalendarType:'machining',masterRecords:[],versions:[],users:[],effectiveData:null,effectiveFilters:{},effectiveView:'order',lockTarget:null,dragAdjustment:null,pendingAdjustment:null};
const $=s=>document.querySelector(s); const content=$('#content');
const labels={dashboard:['OPERATIONS CENTER','生产运营总览'],tasks:['SCHEDULING JOBS','排程任务中心'],versions:['PLAN GOVERNANCE','计划版本管理'],effective:['EFFECTIVE SCHEDULE','生效排程看板'],master:['MASTER DATA','主数据中心'],users:['ACCESS CONTROL','用户与权限'],audit:['AUDIT TRAIL','系统审计日志']};
const entityLabels={order:'生产订单',machine:'设备档案',worker:'人员档案',resource_group:'资源组',calendar:'工厂日历'};
const batchEntityTypes=new Set(['order','machine','worker','resource_group']);
const entityFileNames={order:'orders',machine:'machines',worker:'workers',resource_group:'resource_groups'};
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}
function badge(v){return `<span class="badge ${esc(v)}"><i class="status-dot"></i>${esc(v)}</span>`}
const processStatusLabels={PENDING:'待排程',SCHEDULED:'已排程',CONFIRMED:'已确认',IN_PROGRESS:'执行中',PAUSED:'已暂停',COMPLETED:'已完成',CANCELLED:'已取消'};
function processStatusLabel(value){const status=String(value||'-').toUpperCase();return processStatusLabels[status]?`${processStatusLabels[status]}(${status})`:status}
function processStatusBadge(value){const status=String(value||'-').toUpperCase();return `<span class="badge ${esc(status)}"><i class="status-dot"></i>${esc(processStatusLabel(status))}</span>`}
function toast(msg,error=false){const el=$('#toast');el.textContent=msg;el.className='toast'+(error?' error':'');setTimeout(()=>el.classList.add('hidden'),3000)}
async function api(path,options={}){const headers={'Content-Type':'application/json',...(options.headers||{})};if(state.token)headers.Authorization=`Bearer ${state.token}`;const res=await fetch(path,{...options,headers});let data=null;try{data=await res.json()}catch{}if(!res.ok){if(res.status===401&&path!='/api/auth/login')logout();throw new Error(data?.detail||`请求失败 ${res.status}`)}return data}
function showModal(title,html){document.querySelector('.modal-card').classList.remove('plan-modal','task-modal','task-detail-modal','compare-modal');$('#modalTitle').textContent=title;$('#modalBody').innerHTML=html;$('#modal').classList.remove('hidden')}
function closeModal(){$('#modal').classList.add('hidden')}
document.addEventListener('click',e=>{if(e.target.closest('[data-close-modal]'))closeModal();if(!e.target.closest('#processContextMenu'))hideProcessContextMenu()});
function logout(){state.token='';state.user=null;localStorage.removeItem('aps_token');$('#appView').classList.add('hidden');$('#loginView').classList.remove('hidden')}
$('#logoutBtn').onclick=logout;
$('#loginForm').onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.target);try{const r=await api('/api/auth/login',{method:'POST',body:JSON.stringify(Object.fromEntries(fd))});state.token=r.token;state.user=r.user;localStorage.setItem('aps_token',r.token);startApp()}catch(err){$('#loginError').textContent=err.message}};
async function startApp(){try{if(!state.user)state.user=await api('/api/auth/me');$('#loginView').classList.add('hidden');$('#appView').classList.remove('hidden');$('#userBadge').innerHTML=`<strong>${esc(state.user.display_name)}</strong><small>${esc(state.user.role)}</small>`;navigate(state.page)}catch{logout()}}
function navigate(page){state.page=page;document.querySelectorAll('#nav button').forEach(b=>b.classList.toggle('active',b.dataset.page===page));$('#pageEyebrow').textContent=labels[page][0];$('#pageTitle').textContent=labels[page][1];content.innerHTML='<div class="loading">正在加载…</div>';({dashboard:renderDashboard,tasks:renderTasks,versions:renderVersions,effective:renderEffectiveSchedule,master:renderMaster,users:renderUsers,audit:renderAudit}[page])()}
document.querySelectorAll('#nav button').forEach(b=>b.onclick=()=>navigate(b.dataset.page));
const openTaskModalBase=openTaskModal;
openTaskModal=async function(){let defaults={process_count:0,population_size:56,generations:30};try{defaults=await api('/api/tasks/defaults?schedule_type=machining');state.defaultScheduleStart=defaults.schedule_start||''}catch{state.defaultScheduleStart=''}const result=openTaskModalBase();enhanceTaskModal(defaults);return result};
$('#quickTask').onclick=openTaskModal;
setInterval(()=>{$('#clock').textContent=new Date().toLocaleString('zh-CN',{hour12:false})},1000);

async function renderDashboard(){try{const d=await api('/api/dashboard');const totalTasks=Object.values(d.task_counts).reduce((a,b)=>a+b,0);const active=(d.task_counts.RUNNING||0)+(d.task_counts.QUEUED||0);const totalMaster=Object.values(d.master_counts).reduce((a,b)=>a+b,0);content.innerHTML=`
<div class="grid metric-grid"><div class="metric-card"><div class="metric-label">主数据记录</div><div class="metric-value">${totalMaster}</div><div class="metric-note">订单 ${d.master_counts.order||0} · 资源 ${(d.master_counts.machine||0)+(d.master_counts.worker||0)}</div></div><div class="metric-card"><div class="metric-label">累计排程任务</div><div class="metric-value">${totalTasks}</div><div class="metric-note">当前执行中 ${active}</div></div><div class="metric-card"><div class="metric-label">待审批版本</div><div class="metric-value">${d.version_counts.DRAFT||0}</div><div class="metric-note">已发布 ${d.version_counts.PUBLISHED||0}</div></div><div class="metric-card"><div class="metric-label">任务成功率</div><div class="metric-value">${totalTasks?Math.round((d.task_counts.SUCCEEDED||0)/totalTasks*100):0}%</div><div class="metric-note">失败 ${d.task_counts.FAILED||0} 项</div></div></div>
<div class="grid two-col"><section class="panel"><div class="panel-header"><div><h3>最近排程任务</h3><p>从任务创建到算法结果的执行状态</p></div><button class="button ghost small" onclick="navigate('tasks')">查看全部</button></div>${taskTable(d.latest_tasks)}</section><aside><div class="health-card"><div class="health-line"><strong>算法服务</strong>${badge(d.algorithm.status)}</div><p>${d.algorithm.status==='UP'?'服务连接正常，可下发排程任务':esc(d.algorithm.message||'服务不可用')}</p></div><section class="panel"><div class="panel-header"><div><h3>快捷操作</h3><p>常用生产计划入口</p></div></div><div class="quick-actions"><button class="quick-action" onclick="openTaskModal()"><span>新建智能排程</span><b>→</b></button><button class="quick-action" onclick="navigate('master')"><span>维护订单与资源</span><b>→</b></button><button class="quick-action" onclick="navigate('versions')"><span>审批计划版本</span><b>→</b></button></div></section></aside></div>`}catch(e){content.innerHTML=errorHtml(e)}}
function taskTable(rows){if(!rows?.length)return '<div class="empty">暂无排程任务</div>';return `<div class="table-wrap"><table class="data-table"><thead><tr><th>任务编号</th><th>类型 / 模式</th><th>状态</th><th>创建人</th><th>创建时间</th><th></th></tr></thead><tbody>${rows.map(r=>`<tr><td class="mono">${esc(r.task_id)}</td><td><b>${esc(r.schedule_type)}</b><div class="record-sub">${esc(r.mode)}</div></td><td>${badge(r.status)}</td><td>${esc(r.created_by)}</td><td>${esc(r.created_at)}</td><td><button class="button ghost small" onclick="showTask('${esc(r.task_id)}')">详情</button></td></tr>`).join('')}</tbody></table></div>`}

async function renderTasks(){try{const rows=await api('/api/tasks');content.innerHTML=`<section class="panel"><div class="panel-header"><div><h3>排程任务队列</h3><p>静态全量、动态滚动和局部微调统一管理</p></div><button class="button primary" onclick="openTaskModal()">＋ 新建任务</button></div>${taskTable(rows)}</section>`;if(rows.some(r=>['RUNNING','QUEUED'].includes(r.status)))setTimeout(()=>state.page==='tasks'&&renderTasks(),3000)}catch(e){content.innerHTML=errorHtml(e)}}
function nextWeekdayDayShift(){const date=new Date();date.setDate(date.getDate()+1);while(date.getDay()===0||date.getDay()===6)date.setDate(date.getDate()+1);date.setHours(8,0,0,0);return date}
function localDateTimeValue(date){if(date===undefined&&state.defaultScheduleStart)return String(state.defaultScheduleStart).slice(0,16);const value=date||nextWeekdayDayShift();return new Date(value.getTime()-value.getTimezoneOffset()*60000).toISOString().slice(0,16)}
function openTaskModal(){showModal('新建排程任务',`<form id="taskForm" class="form-grid"><label>工艺类型<select name="schedule_type"><option value="machining">机加工</option><option value="heat_treatment">热处理</option><option value="assembly">装配</option></select></label><label>排程模式<select name="mode" id="taskMode"><option value="static">静态全量排程</option><option value="dynamic">动态滚动排程</option><option value="local">局部微调</option></select></label><label>派工规则<select name="dispatching_rule"><option value="DELIVERY">交期优先(EDD)</option><option value="PRIORITY">优先级优先(PRIORITY)</option><option value="SLACK">最小松弛时间(SLACK)</option><option value="EFFICIENCY">效率优先(EFFICIENCY)</option><option value="FCFS">先到先服务(FCFS)</option></select></label><label>排程基准时间<input name="schedule_start" type="datetime-local" value="${localDateTimeValue()}" required></label><label id="protectionField" style="display:none">滚动保护窗口（小时）<input name="protection_hours" type="number" min="0" step="1" value="24"><span class="field-help">保护计划开工前后窗口内的 CONFIRMED 工序</span></label><div class="full task-config-section"><h4>TOPSIS 目标权重</h4><div class="weight-grid"><label>最大完工时间<input name="weight_makespan" type="number" min="0" step="0.01" value="0.15"></label><label>延期订单数<input name="weight_tardiness_count" type="number" min="0" step="0.01" value="0.20"></label><label>总延期<input name="weight_total_tardiness" type="number" min="0" step="0.01" value="0.15"></label><label>设备空闲率<input name="weight_machine_idle_rate" type="number" min="0" step="0.01" value="0.20"></label><label>设备负荷均衡<input name="weight_machine_balance" type="number" min="0" step="0.01" value="0.15"></label><label>工人负荷均衡<input name="weight_worker_balance" type="number" min="0" step="0.01" value="0.05"></label><label>WIP 等待<input name="weight_wip_waiting" type="number" min="0" step="0.01" value="0.10"></label></div><p id="weightTotal" class="weight-total">权重合计：1.00</p></div><label class="full">其他算法参数覆盖（JSON）<textarea name="config_overrides" rows="6">{"nsga3":{"population_size":8,"generations":3},"tabu_search":{"enabled":false}}</textarea></label><label class="full" id="localField" style="display:none">局部调整（JSON 数组）<textarea name="local_adjustments" rows="7">[]</textarea></label><div class="form-actions"><button type="button" class="button ghost" data-close-modal>取消</button><button class="button primary" type="submit">提交算法任务</button></div></form>`);document.querySelector('.modal-card').classList.add('task-modal');const syncModeFields=()=>{const mode=$('#taskMode').value;$('#localField').style.display=mode==='local'?'flex':'none';$('#protectionField').style.display=mode==='dynamic'?'flex':'none'};const weightNames=['weight_makespan','weight_tardiness_count','weight_total_tardiness','weight_machine_idle_rate','weight_machine_balance','weight_worker_balance','weight_wip_waiting'];const updateWeightTotal=()=>{const total=weightNames.reduce((sum,name)=>sum+(Number(document.querySelector(`[name="${name}"]`).value)||0),0);$('#weightTotal').textContent=`权重合计：${total.toFixed(2)}`;$('#weightTotal').classList.toggle('invalid',Math.abs(total-1)>0.0001)};$('#taskMode').onchange=syncModeFields;weightNames.forEach(name=>document.querySelector(`[name="${name}"]`).addEventListener('input',updateWeightTotal));syncModeFields();$('#taskForm').onsubmit=async e=>{e.preventDefault();const fd=Object.fromEntries(new FormData(e.target));try{const config=JSON.parse(fd.config_overrides||'{}'),weights={};for(const name of weightNames){const value=Number(fd[name]);if(!Number.isFinite(value)||value<0)throw new Error('TOPSIS 权重必须是大于等于 0 的数字');weights[name.replace('weight_','')]=value;delete fd[name]}const total=Object.values(weights).reduce((sum,value)=>sum+value,0);if(Math.abs(total-1)>0.0001)throw new Error(`TOPSIS 权重合计必须为 1，当前为 ${total.toFixed(2)}`);config.topsis={...(config.topsis||{}),weights};const protectionHours=Number(fd.protection_hours);delete fd.protection_hours;if(fd.mode==='dynamic'){if(!Number.isFinite(protectionHours)||protectionHours<0)throw new Error('滚动保护窗口必须是大于等于 0 的数字');config.rolling_window={...(config.rolling_window||{}),confirmed_protection_hours:protectionHours}}fd.config_overrides=config;fd.local_adjustments=JSON.parse(fd.local_adjustments||'[]');const r=await api('/api/tasks',{method:'POST',body:JSON.stringify(fd)});closeModal();toast(`任务 ${r.task_id} 已进入队列`);navigate('tasks')}catch(err){toast(err.message,true)}}}
async function showTask(id){try{const t=await api(`/api/tasks/${encodeURIComponent(id)}`),responseData=t.response??{status:t.status,message:['QUEUED','RUNNING'].includes(t.status)?'任务尚未完成，暂无算法结果':'暂无结果数据'};showModal(`任务详情 · ${id}`,`<div class="detail-head"><div>${badge(t.status)}<p class="muted">${esc(t.schedule_type)} / ${esc(t.mode)} / ${esc(comparisonDispatchingLabel(t.dispatching_rule))}</p></div>${t.status==='FAILED'?`<button class="button danger" onclick="retryTask('${esc(id)}')">重试任务</button>`:''}</div>${t.error_message?`<p class="form-error">${esc(t.error_message)}</p>`:''}<div class="task-data-tabs" role="tablist" aria-label="任务数据类型"><button class="task-data-tab active" type="button" role="tab" aria-selected="true" aria-controls="task-request-panel" data-task-data-tab="request" onclick="switchTaskData('request',this)">请求数据</button><button class="task-data-tab" type="button" role="tab" aria-selected="false" aria-controls="task-result-panel" data-task-data-tab="result" onclick="switchTaskData('result',this)">结果数据</button></div><section class="task-data-section task-data-panel" id="task-request-panel" role="tabpanel" data-task-data-panel="request"><div class="task-data-header"><p>业务系统固化后发送给算法服务的任务参数和数据快照</p><span class="badge">REQUEST</span></div><pre class="code-block task-code-block">${esc(JSON.stringify(t.request??{},null,2))}</pre></section><section class="task-data-section task-data-panel hidden" id="task-result-panel" role="tabpanel" data-task-data-panel="result"><div class="task-data-header"><p>算法服务返回的任务状态、排程明细、KPI 和候选方案</p>${badge(t.response?.status||t.status)}</div><pre class="code-block task-code-block">${esc(JSON.stringify(responseData,null,2))}</pre></section>`);document.querySelector('.modal-card').classList.add('task-detail-modal')}catch(e){toast(e.message,true)}}
function switchTaskData(kind,button){const modal=button.closest('.modal-card');modal.querySelectorAll('[data-task-data-tab]').forEach(tab=>{const active=tab.dataset.taskDataTab===kind;tab.classList.toggle('active',active);tab.setAttribute('aria-selected',String(active))});modal.querySelectorAll('[data-task-data-panel]').forEach(panel=>panel.classList.toggle('hidden',panel.dataset.taskDataPanel!==kind))}
async function retryTask(id){try{await api(`/api/tasks/${encodeURIComponent(id)}/retry`,{method:'POST'});closeModal();toast('任务已重新进入队列');renderTasks()}catch(e){toast(e.message,true)}}

async function renderVersions(){try{state.versions=await api('/api/versions');content.innerHTML=`<section class="panel"><div class="panel-header"><div><h3>排程计划版本</h3><p>算法结果需完成业务审批后才能发布并回写工序</p></div><button class="button ghost" onclick="openCompare()">版本对比</button></div>${versionTable(state.versions)}</section>`}catch(e){content.innerHTML=errorHtml(e)}}
function scheduleTypeLabel(value){return({machining:'机加工',heat_treatment:'热处理',assembly:'装配'}[value]||value||'-')}
function scheduleTypeBadge(value){const type=String(value||'').toLowerCase();return `<span class="schedule-type-badge ${esc(type)}">${esc(scheduleTypeLabel(type))}</span>`}
function scheduleModeBadge(value){const mode=String(value||'').toLowerCase();return `<span class="schedule-mode-badge ${esc(mode)}">${esc(comparisonModeLabel(mode))}</span>`}
function selectedOption(value,current,label=null){return `<option value="${esc(value)}" ${String(value)===String(current||'')?'selected':''}>${esc(label??value)}</option>`}
function effectiveViewClass(kind){return state.effectiveView===kind?'':'hidden'}
async function renderEffectiveSchedule(){try{const filters=state.effectiveFilters||{},params=new URLSearchParams();Object.entries(filters).forEach(([key,value])=>{if(value!==''&&value!==null&&value!==undefined)params.set(key,value)});const data=await api(`/api/effective-schedule${params.size?`?${params}`:''}`);state.effectiveData=data;const summary=data.summary||{},options=data.filter_options||{},versions=data.published_versions||[],schedule=data.schedule||[],processes=data.processes||[],unscheduled=data.unscheduled_processes||[],conflicts=data.conflicts||[];const versionHtml=versions.length?versions.map(v=>`<button class="effective-version" onclick="showVersion('${esc(v.version_id)}')"><span>${esc(scheduleTypeLabel(v.schedule_type))}</span><strong>${esc(v.version_id)}</strong><small>${esc(v.published_by||'-')} · ${esc(v.published_at||'-')}</small></button>`).join(''):'<div class="effective-no-version">当前没有已发布的排程版本，请先完成计划审批和发布。</div>';const warnings=[];if(unscheduled.length)warnings.push(`${unscheduled.length} 道工序未进入当前生效版本`);if(conflicts.length)warnings.push(`${conflicts.length} 项设备或人员时间冲突`);content.innerHTML=`<section class="panel effective-header"><div class="panel-header"><div><h3>当前生效排程</h3><p>以发布后回写的订单工序主数据为准；算法候选方案和历史版本不在此处作为执行依据</p></div><div class="toolbar-group"><button class="button ghost" onclick="exportEffectiveSchedule()">导出生效排程</button><button class="button primary" onclick="renderEffectiveSchedule()">刷新</button></div></div><div class="effective-version-strip">${versionHtml}</div><div class="effective-refresh-time">数据生成时间：${esc(data.generated_at||'-')}</div></section><div class="effective-metrics"><div class="effective-metric"><span>生效工序</span><strong>${summary.effective_processes||0}</strong><small>当前发布版本</small></div><div class="effective-metric"><span>进行中</span><strong>${summary.status_counts?.IN_PROGRESS||0}</strong><small>现场执行状态</small></div><div class="effective-metric"><span>已完成</span><strong>${summary.status_counts?.COMPLETED||0}</strong><small>已完工工序</small></div><div class="effective-metric ${unscheduled.length?'warning':''}"><span>未进入生效</span><strong>${unscheduled.length}</strong><small>未排程或历史版本</small></div><div class="effective-metric"><span>人工锁定</span><strong>${summary.locked_processes||0}</strong><small>仅统计人工主动锁定</small></div><div class="effective-metric ${conflicts.length?'danger':''}"><span>资源冲突</span><strong>${conflicts.length}</strong><small>设备及人员重叠</small></div></div><section class="panel effective-filter-panel"><form id="effectiveFilterForm" class="effective-filter-form"><label>工艺类型<select name="schedule_type"><option value="">全部类型</option>${(options.schedule_types||[]).map(value=>selectedOption(value,filters.schedule_type,scheduleTypeLabel(value))).join('')}</select></label><label>工序状态<select name="process_status"><option value="">全部状态</option>${(options.statuses||[]).map(value=>selectedOption(value,filters.process_status)).join('')}</select></label><label>订单号<input name="order_id" list="effectiveOrders" value="${esc(filters.order_id||'')}" placeholder="全部订单"></label><label>设备<input name="machine_id" list="effectiveMachines" value="${esc(filters.machine_id||'')}" placeholder="全部设备"></label><label>工人<input name="worker_id" list="effectiveWorkers" value="${esc(filters.worker_id||'')}" placeholder="全部人员"></label><label>开始时间<input name="start_time" type="datetime-local" value="${esc(filters.start_time||'')}"></label><label>结束时间<input name="end_time" type="datetime-local" value="${esc(filters.end_time||'')}"></label><div class="effective-filter-actions"><button type="button" class="button ghost" onclick="resetEffectiveFilters()">重置</button><button class="button primary">查询</button></div></form><datalist id="effectiveOrders">${(options.orders||[]).map(value=>`<option value="${esc(value)}">`).join('')}</datalist><datalist id="effectiveMachines">${(options.machines||[]).map(value=>`<option value="${esc(value)}">`).join('')}</datalist><datalist id="effectiveWorkers">${(options.workers||[]).map(value=>`<option value="${esc(value)}">`).join('')}</datalist></section>${warnings.length?`<section class="effective-alert"><div><strong>排程需要关注</strong><span>${esc(warnings.join('；'))}</span></div><button class="button ghost small" onclick="switchEffectiveView('detail',document.querySelector('[data-effective-tab=detail]'))">查看明细</button></section>`:''}<section class="panel effective-workspace"><div class="gantt-switcher effective-switcher" role="tablist"><button data-effective-tab="order" class="gantt-switch ${state.effectiveView==='order'?'active':''}" onclick="switchEffectiveView('order',this)">订单工序甘特图</button><button data-effective-tab="machine" class="gantt-switch ${state.effectiveView==='machine'?'active':''}" onclick="switchEffectiveView('machine',this)">设备甘特图</button><button data-effective-tab="worker" class="gantt-switch ${state.effectiveView==='worker'?'active':''}" onclick="switchEffectiveView('worker',this)">工人甘特图</button><button data-effective-tab="detail" class="gantt-switch ${state.effectiveView==='detail'?'active':''}" onclick="switchEffectiveView('detail',this)">全部工序明细</button></div><div class="gantt-view ${effectiveViewClass('order')}" data-effective-view="order"><div class="gantt-view-note"><span>按订单展开查看当前生效工序，蓝色汇总条代表订单计划跨度</span><span class="badge EFFECTIVE">${summary.order_count||0} 个订单</span></div>${renderOrderGantt(schedule)}</div><div class="gantt-view ${effectiveViewClass('machine')}" data-effective-view="machine"><div class="gantt-view-note"><span>按设备查看当前生效占用，红色描边表示检测到时间冲突</span><span class="badge EFFECTIVE">${summary.machine_count||0} 台设备</span></div>${renderResourceGantt(schedule,'machine_id','未分配设备','machine')}</div><div class="gantt-view ${effectiveViewClass('worker')}" data-effective-view="worker"><div class="gantt-view-note"><span>按工人查看当前生效任务，支持识别人员时间重叠</span><span class="badge EFFECTIVE">${summary.worker_count||0} 名工人</span></div>${renderResourceGantt(schedule,'worker_id','未分配人员','worker')}</div><div class="gantt-view ${effectiveViewClass('detail')}" data-effective-view="detail"><div class="gantt-view-note"><span>包含所有订单工序，并区分当前生效、未排程和历史版本</span><span class="badge">${processes.length} 道工序</span></div>${effectiveScheduleTable(processes)}</div></section>${conflicts.length?effectiveConflictPanel(conflicts):''}`;$('#effectiveFilterForm').onsubmit=e=>{e.preventDefault();state.effectiveFilters=Object.fromEntries([...new FormData(e.target)].filter(([,value])=>value!==''));renderEffectiveSchedule()}}catch(e){content.innerHTML=errorHtml(e)}}
function switchEffectiveView(kind,button){state.effectiveView=kind;document.querySelectorAll('[data-effective-tab]').forEach(item=>item.classList.toggle('active',item===button));document.querySelectorAll('[data-effective-view]').forEach(view=>view.classList.toggle('hidden',view.dataset.effectiveView!==kind))}
function resetEffectiveFilters(){state.effectiveFilters={};renderEffectiveSchedule()}
function exportEffectiveSchedule(){const data=state.effectiveData;if(!data)return;const rows=data.processes||[],blob=new Blob([JSON.stringify(rows,null,2)],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`aps_effective_schedule_${new Date().toISOString().slice(0,10)}.json`;a.click();URL.revokeObjectURL(a.href);toast(`已导出 ${rows.length} 道工序`)}
function effectiveScheduleTable(rows){if(!rows.length)return '<div class="empty">当前筛选条件下没有工序</div>';return `<div class="table-wrap effective-table-wrap"><table class="data-table effective-table"><thead><tr><th>工序</th><th>生效状态</th><th>工序状态</th><th>订单</th><th>设备</th><th>人员</th><th>计划开始</th><th>计划完工</th><th>合批号</th><th>来源版本</th><th>异常</th></tr></thead><tbody>${rows.map(row=>`<tr class="${row.has_conflict?'conflict-row':''}"><td><b>${esc(row.process_id)}</b><div class="record-sub">${esc(row.process_name||'')}</div></td><td>${badge(row.schedule_state)}</td><td><div class="status-lock-cell">${processStatusBadge(row.status||'-')}${lockMarker(row,true)}</div></td><td><b>${esc(row.order_id||'-')}</b><div class="record-sub">${esc(row.product_name||'')}</div></td><td>${esc(row.machine_id||'-')}<div class="record-sub">${esc(row.machine_name||'')}</div></td><td>${esc(row.worker_id||'-')}<div class="record-sub">${esc(row.worker_name||'')}</div></td><td>${esc(row.plan_start_time||'-')}</td><td>${esc(row.plan_end_time||'-')}</td><td class="mono">${esc(row.batch_id||'-')}</td><td>${row.schedule_version_id?`<button class="button ghost small mono" onclick="showVersion('${esc(row.schedule_version_id)}')">${esc(row.schedule_version_id)}</button>`:'-'}</td><td>${row.has_conflict?`<span class="effective-conflict-tag">${esc(row.conflict_types.join(' / '))} 冲突</span>`:'-'}</td></tr>`).join('')}</tbody></table></div>`}
function effectiveConflictPanel(conflicts){return `<section class="panel effective-conflicts"><div class="panel-header"><div><h3>资源冲突明细</h3><p>合批号相同的并行工序不会被判定为冲突</p></div>${badge(`${conflicts.length} CONFLICTS`)}</div><div class="effective-conflict-list">${conflicts.map(item=>`<div><span>${item.resource_type==='machine'?'设备':'人员'} · ${esc(item.resource_id)}</span><strong>${esc(item.process_ids.join(' ↔ '))}</strong><small>${esc(item.start_time)} — ${esc(item.end_time)}</small></div>`).join('')}</div></section>`}
function versionTable(rows){if(!rows.length)return '<div class="empty">暂无计划版本，请先执行排程任务</div>';return `<div class="table-wrap"><table class="data-table"><thead><tr><th>版本</th><th>工艺类型</th><th>来源任务</th><th>状态</th><th>创建时间</th><th>审批 / 发布</th><th></th></tr></thead><tbody>${rows.map(v=>`<tr><td><b>${esc(v.version_id)}</b><div class="record-sub">#${v.version_no}</div></td><td>${esc(v.schedule_type)}</td><td class="mono">${esc(v.task_id)}</td><td>${badge(v.status)}</td><td>${esc(v.created_at)}</td><td><div class="record-sub">${esc(v.reviewed_by||'-')} / ${esc(v.published_by||'-')}</div></td><td><button class="button ghost small" onclick="showVersion('${esc(v.version_id)}')">打开计划</button></td></tr>`).join('')}</tbody></table></div>`}
async function showVersion(id){
    try{
        const v=await api(`/api/versions/${encodeURIComponent(id)}`),result=v.result||{},rows=result.schedule||[];
        const orderCount=new Set(rows.map(x=>x.order_id).filter(Boolean)).size;
        const machineCount=new Set(rows.map(x=>x.machine_id).filter(Boolean)).size;
        const workerCount=new Set(rows.map(x=>x.worker_id).filter(Boolean)).size;
        const actions=[];
        if(v.status==='DRAFT')actions.push(`<button class="button secondary" onclick="reviewVersion('${esc(id)}','REJECTED')">驳回</button><button class="button primary" onclick="reviewVersion('${esc(id)}','APPROVED')">审批通过</button>`);
        if(v.status==='APPROVED')actions.push(`<button class="button primary" onclick="publishPlan('${esc(id)}')">发布计划</button>`);
        showModal(`计划版本 · ${id}`,`<div class="detail-head"><div>${badge(v.status)}<div class="version-title-line"><h3>${esc(id)}</h3>${versionScoreBadge(result)}</div><p class="muted">${esc(v.schedule_type)} · 来源 ${esc(v.task_id)}</p></div><div class="actions">${actions.join('')}</div></div>${versionParameterPanel(v)}<div class="kpi-grid">${kpiCards(result.kpis||{},result.best_objectives||{},result.metadata||{})}</div><div class="gantt-switcher" role="tablist"><button class="gantt-switch active" role="tab" onclick="switchPlanGantt('order',this)">订单工序甘特图</button><button class="gantt-switch" role="tab" onclick="switchPlanGantt('machine',this)">设备甘特图</button><button class="gantt-switch" role="tab" onclick="switchPlanGantt('worker',this)">工人甘特图</button></div><div class="gantt-view" data-gantt-view="order"><div class="gantt-view-note"><span>点击订单行可展开或收起内部工序；工序柱悬浮可查看完整分配信息</span><span class="badge">${orderCount} 个订单</span></div>${renderOrderGantt(rows)}</div><div class="gantt-view hidden" data-gantt-view="machine"><div class="gantt-view-note"><span>按设备展示已分配工序</span><span class="badge">${machineCount} 台设备</span></div>${renderResourceGantt(rows,'machine_id','未分配设备','machine')}</div><div class="gantt-view hidden" data-gantt-view="worker"><div class="gantt-view-note"><span>按工人展示工作安排和占用时间</span><span class="badge">${workerCount} 名工人</span></div>${renderResourceGantt(rows,'worker_id','未分配人员','worker')}</div><div style="margin-top:26px"><h4>排程明细</h4>${scheduleTable(rows)}</div>`);
        document.querySelector('.modal-card').classList.add('plan-modal');
    }catch(e){toast(e.message,true)}
}
function switchPlanGantt(kind,button){const modal=button.closest('.modal-card');modal.querySelectorAll('.gantt-switch').forEach(item=>item.classList.toggle('active',item===button));modal.querySelectorAll('.gantt-view').forEach(view=>view.classList.toggle('hidden',view.dataset.ganttView!==kind))}
function metricNumber(...values){return values.find(value=>typeof value==='number'&&Number.isFinite(value))}
function roundMetric(value,digits=2){const factor=10**digits;return Math.round(value*factor)/factor}
function durationMetric(k,o,minuteKey,hourKey){const hours=metricNumber(k[hourKey],o[hourKey]);if(hours!==undefined)return `${roundMetric(hours)} h`;const minutes=metricNumber(k[minuteKey],o[minuteKey]);return minutes===undefined?'-':`${roundMetric(minutes/60)} h`}
function percentMetric(value){if(value===undefined)return '-';const percent=Math.abs(value)<=1?value*100:value;return `${roundMetric(percent)}%`}
function kpiCards(k,o,metadata){const idleRate=metricNumber(k.machine_idle_rate,o.machine_idle_rate);const utilization=metricNumber(k.machine_utilization,k.average_machine_utilization,o.machine_utilization);const utilizationValue=utilization!==undefined?utilization:(idleRate!==undefined?1-idleRate:undefined);const onTimeDirect=metricNumber(k.on_time_delivery_rate,o.on_time_delivery_rate);const tardyCount=metricNumber(k.tardiness_count,o.tardiness_count);const orderCount=metricNumber(metadata.order_count);const onTimeValue=onTimeDirect!==undefined?onTimeDirect:(tardyCount!==undefined&&orderCount?Math.max(orderCount-tardyCount,0)/orderCount:undefined);const data=[['最大完工时间',durationMetric(k,o,'makespan','makespan_hours')],['总延期',durationMetric(k,o,'total_tardiness','total_tardiness_hours')],['设备利用率',percentMetric(utilizationValue)],['按期交付率',percentMetric(onTimeValue)],['等待时间',durationMetric(k,o,'wip_waiting','total_waiting_hours')]];return data.map(x=>`<div class="kpi"><small>${x[0]}</small><strong>${esc(x[1])}</strong></div>`).join('')}
function lockInfoPayload(item){return encodeURIComponent(JSON.stringify({process_id:item.process_id||'',source_status:item.source_process_status||item.source_status||'',assigned_machine_id:item.machine_id||'',assigned_worker_id:item.worker_id||'',preservation_reason:item.preservation_reason||'',locks:item.lock_details||{}}))}
function lockMarker(item,showText=false){if(!item.manually_locked)return '';return `<button type="button" class="lock-marker ${showText?'with-text':''}" onclick="event.stopPropagation();hideGanttTooltip();showLockInfo('${lockInfoPayload(item)}')" title="点击查看人工锁定信息">🔒${showText?'<span>人工锁定</span>':''}</button>`}
function showLockInfo(encoded){let data={};try{data=JSON.parse(decodeURIComponent(encoded))}catch{}const locks=data.locks||{},known=[['工序号',data.process_id||'-'],['原始工序状态',data.source_status||'-'],['锁定设备',locks.machine_id||'-'],['锁定工人',locks.worker_id||'-'],['锁定开始时间',locks.start_time||'-'],['锁定结束时间',locks.end_time||'-'],['锁定时间',locks.lock_time||'-'],['操作人',locks.operator||'-'],['锁定原因',locks.lock_reason||'-'],['当前分配设备',data.assigned_machine_id||'-'],['当前分配工人',data.assigned_worker_id||'-']];if(data.preservation_reason)known.push(['本次排程保留原因',data.preservation_reason]);$('#lockInfoBody').innerHTML=`<div class="lock-detail-grid">${known.map(([label,value])=>`<span>${esc(label)}</span><strong>${esc(value)}</strong>`).join('')}</div><h4>人工 locks 数据</h4><pre class="lock-json">${esc(JSON.stringify(locks,null,2))}</pre>`;$('#lockInfoOverlay').classList.remove('hidden')}
function closeLockInfo(){$('#lockInfoOverlay').classList.add('hidden')}
function processLockPayload(item){return encodeURIComponent(JSON.stringify({process_id:item.process_id||'',process_name:item.process_name||'',order_id:item.order_id||'',status:item.status||'',resource_group_id:item.resource_group_id||'',machine_id:item.machine_id||'',machine_name:item.machine_name||'',worker_id:item.worker_id||'',worker_name:item.worker_name||'',plan_start_time:item.plan_start_time||'',plan_end_time:item.plan_end_time||'',schedule_version_id:item.schedule_version_id||'',manually_locked:!!item.manually_locked,allow_manual_lock:!!item.allow_manual_lock,allow_manual_adjustment:!!item.allow_manual_adjustment,locks:item.lock_details||{},lock_options:item.lock_options||{machines:[],workers:[]}})).replace(/'/g,'%27')}
function hideProcessContextMenu(){const menu=$('#processContextMenu');if(menu)menu.classList.add('hidden')}
function openProcessContextMenu(event,encoded){event.preventDefault();event.stopPropagation();if(!['admin','planner'].includes(state.user?.role)){toast('当前账号没有工序调整权限',true);return}let item={};try{item=JSON.parse(decodeURIComponent(encoded))}catch{return}state.lockTarget=item;const menu=$('#processContextMenu'),actions=[];if(item.allow_manual_adjustment)actions.push('<button type="button" onclick="openProcessAdjustmentModal()">调整资源和时间</button>');if(item.allow_manual_lock)actions.push(`<button type="button" onclick="openManualLockModal()">${item.manually_locked?'修改人工锁':'人工锁定'}</button>`);if(item.manually_locked)actions.push('<button type="button" class="danger" onclick="openManualUnlockModal()">解除人工锁</button>');menu.innerHTML=actions.join('')||'<button type="button" disabled>当前工序不可调整</button>';menu.classList.remove('hidden');const width=210,height=Math.max(actions.length,1)*42+12;menu.style.left=`${Math.max(8,Math.min(event.clientX,window.innerWidth-width-8))}px`;menu.style.top=`${Math.max(8,Math.min(event.clientY,window.innerHeight-height-8))}px`}
function lockDateTimeValue(value){return value?String(value).slice(0,16):''}
function openManualLockModal(){hideProcessContextMenu();const item=state.lockTarget;if(!item)return;const locks=item.locks||{},options=item.lock_options||{},machineValue=locks.machine_id||item.machine_id||'',workerValue=locks.worker_id||item.worker_id||'',startValue=locks.start_time||item.plan_start_time||'',endValue=locks.end_time||item.plan_end_time||'';showModal(`${item.manually_locked?'修改':'新增'}人工锁 · ${item.process_id}`,`<form id="manualLockForm" class="form-grid"><label>锁定设备<select name="machine_id"><option value="">不锁定设备</option>${(options.machines||[]).map(x=>selectedOption(x.machine_id,machineValue,`${x.machine_id} · ${x.machine_name||''}`)).join('')}</select></label><label>锁定人员<select name="worker_id"><option value="">不锁定人员</option>${(options.workers||[]).map(x=>selectedOption(x.worker_id,workerValue,`${x.worker_id} · ${x.worker_name||''}`)).join('')}</select></label><label>锁定开始时间<input name="start_time" type="datetime-local" value="${esc(lockDateTimeValue(startValue))}"></label><label>锁定结束时间<input name="end_time" type="datetime-local" value="${esc(lockDateTimeValue(endValue))}"></label><label class="full">锁定原因<textarea name="lock_reason" rows="4" maxlength="500" required placeholder="请填写人工锁定原因">${esc(locks.lock_reason||'')}</textarea></label><div class="full field-help">设备、人员和时间可以按需锁定；开始与结束时间必须同时填写。当前有排程时已自动带入现有分配。</div><div class="form-actions"><button type="button" class="button ghost" data-close-modal>取消</button><button class="button primary" type="submit">保存人工锁</button></div></form>`);document.querySelector('.modal-card').classList.add('task-modal');$('#manualLockForm').onsubmit=async e=>{e.preventDefault();const fd=Object.fromEntries(new FormData(e.target));try{await api(`/api/processes/${encodeURIComponent(item.process_id)}/lock`,{method:'POST',body:JSON.stringify({...fd,schedule_version_id:item.schedule_version_id,expected_lock_time:locks.lock_time||''})});closeModal();toast(`工序 ${item.process_id} 已人工锁定`);renderEffectiveSchedule()}catch(err){toast(err.message,true)}}}
function openManualUnlockModal(){hideProcessContextMenu();const item=state.lockTarget;if(!item)return;showModal(`解除人工锁 · ${item.process_id}`,`<form id="manualUnlockForm" class="form-grid"><div class="full task-config-section"><h4>当前人工锁</h4><pre class="lock-json">${esc(JSON.stringify(item.locks||{},null,2))}</pre></div><label class="full">解锁原因<textarea name="unlock_reason" rows="4" maxlength="500" required placeholder="请填写解除人工锁的原因"></textarea></label><div class="form-actions"><button type="button" class="button ghost" data-close-modal>取消</button><button class="button danger" type="submit">确认解锁</button></div></form>`);$('#manualUnlockForm').onsubmit=async e=>{e.preventDefault();const fd=Object.fromEntries(new FormData(e.target));try{await api(`/api/processes/${encodeURIComponent(item.process_id)}/lock`,{method:'DELETE',body:JSON.stringify({...fd,schedule_version_id:item.schedule_version_id,expected_lock_time:(item.locks||{}).lock_time||''})});closeModal();toast(`工序 ${item.process_id} 已解除人工锁`);renderEffectiveSchedule()}catch(err){toast(err.message,true)}}}
function adjustmentDateTimeValue(value){return value?String(value).slice(0,16):''}
function openProcessAdjustmentModal(){hideProcessContextMenu();const item=state.lockTarget;if(!item||!item.allow_manual_adjustment)return;const options=item.lock_options||{machines:[],workers:[]};showModal(`调整工序 · ${item.process_id}`,`<form id="processAdjustmentForm" class="form-grid"><div class="full adjustment-summary"><strong>${esc(item.process_id)} · ${esc(item.process_name||'')}</strong><span>订单 ${esc(item.order_id||'-')} · 当前设备 ${esc(item.machine_id||'-')} · 当前人员 ${esc(item.worker_id||'-')}</span></div><label>目标设备<select name="assigned_machine_id" id="adjustmentMachine">${(options.machines||[]).map(x=>selectedOption(x.machine_id,item.machine_id,`${x.machine_id} · ${x.machine_name||''}`)).join('')}</select></label><label>目标人员<select name="assigned_worker_id" id="adjustmentWorker">${(options.workers||[]).map(x=>selectedOption(x.worker_id,item.worker_id,`${x.worker_id} · ${x.worker_name||''}`)).join('')}</select></label><label>新开工时间<input name="plan_start_time" type="datetime-local" value="${esc(adjustmentDateTimeValue(item.plan_start_time))}" required></label><label>新完工时间<input name="plan_end_time" type="datetime-local" value="${esc(adjustmentDateTimeValue(item.plan_end_time))}" required></label><div class="full field-help">同设备调整只改变时间；更换设备仅允许选择当前资源组内设备，系统会继续校验人员授权、日历、前后序和资源占用。</div><div class="form-actions"><button type="button" class="button ghost" data-close-modal>取消</button><button class="button primary" type="submit">校验影响</button></div></form>`);document.querySelector('.modal-card').classList.add('task-modal');const machine=$('#adjustmentMachine'),worker=$('#adjustmentWorker');machine.onchange=()=>{const selected=(options.machines||[]).find(x=>x.machine_id===machine.value),allowed=new Set(selected?.allowed_worker_ids||[]);[...worker.options].forEach(option=>option.disabled=allowed.size&&!allowed.has(option.value));if(worker.selectedOptions[0]?.disabled){const first=[...worker.options].find(option=>!option.disabled);if(first)worker.value=first.value}};machine.onchange();$('#processAdjustmentForm').onsubmit=e=>{e.preventDefault();previewProcessAdjustment(item,Object.fromEntries(new FormData(e.target)))}}
function adjustmentIssueList(items,kind){if(!items?.length)return '';return `<div class="adjustment-issues ${kind}">${items.map(item=>`<div><span>${kind==='hard'?'✕':kind==='warning'?'!':'✓'}</span><p><strong>${esc(item.code)} · ${esc(item.title)}</strong><small>${esc(item.message)}</small></p></div>`).join('')}</div>`}
async function previewProcessAdjustment(item,payload){try{const request={...payload,schedule_version_id:item.schedule_version_id},preview=await api(`/api/processes/${encodeURIComponent(item.process_id)}/adjustments/preview`,{method:'POST',body:JSON.stringify(request)});state.pendingAdjustment={item,payload:request,preview};openAdjustmentConfirmation()}catch(err){toast(err.message,true)}}
function adjustmentImpactList(title,items){if(!items?.length)return '';return `<div class="adjustment-impact"><h4>${esc(title)}</h4>${items.map(item=>`<div><strong>${esc(item.process_id)}</strong><span>${esc(item.order_id||item.process_name||'')}</span><small>${esc(item.plan_start_time||'')} — ${esc(item.plan_end_time||'')}${item.locked?' · 已锁定':''}</small></div>`).join('')}</div>`}
function openAdjustmentConfirmation(){const pending=state.pendingAdjustment;if(!pending)return;const {preview}=pending,target=preview.target,current=preview.current,hard=preview.hard_errors||[];if(hard.length){showModal('无法放置',`<div class="adjustment-blocked"><div class="adjustment-blocked-title">⛔ 工序 ${esc(preview.process_id)} 无法放置到目标位置</div>${adjustmentIssueList(hard,'hard')}${adjustmentIssueList(preview.warnings||[],'warning')}<div class="adjustment-suggestion"><strong>建议操作</strong><span>吸附到当前校验得到的最早可用边界，或返回甘特图调整前序/锁定任务。</span></div><div class="form-actions">${preview.snap_suggestion?'<button type="button" class="button secondary" onclick="applyAdjustmentSnap()">吸附到最早时间</button>':''}<button type="button" class="button ghost" data-close-modal>放弃</button></div></div>`);return}const direction=preview.operation==='move_forward'?'向前移动':preview.operation==='move_backward'?'向后移动':preview.operation==='machine_change'?'更换设备':preview.operation==='worker_change'?'更换人员':'调整资源和时间';const changeover=preview.changeover||{};showModal(`拖拽确认：${direction}`,`<form id="adjustmentConfirmForm"><div class="adjustment-confirm-head"><div><strong>${esc(preview.process_id)} · ${esc(preview.process_name||'')}</strong><span>订单 ${esc(preview.order_id||'-')}</span></div><span class="badge EFFECTIVE">${esc(direction)}</span></div><div class="adjustment-change-grid"><div><small>原时间</small><strong>${esc(current.plan_start_time)} — ${esc(current.plan_end_time)}</strong><span>${esc(current.machine_id)} / ${esc(current.worker_id)}</span></div><div><small>新时间</small><strong>${esc(target.plan_start_time)} — ${esc(target.plan_end_time)}</strong><span>${esc(target.machine_id)} / ${esc(target.worker_id)}</span></div></div>${adjustmentIssueList(preview.checks||[],'pass')}${adjustmentIssueList(preview.warnings||[],'warning')}${changeover.total_minutes?`<div class="adjustment-changeover"><strong>换产成本合计约 ${esc(changeover.total_minutes)} 分钟</strong><span>${esc((changeover.details||[]).join('；'))}</span></div>`:''}<div class="adjustment-impact-grid">${adjustmentImpactList('被挤任务',preview.displaced_tasks)}${adjustmentImpactList('后续工序',preview.downstream)}</div><div class="adjustment-options"><h4>调整方案</h4>${(preview.options||[]).map(option=>`<label><input type="radio" name="strategy" value="${esc(option.value)}" ${option.recommended?'checked':''}><span><strong>${esc(option.label)}${option.recommended?'（推荐）':''}</strong><small>${esc(option.description)}</small></span></label>`).join('')}</div><label class="adjustment-lock-after"><input type="checkbox" name="lock_after_adjustment"> 移动后锁定此任务</label><label class="adjustment-lock-reason hidden" id="adjustmentLockReason">锁定原因<input name="lock_reason" value="单工序调整确认后锁定" maxlength="500"></label><div class="form-actions"><button type="button" class="button ghost" data-close-modal>放弃</button><button class="button primary" type="submit">确认执行</button></div></form>`);document.querySelector('.modal-card').classList.add('task-modal');const form=$('#adjustmentConfirmForm'),lockBox=form.lock_after_adjustment;lockBox.onchange=()=>$('#adjustmentLockReason').classList.toggle('hidden',!lockBox.checked);form.onsubmit=executeConfirmedAdjustment}
function applyAdjustmentSnap(){const pending=state.pendingAdjustment;if(!pending?.preview?.snap_suggestion)return;pending.payload.plan_start_time=pending.preview.snap_suggestion.plan_start_time;pending.payload.plan_end_time=pending.preview.snap_suggestion.plan_end_time;previewProcessAdjustment(pending.item,pending.payload)}
async function executeConfirmedAdjustment(event){event.preventDefault();const pending=state.pendingAdjustment;if(!pending)return;const form=event.target,button=form.querySelector('[type=submit]'),fd=Object.fromEntries(new FormData(form));button.disabled=true;button.textContent='正在局部排程…';try{const result=await api(`/api/processes/${encodeURIComponent(pending.item.process_id)}/adjustments/execute`,{method:'POST',body:JSON.stringify({...pending.payload,...fd,confirm_warnings:true,lock_after_adjustment:form.lock_after_adjustment.checked})});state.pendingAdjustment=null;closeModal();toast(result.lock_warning?`调整已生效，但锁定失败：${result.lock_warning}`:`工序 ${result.process_id} 已调整并生效`);renderEffectiveSchedule()}catch(err){button.disabled=false;button.textContent='确认执行';toast(err.message,true)}}
function scheduleTable(rows){return `<div class="table-wrap"><table class="data-table"><thead><tr><th>工序</th><th>状态</th><th>订单</th><th>设备</th><th>人员</th><th>开始</th><th>结束</th></tr></thead><tbody>${rows.map(x=>{const displayStatus=x.effective_status||x.status||x.source_status||'-';return `<tr><td><b>${esc(x.process_id)}</b><div class="record-sub">${esc(x.process_name||'')}</div></td><td><div class="status-lock-cell">${processStatusBadge(displayStatus)}${lockMarker(x,true)}</div>${x.effective_status&&x.status&&x.effective_status!==x.status?`<div class="record-sub">算法状态 ${esc(processStatusLabel(x.status))}</div>`:''}${x.source_status&&x.source_status!==displayStatus?`<div class="record-sub">排程前状态 ${esc(processStatusLabel(x.source_status))}</div>`:''}</td><td>${esc(x.order_id||'')}</td><td>${esc(x.machine_id||'-')}</td><td>${esc(x.worker_id||'-')}</td><td>${esc(x.plan_start_time)}</td><td>${esc(x.plan_end_time)}</td></tr>`}).join('')}</tbody></table></div>`}
function ganttScale(rows){const valid=rows.filter(x=>x.plan_start_time&&x.plan_end_time&&Number.isFinite(new Date(x.plan_start_time).getTime())&&Number.isFinite(new Date(x.plan_end_time).getTime()));if(!valid.length)return null;const min=Math.min(...valid.map(x=>new Date(x.plan_start_time).getTime())),max=Math.max(...valid.map(x=>new Date(x.plan_end_time).getTime())),span=Math.max(max-min,3600000),now=Date.now();const axis=Array.from({length:7},(_,i)=>{const t=min+span*i/6;return `<span style="left:${i/6*98}%">${new Date(t).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})}</span>`}).join(''),nowLine=now>=min&&now<=max?`<i class="gantt-now-line" style="left:${(now-min)/span*100}%" title="当前时间"></i>`:'';return{valid,min,max,span,axis,nowLine}}
function ganttTooltipPayload(item){const materialTime=item.material_ready_time||((item.material_ready===true)?'已齐套（未提供具体时间）':(item.material_ready===false?'未齐套/未提供时间':'-'));const fields=[['订单号',item.order_id||'-'],['工序号',item.process_id||'-'],['工序状态',processStatusLabel(item.status||item.effective_status||'-')],['分配设备',item.machine_id||'-'],['分配工人',item.worker_id||'-'],['物料齐套时间',materialTime],['计划开始时间',item.plan_start_time||'-'],['计划完工时间',item.plan_end_time||'-']];if(item.batch_id)fields.push(['合批号',item.batch_id]);if(item.schedule_version_id||item.effective_schedule_version_id)fields.push(['生效版本',item.schedule_version_id||item.effective_schedule_version_id]);if(item.published_at)fields.push(['发布时间',item.published_at]);if(item.has_conflict)fields.push(['资源冲突',(item.conflict_types||[]).join(' / ')]);return encodeURIComponent(JSON.stringify(fields))}
function safeClassToken(value){return String(value||'').replace(/[^a-zA-Z0-9_-]/g,'-')}
function ganttBar(item,scale,extraClass='',showTooltip=true,manualLockMenu=false){const start=new Date(item.plan_start_time).getTime(),end=new Date(item.plan_end_time).getTime(),left=(start-scale.min)/scale.span*100,width=Math.max((end-start)/scale.span*100,.7),tooltip=showTooltip&&item.process_id?` data-tooltip="${ganttTooltipPayload(item)}" onmouseenter="showGanttTooltip(event,this)" onmousemove="moveGanttTooltip(event)" onmouseleave="hideGanttTooltip()"`:'',menuEnabled=manualLockMenu&&(item.allow_manual_lock||item.allow_manual_adjustment),context=menuEnabled?` oncontextmenu="openProcessContextMenu(event,'${processLockPayload(item)}')"`:'',adjustable=manualLockMenu&&item.allow_manual_adjustment&&extraClass!=='gantt-process-bar',drag=adjustable?` draggable="true" ondragstart="startGanttAdjustmentDrag(event,'${processLockPayload(item)}')" ondragend="endGanttAdjustmentDrag()"`:'';return `<div class="gantt-bar ${extraClass} ${adjustable?'gantt-bar-adjustable':''} gantt-status-${safeClassToken(item.status)} ${item.manually_locked?'gantt-bar-locked':''} ${item.has_conflict?'gantt-bar-conflict':''}" style="left:${left}%;width:${width}%"${tooltip}${context}${drag}><span>${esc(item.process_id||item.order_id)}</span>${lockMarker(item)}</div>`}
function showGanttTooltip(event,element){const tooltip=$('#ganttTooltip');let fields=[];try{fields=JSON.parse(decodeURIComponent(element.dataset.tooltip||''))}catch{}tooltip.innerHTML=fields.map(([label,value])=>`<div class="gantt-tooltip-row"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join('');tooltip.classList.remove('hidden');moveGanttTooltip(event)}
function moveGanttTooltip(event){const tooltip=$('#ganttTooltip');if(tooltip.classList.contains('hidden'))return;let left=event.clientX+16,top=event.clientY+16;const rect=tooltip.getBoundingClientRect();if(left+rect.width>window.innerWidth-12)left=event.clientX-rect.width-16;if(top+rect.height>window.innerHeight-12)top=event.clientY-rect.height-16;tooltip.style.left=`${Math.max(left,8)}px`;tooltip.style.top=`${Math.max(top,8)}px`}
function hideGanttTooltip(){$('#ganttTooltip').classList.add('hidden')}
function startGanttAdjustmentDrag(event,encoded){hideGanttTooltip();let item={};try{item=JSON.parse(decodeURIComponent(encoded))}catch{event.preventDefault();return}const rect=event.currentTarget.getBoundingClientRect();state.dragAdjustment={item,duration:new Date(item.plan_end_time).getTime()-new Date(item.plan_start_time).getTime(),offsetX:event.clientX-rect.left};event.dataTransfer.effectAllowed='move';event.dataTransfer.setData('text/plain',item.process_id);const allowed=new Set((item.lock_options?.machines||[]).map(machine=>machine.machine_id));document.querySelectorAll('.gantt-resource-row[data-machine-id]').forEach(row=>row.classList.toggle('gantt-row-unavailable',!allowed.has(row.dataset.machineId)))}
function ganttDropTarget(event,machineId,scaleMin,scaleSpan){const drag=state.dragAdjustment;if(!drag)return null;const track=event.currentTarget,rect=track.getBoundingClientRect(),ratio=Math.max(0,Math.min(1,(event.clientX-(drag.offsetX||0)-rect.left)/rect.width)),raw=Number(scaleMin)+Number(scaleSpan)*ratio,snap=15*60*1000,start=Math.round(raw/snap)*snap,end=start+drag.duration,targetMachine=machineId||drag.item.machine_id,machine=(drag.item.lock_options?.machines||[]).find(item=>item.machine_id===targetMachine),allowedWorkerIds=new Set(machine?.allowed_worker_ids||[]),validMachine=!!machine,validWorker=!allowedWorkerIds.size||allowedWorkerIds.has(drag.item.worker_id),future=start>Date.now(),lockedConflict=(state.effectiveData?.schedule||[]).some(item=>item.process_id!==drag.item.process_id&&item.manually_locked&&item.machine_id===targetMachine&&start<new Date(item.plan_end_time).getTime()&&new Date(item.plan_start_time).getTime()<end);return{track,start,end,targetMachine,valid:validMachine&&validWorker&&future&&!lockedConflict}}
function previewGanttDrop(event,machineId,scaleMin,scaleSpan){const target=ganttDropTarget(event,machineId,scaleMin,scaleSpan);if(!target)return;event.preventDefault();event.dataTransfer.dropEffect=target.valid?'move':'none';target.track.classList.toggle('gantt-drop-valid',target.valid);target.track.classList.toggle('gantt-drop-invalid',!target.valid)}
function leaveGanttDrop(event){event.currentTarget.classList.remove('gantt-drop-valid','gantt-drop-invalid')}
function localIsoFromMillis(value){const date=new Date(value);return new Date(value-date.getTimezoneOffset()*60000).toISOString().slice(0,19)}
function dropGanttAdjustment(event,machineId,scaleMin,scaleSpan){const target=ganttDropTarget(event,machineId,scaleMin,scaleSpan),drag=state.dragAdjustment;if(!target||!drag)return;event.preventDefault();leaveGanttDrop(event);if(!target.valid){toast('目标位置不满足资源组、人员授权、时间或锁定占用约束',true);endGanttAdjustmentDrag();return}const payload={plan_start_time:localIsoFromMillis(target.start),plan_end_time:localIsoFromMillis(target.end),assigned_machine_id:target.targetMachine,assigned_worker_id:drag.item.worker_id};state.lockTarget=drag.item;endGanttAdjustmentDrag();previewProcessAdjustment(drag.item,payload)}
function endGanttAdjustmentDrag(){state.dragAdjustment=null;document.querySelectorAll('.gantt-row-unavailable,.gantt-drop-valid,.gantt-drop-invalid').forEach(element=>element.classList.remove('gantt-row-unavailable','gantt-drop-valid','gantt-drop-invalid'))}
function renderResourceGantt(rows,field,emptyLabel,kind){const scale=ganttScale(rows);if(!scale)return '<div class="empty">无可展示的生效排程工序</div>';const grouped={};scale.valid.forEach(item=>(grouped[item[field]||emptyLabel]??=[]).push(item));if(kind==='machine'){scale.valid.forEach(item=>(item.lock_options?.machines||[]).forEach(machine=>{if(!(machine.machine_id in grouped))grouped[machine.machine_id]=[]}))}return `<div class="gantt-shell"><div class="gantt"><div class="gantt-row gantt-axis"><div class="gantt-label">资源 / 时间</div><div class="gantt-track">${scale.axis}${scale.nowLine}</div></div>${Object.entries(grouped).map(([key,items])=>{const source=items[0]||(scale.valid.find(item=>(item.lock_options?.machines||[]).some(machine=>machine.machine_id===key))||{}),machineOption=(source.lock_options?.machines||[]).find(machine=>machine.machine_id===key),name=kind==='worker'?source.worker_name:(source.machine_id===key?source.machine_name:machineOption?.machine_name),interactive=kind==='machine'&&scale.valid.some(item=>item.allow_manual_adjustment),drop=interactive?` ondragover="previewGanttDrop(event,'${esc(key)}',${scale.min},${scale.span})" ondragleave="leaveGanttDrop(event)" ondrop="dropGanttAdjustment(event,'${esc(key)}',${scale.min},${scale.span})"`:'';return `<div class="gantt-row ${kind==='machine'?'gantt-resource-row':''}" ${kind==='machine'?`data-machine-id="${esc(key)}"`:''}><div class="gantt-label" title="${esc(key)}">${esc(key)}${name?`<small>${esc(name)}</small>`:''}</div><div class="gantt-track"${drop}>${scale.nowLine}${items.map(item=>ganttBar(item,scale,kind==='worker'?'gantt-worker-bar':'',true,kind==='machine')).join('')}</div></div>`}).join('')}</div></div>`}
function renderOrderGantt(rows){const scale=ganttScale(rows);if(!scale)return '<div class="empty">无可展示的生效排程工序</div>';const grouped={};scale.valid.forEach(item=>(grouped[item.order_id||'未分配订单']??=[]).push(item));return `<div class="gantt-shell"><div class="gantt gantt-order"><div class="gantt-row gantt-axis"><div class="gantt-label">订单 / 工序</div><div class="gantt-track">${scale.axis}${scale.nowLine}</div></div>${Object.entries(grouped).map(([orderId,items],index)=>{items.sort((a,b)=>(a.sequence??0)-(b.sequence??0)||new Date(a.plan_start_time)-new Date(b.plan_start_time));const groupId=`order-gantt-${index}`,summary={order_id:orderId,process_id:orderId,plan_start_time:items.reduce((v,x)=>new Date(x.plan_start_time)<new Date(v)?x.plan_start_time:v,items[0].plan_start_time),plan_end_time:items.reduce((v,x)=>new Date(x.plan_end_time)>new Date(v)?x.plan_end_time:v,items[0].plan_end_time)};return `<div class="gantt-row gantt-order-toggle" onclick="toggleOrderGantt('${groupId}',this)"><div class="gantt-label" title="${esc(orderId)}"><span class="gantt-caret">▼</span>${esc(orderId)} <small>${items.length} 道</small></div><div class="gantt-track">${scale.nowLine}${ganttBar(summary,scale,'gantt-order-bar',false)}</div></div>${items.map(item=>`<div class="gantt-row gantt-child" data-order-group="${groupId}"><div class="gantt-label" title="${esc(item.process_id)}">${esc(item.process_id)}<small>${esc(item.process_name||'')}</small></div><div class="gantt-track">${scale.nowLine}${ganttBar(item,scale,'gantt-process-bar',true,true)}</div></div>`).join('')}`}).join('')}</div></div>`}
function toggleOrderGantt(groupId,row){const children=document.querySelectorAll(`[data-order-group="${groupId}"]`),willCollapse=[...children].some(item=>!item.classList.contains('gantt-hidden'));children.forEach(item=>item.classList.toggle('gantt-hidden',willCollapse));const caret=row.querySelector('.gantt-caret');if(caret)caret.textContent=willCollapse?'▶':'▼'}
async function reviewVersion(id,decision){const comment=prompt(decision==='APPROVED'?'请输入审批意见（可选）':'请输入驳回原因')??null;if(comment===null)return;try{await api(`/api/versions/${encodeURIComponent(id)}/review`,{method:'POST',body:JSON.stringify({decision,comment})});closeModal();toast(decision==='APPROVED'?'审批完成':'版本已驳回');renderVersions()}catch(e){toast(e.message,true)}}
async function publishPlan(id){if(!confirm('发布后会将计划回写订单工序，并替代同工艺的当前发布版本。确认继续？'))return;try{const r=await api(`/api/versions/${encodeURIComponent(id)}/publish`,{method:'POST'});closeModal();toast(`计划已发布，回写 ${r.updated_processes} 道工序`);renderVersions()}catch(e){toast(e.message,true)}}
function openCompare(){if(state.versions.length<2){toast('至少需要两个版本才能对比',true);return}showModal('计划版本对比',`<form id="compareForm" class="form-grid"><label>基准版本<select name="left">${state.versions.map(v=>`<option>${esc(v.version_id)}</option>`).join('')}</select></label><label>对比版本<select name="right">${state.versions.map((v,i)=>`<option ${i===1?'selected':''}>${esc(v.version_id)}</option>`).join('')}</select></label><div class="form-actions"><button class="button primary">开始对比</button></div></form>`);$('#compareForm').onsubmit=async e=>{e.preventDefault();const f=Object.fromEntries(new FormData(e.target));try{const d=await api(`/api/versions/compare/${encodeURIComponent(f.left)}/${encodeURIComponent(f.right)}`);$('#modalTitle').textContent='版本差异';$('#modalBody').innerHTML=`<div class="detail-head"><div><h3>${d.changed_process_count} 道工序发生变化</h3><p class="muted">${esc(d.left_version_id)} → ${esc(d.right_version_id)}</p></div></div>${d.changes.length?d.changes.map(x=>`<div class="diff-card"><strong>${esc(x.process_id)} · ${esc(x.change_type)}</strong><div class="diff-fields">${Object.entries(x.fields).map(([k,v])=>`${esc(k)}: ${esc(v.before)} → ${esc(v.after)}`).join('<br>')}</div></div>`).join(''):'<div class="empty">两个版本的工序安排完全一致</div>'}`}catch(err){toast(err.message,true)}}}

async function renderMaster(){try{state.masterRecords=await api(`/api/master-data/${state.masterType}`);const batchActions=batchEntityTypes.has(state.masterType)?`<button class="button ghost" onclick="exportBatch()">批量导出</button><button class="button ghost" onclick="openBatchImport()">批量导入</button>`:'';content.innerHTML=`<section class="panel"><div class="master-header"><div class="tabs">${Object.entries(entityLabels).map(([k,v])=>`<button class="${k===state.masterType?'active':''}" onclick="switchMaster('${k}')">${v}</button>`).join('')}</div><div class="snapshot-actions"><button class="button ghost" onclick="checkMaster()">校验快照</button><button class="button ghost" onclick="exportSnapshot()">导出快照</button><button class="button ghost" onclick="openSnapshotImport()">导入快照</button></div></div><div class="toolbar master-toolbar"><input id="masterSearch" class="search" placeholder="搜索编号或名称…"><div class="toolbar-group">${batchActions}<button class="button primary" onclick="newMaster()">＋ 新建记录</button></div></div><div id="masterTable">${masterTable(state.masterRecords)}</div></section>`;$('#masterSearch').oninput=e=>{$('#masterTable').innerHTML=masterTable(state.masterRecords.filter(r=>JSON.stringify(r.payload).toLowerCase().includes(e.target.value.toLowerCase())))}}catch(e){content.innerHTML=errorHtml(e)}}
function switchMaster(type){state.masterType=type;renderMaster()}
function recordName(p){return p.order_id||p.machine_id||p.worker_id||p.resource_group_id||p.calendar_id||'-'}
function recordSub(p){return p.product_name||p.machine_name||p.worker_name||p.resource_group_name||p.calendar_name||''}
function masterTable(rows){if(!rows.length)return '<div class="empty">暂无此类主数据</div>';return `<div class="table-wrap"><table class="data-table"><thead><tr><th>编号 / 名称</th><th>状态</th><th>版本</th><th>更新人</th><th>更新时间</th><th></th></tr></thead><tbody>${rows.map(r=>`<tr><td><div class="record-title">${esc(recordName(r.payload))}</div><div class="record-sub">${esc(recordSub(r.payload))}</div></td><td>${badge(r.payload.status||'ACTIVE')}</td><td>R${r.revision}</td><td>${esc(r.updated_by)}</td><td>${esc(r.updated_at)}</td><td class="actions"><button class="button ghost small" onclick="editMaster('${esc(r.entity_id)}')">编辑 JSON</button><button class="button danger small" onclick="deleteMaster('${esc(r.entity_id)}')">删除</button></td></tr>`).join('')}</tbody></table></div>`}
function defaultRecord(){const ids={order:'order_id',machine:'machine_id',worker:'worker_id',resource_group:'resource_group_id',calendar:'calendar_id'};return {[ids[state.masterType]]:'NEW_ID',status:'ACTIVE'}}
function newMaster(){openMasterEditor(defaultRecord(),true)}
function editMaster(id){const r=state.masterRecords.find(x=>x.entity_id===id);openMasterEditor(r.payload,false)}
function openMasterEditor(record,isNew){showModal(`${isNew?'新建':'编辑'}${entityLabels[state.masterType]}`,`<form id="masterForm"><textarea class="json-editor" name="json">${esc(JSON.stringify(record,null,2))}</textarea><div class="form-actions" style="display:flex;margin-top:14px"><button type="button" class="button ghost" data-close-modal>取消</button><button class="button primary">保存记录</button></div></form>`);$('#masterForm').onsubmit=async e=>{e.preventDefault();try{const obj=JSON.parse(new FormData(e.target).get('json'));const idField={order:'order_id',machine:'machine_id',worker:'worker_id',resource_group:'resource_group_id',calendar:'calendar_id'}[state.masterType];if(!obj[idField])throw new Error(`缺少 ${idField}`);await api(`/api/master-data/${state.masterType}/${encodeURIComponent(obj[idField])}`,{method:'PUT',body:JSON.stringify(obj)});closeModal();toast('主数据已保存');renderMaster()}catch(err){toast(err.message,true)}}}
async function deleteMaster(id){if(!confirm(`确认删除 ${id}？`))return;try{await api(`/api/master-data/${state.masterType}/${encodeURIComponent(id)}`,{method:'DELETE'});toast('记录已删除');renderMaster()}catch(e){toast(e.message,true)}}
async function exportBatch(){if(!batchEntityTypes.has(state.masterType))return;try{const data=await api(`/api/master-data/${state.masterType}/batch`);const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`aps_${entityFileNames[state.masterType]}_${new Date().toISOString().slice(0,10)}.json`;a.click();URL.revokeObjectURL(a.href);toast(`已导出 ${data.length} 条${entityLabels[state.masterType]}`)}catch(e){toast(e.message,true)}}
function openBatchImport(){if(!batchEntityTypes.has(state.masterType))return;const label=entityLabels[state.masterType];showModal(`批量导入${label}`,`<form id="importForm"><p class="muted">文件内容必须是 JSON 数组，格式为 <span class="mono">[{}, {}]</span>。同编号记录将覆盖并增加修订号，文件中未包含的旧记录不会删除。</p><label class="button ghost" style="display:inline-block;margin-bottom:12px">选择 JSON 文件<input id="importFile" type="file" accept="application/json" hidden></label><textarea id="importText" class="json-editor" placeholder="格式示例：[{}, {}]"></textarea><div class="form-actions" style="display:flex;margin-top:14px"><button type="button" class="button ghost" data-close-modal>取消</button><button class="button primary">批量导入</button></div></form>`);$('#importFile').onchange=async e=>$('#importText').value=await e.target.files[0].text();$('#importForm').onsubmit=async e=>{e.preventDefault();try{const data=JSON.parse($('#importText').value);if(!Array.isArray(data))throw new Error('批量导入格式必须是 JSON 数组：[{}, {}]');if(!data.length)throw new Error('批量导入数组不能为空');if(data.some(item=>!item||Array.isArray(item)||typeof item!=='object'))throw new Error('数组中的每一项都必须是 JSON 对象');const r=await api(`/api/master-data/${state.masterType}/batch`,{method:'POST',body:JSON.stringify(data)});closeModal();toast(`成功导入 ${r.imported} 条${label}`);renderMaster()}catch(err){toast(err.message,true)}}}
async function exportSnapshot(){try{const data=await api('/api/master-data/snapshot');const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`aps_full_snapshot_${new Date().toISOString().slice(0,10)}.json`;a.click();URL.revokeObjectURL(a.href);toast('完整数据快照已导出')}catch(e){toast(e.message,true)}}
function openSnapshotImport(){showModal('导入完整数据快照',`<form id="snapshotImportForm"><p class="muted">完整快照必须包含 machine_calendar、machine_profiles、worker_profiles、resource_group_profiles、order_processes 五个根字段。单类数组数据请使用下方“批量导入”。</p><label class="button ghost" style="display:inline-block;margin-bottom:12px">选择快照 JSON 文件<input id="snapshotImportFile" type="file" accept="application/json" hidden></label><textarea id="snapshotImportText" class="json-editor" placeholder="粘贴完整 data_snapshot JSON"></textarea><div class="form-actions" style="display:flex;margin-top:14px"><button type="button" class="button ghost" data-close-modal>取消</button><button class="button primary">导入完整快照</button></div></form>`);$('#snapshotImportFile').onchange=async e=>$('#snapshotImportText').value=await e.target.files[0].text();$('#snapshotImportForm').onsubmit=async e=>{e.preventDefault();try{const data=JSON.parse($('#snapshotImportText').value);if(!data||Array.isArray(data)||typeof data!=='object')throw new Error('完整快照必须是 JSON 对象，单类数组请使用批量导入');const required=['machine_calendar','machine_profiles','worker_profiles','resource_group_profiles','order_processes'];const missing=required.filter(key=>!(key in data));if(missing.length)throw new Error(`完整快照缺少字段：${missing.join(', ')}`);const r=await api('/api/master-data/import',{method:'POST',body:JSON.stringify(data)});closeModal();toast(`完整快照导入成功，共处理 ${r.imported} 条记录`);renderMaster()}catch(err){toast(err.message,true)}}}
async function checkMaster(){try{const r=await api('/api/master-data/validate');if(r.valid)toast('主数据快照校验通过');else showModal('快照校验未通过',r.errors.map(x=>`<div class="diff-card">${esc(x)}</div>`).join(''))}catch(e){toast(e.message,true)}}

async function renderUsers(){try{state.users=await api('/api/users');content.innerHTML=`<section class="panel"><div class="panel-header"><div><h3>用户与角色权限</h3><p>计划员提交任务，审批人审核发布，查看者只读访问</p></div><button class="button primary" onclick="openUser()">＋ 新建用户</button></div><div class="table-wrap"><table class="data-table"><thead><tr><th>用户</th><th>角色</th><th>状态</th><th>更新时间</th><th></th></tr></thead><tbody>${state.users.map(u=>`<tr><td><b>${esc(u.display_name)}</b><div class="record-sub mono">${esc(u.username)}</div></td><td>${badge(u.role)}</td><td>${badge(u.active?'ACTIVE':'DISABLED')}</td><td>${esc(u.updated_at)}</td><td><button class="button ghost small" onclick="editUser('${esc(u.username)}')">编辑</button></td></tr>`).join('')}</tbody></table></div></section>`}catch(e){content.innerHTML=errorHtml(e)}}
function editUser(username){openUser(state.users.find(x=>x.username===username))}
function openUser(user=null){const isNew=!user;showModal(isNew?'新建用户':`编辑用户 · ${user.username}`,`<form id="userForm" class="form-grid"><label>用户名<input name="username" value="${esc(user?.username||'')}" ${isNew?'':'readonly'} required></label><label>显示名称<input name="display_name" value="${esc(user?.display_name||'')}" required></label><label>角色<select name="role">${['admin','planner','approver','viewer'].map(r=>`<option ${user?.role===r?'selected':''}>${r}</option>`).join('')}</select></label><label>密码${isNew?'':'（留空不修改）'}<input name="password" type="password" ${isNew?'required':''}></label><label class="full"><span><input name="active" type="checkbox" ${user?.active!==0?'checked':''}> 账号启用</span></label><div class="form-actions"><button type="button" class="button ghost" data-close-modal>取消</button><button class="button primary">保存用户</button></div></form>`);$('#userForm').onsubmit=async e=>{e.preventDefault();const fd=Object.fromEntries(new FormData(e.target));fd.active=e.target.active.checked;try{await api(isNew?'/api/users':`/api/users/${encodeURIComponent(user.username)}`,{method:isNew?'POST':'PUT',body:JSON.stringify(fd)});closeModal();toast('用户已保存');renderUsers()}catch(err){toast(err.message,true)}}}

async function renderAudit(){try{const rows=await api('/api/audit-logs');content.innerHTML=`<section class="panel"><div class="panel-header"><div><h3>操作审计记录</h3><p>记录登录、主数据变更、任务、审批和发布行为</p></div></div><div class="table-wrap"><table class="data-table"><thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>对象</th><th>详情</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${esc(r.created_at)}</td><td><b>${esc(r.actor)}</b></td><td class="mono">${esc(r.action)}</td><td>${esc(r.target_type)} / ${esc(r.target_id)}</td><td class="record-sub">${esc(JSON.stringify(r.detail))}</td></tr>`).join('')}</tbody></table></div></section>`}catch(e){content.innerHTML=errorHtml(e)}}
function errorHtml(e){return `<div class="panel empty"><h3>页面加载失败</h3><p>${esc(e.message)}</p><button class="button ghost" onclick="navigate('${state.page}')">重新加载</button></div>`}

function formatRunDuration(seconds){if(seconds===null||seconds===undefined)return '未记录';const value=Number(seconds);if(!Number.isFinite(value))return '未记录';if(value===0)return '< 1 秒';if(value<60)return `${roundMetric(value)} 秒`;const minutes=Math.floor(value/60),remaining=Math.round(value%60);return remaining?`${minutes} 分 ${remaining} 秒`:`${minutes} 分钟`}
function gaScaleDescription(count){if(count<=100)return '100 道以内';if(count<=500)return '101～500 道';if(count<=1000)return '501～1000 道';if(count<5000)return '1001～4999 道';return '5000 道及以上'}
function cleanTaskOverrideJson(textarea){try{const config=JSON.parse(textarea.value||'{}');if(config.nsga3){delete config.nsga3.population_size;delete config.nsga3.generations;if(!Object.keys(config.nsga3).length)delete config.nsga3}textarea.value=JSON.stringify(config)}catch{}}
function applyGaTaskDefaults(form,defaults){const count=Number(defaults.process_count)||0,population=Number(defaults.population_size)||56,generations=Number(defaults.generations)||30;form.dataset.processCount=String(count);$('#taskProcessCount').textContent=String(count);$('#taskPopulationSize').value=String(population);$('#taskGenerations').value=String(generations);$('#taskGaScaleHelp').textContent=`${gaScaleDescription(count)}工序：推荐种群 ${population}，进化 ${generations} 代`}
function enhanceTaskModal(defaults){const form=$('#taskForm');if(!form)return;const topsis=form.querySelector('.task-config-section');topsis.insertAdjacentHTML('beforebegin',`<div class="full task-config-section ga-config-section"><div class="ga-config-head"><div><h4>遗传算法规模参数</h4><p>当前工艺有效工序数：<strong id="taskProcessCount">0</strong></p></div><span id="taskGaScaleHelp"></span></div><div class="ga-parameter-grid"><label>种群数量<input id="taskPopulationSize" type="number" min="1" step="1" required></label><label>进化代数<input id="taskGenerations" type="number" min="0" step="1" required></label></div><p class="field-help">系统根据工序规模自动给出默认值，提交前可由用户修改。种群设为 1、代数设为 0 时，仅生成初始排程方案，不执行遗传寻优。</p></div>`);const textarea=form.querySelector('[name="config_overrides"]');cleanTaskOverrideJson(textarea);applyGaTaskDefaults(form,defaults);const scheduleType=form.querySelector('[name="schedule_type"]');scheduleType.addEventListener('change',async()=>{try{const next=await api(`/api/tasks/defaults?schedule_type=${encodeURIComponent(scheduleType.value)}`);applyGaTaskDefaults(form,next)}catch(error){toast(error.message,true)}});const baseSubmit=form.onsubmit;form.onsubmit=event=>{const population=Number($('#taskPopulationSize').value),generations=Number($('#taskGenerations').value);if(!Number.isInteger(population)||population<1||!Number.isInteger(generations)||generations<0){event.preventDefault();toast('种群数量必须是大于等于 1 的整数，进化代数必须是大于等于 0 的整数',true);return}try{const config=JSON.parse(textarea.value||'{}');config.nsga3={...(config.nsga3||{}),population_size:population,generations};config.nsga3.hierarchical={...(config.nsga3.hierarchical||{}),population_size:population,generations};config.nsga3.hybrid_large_scale={...(config.nsga3.hybrid_large_scale||{}),local_population_size:population,local_generations:generations};textarea.value=JSON.stringify(config)}catch{}return baseSubmit.call(form,event)}}
function versionTableEnhanced(rows){if(!rows.length)return '<div class="empty">暂无计划版本，请先执行排程任务</div>';return `<div class="table-wrap version-table-wrap"><table class="data-table version-list-table"><thead><tr><th>版本</th><th>工艺类型</th><th>排程模式 / 派工规则</th><th>来源任务</th><th>种群 / 代数</th><th>排程用时</th><th>状态</th><th>创建人</th><th>创建时间</th><th>审批 / 发布</th><th></th></tr></thead><tbody>${rows.map(v=>`<tr><td><b>${esc(v.version_id)}</b><div class="record-sub">#${v.version_no}</div></td><td>${scheduleTypeBadge(v.schedule_type)}</td><td>${scheduleModeBadge(v.mode)}<div class="record-sub">${esc(comparisonDispatchingLabel(v.dispatching_rule))}</div></td><td class="mono">${esc(v.task_id)}</td><td><b>${v.population_size??'未记录'} / ${v.generations??'未记录'}</b><div class="record-sub">种群 / 代</div></td><td>${esc(formatRunDuration(v.duration_seconds))}</td><td>${badge(v.status)}</td><td>${esc(v.created_by||'-')}</td><td>${esc(String(v.created_at||'-').replace('T',' '))}</td><td><div class="record-sub">${esc(v.reviewed_by||'-')} / ${esc(v.published_by||'-')}</div></td><td><button class="button ghost small" onclick="showVersion('${esc(v.version_id)}')">打开计划</button></td></tr>`).join('')}</tbody></table></div>`}
versionTable=versionTableEnhanced;
const comparisonFieldLabels={plan_start_time:'计划开始时间',plan_end_time:'计划完工时间',machine_id:'设备',worker_id:'人员',batch_id:'合批号'};
const comparisonChangeLabels={ADDED:'新增工序',REMOVED:'移除工序',MODIFIED:'调整工序'};
function comprehensiveScore(result){
    const direct=Number(result?.topsis_score??result?.metadata?.topsis_score);
    if(Number.isFinite(direct))return direct;
    const ranking=Array.isArray(result?.topsis_ranking)?result.topsis_ranking:[];
    const candidates=ranking.map((item,index)=>({rank:Number(item?.rank)||index+1,score:Number(item?.topsis_score)})).filter(item=>Number.isFinite(item.score));
    candidates.sort((a,b)=>a.rank-b.rank||b.score-a.score);
    return candidates.length?candidates[0].score:null;
}
function formatComprehensiveScore(value){const score=Number(value);return Number.isFinite(score)?score.toFixed(6):'未记录'}
function versionScoreBadge(result){return `<span class="version-score-badge">综合得分：<strong>${esc(formatComprehensiveScore(comprehensiveScore(result)))}</strong></span>`}
function comparisonMetricValue(value,unit){if(value===null||value===undefined)return '-';if(unit==='percent')return `${roundMetric(value*100)}%`;if(unit==='score')return formatComprehensiveScore(value);return `${roundMetric(value)} h`}
function comparisonMetricCard(metric){const arrow=metric.trend==='up'?'↑':metric.trend==='down'?'↓':'—';const trendLabel=metric.trend==='up'?'上升':metric.trend==='down'?'下降':metric.trend==='same'?'持平':'暂无数据';const delta=metric.delta===null||metric.delta===undefined?'':` ${metric.unit==='percent'?`${roundMetric(Math.abs(metric.delta)*100)} 个百分点`:`${roundMetric(Math.abs(metric.delta))} h`}`;return `<div class="compare-metric ${esc(metric.outcome||'unavailable')}"><small>${esc(metric.label)}</small><div class="compare-metric-values"><span>${esc(comparisonMetricValue(metric.before,metric.unit))}</span><b>→</b><strong>${esc(comparisonMetricValue(metric.after,metric.unit))}</strong></div><div class="compare-trend ${esc(metric.trend||'unavailable')}"><i>${arrow}</i>${trendLabel}${esc(delta)}</div></div>`}
function comparisonScoreCard(metric){
    const available=metric&&metric.before!==null&&metric.before!==undefined&&metric.after!==null&&metric.after!==undefined;
    const delta=available?Number(metric.delta):null,rate=Number(metric?.change_rate_percent);
    const arrow=metric?.trend==='up'?'↑':metric?.trend==='down'?'↓':'—';
    const trendLabel=metric?.outcome==='improved'?'提升':metric?.outcome==='worsened'?'下降':metric?.outcome==='same'?'持平':'暂无对比数据';
    const deltaText=Number.isFinite(delta)?`${delta>=0?'+':''}${delta.toFixed(6)}`:'-';
    const rateText=Number.isFinite(rate)?`${rate>=0?'+':''}${roundMetric(rate)}%`:'-';
    return `<section class="compare-score-card ${esc(metric?.outcome||'unavailable')}"><div class="compare-score-intro"><small>综合得分对比</small><strong>得分越高，综合表现越优</strong></div><div class="compare-score-value before"><small>基准版本</small><strong>${esc(formatComprehensiveScore(metric?.before))}</strong></div><div class="compare-score-change"><span>${arrow} ${trendLabel}</span><strong>${esc(deltaText)}</strong><small>变化率 ${esc(rateText)}</small></div><div class="compare-score-value after"><small>对比版本</small><strong>${esc(formatComprehensiveScore(metric?.after))}</strong></div></section>`;
}
function comparisonDiffCard(change){const changeType=change.change_type||'MODIFIED';const fields=Object.entries(change.fields||{});return `<article class="diff-card ${esc(changeType)}"><header><strong>${esc(change.process_id)}</strong><span>${esc(comparisonChangeLabels[changeType]||changeType)}</span></header><div class="diff-fields">${fields.map(([field,value])=>`<div class="diff-field-row"><b>${esc(comparisonFieldLabels[field]||field)}</b><span class="diff-value before">${esc(value.before??'未设置')}</span><i>→</i><span class="diff-value after">${esc(value.after??'未设置')}</span></div>`).join('')}</div></article>`}
function comparisonRunCard(title,versionId,run){return `<div class="compare-run-card"><small>${esc(title)}</small><strong>${esc(versionId)}</strong><div><span>种群 <b>${run?.population_size??'未记录'}</b></span><span>代数 <b>${run?.generations??'未记录'}</b></span><span>用时 <b>${esc(formatRunDuration(run?.duration_seconds))}</b></span><span>工艺类型 <b>${esc(scheduleTypeLabel(run?.schedule_type))}</b></span><span>排程模式 <b>${esc(comparisonModeLabel(run?.mode))}</b></span><span>派工规则 <b>${esc(comparisonDispatchingLabel(run?.dispatching_rule))}</b></span></div></div>`}
function renderVersionComparison(data){const metrics=data.metric_comparison||[],runs=data.run_comparison||{};return `<div class="compare-heading"><div><h3>核心指标对比</h3><p>${esc(data.left_version_id)} → ${esc(data.right_version_id)}</p></div><span class="compare-count">${data.changed_process_count} 道工序变化</span></div><div class="compare-run-grid">${comparisonRunCard('基准版本',data.left_version_id,runs.before)}${comparisonRunCard('对比版本',data.right_version_id,runs.after)}</div>${comparisonScoreCard(data.score_comparison||{})}<div class="compare-metric-grid">${metrics.map(comparisonMetricCard).join('')}</div><div class="compare-detail-title"><div><h3>工序差异明细</h3><p>旧值与新值使用不同颜色标记</p></div></div>${data.changes?.length?`<div class="diff-list">${data.changes.map(comparisonDiffCard).join('')}</div>`:'<div class="empty">两个版本的工序安排完全一致</div>'}`}
function openCompareEnhanced(){if(state.versions.length<2){toast('至少需要两个版本才能对比',true);return}showModal('计划版本对比',`<form id="compareForm" class="form-grid"><label>基准版本<select name="left">${state.versions.map(v=>`<option>${esc(v.version_id)}</option>`).join('')}</select></label><label>对比版本<select name="right">${state.versions.map((v,i)=>`<option ${i===1?'selected':''}>${esc(v.version_id)}</option>`).join('')}</select></label><div class="form-actions"><button class="button primary">开始对比</button></div></form>`);$('#compareForm').onsubmit=async event=>{event.preventDefault();const form=Object.fromEntries(new FormData(event.target));if(form.left===form.right){toast('请选择两个不同的计划版本',true);return}try{const data=await api(`/api/versions/compare/${encodeURIComponent(form.left)}/${encodeURIComponent(form.right)}`);$('#modalTitle').textContent='版本差异';$('#modalBody').innerHTML=renderVersionComparison(data);document.querySelector('.modal-card').classList.add('compare-modal')}catch(error){toast(error.message,true)}}}
function openCompareWithLatestDefaults(){if(state.versions.length<2){openCompareEnhanced();return}const versions=[...state.versions].sort((a,b)=>(Number(b.version_no)||0)-(Number(a.version_no)||0)||String(b.created_at||'').localeCompare(String(a.created_at||'')));openCompareEnhanced();const form=$('#compareForm');if(!form)return;form.elements.left.value=versions[1].version_id;form.elements.right.value=versions[0].version_id}
openCompare=openCompareWithLatestDefaults;
function comparisonModeLabel(value){return({static:'静态全量',dynamic:'动态滚动',local:'局部微调'}[value]||value||'未记录')}
function comparisonDispatchingLabel(value){return({DELIVERY:'交期优先(EDD)',PRIORITY:'优先级优先(PRIORITY)',SLACK:'最小松弛时间(SLACK)',EFFICIENCY:'效率优先(EFFICIENCY)',FCFS:'先到先服务(FCFS)'}[value]||value||'未记录')}
function versionParameterPanel(version){const params=version.schedule_parameters||version;const items=[['工艺类型',scheduleTypeLabel(params.schedule_type)],['排程模式',comparisonModeLabel(params.mode)],['派工规则',comparisonDispatchingLabel(params.dispatching_rule)],['排程基准时间',params.schedule_start||'未记录'],['种群数量',params.population_size??'未记录'],['进化代数',params.generations??'未记录'],['排程用时',formatRunDuration(params.duration_seconds)]];return '<section class="version-parameter-panel"><div class="version-parameter-head"><div><h4>本次排程参数</h4><p>来源任务创建时实际提交的排程配置</p></div></div><div class="version-parameter-grid">'+items.map(item=>'<div><small>'+esc(item[0])+'</small><strong>'+esc(item[1])+'</strong></div>').join('')+'</div><details class="version-config-details"><summary>查看完整算法参数</summary><pre class="code-block">'+esc(JSON.stringify(params.config_overrides||{},null,2))+'</pre></details></section>'}
const orderMasterTypes=[
    {value:'machining',label:'机加订单'},
    {value:'heat_treatment',label:'热表订单'},
    {value:'assembly',label:'装配订单'}
];
const calendarMasterTypes=[
    {value:'machining',label:'机加日历'},
    {value:'heat_treatment',label:'热表日历'},
    {value:'assembly',label:'装配日历'}
];
const orderResourceTypeMapping={
    MACHINING:'machining',
    HEAT_TREAT:'heat_treatment',
    SURFACE_TREAT:'heat_treatment',
    ASSEMBLY:'assembly'
};
state.masterOrderType=state.masterOrderType||'machining';
state.masterResourceGroups=state.masterResourceGroups||[];

function orderMasterTypeLabel(value){
    return orderMasterTypes.find(item=>item.value===value)?.label||value;
}

function calendarMasterTypeLabel(value){
    return calendarMasterTypes.find(item=>item.value===value)?.label||value;
}

function calendarScheduleType(record){
    const explicit=String(record?.schedule_type||'').toLowerCase();
    if(calendarMasterTypes.some(item=>item.value===explicit))return explicit;
    const marker=(String(record?.calendar_id||'')+' '+String(record?.calendar_name||'')).toUpperCase();
    if(marker.includes('HEAT')||marker.includes('热'))return 'heat_treatment';
    if(marker.includes('ASSEMBLY')||marker.includes('装配'))return 'assembly';
    return 'machining';
}

function orderProcessScheduleTypes(record){
    const groupIndex=new Map(
        state.masterResourceGroups.map(row=>[
            String(row.entity_id||row.payload?.resource_group_id||''),
            row.payload||{}
        ])
    );
    const types=new Set();
    (record?.processes||[]).forEach(process=>{
        const group=groupIndex.get(String(process.resource_group_id||''));
        const scheduleType=orderResourceTypeMapping[String(group?.resource_group_type||'').toUpperCase()];
        if(scheduleType)types.add(scheduleType);
    });
    return types;
}

function orderScheduleType(record){
    const explicit=String(record?.schedule_type||'').toLowerCase();
    if(orderMasterTypes.some(item=>item.value===explicit))return explicit;
    const inferred=orderProcessScheduleTypes(record);
    if(inferred.size===1)return [...inferred][0];
    const marker=(String(record?.order_id||'')+' '+String(record?.order_type||'')).toUpperCase();
    if(marker.includes('HEAT')||marker.includes('_RB_'))return 'heat_treatment';
    if(marker.includes('ASSEMBLY')||marker.includes('_ASM_')||marker.includes('_ZP_'))return 'assembly';
    return 'machining';
}

function ensureOrderScope(record,scheduleType){
    const explicit=String(record?.schedule_type||'').toLowerCase();
    if(explicit&&explicit!==scheduleType){
        throw new Error('订单声明类型与当前“'+orderMasterTypeLabel(scheduleType)+'”不一致');
    }
    const inferred=orderProcessScheduleTypes(record);
    if(inferred.size>1){
        throw new Error('同一订单包含多个工艺类型的工序，请拆分到机加、热表、装配订单中分别管理');
    }
    if(inferred.size===1&&!inferred.has(scheduleType)){
        throw new Error('订单工序所属资源组与当前“'+orderMasterTypeLabel(scheduleType)+'”不一致');
    }
    return {...record,schedule_type:scheduleType};
}

function visibleMasterRecords(){
    if(state.masterType==='order')return state.masterRecords.filter(row=>orderScheduleType(row.payload)===state.masterOrderType);
    if(state.masterType==='calendar')return state.masterRecords.filter(row=>calendarScheduleType(row.payload)===state.masterCalendarType);
    return state.masterRecords;
}

function orderMasterTabsHtml(){
    if(state.masterType!=='order')return '';
    return '<div class="order-type-tabs">'+orderMasterTypes.map(option=>{
        const count=state.masterRecords.filter(row=>orderScheduleType(row.payload)===option.value).length;
        const active=option.value===state.masterOrderType?' active':'';
        return '<button type="button" data-order-type="'+esc(option.value)+'" class="order-type-tab '+esc(option.value)+active+'"><span>'+esc(option.label)+'</span><b>'+count+'</b></button>';
    }).join('')+'</div>';
}

function calendarMasterTabsHtml(){
    if(state.masterType!=='calendar')return '';
    return '<div class="order-type-tabs calendar-type-tabs">'+calendarMasterTypes.map(option=>{
        const count=state.masterRecords.filter(row=>calendarScheduleType(row.payload)===option.value).length;
        const active=option.value===state.masterCalendarType?' active':'';
        return '<button type="button" data-calendar-type="'+esc(option.value)+'" class="order-type-tab '+esc(option.value)+active+'"><span>'+esc(option.label)+'</span><b>'+count+'</b></button>';
    }).join('')+'</div>';
}

async function renderMasterByOrderType(){
    try{
        const requests=[api('/api/master-data/'+state.masterType)];
        if(state.masterType==='order')requests.push(api('/api/master-data/resource_group'));
        const results=await Promise.all(requests);
        state.masterRecords=results[0];
        if(state.masterType==='order')state.masterResourceGroups=results[1];
        const batchActions=batchEntityTypes.has(state.masterType)
            ?'<button class="button ghost" onclick="exportBatch()">批量导出</button><button class="button ghost" onclick="openBatchImport()">批量导入</button>'
            :'';
        const masterTabs=Object.entries(entityLabels).map(([key,label])=>
            '<button class="'+(key===state.masterType?'active':'')+'" onclick="switchMaster(\''+esc(key)+'\')">'+esc(label)+'</button>'
        ).join('');
        const visible=visibleMasterRecords();
        const newRecordAction=state.masterType==='calendar'&&visible.length
            ?''
            :'<button class="button primary" onclick="newMaster()">＋ 新建记录</button>';
        const recordActions='<div class="toolbar master-toolbar"><input id="masterSearch" class="search" placeholder="搜索编号或名称…"><div class="toolbar-group">'+batchActions+newRecordAction+'</div></div>';
        const scopeTabs=state.masterType==='order'?orderMasterTabsHtml():calendarMasterTabsHtml();
        const recordToolbar=['order','calendar'].includes(state.masterType)
            ?'<div class="master-record-toolbar">'+scopeTabs+recordActions+'</div>'
            :recordActions;
        content.innerHTML='<section class="panel">'
            +'<div class="master-header"><div class="tabs">'+masterTabs+'</div>'
            +'<div class="snapshot-actions"><button class="button ghost" onclick="checkMaster()">校验快照</button><button class="button ghost" onclick="exportSnapshot()">导出快照</button><button class="button ghost" onclick="openSnapshotImport()">导入快照</button></div></div>'
            +recordToolbar
            +'<div id="masterTable">'+masterTable(visible)+'</div></section>';
        document.querySelectorAll('[data-order-type]').forEach(button=>{
            button.onclick=()=>switchOrderType(button.dataset.orderType);
        });
        document.querySelectorAll('[data-calendar-type]').forEach(button=>{
            button.onclick=()=>switchCalendarType(button.dataset.calendarType);
        });
        if(state.masterType==='calendar'){
            document.querySelectorAll('#masterTable .button.danger').forEach(button=>button.remove());
        }
        $('#masterSearch').oninput=event=>{
            const keyword=event.target.value.toLowerCase();
            $('#masterTable').innerHTML=masterTable(
                visibleMasterRecords().filter(row=>JSON.stringify(row.payload).toLowerCase().includes(keyword))
            );
        };
    }catch(error){
        content.innerHTML=errorHtml(error);
    }
}
renderMaster=renderMasterByOrderType;

function switchOrderType(scheduleType){
    if(!orderMasterTypes.some(item=>item.value===scheduleType))return;
    state.masterOrderType=scheduleType;
    renderMaster();
}

function switchCalendarType(scheduleType){
    if(!calendarMasterTypes.some(item=>item.value===scheduleType))return;
    state.masterCalendarType=scheduleType;
    renderMaster();
}

const defaultRecordBase=defaultRecord;
function defaultCalendarWeeklyShifts(scheduleType){
    if(scheduleType==='heat_treatment'){
        return Object.fromEntries(Array.from({length:7},(_,day)=>[String(day),[{name:'continuous',segments:[{start:'00:00',end:'00:00',capacity_factor:1}]}]]));
    }
    return Object.fromEntries(Array.from({length:7},(_,day)=>[
        String(day),
        day===0?[]:[{name:'day',segments:[{start:'08:00',end:'12:00',capacity_factor:1},{start:'13:00',end:'17:00',capacity_factor:1}]}]
    ]));
}
function defaultRecordByOrderType(){
    if(state.masterType==='calendar'){
        const scheduleType=state.masterCalendarType;
        const prefixes={machining:'NEW_MACHINING_CALENDAR',heat_treatment:'NEW_HEAT_CALENDAR',assembly:'NEW_ASSEMBLY_CALENDAR'};
        return {
            calendar_id:prefixes[scheduleType],
            calendar_name:calendarMasterTypeLabel(scheduleType),
            schedule_type:scheduleType,
            calendar_level:'WORKSHOP',
            time_zone:'Asia/Shanghai',
            day_shift_start:'08:00',
            status:'ACTIVE',
            weekly_shifts:defaultCalendarWeeklyShifts(scheduleType),
            special_shifts:{},
            special_rules:[]
        };
    }
    if(state.masterType!=='order')return defaultRecordBase();
    const prefixes={machining:'NEW_MC_ORDER',heat_treatment:'NEW_HEAT_ORDER',assembly:'NEW_ASM_ORDER'};
    return {
        order_id:prefixes[state.masterOrderType],
        schedule_type:state.masterOrderType,
        order_type:'STANDARD',
        product_id:'',
        product_name:'',
        quantity:1,
        priority:1,
        due_date:'',
        release_date:'',
        status:'RELEASED',
        processes:[]
    };
}
defaultRecord=defaultRecordByOrderType;

const recordSubBase=recordSub;
function recordSubWithProcessCount(payload){
    if(!payload?.order_id)return recordSubBase(payload);
    const name=payload.product_name||'未填写产品名称';
    return name+' · '+(payload.processes||[]).length+' 道工序';
}
recordSub=recordSubWithProcessCount;

const openMasterEditorBase=openMasterEditor;
function openMasterEditorByOrderType(record,isNew){
    if(state.masterType==='calendar'){
        openMasterEditorBase({...record,schedule_type:state.masterCalendarType},isNew);
        $('#modalTitle').textContent=(isNew?'新建':'编辑')+calendarMasterTypeLabel(state.masterCalendarType);
        const form=$('#masterForm');
        const baseSubmit=form.onsubmit;
        form.onsubmit=event=>{
            try{
                const textarea=form.querySelector('[name="json"]');
                const parsed=JSON.parse(textarea.value);
                const explicit=String(parsed.schedule_type||'').toLowerCase();
                if(explicit&&explicit!==state.masterCalendarType){
                    throw new Error('日历声明类型与当前“'+calendarMasterTypeLabel(state.masterCalendarType)+'”不一致');
                }
                parsed.schedule_type=state.masterCalendarType;
                textarea.value=JSON.stringify(parsed,null,2);
            }catch(error){
                event.preventDefault();
                toast(error.message,true);
                return;
            }
            return baseSubmit.call(form,event);
        };
        return;
    }
    if(state.masterType!=='order'){
        openMasterEditorBase(record,isNew);
        return;
    }
    openMasterEditorBase({...record,schedule_type:state.masterOrderType},isNew);
    $('#modalTitle').textContent=(isNew?'新建':'编辑')+orderMasterTypeLabel(state.masterOrderType);
    const form=$('#masterForm');
    const baseSubmit=form.onsubmit;
    form.onsubmit=event=>{
        try{
            const textarea=form.querySelector('[name="json"]');
            const scoped=ensureOrderScope(JSON.parse(textarea.value),state.masterOrderType);
            textarea.value=JSON.stringify(scoped,null,2);
        }catch(error){
            event.preventDefault();
            toast(error.message,true);
            return;
        }
        return baseSubmit.call(form,event);
    };
}
openMasterEditor=openMasterEditorByOrderType;

async function exportBatchByOrderType(){
    if(!batchEntityTypes.has(state.masterType))return;
    try{
        let data=await api('/api/master-data/'+state.masterType+'/batch');
        let fileName=entityFileNames[state.masterType];
        let label=entityLabels[state.masterType];
        if(state.masterType==='order'){
            data=data.filter(order=>orderScheduleType(order)===state.masterOrderType);
            fileName=state.masterOrderType+'_orders';
            label=orderMasterTypeLabel(state.masterOrderType);
        }
        const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});
        const anchor=document.createElement('a');
        anchor.href=URL.createObjectURL(blob);
        anchor.download='aps_'+fileName+'_'+new Date().toISOString().slice(0,10)+'.json';
        anchor.click();
        URL.revokeObjectURL(anchor.href);
        toast('已导出 '+data.length+' 条'+label);
    }catch(error){
        toast(error.message,true);
    }
}
exportBatch=exportBatchByOrderType;

function openBatchImportByOrderType(){
    if(!batchEntityTypes.has(state.masterType))return;
    const label=state.masterType==='order'?orderMasterTypeLabel(state.masterOrderType):entityLabels[state.masterType];
    showModal('批量导入'+label,'<form id="importForm"><p class="muted">文件内容必须是 JSON 数组。同编号记录将覆盖并增加修订号，未包含的旧记录不会删除。</p><label class="button ghost" style="display:inline-block;margin-bottom:12px">选择 JSON 文件<input id="importFile" type="file" accept="application/json" hidden></label><textarea id="importText" class="json-editor" placeholder="格式示例：[{}, {}]"></textarea><div class="form-actions" style="display:flex;margin-top:14px"><button type="button" class="button ghost" data-close-modal>取消</button><button class="button primary">批量导入</button></div></form>');
    $('#importFile').onchange=async event=>$('#importText').value=await event.target.files[0].text();
    $('#importForm').onsubmit=async event=>{
        event.preventDefault();
        try{
            let data=JSON.parse($('#importText').value);
            if(!Array.isArray(data))throw new Error('批量导入格式必须是 JSON 数组：[{}, {}]');
            if(!data.length)throw new Error('批量导入数组不能为空');
            if(data.some(item=>!item||Array.isArray(item)||typeof item!=='object')){
                throw new Error('数组中的每一项都必须是 JSON 对象');
            }
            if(state.masterType==='order'){
                data=data.map(item=>ensureOrderScope(item,state.masterOrderType));
            }
            const result=await api('/api/master-data/'+state.masterType+'/batch',{
                method:'POST',
                body:JSON.stringify(data)
            });
            closeModal();
            toast('成功导入 '+result.imported+' 条'+label);
            renderMaster();
        }catch(error){
            toast(error.message,true);
        }
    };
}
openBatchImport=openBatchImportByOrderType;
state.versionFilters=state.versionFilters||{keyword:'',schedule_type:'',status:''};

function filteredPlanVersions(){
    const filters=state.versionFilters||{};
    const keyword=String(filters.keyword||'').trim().toLowerCase();
    return state.versions.filter(version=>{
        if(filters.schedule_type&&version.schedule_type!==filters.schedule_type)return false;
        if(filters.status&&version.status!==filters.status)return false;
        if(!keyword)return true;
        const searchable=[
            version.version_id,
            version.task_id,
            version.created_by,
            version.reviewed_by,
            version.published_by,
            version.status,
            version.schedule_type,
            scheduleTypeLabel(version.schedule_type),
            version.mode,
            version.dispatching_rule
        ].filter(Boolean).join(' ').toLowerCase();
        return searchable.includes(keyword);
    });
}

function updateVersionSearchResults(){
    const rows=filteredPlanVersions();
    const table=$('#versionSearchTable');
    const count=$('#versionSearchCount');
    if(table)table.innerHTML=versionTable(rows);
    if(count)count.textContent='显示 '+rows.length+' / '+state.versions.length+' 个版本';
}

function resetVersionSearch(){
    state.versionFilters={keyword:'',schedule_type:'',status:''};
    const keyword=$('#versionSearch');
    const type=$('#versionTypeFilter');
    const status=$('#versionStatusFilter');
    if(keyword)keyword.value='';
    if(type)type.value='';
    if(status)status.value='';
    updateVersionSearchResults();
}

async function renderVersionsWithSearch(){
    try{
        state.versions=await api('/api/versions');
        const filters=state.versionFilters||{};
        const scheduleTypes=[...new Set(state.versions.map(item=>item.schedule_type).filter(Boolean))];
        const statuses=[...new Set(state.versions.map(item=>item.status).filter(Boolean))];
        const typeOptions=scheduleTypes.map(value=>'<option value="'+esc(value)+'" '+(filters.schedule_type===value?'selected':'')+'>'+esc(scheduleTypeLabel(value))+'</option>').join('');
        const statusOptions=statuses.map(value=>'<option value="'+esc(value)+'" '+(filters.status===value?'selected':'')+'>'+esc(value)+'</option>').join('');
        content.innerHTML='<section class="panel">'
            +'<div class="panel-header"><div><h3>排程计划版本</h3><p>算法结果需完成业务审批后才能发布并回写工序</p></div><button class="button ghost" onclick="openCompare()">版本对比</button></div>'
            +'<div class="version-search-toolbar"><input id="versionSearch" class="search" value="'+esc(filters.keyword||'')+'" placeholder="搜索版本号、任务、创建人、模式、派工规则…">'
            +'<select id="versionTypeFilter"><option value="">全部工艺类型</option>'+typeOptions+'</select>'
            +'<select id="versionStatusFilter"><option value="">全部版本状态</option>'+statusOptions+'</select>'
            +'<button type="button" class="button ghost" onclick="resetVersionSearch()">重置</button>'
            +'<span id="versionSearchCount"></span></div>'
            +'<div id="versionSearchTable"></div></section>';
        $('#versionSearch').oninput=event=>{
            state.versionFilters.keyword=event.target.value;
            updateVersionSearchResults();
        };
        $('#versionTypeFilter').onchange=event=>{
            state.versionFilters.schedule_type=event.target.value;
            updateVersionSearchResults();
        };
        $('#versionStatusFilter').onchange=event=>{
            state.versionFilters.status=event.target.value;
            updateVersionSearchResults();
        };
        updateVersionSearchResults();
    }catch(error){
        content.innerHTML=errorHtml(error);
    }
}
renderVersions=renderVersionsWithSearch;
state.taskFilters=state.taskFilters||{keyword:'',schedule_type:'',status:''};

function taskTableDetailed(rows){
    if(!rows?.length)return '<div class="empty">当前筛选条件下没有排程任务</div>';
    return '<div class="table-wrap"><table class="data-table task-detail-table"><thead><tr><th>任务编号</th><th>工艺类型</th><th>排程模式 / 派工规则</th><th>种群 / 代数</th><th>排程用时</th><th>状态</th><th>创建人</th><th>创建时间</th><th></th></tr></thead><tbody>'
        +rows.map(task=>'<tr>'
            +'<td class="mono">'+esc(task.task_id)+'</td>'
            +'<td>'+scheduleTypeBadge(task.schedule_type)+'</td>'
            +'<td>'+scheduleModeBadge(task.mode)+'<div class="record-sub">'+esc(comparisonDispatchingLabel(task.dispatching_rule))+'</div></td>'
            +'<td><b>'+(task.population_size??'未记录')+' / '+(task.generations??'未记录')+'</b><div class="record-sub">种群 / 代</div></td>'
            +'<td>'+esc(formatRunDuration(task.duration_seconds))+'</td>'
            +'<td>'+badge(task.status)+'</td>'
            +'<td>'+esc(task.created_by||'-')+'</td>'
            +'<td>'+esc(task.created_at||'-')+'</td>'
            +'<td><button class="button ghost small" onclick="showTask(\''+esc(task.task_id)+'\')">详情</button></td>'
            +'</tr>').join('')
        +'</tbody></table></div>';
}

function filteredScheduleTasks(){
    const filters=state.taskFilters||{};
    const keyword=String(filters.keyword||'').trim().toLowerCase();
    return state.tasks.filter(task=>{
        if(filters.schedule_type&&task.schedule_type!==filters.schedule_type)return false;
        if(filters.status&&task.status!==filters.status)return false;
        if(!keyword)return true;
        const searchable=[
            task.task_id,
            task.created_by,
            task.status,
            task.schedule_type,
            scheduleTypeLabel(task.schedule_type),
            task.mode,
            comparisonModeLabel(task.mode),
            task.dispatching_rule,
            comparisonDispatchingLabel(task.dispatching_rule),
            task.error_message
        ].filter(Boolean).join(' ').toLowerCase();
        return searchable.includes(keyword);
    });
}

function updateTaskSearchResults(){
    const rows=filteredScheduleTasks();
    const table=$('#taskSearchTable');
    const count=$('#taskSearchCount');
    if(table)table.innerHTML=taskTableDetailed(rows);
    if(count)count.textContent='显示 '+rows.length+' / '+state.tasks.length+' 个任务';
}

function resetTaskSearch(){
    state.taskFilters={keyword:'',schedule_type:'',status:''};
    const keyword=$('#taskSearch');
    const type=$('#taskTypeFilter');
    const status=$('#taskStatusFilter');
    if(keyword)keyword.value='';
    if(type)type.value='';
    if(status)status.value='';
    updateTaskSearchResults();
}

async function renderTasksWithSearch(){
    try{
        state.tasks=await api('/api/tasks');
        const filters=state.taskFilters||{};
        const scheduleTypes=[...new Set(state.tasks.map(item=>item.schedule_type).filter(Boolean))];
        const statuses=[...new Set(state.tasks.map(item=>item.status).filter(Boolean))];
        const typeOptions=scheduleTypes.map(value=>'<option value="'+esc(value)+'" '+(filters.schedule_type===value?'selected':'')+'>'+esc(scheduleTypeLabel(value))+'</option>').join('');
        const statusOptions=statuses.map(value=>'<option value="'+esc(value)+'" '+(filters.status===value?'selected':'')+'>'+esc(value)+'</option>').join('');
        content.innerHTML='<section class="panel">'
            +'<div class="panel-header"><div><h3>排程任务队列</h3><p>静态全量、动态滚动和局部微调统一管理</p></div><button class="button primary" onclick="openTaskModal()">＋ 新建任务</button></div>'
            +'<div class="version-search-toolbar"><input id="taskSearch" class="search" value="'+esc(filters.keyword||'')+'" placeholder="搜索任务号、创建人、模式、派工规则…">'
            +'<select id="taskTypeFilter"><option value="">全部工艺类型</option>'+typeOptions+'</select>'
            +'<select id="taskStatusFilter"><option value="">全部任务状态</option>'+statusOptions+'</select>'
            +'<button type="button" class="button ghost" onclick="resetTaskSearch()">重置</button>'
            +'<span id="taskSearchCount" class="search-result-count"></span></div>'
            +'<div id="taskSearchTable"></div></section>';
        $('#taskSearch').oninput=event=>{
            state.taskFilters.keyword=event.target.value;
            updateTaskSearchResults();
        };
        $('#taskTypeFilter').onchange=event=>{
            state.taskFilters.schedule_type=event.target.value;
            updateTaskSearchResults();
        };
        $('#taskStatusFilter').onchange=event=>{
            state.taskFilters.status=event.target.value;
            updateTaskSearchResults();
        };
        updateTaskSearchResults();
        if(state.tasks.some(task=>['RUNNING','QUEUED'].includes(task.status))){
            setTimeout(()=>state.page==='tasks'&&renderTasks(),3000);
        }
    }catch(error){
        content.innerHTML=errorHtml(error);
    }
}
renderTasks=renderTasksWithSearch;
if(state.token)startApp();
