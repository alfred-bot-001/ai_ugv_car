"use strict";
// Map page: live map, scan, pose, path, camera, status and logs, plus the controls:
// mapping, saving, loading a map, setting the pose, "go here", cancel and the wheel
// joystick. Everything goes through the Mini (/ws/nav, /ws/control, /api); the car's
// ugv-navd runs the commands and holds any motion window. Motion needs a confirmation.
const $=selector=>document.querySelector(selector);
const ROBOT_RADIUS=0.17,POSE_STALE_S=3,TRAIL_MAX=300,LOG_MAX=400;
const MAP_COLORS=[[251,252,250,255],[38,58,50,255],[0,0,0,0]];
let csrf='',stopped=false,toastTimer=null,navSocket=null,navRetry=null,mapGeneration=0;
let videoSocket=null,videoGeneration=0,videoPending=null,videoPainting=false,videoStale=null,videoSource='video';
let clockOffset=null,lastStatus={};
const nav={map:null,mapImage:null,nextMap:null,pose:null,trail:[],scan:[],plan:[],state:null,agent:null};
let view={cx:0,cy:0,scale:60,width:0,height:0},follow=true,fitPending=true,dirty=true,dpr=1;
const layers={scan:true,trail:true,plan:true,grid:false};
const logKeys=new Set();
// Map actions: a right click (long press on touch) opens a menu at a map point; "set the
// pose" then takes one more click for the heading. arrow is that heading being chosen.
let tool='view',arrow=null,pendingTarget=null,menuPoint=null,longPress=null,maps=[],defaultMap=null,mapsLoaded=false,mapsLoading=false;
let rotationFor='';
// The patrol route being edited (for the localized map) and the recent reports.
let route={map:null,loops:1,waypoints:[]},routeDirty=false,routeLoading=false,reports=[];
let jobSeen=false,lastJobKey='',overwriteName='',dialogPending=null;
// Wheel and gimbal joysticks (same held-state relay as the dashboard: the knobs stay put).
let controlSocket=null,press={wheels:null,gimbal:null},dragging=null,pressGeneration=0,controlSequence=0,lease='',leasing=null,streamTimer=null,lastSend=0;
const TOOL_HINT={view:'右键地图（手机长按）：去这里、把老三的位置设在这里 · 拖动平移，滚轮或双指缩放',heading:'朝老三车头的方向点一下，确定朝向（Esc 或右键取消）'};

function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,6000);}
async function api(path,body={}){
  const response=await fetch('/api/v1/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});
  if(response.status===401){stopAll();location.replace('/');throw new Error('登录已失效');}
  if(!response.ok){const data=await response.json().catch(()=>({}));throw new Error(typeof data.detail==='string'?data.detail:'操作失败');}
  return response;
}
async function action(button,callback){if(button.disabled)return;button.disabled=true;button.dataset.busy='1';try{await callback();}catch(error){toast(error.message);}finally{button.dataset.busy='';button.disabled=false;}}
function socket(channel){const ws=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/ws/'+channel,[channel==='video'?'console.video.v2':'console.v1']);ws.binaryType='arraybuffer';ws.onopen=()=>ws.send(JSON.stringify({csrf}));return ws;}

// Seconds on the Pi's clock, estimated from the newest stamped status packet.
function piNow(){return clockOffset==null?null:Date.now()/1000+clockOffset;}
function ageOf(stamp){const now=piNow();return now==null||!Number.isFinite(stamp)?NaN:Math.max(0,now-stamp);}

// ---- live feed
function connectNav(){
  if(stopped||navSocket||document.hidden)return;
  const ws=socket('nav');navSocket=ws;
  ws.onmessage=e=>{if(navSocket!==ws)return;try{handlePacket(parseNavPacket(e.data));}catch(error){console.warn(error);}};
  ws.onclose=()=>{if(navSocket===ws)navSocket=null;clearTimeout(navRetry);if(!stopped)navRetry=setTimeout(connectNav,2000);};
}
function handlePacket({meta,payload}){
  if(Number.isFinite(meta.stamp)&&(meta.kind==='state'||meta.kind==='agent'))clockOffset=meta.stamp-Date.now()/1000;
  if(meta.kind==='map'){
    // A new container's map can arrive before its first state packet: keep it until then.
    if(sameSession(meta.session,nav.state?.session))loadMap(meta,payload);else nav.nextMap={meta,payload};
  }
  else if(meta.kind==='pose'&&Number.isFinite(meta.x)&&Number.isFinite(meta.y)&&Number.isFinite(meta.yaw)){nav.pose=meta;pushTrail(meta);}
  else if(meta.kind==='scan')nav.scan=Array.isArray(meta.points)?meta.points:[];
  else if(meta.kind==='plan')nav.plan=Array.isArray(meta.points)?meta.points:[];
  else if(meta.kind==='state'){
    // A new container run: the cached map, trail and scan belong to the previous one.
    const previous=nav.state?.session??nav.map?.session;
    if(!sameSession(previous,meta.session)){resetMap();nav.pose=null;}
    nav.state=meta;
    const next=nav.nextMap;
    if(next&&sameSession(next.meta.session,meta.session)){nav.nextMap=null;loadMap(next.meta,next.payload);}
  }
  else if(meta.kind==='agent')nav.agent=meta;
  else if(meta.kind==='log')addLog(meta);
  dirty=true;
}
function pushTrail(pose){const last=nav.trail[nav.trail.length-1];if(!last||Math.hypot(pose.x-last[0],pose.y-last[1])>0.03){nav.trail.push([pose.x,pose.y]);if(nav.trail.length>TRAIL_MAX)nav.trail.shift();}}
async function loadMap(meta,payload){
  const generation=++mapGeneration;let bitmap=null;
  try{
    bitmap=await createImageBitmap(new Blob([payload],{type:'image/png'}),{colorSpaceConversion:'none',premultiplyAlpha:'none'});
    if(generation!==mapGeneration)return;
    const canvas=document.createElement('canvas');canvas.width=bitmap.width;canvas.height=bitmap.height;
    const context=canvas.getContext('2d',{willReadFrequently:true});context.drawImage(bitmap,0,0);
    const image=context.getImageData(0,0,canvas.width,canvas.height);recolorMap(image.data,MAP_COLORS);context.putImageData(image,0,0);
    const first=!nav.map;nav.map=meta;nav.mapImage=canvas;$('#map-empty').hidden=true;
    if(fitPending||first){fitPending=false;resize();view=fitView(view,mapBounds(meta));if(follow&&nav.pose){view.cx=nav.pose.x;view.cy=nav.pose.y;}}
    dirty=true;
  }catch(error){console.warn('地图解码失败',error);}finally{bitmap?.close();}
}

// ---- canvas
const stage=$('#map-stage'),canvas=$('#map-canvas'),context=canvas.getContext('2d');
function resize(){const rect=stage.getBoundingClientRect(),ratio=window.devicePixelRatio||1;if(rect.width!==view.width||rect.height!==view.height||ratio!==dpr){dpr=ratio;canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);view={...view,width:rect.width,height:rect.height};dirty=true;}}
function polyline(points,color,width,dash=[]){if(points.length<2)return;context.beginPath();points.forEach(([x,y],i)=>{const [sx,sy]=worldToScreen(view,x,y);if(i)context.lineTo(sx,sy);else context.moveTo(sx,sy);});context.strokeStyle=color;context.lineWidth=width;context.setLineDash(dash);context.lineJoin='round';context.stroke();context.setLineDash([]);}
function drawGrid(){
  const corners=[[0,0],[view.width,0],[0,view.height],[view.width,view.height]].map(([sx,sy])=>screenToWorld(view,sx,sy));
  const x0=Math.min(...corners.map(c=>c[0])),x1=Math.max(...corners.map(c=>c[0])),y0=Math.min(...corners.map(c=>c[1])),y1=Math.max(...corners.map(c=>c[1]));
  if(x1-x0>200||y1-y0>200)return;
  context.strokeStyle='rgba(32,52,45,.12)';context.lineWidth=1;context.beginPath();
  for(let x=Math.ceil(x0);x<=x1;x++){context.moveTo(...worldToScreen(view,x,y0));context.lineTo(...worldToScreen(view,x,y1));}
  for(let y=Math.ceil(y0);y<=y1;y++){context.moveTo(...worldToScreen(view,x0,y));context.lineTo(...worldToScreen(view,x1,y));}
  context.stroke();
}
function drawRobot(pose,stale){
  const [sx,sy]=worldToScreen(view,pose.x,pose.y),r=Math.max(7,ROBOT_RADIUS*view.scale),heading=pose.yaw+(view.rot||0),c=Math.cos(heading),s=Math.sin(heading);
  context.beginPath();context.arc(sx,sy,r,0,Math.PI*2);context.fillStyle=stale?'rgba(150,160,156,.45)':'rgba(201,233,160,.6)';context.fill();context.lineWidth=2;context.strokeStyle=stale?'#7d8a84':'#16705b';context.stroke();
  const tip=[sx+c*r*1.35,sy-s*r*1.35],left=[sx+(-s)*r*0.55,sy-(c)*r*0.55],right=[sx-(-s)*r*0.55,sy+(c)*r*0.55];
  context.beginPath();context.moveTo(...tip);context.lineTo(...left);context.lineTo(...right);context.closePath();context.fillStyle=stale?'#7d8a84':'#16705b';context.fill();
}
// A target pose: dot, footprint circle and heading arrow.
function drawTarget(target,color){
  const p={...target,yaw:target.yaw+(view.rot||0)};
  const [sx,sy]=worldToScreen(view,p.x,p.y),len=Math.max(26,0.35*view.scale),c=Math.cos(p.yaw),s=Math.sin(p.yaw),ex=sx+c*len,ey=sy-s*len,head=10,spread=0.5;
  context.strokeStyle=color;context.fillStyle=color;
  context.beginPath();context.arc(sx,sy,Math.max(8,ROBOT_RADIUS*view.scale),0,Math.PI*2);context.setLineDash([4,4]);context.lineWidth=1.5;context.stroke();context.setLineDash([]);
  context.beginPath();context.arc(sx,sy,5,0,Math.PI*2);context.fill();
  context.beginPath();context.moveTo(sx,sy);context.lineTo(ex,ey);context.lineWidth=3;context.stroke();
  context.beginPath();context.moveTo(ex,ey);context.lineTo(ex-head*Math.cos(p.yaw-spread),ey+head*Math.sin(p.yaw-spread));context.lineTo(ex-head*Math.cos(p.yaw+spread),ey+head*Math.sin(p.yaw+spread));context.closePath();context.fill();
}
function activeGoal(){
  const job=nav.agent?.navd?.job;if(!job||job.state!=='running')return null;
  const target=job.op==='goal'?job.detail:job.op==='explore'||job.op==='patrol'?job.result?.target:null;
  return target&&Number.isFinite(target.x)?target:null;
}
// Without a chosen heading: a goal faces away from the car, a pose keeps the current one.
function fallbackYaw(x,y,kind){
  const pose=nav.pose;if(!pose)return 0;
  if(kind==='goal'&&Math.hypot(x-pose.x,y-pose.y)>0.05)return Math.atan2(y-pose.y,x-pose.x);
  return pose.yaw;
}
function headingTarget(a){return arrowPose(a.x0,a.y0,a.x1,a.y1,fallbackYaw(a.x0,a.y0,'pose'),12/view.scale);}
// Where the menu was opened: a small orange ring and cross.
function drawMark(p){
  const [sx,sy]=worldToScreen(view,p.x,p.y);context.strokeStyle='#d9822b';context.lineWidth=2;
  context.beginPath();context.arc(sx,sy,7,0,Math.PI*2);context.moveTo(sx-11,sy);context.lineTo(sx+11,sy);context.moveTo(sx,sy-11);context.lineTo(sx,sy+11);context.stroke();
}
function draw(){
  requestAnimationFrame(draw);resize();
  const stale=nav.pose&&!(ageOf(nav.pose.stamp)<POSE_STALE_S);
  if(follow&&nav.pose&&(view.cx!==nav.pose.x||view.cy!==nav.pose.y)){view={...view,cx:nav.pose.x,cy:nav.pose.y};dirty=true;}
  if(!dirty)return;dirty=false;
  context.setTransform(dpr,0,0,dpr,0,0);context.clearRect(0,0,view.width,view.height);
  if(nav.mapImage&&nav.map){
    // Image pixel (i, j) is world (minX + i*res, maxY - j*res); map it with one affine.
    const b=mapBounds(nav.map),res=nav.map.resolution,[ox,oy]=worldToScreen(view,b.minX,b.maxY),[ux,uy]=worldToScreen(view,b.minX+res,b.maxY),[vx,vy]=worldToScreen(view,b.minX,b.maxY-res);
    context.save();context.setTransform(dpr*(ux-ox),dpr*(uy-oy),dpr*(vx-ox),dpr*(vy-oy),dpr*ox,dpr*oy);
    context.imageSmoothingEnabled=view.scale*res<2;context.drawImage(nav.mapImage,0,0);context.restore();
  }
  if(layers.grid)drawGrid();
  if(layers.trail)polyline(nav.trail,'rgba(22,112,91,.38)',2);
  if(layers.plan)polyline(nav.plan,'#2a7de1',3,[8,5]);
  if(layers.scan&&nav.scan.length){context.fillStyle='#e0533d';const size=Math.max(2,Math.min(4,0.03*view.scale));for(const [x,y] of nav.scan){const [sx,sy]=worldToScreen(view,x,y);context.fillRect(sx-size/2,sy-size/2,size,size);}}
  const goal=activeGoal();if(goal)drawTarget(goal,'#2a7de1');
  if(pendingTarget)drawTarget(pendingTarget,pendingTarget.tool==='pose'?'#16705b':'#d9822b');
  if(arrow)drawTarget(headingTarget(arrow),'#16705b');
  if(menuPoint)drawMark(menuPoint);
  drawRoute();
  if(nav.pose)drawRobot(nav.pose,stale);
  const meters=scaleBar(view.scale);$('#map-scale i').style.width=Math.round(meters*view.scale)+'px';$('#map-scale b').textContent=meters>=1?meters+' m':Math.round(meters*100)+' cm';
}
function setFollow(value){follow=value;$('#follow').setAttribute('aria-pressed',String(value));dirty=true;}
const pointers=new Map();let pinch=null;
stage.addEventListener('wheel',e=>{e.preventDefault();const rect=stage.getBoundingClientRect();view=zoomAt(view,e.clientX-rect.left,e.clientY-rect.top,Math.exp(-e.deltaY*0.0015));if(follow&&nav.pose)setFollow(false);dirty=true;},{passive:false});
function stagePoint(e){const rect=stage.getBoundingClientRect();return screenToWorld(view,e.clientX-rect.left,e.clientY-rect.top);}
function cancelLongPress(){if(longPress){clearTimeout(longPress.timer);longPress=null;}}
stage.addEventListener('pointerdown',e=>{
  if(e.target.closest('#map-menu'))return;
  closeMenu();
  stage.setPointerCapture(e.pointerId);pointers.set(e.pointerId,[e.clientX,e.clientY]);
  if(pointers.size===2){cancelLongPress();const [a,b]=[...pointers.values()];pinch={distance:Math.hypot(a[0]-b[0],a[1]-b[1])};return;}
  if(tool==='heading'){if(e.button===0&&arrow){[arrow.x1,arrow.y1]=stagePoint(e);arrow.id=e.pointerId;dirty=true;}return;}
  if(e.pointerType==='touch'){const x=e.clientX,y=e.clientY;cancelLongPress();longPress={id:e.pointerId,x,y,timer:setTimeout(()=>{longPress=null;pointers.clear();pinch=null;openMenu(x,y);},550)};}
});
stage.addEventListener('pointermove',e=>{
  if(tool==='heading'&&arrow&&(e.pointerType==='mouse'||arrow.id===e.pointerId)){[arrow.x1,arrow.y1]=stagePoint(e);dirty=true;}
  const previous=pointers.get(e.pointerId);if(!previous)return;pointers.set(e.pointerId,[e.clientX,e.clientY]);
  if(longPress&&longPress.id===e.pointerId&&Math.hypot(e.clientX-longPress.x,e.clientY-longPress.y)>10)cancelLongPress();
  if(tool==='heading')return;
  if(pointers.size===1){const dx=e.clientX-previous[0],dy=e.clientY-previous[1];if(Math.abs(dx)+Math.abs(dy)<1)return;setFollow(false);view=panBy(view,dx,dy);dirty=true;}
  else if(pointers.size===2&&pinch){const [a,b]=[...pointers.values()],distance=Math.hypot(a[0]-b[0],a[1]-b[1]),rect=stage.getBoundingClientRect();if(distance>0&&pinch.distance>0){view=zoomAt(view,(a[0]+b[0])/2-rect.left,(a[1]+b[1])/2-rect.top,distance/pinch.distance);pinch.distance=distance;dirty=true;}}
});
for(const kind of ['pointerup','pointercancel','lostpointercapture'])stage.addEventListener(kind,e=>{
  const had=pointers.delete(e.pointerId);if(pointers.size<2)pinch=null;
  if(longPress&&longPress.id===e.pointerId)cancelLongPress();
  if(tool==='heading'&&kind==='pointerup'&&had&&arrow&&arrow.id===e.pointerId)finishHeading();
});
stage.addEventListener('contextmenu',e=>{
  e.preventDefault();cancelLongPress();
  if(tool==='heading'){setTool('view');return;}
  openMenu(e.clientX,e.clientY);
});
stage.addEventListener('dblclick',e=>{if(tool!=='view')return;const rect=stage.getBoundingClientRect();view=zoomAt(view,e.clientX-rect.left,e.clientY-rect.top,1.6);dirty=true;});
$('#zoom-in').onclick=()=>{view=zoomAt(view,view.width/2,view.height/2,1.4);dirty=true;};
$('#zoom-out').onclick=()=>{view=zoomAt(view,view.width/2,view.height/2,1/1.4);dirty=true;};
$('#fit-map').onclick=()=>{if(nav.map){setFollow(false);view=fitView(view,mapBounds(nav.map));dirty=true;}else toast('还没有收到地图');};
$('#follow').onclick=()=>{if(!nav.pose){toast('还没有收到小车位置');return;}setFollow(!follow);};
for(const name of Object.keys(layers)){const box=$('#layer-'+name);box.checked=layers[name];box.onchange=()=>{layers[name]=box.checked;dirty=true;};}
$('#clear-trail').onclick=()=>{nav.trail=[];dirty=true;};

// ---- facts and status (refreshed twice a second)
function setText(id,text,level){const element=$(id);element.textContent=text;element.className=level||'';}
function renderFacts(){
  const state=nav.state,map=nav.map,pose=nav.pose;
  const mode=modeText(state);$('#nav-mode').textContent=mode;$('#nav-mode').className='badge '+(state?'':'neutral');
  $('#map-caption').textContent=map?map.width+' × '+map.height+' 格 · '+Math.round(map.resolution*100)+' cm/格':'等待地图';
  setText('#fact-map',map?formatAge(ageOf(map.stamp)):'—');setText('#fact-area',areaText(map));
  setText('#fact-pose',pose?pose.x.toFixed(2)+', '+pose.y.toFixed(2)+' m · '+Math.round(pose.yaw*180/Math.PI)+'°':'—');
  const poseAge=pose?ageOf(pose.stamp):NaN,stale=pose&&!(poseAge<POSE_STALE_S);
  setText('#fact-pose-age',pose?formatAge(poseAge):'—',stale?'fault':'');
  setText('#fact-scan',state?(state.scan_hz>0?state.scan_hz.toFixed(1)+' Hz':'无激光数据'):'—',state&&!(state.scan_hz>0)?'fault':'');
  const health=healthText(state);setText('#fact-health',health.text,health.level==='good'?'good':health.level==='fault'?'fault':'');
  $('#map-stale').hidden=!stale;
  const agent=nav.agent,fault=faultText(agent?.fault);
  setText('#status-fault',fault||'无',fault?'fault':'');
  setText('#status-owned',agent?(agent.owned?'导航正在控制底盘':'未占用'):'—');
  const stateAge=state?ageOf(state.stamp):NaN;
  setText('#status-relay',!navSocket?'未连接':!agent?'等待 Pi 代理':!agent.relay?'导航容器未连接':stateAge<5?'正常 · 导航容器在线':'导航容器无数据',navSocket&&agent?.relay&&stateAge<5?'good':'fault');
  const banner=$('#nav-banner');
  if(!lastStatus.online){banner.className='notice';banner.textContent='老三尚未连接或数据已过期，地图暂停更新。';}
  else if(!navSocket){banner.className='notice';banner.textContent='正在连接地图数据…';}
  else if(agent&&!agent.relay){banner.className='notice';banner.textContent=/^(running|starting)/.test(String(agent.navd?.container||''))?'导航容器正在启动，大约 30 秒后出现地图…':'导航容器没有运行。点右侧「开始建图」，或选一张已保存的地图「加载并定位」。';}
  else if(state?.mode==='localization'&&!(poseAge<POSE_STALE_S)){banner.className='notice';banner.textContent='还不知道老三在这张图上的位置：在地图上右键（手机长按）老三所在的地方，选「把老三的位置设在这里」，再点一下车头朝向。';}
  else if(fault){banner.className='notice';const age=formatAge(ageOf(agent.fault.at));banner.textContent=fault+(age==='—'?'':'（'+age+'）')+'。下次出发时自动解除。';}
  else if(stale){banner.className='notice';banner.textContent='位姿数据中断，地图上显示的是最后已知位置。';}
  else if(activeGoal()){banner.className='notice good';banner.textContent=({explore:'老三正在自动探索建图，蓝色箭头是这一段的目标',patrol:'老三正在巡航，蓝色箭头是下一个巡航点'}[nav.agent?.navd?.job?.op]||'老三正在自主导航去目标点（蓝色箭头）')+' · 随时可点「取消导航」或「紧急停止」。';}
  else{banner.className='notice good';banner.textContent=(state?.mode==='mapping'?'建图中':'定位导航中')+' · 地图、激光和位置实时更新。';}
  $('#last-update').textContent=state?'导航数据 '+formatAge(stateAge):'等待数据';
  loadRotation();
  renderControls();
}

// ---- display rotation ("摆正"), remembered per map in this browser
function rotationKey(){const state=nav.state;return 'nav-rotation:'+(state?.mode==='localization'&&state.map?state.map:'mapping');}
function readRotation(key){try{return Number(localStorage.getItem(key))||0;}catch{return 0;}}
function loadRotation(){const key=rotationKey();if(key===rotationFor)return;rotationFor=key;setRotation(readRotation(key)*Math.PI/180,false);}
function setRotation(radians,save=true){
  view={...view,rot:Math.atan2(Math.sin(radians),Math.cos(radians))};dirty=true;
  if(save){try{localStorage.setItem(rotationFor||rotationKey(),String(Math.round(view.rot*180/Math.PI)));}catch{}}
}
function moveRotation(from,to,keep=false){
  try{const value=localStorage.getItem('nav-rotation:'+from);if(value!==null){localStorage.setItem('nav-rotation:'+to,value);if(!keep)localStorage.removeItem('nav-rotation:'+from);}}catch{}
}
function wallCells(){
  const image=nav.mapImage,data=image.getContext('2d',{willReadFrequently:true}).getImageData(0,0,image.width,image.height).data,cells=[];
  for(let j=0;j<image.height;j++)for(let i=0;i<image.width;i++){const k=(j*image.width+i)*4;if(data[k+3]===255&&data[k]<128)cells.push([i,image.height-1-j]);}
  return cells;
}
$('#rotate-left').onclick=()=>setRotation((view.rot||0)+Math.PI/12);
$('#rotate-right').onclick=()=>setRotation((view.rot||0)-Math.PI/12);
$('#straighten').onclick=()=>{
  if(!nav.mapImage){toast('还没有收到地图');return;}
  const before=view.rot||0,angle=dominantAngle(wallCells());setRotation(-angle);
  toast(Math.abs(Math.sin(before+angle))<0.01?'地图已经是正的':'已按墙的方向摆正'+(Math.abs(angle)>=0.01?'（墙原来偏 '+Math.round(angle*180/Math.PI)+'°）':''));
};

// ---- controls
function renderControls(){
  const agent=nav.agent,navd=agent?.navd,online=!!lastStatus.online&&!!navSocket&&!!agent,job=navd?.job;
  const busy=job?.state==='running',running=containerRunning(navd),navdUp=online&&!!navd?.online;
  const container=containerText(online?navd:null);
  $('#ctl-container').textContent=online?container.text:'—';$('#ctl-mode').textContent=online?navdModeText(navd):'—';
  const badge=$('#container-badge');badge.textContent=!navdUp?'不可用':busy?'任务进行中':running?'可以操作':'未运行';badge.className='badge '+(navdUp&&running&&!busy?'':navdUp?'neutral':'warning');
  const line=$('#ctl-job');line.textContent=!lastStatus.online?'老三离线':!navdUp?(navd?.error||'等待导航服务'):jobText(job,piNow());
  line.className='job-line'+(busy?' running':!navdUp||job?.state==='failed'?' fault':job?.state==='ok'?' good':'');
  const ready=navdUp&&!busy,mapping=running&&navd.mode==='mapping';
  $('#start-mapping').disabled=!ready;
  $('#save-map').disabled=!(ready&&mapping&&nav.map&&agent.relay);
  const exploring=exploreBlocker(menuState());$('#explore').disabled=!!exploring;
  $('#explore-hint').textContent=busy&&job.op==='explore'?'自动探索中，点「取消导航」可随时停下':exploring&&mapping?exploring:'';
  $('#localize').disabled=!(ready&&$('#map-select').value);
  const menu=menuState();
  if(tool==='heading'&&poseBlocker(menu))setTool('view');
  if(!$('#map-menu').hidden){setItem('goal',goalBlocker(menu));setItem('pose',poseBlocker(menu));setItem('route',routeBlocker(menu,route.waypoints.length));}
  renderRoute(menu);
  $('#cancel-nav').disabled=!lastStatus.online;
  if(navdUp){
    const key=job?job.id+':'+job.state:'';
    if(!jobSeen){jobSeen=true;lastJobKey=key;}
    else if(key!==lastJobKey){lastJobKey=key;if(job&&job.state!=='running'){toast(jobText(job,NaN));if(job.op==='save'&&job.state==='ok')loadMaps();if(job.op==='patrol')loadReports();}dirty=true;}
  }
  const board=lastStatus.state?.board||{},telemetry=board.telemetry||{},driving=busy&&job.op==='goal';
  const wheels=!!lastStatus.online&&board.wheels_ready&&!board.estopped;
  $('#stick-wheels').setAttribute('aria-disabled',String(!wheels||driving));
  $('#stick-gimbal').setAttribute('aria-disabled',String(!(lastStatus.online&&board.gimbal_ready&&!board.estopped)));
  const gate=$('#gate-wheels');gate.textContent=board.estopped?'急停锁定':driving?'导航中':wheels?'可以控制':!lastStatus.online?'设备离线':board.control_qualified?'等待硬件恢复':'硬件待检查';gate.className='badge '+(wheels&&!driving?'':'warning');
  $('#feedback-wheels').textContent=driving?'正在自主导航；要手动开，先点「取消导航」。':wheels&&Number.isFinite(telemetry.L)?'速度上限 '+wheelSpeed().toFixed(2)+' m/s（在设备总览页调）· 左 '+telemetry.L.toFixed(2)+' / 右 '+telemetry.R.toFixed(2):board.gate_reason||'支持触摸、鼠标与键盘';
  $('#reset-estop').hidden=!board.estopped;
}
function setTool(name){
  tool=name;if(name==='view')arrow=null;stage.dataset.tool=name;
  const hint=$('#tool-hint');hint.textContent=TOOL_HINT[name];hint.classList.toggle('active',name!=='view');
  if(name!=='view'&&follow)setFollow(false);
  dirty=true;
}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('#nav-dialog').open){closeMenu();if(tool!=='view')setTool('view');}});

// ---- map context menu
function menuState(){const agent=nav.agent;return {online:!!lastStatus.online&&!!navSocket&&!!agent,navd:agent?.navd,relay:!!agent?.relay,hasMap:!!nav.map,poseFresh:!!nav.pose&&ageOf(nav.pose.stamp)<POSE_STALE_S};}
const MENU_HINT={goal:'老三自己规划路线开过去',pose:'选好后再点一下车头朝向',route:'按顺序巡航到这里'};
function setItem(action,reason){const item=$('#map-menu [data-action='+action+']');item.disabled=!!reason;item.querySelector('small').textContent=reason||MENU_HINT[action];}
function openMenu(clientX,clientY){
  const rect=stage.getBoundingClientRect(),sx=clientX-rect.left,sy=clientY-rect.top,[x,y]=screenToWorld(view,sx,sy);
  menuPoint={x,y};const menu=$('#map-menu'),state=menuState();
  setItem('goal',goalBlocker(state));setItem('pose',poseBlocker(state));setItem('route',routeBlocker(state,route.waypoints.length));
  menu.hidden=false;
  menu.style.left=Math.max(8,Math.min(sx,view.width-menu.offsetWidth-8))+'px';
  menu.style.top=Math.max(8,Math.min(sy,view.height-menu.offsetHeight-8))+'px';
  menu.querySelector('button:not(:disabled)')?.focus({preventScroll:true});
  dirty=true;
}
function closeMenu(){const menu=$('#map-menu');if(menu.hidden)return;menu.hidden=true;menuPoint=null;dirty=true;}
$('#map-menu').addEventListener('click',e=>{
  const item=e.target.closest('button[data-action]');if(!item||item.disabled||!menuPoint)return;
  const p=menuPoint;closeMenu();
  if(item.dataset.action==='goal')runTarget('goal',{x:p.x,y:p.y,yaw:fallbackYaw(p.x,p.y,'goal')});
  else if(item.dataset.action==='route')addWaypoint(p.x,p.y);
  else{setTool('heading');arrow={id:null,x0:p.x,y0:p.y,x1:p.x,y1:p.y};dirty=true;}
});
$('#map-menu').addEventListener('contextmenu',e=>e.preventDefault());
document.addEventListener('pointerdown',e=>{if(!e.target.closest('#map-menu')&&!stage.contains(e.target))closeMenu();});
window.addEventListener('blur',closeMenu);

// A modal question; resolves to true / the typed text, or null when cancelled.
function ask({title,text,confirm='确认',danger=false,input=null,validate=null}){
  const dialog=$('#nav-dialog');if(dialog.open)dialog.close();
  $('#nav-dialog-title').textContent=title;$('#nav-dialog-text').textContent=text;$('#nav-dialog-error').textContent='';
  const field=$('#nav-dialog-field'),box=$('#nav-dialog-input'),ok=$('#nav-dialog-ok');
  field.hidden=input==null;box.value=input??'';ok.textContent=confirm;ok.className=danger?'danger':'primary';
  return new Promise(resolve=>{
    dialogPending={resolve,validate,input:input!=null};dialog.showModal();
    if(input!=null){box.focus();box.select();}else ok.focus();
  });
}
$('#nav-dialog-form').addEventListener('submit',e=>{
  e.preventDefault();const pending=dialogPending;if(!pending)return;
  const value=pending.input?$('#nav-dialog-input').value.trim():true;
  const error=pending.validate?pending.validate(value):'';
  if(error){$('#nav-dialog-error').textContent=error;return;}
  dialogPending=null;$('#nav-dialog').close();pending.resolve(value);
});
$('#nav-dialog-cancel').onclick=()=>$('#nav-dialog').close();
$('#nav-dialog').addEventListener('close',()=>{const pending=dialogPending;dialogPending=null;pending?.resolve(null);});

async function navCommand(body){return (await api('nav/command',body)).json();}
async function guarded(button,callback){if(button.disabled)return;button.disabled=true;try{await callback();}catch(error){toast(error.message);}finally{renderControls();}}
function resetMap(){mapGeneration++;nav.map=null;nav.mapImage=null;nav.trail=[];nav.scan=[];nav.plan=[];fitPending=true;$('#map-empty').hidden=false;dirty=true;}
const fixed=(value,digits)=>Number(value.toFixed(digits));

// What the map shows under a world point: free, wall, unknown or outside the map.
function cellKind(x,y){
  if(!nav.map||!nav.mapImage)return 'outside';
  const cell=mapCell(nav.map,x,y);if(!cell)return 'outside';
  const pixel=nav.mapImage.getContext('2d',{willReadFrequently:true}).getImageData(cell[0],cell[1],1,1).data;
  return pixel[3]===0?'unknown':pixel[0]<128?'wall':'free';
}
function finishHeading(){const target=headingTarget(arrow);setTool('view');runTarget('pose',target);}
// Metres from a map point to the nearest wall within 0.5 m (Infinity when farther).
const WALL_WARN_M=0.35;
function wallDistance(x,y){
  if(!nav.map||!nav.mapImage)return Infinity;
  const cell=mapCell(nav.map,x,y);if(!cell)return Infinity;
  const reach=Math.ceil(0.5/nav.map.resolution),c0=Math.max(0,cell[0]-reach),r0=Math.max(0,cell[1]-reach);
  const w=Math.min(nav.map.width,cell[0]+reach+1)-c0,h=Math.min(nav.map.height,cell[1]+reach+1)-r0;
  const pixels=nav.mapImage.getContext('2d',{willReadFrequently:true}).getImageData(c0,r0,w,h).data;
  return nearestWall(pixels,w,h,cell[0]-c0,cell[1]-r0,reach)*nav.map.resolution;
}
async function runTarget(kind,target){
  pendingTarget={...target,tool:kind};dirty=true;
  try{if(kind==='goal')await confirmGoal(target);else if(kind==='pose')await confirmPose(target);}
  catch(error){toast(error.message);}
  finally{pendingTarget=null;dirty=true;}
}
// Nav2 plans only through known free space with room for the car (allow_unknown false).
const REACH_CLEAR_M=0.20;
function detourFor(target){
  if(!nav.map||!nav.mapImage||!nav.pose)return null;
  const start=mapCell(nav.map,nav.pose.x,nav.pose.y),goal=mapCell(nav.map,target.x,target.y);
  if(!start||!goal)return null;
  const image=nav.mapImage,pixels=image.getContext('2d',{willReadFrequently:true}).getImageData(0,0,image.width,image.height).data;
  const result=reachCheck(pixels,image.width,image.height,start,goal,Math.ceil(REACH_CLEAR_M/nav.map.resolution));
  if(result.reachable)return null;
  const [x,y]=cellCentre(nav.map,result.cell[0],result.cell[1]);
  return {x,y,yaw:Math.atan2(target.y-y,target.x-x),gap:Math.hypot(target.x-x,target.y-y)};
}
async function confirmGoal(target){
  if(!nav.pose)throw new Error('还没有收到小车位置');
  const detour=detourFor(target);
  if(detour){
    const plan=goalPlan(nav.pose,detour),wall=wallDistance(target.x,target.y);
    if(plan.distance<0.1)throw new Error('这里到不了，老三已经在离它最近的能到的位置了');
    pendingTarget={x:detour.x,y:detour.y,yaw:detour.yaw,tool:'goal'};dirty=true;
    const ok=await ask({title:'这里可能到不了',confirm:'去最近能到的点',text:'地图上找不到从老三到这里、宽度够车通过的已知通道'+(wall<REACH_CLEAR_M?'（目标离墙只有约 '+Math.round(wall*100)+' cm）':'')+'。最近能走到的点（橙色箭头）离目标 '+detour.gap.toFixed(1)+' 米：直线约 '+plan.distance.toFixed(1)+' 米，预计 '+plan.seconds+' 秒左右。\n也可以先遥控或「自动探索」把中间的路建出来。出发前请确认路上没有人、宠物和容易碰倒的东西。'});
    if(!ok)return;
    await navCommand({op:'goal',x:fixed(detour.x,3),y:fixed(detour.y,3),yaw:fixed(detour.yaw,4)});
    setTool('view');toast('老三出发了（去最近能到的点）');return;
  }
  const plan=goalPlan(nav.pose,target);
  if(plan.distance<0.1)throw new Error('目标离老三太近（不到 10 cm）');
  const cell=cellKind(target.x,target.y),wall=wallDistance(target.x,target.y);
  const warning=cell==='wall'?'\n注意：目标点在障碍物上，很可能到不了。':cell!=='free'?'\n注意：目标点在还没探索过的区域，可能规划不出路线。':wall<WALL_WARN_M?'\n注意：目标离障碍只有约 '+Math.round(wall*100)+' cm。老三前方 30 cm 内有障碍时会停下，可能停在目标前面；最好选离墙 40 cm 以上的点。':'';
  const ok=await ask({title:'让老三开到这里？',confirm:'出发',text:'直线约 '+plan.distance.toFixed(1)+' 米，预计 '+plan.seconds+' 秒左右。老三会自己规划路线，遇到障碍会绕开或停下；随时可以点「取消导航」或「紧急停止」。\n请确认周围没有人、宠物和容易碰倒的东西。'+warning});
  if(!ok)return;
  await navCommand({op:'goal',x:fixed(target.x,3),y:fixed(target.y,3),yaw:fixed(target.yaw,4)});
  setTool('view');toast('老三出发了');
}
async function confirmPose(target){
  const ok=await ask({title:'把老三的位置设在这里？',confirm:'设置位置',text:'箭头方向是老三车头的朝向。设好后看红色激光点是否贴着墙；对不上就再设一次。'});
  if(!ok)return;
  await navCommand({op:'pose',x:fixed(target.x,3),y:fixed(target.y,3),yaw:fixed(target.yaw,4)});
  setTool('view');toast('位置已发送，定位几秒内会收敛');
}
$('#start-mapping').onclick=()=>guarded($('#start-mapping'),async()=>{
  const navd=nav.agent?.navd,again=containerRunning(navd)&&navd.mode==='mapping';
  const ok=await ask({title:again?'重新开始建图？':'开始建图？',confirm:again?'清空并重新建图':'开始建图',danger:again,
    text:(again?'当前正在建的地图如果还没保存，会被清空。':'导航容器会重启进入建图模式，当前没保存的地图会丢失；已保存的地图不受影响。')+'\n老三不会自己动。启动大约 30 秒，之后用圆盘遥控它把房间走一遍，或在地图上点「去这里」。'});
  if(!ok)return;
  await navCommand({op:'mapping'});resetMap();toast('正在切换到建图模式，大约 30 秒后出现地图');
});
$('#explore').onclick=()=>guarded($('#explore'),async()=>{
  const ok=await ask({title:'让老三自动探索建图？',confirm:'开始探索',text:'老三会自己去还没探索过的地方，一段一段把地图补全：最多 30 段、20 分钟，每段都自己规划路线，遇到障碍会绕开或停下。\n整个过程请留在附近，确认地上没有人、宠物和容易碰倒的东西；随时可以点「取消导航」或「紧急停止」。探索结束后记得「保存地图」。'});
  if(!ok)return;
  await navCommand({op:'explore'});toast('老三开始自动探索');
});
$('#save-map').onclick=()=>guarded($('#save-map'),async()=>{
  overwriteName='';
  const name=await ask({title:'保存地图',confirm:'保存',input:defaultMapName(new Date()),text:'给这张地图起个名字（字母、数字、- 或 _）。地图只保存在老三的 Pi 上。',
    validate:value=>{if(!mapNameValid(value))return '只能用字母、数字、- 或 _，最多 64 个字符';if(maps.some(m=>m.name===value)&&overwriteName!==value){overwriteName=value;return '已有同名地图「'+value+'」，再点一次「保存」会覆盖它';}return '';}});
  if(!name)return;
  await navCommand({op:'save',map:name});moveRotation('mapping',name,true);toast('正在保存地图 '+name);
});
$('#localize').onclick=()=>guarded($('#localize'),async()=>{
  const name=$('#map-select').value;if(!name)return;
  const ok=await ask({title:'加载地图「'+name+'」？',confirm:'加载地图',text:'导航容器会重启并加载这张地图（大约 30 秒），正在建的地图如果没保存会丢失。\n加载后在地图上右键（手机长按）老三所在的地方，选「把老三的位置设在这里」，再点一下车头朝向。'});
  if(!ok)return;
  await navCommand({op:'localize',map:name});resetMap();toast('正在加载地图 '+name);
});
$('#dock-cancel').onclick=()=>$('#cancel-nav').click();
$('#dock-estop').onclick=()=>$('#estop').click();
$('#cancel-nav').onclick=()=>{releaseStick();navCommand({op:'stop'}).then(reply=>toast(reply.navd_error?'已停车；导航服务没有响应：'+reply.navd_error:'已停车并取消导航')).catch(error=>toast(error.message));};

// ---- saved maps
async function loadMaps(){
  if(mapsLoading)return;mapsLoading=true;
  try{
    const response=await fetch('/api/v1/nav/maps');if(response.status===401){stopAll();location.replace('/');return;}
    const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'读取地图列表失败');
    maps=Array.isArray(data.maps)?data.maps:[];defaultMap=typeof data.default==='string'?data.default:null;mapsLoaded=true;renderMapList();
  }catch(error){if(!maps.length)$('#map-select').replaceChildren(new Option(error.message||'读取地图列表失败',''));}
  finally{mapsLoading=false;renderControls();}
}
function renderMapList(){
  const select=$('#map-select'),current=select.value||nav.agent?.navd?.map||defaultMap||'';
  select.replaceChildren(...(maps.length?maps.map(m=>{const option=new Option((m.name===defaultMap?'★ ':'')+mapOptionText(m),m.name);option.disabled=!!m.error;return option;}):[new Option('还没有保存的地图','')]));
  if(maps.some(m=>m.name===current&&!m.error))select.value=current;
  renderMapCards();
}
// "管理地图": thumbnails load only while the list is open (each is a request to the car).
function renderMapCards(){
  const open=$('#map-admin').open;
  $('#map-cards').replaceChildren(...maps.map(m=>{
    const item=document.createElement('li');item.className='map-card';
    const image=document.createElement('img');image.alt='';image.width=64;image.height=64;image.loading='lazy';
    if(open&&!m.error)image.src='/api/v1/nav/maps/'+encodeURIComponent(m.name)+'/preview.png?v='+m.modified;
    const info=document.createElement('div');
    const title=document.createElement('strong');title.textContent=m.name+(m.name===defaultMap?' ★ 默认':'');
    const meta=document.createElement('small');meta.textContent=m.error?'无法读取':mapOptionText(m).split(' · ').slice(1).join(' · ');
    const actions=document.createElement('div');actions.className='map-card-actions';
    for(const [label,handler,danger] of [['设为默认',()=>setDefault(m.name),false],['改名',()=>renameMap(m.name),false],['删除',()=>deleteMap(m.name),true]]){
      const button=document.createElement('button');button.type='button';button.textContent=label;if(danger)button.className='danger';
      button.disabled=label==='设为默认'&&(m.name===defaultMap||!!m.error);button.onclick=()=>guarded(button,handler);actions.append(button);
    }
    info.append(title,meta,actions);item.append(image,info);return item;
  }));
}
$('#map-admin').addEventListener('toggle',()=>{if($('#map-admin').open)renderMapCards();});
async function setDefault(name){await navCommand({op:'default',map:name});toast('已把「'+name+'」设为默认地图');await loadMaps();}
async function renameMap(name){
  const to=await ask({title:'给地图「'+name+'」改名',confirm:'改名',input:name,text:'新名字只能用字母、数字、- 或 _。地点、禁行区和默认设置会跟着改。',
    validate:value=>!mapNameValid(value)?'只能用字母、数字、- 或 _，最多 64 个字符':value===name?'名字没有变化':maps.some(m=>m.name===value)?'已经有叫这个名字的地图了':''});
  if(!to)return;
  await navCommand({op:'rename',map:name,to});moveRotation(name,to);toast('已改名为「'+to+'」');await loadMaps();
}
async function deleteMap(name){
  const ok=await ask({title:'删除地图「'+name+'」？',confirm:'删除',danger:true,text:'这张地图和它的地点、禁行区文件会移到老三 Pi 上的回收区（maps/.trash），需要时可以找回。正在使用的地图不能删除。'});
  if(!ok)return;
  await navCommand({op:'delete',map:name});toast('已删除「'+name+'」（在回收区，可找回）');await loadMaps();
}
$('#refresh-maps').onclick=()=>loadMaps();
$('#map-select').onchange=()=>renderControls();

// ---- joysticks. The knobs stay where they are put and the car keeps driving: the
// page relays every held state ten times a second (no per-command handshake). Stop by
// dragging a knob back to the middle, tapping the middle, 取消导航 or the emergency
// stop; hiding the page or losing the link also stops. The wheels pad sits bottom-left
// of the live picture (keys W/A/S/D), the gimbal pad bottom-right (keys I/J/K/L).
const STREAM_MS=100,DEAD_ZONE=.12,TARGETS=['wheels','gimbal'];
// The speed chosen with the dashboard slider (same 0.07..1.3 m/s band as tvpet_pi.protocol).
function wheelSpeed(){try{const speed=Number(localStorage.getItem('wheels-speed'));return Number.isFinite(speed)&&speed>0?Math.min(1.3,Math.max(.07,speed)):.1;}catch{return .1;}}
function engaged(){return !!(press.wheels||press.gimbal);}
function connectControl(){
  if(stopped||controlSocket)return;
  const ws=socket('control');controlSocket=ws;
  ws.onmessage=e=>{const data=JSON.parse(e.data);if(data.code==='lease'){lease='';if(engaged())acquireLease();return;}if(data.error){toast(data.error);releaseStick();return;}if(data.online===false&&engaged())releaseStick();};
  ws.onclose=()=>{if(controlSocket===ws)controlSocket=null;releaseStick();if(!stopped)setTimeout(connectControl,1500);};
}
function stickMessage(target,state){const message={target,x:state.x,y:state.y,lease,sequence:++controlSequence};if(target==='wheels')message.speed=wheelSpeed();return message;}
function sendSticks(){if(!lease||!controlSocket||controlSocket.readyState!==WebSocket.OPEN)return;lastSend=performance.now();for(const target of TARGETS)if(press[target])controlSocket.send(JSON.stringify(stickMessage(target,press[target])));}
function sendSoon(){if(lease&&performance.now()-lastSend>40)sendSticks();}
function startStream(){clearInterval(streamTimer);streamTimer=setInterval(sendSticks,STREAM_MS);sendSticks();}
function acquireLease(){if(leasing)return leasing;const generation=pressGeneration;leasing=(async()=>{try{const result=await(await api('control/lease')).json();if(generation!==pressGeneration||!engaged()){await api('control/stop').catch(()=>{});return;}lease=result.lease;startStream();}catch(error){toast(error.message);releaseStick();}finally{leasing=null;}})();return leasing;}
// The knob travels to the ring just inside the pad's edge, whatever size the pad is.
function stickRadius(element){return element.getBoundingClientRect().width/2-element.querySelector('.stick-knob').offsetWidth/2-4;}
function knob(target,x,y){const element=$('#stick-'+target),radius=stickRadius(element);element.querySelector('.stick-knob').style.transform=`translate(${x*radius}px,${-y*radius}px)`;element.classList.toggle('engaged',!!(x||y));}
function positionStick(target,element,event){const rect=element.getBoundingClientRect(),radius=stickRadius(element);let x=(event.clientX-rect.left-rect.width/2)/radius,y=-(event.clientY-rect.top-rect.height/2)/radius;const scale=Math.max(1,Math.hypot(x,y));x/=scale;y/=scale;if(Math.hypot(x,y)<DEAD_ZONE){x=0;y=0;}press[target]={x,y};knob(target,x,y);sendSoon();}
function beginStick(element,event){
  const target=element.dataset.target;
  if(element.getAttribute('aria-disabled')==='true'){toast(target==='wheels'?$('#feedback-wheels').textContent||'现在不能遥控':lastStatus.state?.board?.gate_reason||'云台现在不能控制');return;}
  event.preventDefault();if(!engaged())pressGeneration++;dragging=target;
  if(event.pointerId!=null){element.setPointerCapture(event.pointerId);positionStick(target,element,event);}else if(!press[target])press[target]={x:0,y:0};
  if(!lease)acquireLease();
}
function endDrag(element){const target=element.dataset.target;if(dragging!==target)return;dragging=null;const state=press[target];if(state&&!state.x&&!state.y)stopStick(target);}
// Centre one knob and drop it from the relay; the lease ends once nothing is held.
function stopStick(target){if(press[target]){press[target]={x:0,y:0};sendSticks();press[target]=null;}knob(target,0,0);if(dragging===target)dragging=null;if(!engaged())releaseStick();}
function releaseStick(){const had=engaged()||!!lease;pressGeneration++;clearInterval(streamTimer);streamTimer=null;if(lease){for(const target of TARGETS)if(press[target])press[target]={x:0,y:0};sendSticks();}press={wheels:null,gimbal:null};dragging=null;lease='';for(const target of TARGETS)knob(target,0,0);if(had&&csrf)api('control/stop').catch(()=>{});}
for(const element of document.querySelectorAll('.joystick')){
  const target=element.dataset.target;
  element.addEventListener('pointerdown',e=>beginStick(element,e));
  element.addEventListener('pointermove',e=>{if(dragging===target&&press[target])positionStick(target,element,e);});
  for(const kind of ['pointerup','pointercancel','lostpointercapture'])element.addEventListener(kind,()=>endDrag(element));
}
// Keyboard, game-pad style anywhere on the page (keypad.js): W/A/S/D hold the wheels,
// I/J/K/L hold the gimbal; the held keys add up and letting go of the last one
// of a target stops it. Fields and the confirmation dialog keep their keys.
const heldKeys=new Set();
function keyStick(target){const state=keypadVector(heldKeys,target);if(!state){stopStick(target);return;}press[target]=state;knob(target,state.x,state.y);if(dragging===target)dragging=null;sendSoon();}
document.addEventListener('keydown',e=>{const binding=KEY_BINDINGS[e.code];if(!binding||keypadIgnores(e,document))return;e.preventDefault();const target=binding.target;if(!press[target]){beginStick($('#stick-'+target),e);if(!press[target])return;}heldKeys.add(e.code);keyStick(target);});
document.addEventListener('keyup',e=>{const binding=KEY_BINDINGS[e.code];if(!binding||!heldKeys.has(e.code))return;heldKeys.delete(e.code);if(press[binding.target])keyStick(binding.target);});
window.addEventListener('blur',()=>heldKeys.clear());
async function poll(){
  if(stopped)return;
  try{const response=await fetch('/api/v1/status');if(response.status===401){stopAll();location.replace('/');return;}if(!response.ok)throw new Error();lastStatus=await response.json();}catch{lastStatus={online:false};}
  const online=!!lastStatus.online,board=lastStatus.state?.board||{},t=board.telemetry||{};
  $('#online-badge').textContent=online?'老三在线':'老三离线';$('#online-badge').className='badge '+(online?'':'warning');
  $('#sidebar-status').textContent=online?'设备连接正常':'等待设备连接';
  setText('#status-battery',Number.isFinite(t.v)&&t.v>0?t.v.toFixed(2)+' V':'—',Number.isFinite(t.v)&&t.v>0&&t.v<10?'fault':'');
  setText('#status-wheels',Number.isFinite(t.L)&&Number.isFinite(t.R)?t.L.toFixed(3)+' / '+t.R.toFixed(3)+' m/s':'—');
  setText('#status-estop',online?(board.estopped?'已锁定急停':'未锁定'):'—',board.estopped?'fault':'');
  const front=lastStatus.state?.front_camera;
  setText('#status-front',!online||!front?'—':{disabled:'未配置',fault:'故障：'+(front.error||'无法读取'),active:'采集中',available:'已连接',unknown:'未找到设备'}[front.state]||'未知',front?.state==='fault'||front?.state==='unknown'?'fault':'');
  for(const id of ['video-toggle','snapshot'])if(!$('#'+id).dataset.busy)$('#'+id).disabled=!online;
  renderLights(board,online);
  if(!online&&videoSocket)stopVideo();else if(online&&!videoSocket)faceState('idle');
  if(online&&!navSocket)connectNav();
  if(online&&!mapsLoaded)loadMaps();
  if(!online)releaseStick();
  setTimeout(poll,1000);
}

// ---- logs
function addLog(meta){
  const key=[meta.stamp,meta.node,meta.text].join('|');if(logKeys.has(key))return;logKeys.add(key);
  const item=document.createElement('li');item.className='log '+logLevelClass(meta.level);item.dataset.key=key;item.dataset.level=meta.level;
  const time=document.createElement('time');time.textContent=logTime(meta.stamp);
  const node=document.createElement('b');node.textContent=String(meta.node||'');node.title=node.textContent;
  const text=document.createElement('span');text.title=String(meta.text||'');text.textContent=logText(meta.text)+(meta.repeat>0?'（此前 30 秒内又出现 '+meta.repeat+' 次）':'');
  item.append(time,node,text);item.hidden=$('#log-level').value==='warn'&&logLevelClass(meta.level)==='info';
  const list=$('#log-list');list.append(item);
  while(list.children.length>LOG_MAX){logKeys.delete(list.firstElementChild.dataset.key);list.firstElementChild.remove();}
  $('#log-empty').hidden=true;if(!$('#log-pause').checked)list.scrollTop=list.scrollHeight;
}
$('#log-level').onchange=()=>{const warn=$('#log-level').value==='warn';for(const item of $('#log-list').children)item.hidden=warn&&logLevelClass(item.dataset.level)==='info';};
$('#log-clear').onclick=()=>{$('#log-list').replaceChildren();logKeys.clear();$('#log-empty').hidden=false;};

// ---- camera: the dashboard's live video panel (video.js draws the face and pose
// overlays and knows the light messages); only the page's own status and buttons differ.
function videoState(active){$('#video-toggle').textContent=active?'停止视频':'开启视频';$('#video-state').textContent=active?'实时画面':'未预览';$('#video-state').className='badge '+(active?'':'neutral');$('#video-label').textContent=active?'LIVE · '+({front:'前置相机',depth:'深度',video:'云台相机'}[videoSource]):'预览已关闭';$('#video-placeholder').hidden=active;$('#video-frame').hidden=!active;$('#depth-legend').hidden=!(active&&videoSource==='depth');}
function clearVideo(){const canvas=$('#video-frame');canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);videoPending=null;clearTimeout(videoStale);}
function idleFaceText(review){if(!review||review.state==='disabled')return '开启视频后识别家庭成员';if(review.state==='daily_limit')return '今日采样已达上限，明日新加坡时间 00:00 自动恢复';if(review.state==='paused')return '后台本机审核采样已暂停'+(review.error?'：'+review.error:'');return '后台本机审核采样继续运行';}
function faceState(state,faces=[],milliseconds=null){const badge=$('#face-state');badge.textContent={active:'家庭人脸识别',unavailable:'识别服务暂不可用',disabled:'人脸识别未启用',idle:'人脸预览已关闭',waiting:'等待新画面',front:'前置相机',depth:'深度画面'}[state]||'等待新画面';badge.className='badge '+(state==='active'?'':state==='unavailable'?'warning':'neutral');$('#face-detail').textContent=state==='active'?(faces.length?faces.map(face=>face.label).join(' · '):'画面中未检测到人脸')+(milliseconds!=null?' · '+Math.round(milliseconds)+' ms':''):state==='unavailable'?'视频继续显示，正在重试识别':state==='idle'?idleFaceText(lastStatus.pose_review):state==='front'?'前置相机只显示画面，不做人脸和姿态识别':state==='depth'?'颜色表示距离：蓝近红远，黑色是测不到':'';}
function stopVideo(){videoGeneration++;if(videoSocket){const old=videoSocket;videoSocket=null;old.close();}clearVideo();videoState(false);faceState('idle');}
async function paintVideo(){
  if(videoPainting)return;videoPainting=true;
  try{while(videoPending){const {data,generation,source}=videoPending;videoPending=null;let bitmap=null;
    try{const front=source==='front',depth=source==='depth'?parseDepthPacket(data):null,packet=front?{meta:{},jpeg:new Uint8Array(data)}:depth?null:parseVideoPacket(data);
      const meta=packet?packet.meta:{},image=depth?depth.jpeg:packet.jpeg;
      bitmap=await createImageBitmap(new Blob([image],{type:'image/jpeg'}));if(generation!==videoGeneration||!videoSocket)continue;
      const canvas=$('#video-frame'),context=canvas.getContext('2d');canvas.width=bitmap.width;canvas.height=bitmap.height;context.drawImage(bitmap,0,0);videoState(true);
      if(depth){faceState('depth');const [near,far]=depthScaleText(depth.meta);$('#depth-near').textContent=near;$('#depth-far').textContent=far;}
      else if(front)faceState('front');
      else{const faces=Array.isArray(meta.faces)?meta.faces.slice(0,16):[];drawPoses(context,meta.pose,bitmap.width,bitmap.height);drawFaces(context,faces,bitmap.width,bitmap.height);faceState(meta.recognition,faces,meta.inference_ms);}
      $('#frame-detail').textContent=depth?depthText(depth.meta):'新帧 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
      clearTimeout(videoStale);videoStale=setTimeout(()=>{if(generation!==videoGeneration)return;videoGeneration++;clearVideo();videoState(false);$('#video-toggle').textContent='停止视频';$('#video-label').textContent='等待新画面';$('#video-placeholder strong').textContent='视频暂时中断';faceState('waiting');},2000);
    }catch(error){if(generation===videoGeneration){toast(error.message);stopVideo();}}finally{bitmap?.close();}}}
  finally{videoPainting=false;}
}
function startVideo(){
  clearVideo();videoGeneration++;$('#video-placeholder strong').textContent='正在连接摄像头…';faceState('waiting');
  const source=videoSource,ws=socket(source);videoSocket=ws;
  ws.onmessage=e=>{if(videoSocket!==ws)return;videoPending={data:e.data,generation:videoGeneration,source};paintVideo();};
  ws.onclose=()=>{if(videoSocket===ws)stopVideo();};
}
$('#video-toggle').onclick=()=>{
  if(videoSocket){stopVideo();return;}
  if(videoSource==='front'&&lastStatus.state?.front_camera?.state==='disabled'){toast('前置相机还没有配置');return;}
  if(videoSource==='depth'&&lastStatus.state?.depth_camera?.state==='disabled'){toast('深度相机还没有配置');return;}
  startVideo();
};
for(const button of document.querySelectorAll('[data-source]'))button.onclick=()=>{
  if(button.dataset.source===videoSource)return;videoSource=button.dataset.source;
  for(const other of document.querySelectorAll('[data-source]'))other.setAttribute('aria-pressed',String(other===button));
  $('#camera-caption').textContent={front:'前置 Orbbec 彩色相机 · 只显示画面',depth:'前置 Orbbec 深度 · 颜色表示距离',video:'云台 USB 摄像头 · 人脸与姿态识别'}[videoSource];
  $('#camera-format').textContent={front:'640 × 480 · 前置相机',depth:'640 × 400 · 约 5 帧/秒 · 深度',video:'640 × 480 · 人脸与姿态识别'}[videoSource];
  if(videoSocket){stopVideo();startVideo();}
};
$('#snapshot').onclick=()=>action($('#snapshot'),async()=>{const response=await api('snapshot');const url=URL.createObjectURL(await response.blob());const link=document.createElement('a');link.href=url;link.download='老三-'+Date.now()+'.jpg';link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);toast('新照片已下载');});
$('#fullscreen').onclick=()=>$('.video-stage').requestFullscreen?.();
let lightsPending={};
function renderLights(board,online){const now=Date.now();for(const name of ['base','head']){const button=$('#light-'+name),on=lightState(name,board.lights,lightsPending,now);button.setAttribute('aria-pressed',String(on));if(!button.dataset.busy)button.disabled=!online||board.state!=='available';button.title=(name==='base'?'底盘前灯':'云台灯')+(on?' · 已开':' · 已关');}}
for(const name of ['base','head'])$('#light-'+name).onclick=()=>action($('#light-'+name),async()=>{const data=await (await api('control/lights',lightMessage(name,lastStatus.state?.board?.lights))).json();for(const [channel,on] of Object.entries(data.lights||{}))lightsPending[channel]={on,until:Date.now()+LIGHT_HOLD};renderLights(lastStatus.state?.board||{},true);});

// ---- page lifecycle
function stopAll(){stopped=true;releaseStick();stopVideo();clearTimeout(navRetry);if(navSocket){const old=navSocket;navSocket=null;old.close();}if(controlSocket){const old=controlSocket;controlSocket=null;old.close();}}
$('#estop').onclick=()=>{releaseStick();api('control/estop').then(()=>toast('老三已锁定急停')).catch(error=>toast(error.message));};
$('#reset-estop').onclick=()=>api('control/reset').then(()=>toast('急停已解除')).catch(error=>toast(error.message));
window.addEventListener('blur',releaseStick);
$('#logout').onclick=async()=>{const button=$('#logout');button.disabled=true;stopAll();try{await api('auth/logout');}catch(error){toast(error.message);}location.replace('/');};
document.addEventListener('visibilitychange',()=>{if(document.hidden){releaseStick();stopVideo();clearTimeout(navRetry);if(navSocket){const old=navSocket;navSocket=null;old.close();}}else if(!stopped)connectNav();});
window.addEventListener('pagehide',stopAll);
window.addEventListener('resize',()=>{dirty=true;});
(async()=>{
  try{const response=await fetch('/api/v1/auth/session');if(!response.ok){location.replace('/');return;}csrf=(await response.json()).csrf;}
  catch{toast('无法连接 Mini 服务');return;}
  resize();requestAnimationFrame(draw);poll();connectNav();connectControl();setInterval(renderFacts,500);renderFacts();
})();

// ---- patrol route: edited here, saved on the car per map, run by ugv-navd
function routeMap(){return nav.state?.mode==='localization'?nav.state.map||null:null;}
async function loadRoute(name){
  routeLoading=true;
  try{
    const response=await fetch('/api/v1/nav/routes/'+encodeURIComponent(name));if(response.status===401){stopAll();location.replace('/');return;}
    const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'读取巡航路线失败');
    const stored=data.route||{};
    route={map:name,loops:Number.isInteger(stored.loops)?stored.loops:1,waypoints:Array.isArray(stored.waypoints)?stored.waypoints.map(p=>({x:p.x,y:p.y,yaw:p.yaw??0})):[]};
    routeDirty=false;dirty=true;
  }catch(error){toast(error.message);route={map:name,loops:1,waypoints:[]};}
  finally{routeLoading=false;renderControls();}
}
async function loadReports(){
  try{const response=await fetch('/api/v1/nav/reports');if(!response.ok)return;reports=(await response.json()).reports||[];}catch{return;}
  $('#route-report').textContent=reportText(reports[0]);
}
function addWaypoint(x,y){
  const previous=route.waypoints[route.waypoints.length-1]||nav.pose;
  const yaw=previous&&Math.hypot(x-previous.x,y-previous.y)>0.05?Math.atan2(y-previous.y,x-previous.x):0;
  route.waypoints.push({x:fixed(x,3),y:fixed(y,3),yaw:fixed(yaw,4)});routeDirty=true;dirty=true;renderControls();
  toast('已加入第 '+route.waypoints.length+' 个巡航点，记得「保存路线」');
}
function renderRoute(state){
  const current=routeMap();
  if(current&&route.map!==current&&!routeLoading){route={map:current,loops:1,waypoints:[]};loadRoute(current);}
  const count=route.waypoints.length,legs=count*route.loops,usable=!!current&&route.map===current;
  $('#route-badge').textContent=!usable?'—':count?count+' 个点 · '+route.loops+' 圈 · '+legs+' 段'+(routeDirty?' · 未保存':''):'还没有巡航点';
  $('#route-badge').className='badge '+(usable&&count&&!routeDirty?'':'neutral');
  const list=$('#route-list');
  if(list.dataset.version!==JSON.stringify(route)){
    list.dataset.version=JSON.stringify(route);
    list.replaceChildren(...route.waypoints.map((p,i)=>{
      const item=document.createElement('li');const label=document.createElement('span');label.textContent='('+p.x.toFixed(2)+', '+p.y.toFixed(2)+')';item.append(label);
      for(const [text,title,action] of [['↑','往前移',()=>moveWaypoint(i,-1)],['↓','往后移',()=>moveWaypoint(i,1)],['✕','删除这个点',()=>removeWaypoint(i)]]){
        const button=document.createElement('button');button.type='button';button.textContent=text;button.title=title;button.setAttribute('aria-label',title+'（第 '+(i+1)+' 个点）');button.onclick=action;item.append(button);
      }
      return item;
    }));
  }
  if(document.activeElement!==$('#route-loops'))$('#route-loops').value=String(route.loops);
  $('#route-save').disabled=!(usable&&count&&routeDirty);$('#route-clear').disabled=!(usable&&count);
  const blocker=patrolBlocker(state,usable?count:0,routeDirty);$('#patrol-start').disabled=!!blocker;$('#patrol-start').title=blocker;
  $('#route-hint').textContent=!current?'先「加载并定位」一张已保存的地图，再在地图上右键选「加入巡航路线」。':blocker&&count?'开始巡航前：'+blocker+'。':'右键地图选「加入巡航路线」添加巡航点（最多 20 个），老三会按顺序依次开过去。';
}
function moveWaypoint(index,step){const to=index+step;if(to<0||to>=route.waypoints.length)return;const [p]=route.waypoints.splice(index,1);route.waypoints.splice(to,0,p);routeDirty=true;dirty=true;renderControls();}
function removeWaypoint(index){route.waypoints.splice(index,1);routeDirty=true;dirty=true;renderControls();}
$('#route-loops').onchange=()=>{const loops=Math.round(Number($('#route-loops').value));route.loops=Math.min(10,Math.max(1,Number.isFinite(loops)?loops:1));$('#route-loops').value=String(route.loops);routeDirty=true;renderControls();};
$('#route-clear').onclick=()=>guarded($('#route-clear'),async()=>{
  const ok=await ask({title:'清空巡航路线？',confirm:'清空',danger:true,text:'会去掉这张地图上的全部巡航点；点「保存路线」之前，车上保存的路线不变。'});
  if(!ok)return;route.waypoints=[];routeDirty=true;dirty=true;
});
$('#route-save').onclick=()=>guarded($('#route-save'),async()=>{
  await navCommand({op:'route_save',map:route.map,route:{loops:route.loops,waypoints:route.waypoints}});
  routeDirty=false;toast('巡航路线已保存到地图「'+route.map+'」');
});
$('#patrol-start').onclick=()=>guarded($('#patrol-start'),async()=>{
  const count=route.waypoints.length,legs=count*route.loops;
  const ok=await ask({title:'开始巡航？',confirm:'开始巡航',text:'老三会按路线依次开到 '+count+' 个巡航点，共 '+route.loops+' 圈（'+legs+' 段），每段都自己规划路线，遇到障碍会绕开或停下；电量低或某一段没到达就停止。\n请确认路上没有人、宠物和容易碰倒的东西；随时可以点「取消导航」或「紧急停止」。'});
  if(!ok)return;
  await navCommand({op:'patrol'});toast('老三开始巡航');
});
// Numbered waypoints joined in driving order (orange), back to the first when looping.
function drawRoute(){
  if(!route.waypoints.length||route.map!==routeMap())return;
  const points=route.waypoints.map(p=>[p.x,p.y]);if(route.loops>1&&points.length>2)points.push(points[0]);
  polyline(points,'rgba(217,130,43,.8)',2,[6,4]);
  context.font='600 11px sans-serif';context.textAlign='center';context.textBaseline='middle';
  route.waypoints.forEach((p,i)=>{const [sx,sy]=worldToScreen(view,p.x,p.y);context.beginPath();context.arc(sx,sy,9,0,Math.PI*2);context.fillStyle='#d9822b';context.fill();context.fillStyle='white';context.fillText(String(i+1),sx,sy+0.5);});
  context.textAlign='start';context.textBaseline='alphabetic';
}
loadReports();
