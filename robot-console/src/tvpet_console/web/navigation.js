"use strict";
// Map page: live map, scan, pose, path, camera, status and logs. Read-only apart from
// the emergency stop; all data arrives through the Mini (/ws/nav, /ws/video, /api).
const $=selector=>document.querySelector(selector);
const ROBOT_RADIUS=0.17,POSE_STALE_S=3,TRAIL_MAX=300,LOG_MAX=400;
const MAP_COLORS=[[251,252,250,255],[38,58,50,255],[0,0,0,0]];
let csrf='',stopped=false,toastTimer=null,navSocket=null,navRetry=null,mapGeneration=0;
let videoSocket=null,videoGeneration=0,videoPending=null,videoPainting=false,videoStale=null,videoSource='front';
let clockOffset=null,lastStatus={};
const nav={map:null,mapImage:null,pose:null,trail:[],scan:[],plan:[],state:null,agent:null};
let view={cx:0,cy:0,scale:60,width:0,height:0},follow=true,fitPending=true,dirty=true,dpr=1;
const layers={scan:true,trail:true,plan:true,grid:false};
const logKeys=new Set();

function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,6000);}
async function api(path,body={}){
  const response=await fetch('/api/v1/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});
  if(response.status===401){stopAll();location.replace('/');throw new Error('登录已失效');}
  if(!response.ok){const data=await response.json().catch(()=>({}));throw new Error(typeof data.detail==='string'?data.detail:'操作失败');}
  return response;
}
function socket(channel){const ws=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/ws/'+channel,['console.v1']);ws.binaryType='arraybuffer';ws.onopen=()=>ws.send(JSON.stringify({csrf}));return ws;}

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
  if(meta.kind==='map')loadMap(meta,payload);
  else if(meta.kind==='pose'&&Number.isFinite(meta.x)&&Number.isFinite(meta.y)&&Number.isFinite(meta.yaw)){nav.pose=meta;pushTrail(meta);}
  else if(meta.kind==='scan')nav.scan=Array.isArray(meta.points)?meta.points:[];
  else if(meta.kind==='plan')nav.plan=Array.isArray(meta.points)?meta.points:[];
  else if(meta.kind==='state')nav.state=meta;
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
  const [x0,y1]=screenToWorld(view,0,0),[x1,y0]=screenToWorld(view,view.width,view.height);
  if(x1-x0>200)return;
  context.strokeStyle='rgba(32,52,45,.12)';context.lineWidth=1;context.beginPath();
  for(let x=Math.ceil(x0);x<=x1;x++){const [sx]=worldToScreen(view,x,0);context.moveTo(sx,0);context.lineTo(sx,view.height);}
  for(let y=Math.ceil(y0);y<=y1;y++){const [,sy]=worldToScreen(view,0,y);context.moveTo(0,sy);context.lineTo(view.width,sy);}
  context.stroke();
}
function drawRobot(pose,stale){
  const [sx,sy]=worldToScreen(view,pose.x,pose.y),r=Math.max(7,ROBOT_RADIUS*view.scale),c=Math.cos(pose.yaw),s=Math.sin(pose.yaw);
  context.beginPath();context.arc(sx,sy,r,0,Math.PI*2);context.fillStyle=stale?'rgba(150,160,156,.45)':'rgba(201,233,160,.6)';context.fill();context.lineWidth=2;context.strokeStyle=stale?'#7d8a84':'#16705b';context.stroke();
  const tip=[sx+c*r*1.35,sy-s*r*1.35],left=[sx+(-s)*r*0.55,sy-(c)*r*0.55],right=[sx-(-s)*r*0.55,sy+(c)*r*0.55];
  context.beginPath();context.moveTo(...tip);context.lineTo(...left);context.lineTo(...right);context.closePath();context.fillStyle=stale?'#7d8a84':'#16705b';context.fill();
}
function draw(){
  requestAnimationFrame(draw);resize();
  const stale=nav.pose&&!(ageOf(nav.pose.stamp)<POSE_STALE_S);
  if(follow&&nav.pose&&(view.cx!==nav.pose.x||view.cy!==nav.pose.y)){view={...view,cx:nav.pose.x,cy:nav.pose.y};dirty=true;}
  if(!dirty)return;dirty=false;
  context.setTransform(dpr,0,0,dpr,0,0);context.clearRect(0,0,view.width,view.height);
  if(nav.mapImage&&nav.map){const b=mapBounds(nav.map),[x0,y0]=worldToScreen(view,b.minX,b.maxY);context.imageSmoothingEnabled=view.scale*nav.map.resolution<2;context.drawImage(nav.mapImage,x0,y0,(b.maxX-b.minX)*view.scale,(b.maxY-b.minY)*view.scale);}
  if(layers.grid)drawGrid();
  if(layers.trail)polyline(nav.trail,'rgba(22,112,91,.38)',2);
  if(layers.plan)polyline(nav.plan,'#2a7de1',3,[8,5]);
  if(layers.scan&&nav.scan.length){context.fillStyle='#e0533d';const size=Math.max(2,Math.min(4,0.03*view.scale));for(const [x,y] of nav.scan){const [sx,sy]=worldToScreen(view,x,y);context.fillRect(sx-size/2,sy-size/2,size,size);}}
  if(nav.pose)drawRobot(nav.pose,stale);
  const meters=scaleBar(view.scale);$('#map-scale i').style.width=Math.round(meters*view.scale)+'px';$('#map-scale b').textContent=meters>=1?meters+' m':Math.round(meters*100)+' cm';
}
function setFollow(value){follow=value;$('#follow').setAttribute('aria-pressed',String(value));dirty=true;}
const pointers=new Map();let pinch=null;
stage.addEventListener('wheel',e=>{e.preventDefault();const rect=stage.getBoundingClientRect();view=zoomAt(view,e.clientX-rect.left,e.clientY-rect.top,Math.exp(-e.deltaY*0.0015));if(follow&&nav.pose)setFollow(false);dirty=true;},{passive:false});
stage.addEventListener('pointerdown',e=>{stage.setPointerCapture(e.pointerId);pointers.set(e.pointerId,[e.clientX,e.clientY]);if(pointers.size===2){const [a,b]=[...pointers.values()];pinch={distance:Math.hypot(a[0]-b[0],a[1]-b[1])};}});
stage.addEventListener('pointermove',e=>{
  const previous=pointers.get(e.pointerId);if(!previous)return;pointers.set(e.pointerId,[e.clientX,e.clientY]);
  if(pointers.size===1){const dx=e.clientX-previous[0],dy=e.clientY-previous[1];if(Math.abs(dx)+Math.abs(dy)<1)return;setFollow(false);view={...view,cx:view.cx-dx/view.scale,cy:view.cy+dy/view.scale};dirty=true;}
  else if(pointers.size===2&&pinch){const [a,b]=[...pointers.values()],distance=Math.hypot(a[0]-b[0],a[1]-b[1]),rect=stage.getBoundingClientRect();if(distance>0&&pinch.distance>0){view=zoomAt(view,(a[0]+b[0])/2-rect.left,(a[1]+b[1])/2-rect.top,distance/pinch.distance);pinch.distance=distance;dirty=true;}}
});
for(const kind of ['pointerup','pointercancel','lostpointercapture'])stage.addEventListener(kind,e=>{pointers.delete(e.pointerId);if(pointers.size<2)pinch=null;});
stage.addEventListener('dblclick',e=>{const rect=stage.getBoundingClientRect();view=zoomAt(view,e.clientX-rect.left,e.clientY-rect.top,1.6);dirty=true;});
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
  else if(agent&&!agent.relay){banner.className='notice';banner.textContent='导航容器没有运行（ugv-navctl mapping 或 localize 启动后这里会出现地图）。';}
  else if(fault){banner.className='notice';banner.textContent=fault+'。需要重新授权后才能再动。';}
  else if(stale){banner.className='notice';banner.textContent='位姿数据中断，地图上显示的是最后已知位置。';}
  else{banner.className='notice good';banner.textContent=(state?.mode==='mapping'?'建图中':'定位导航中')+' · 地图、激光和位置实时更新。';}
  $('#last-update').textContent=state?'导航数据 '+formatAge(stateAge):'等待数据';
}
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
  if(!online&&videoSocket)stopVideo();
  if(online&&!navSocket)connectNav();
  setTimeout(poll,1000);
}

// ---- logs
function addLog(meta){
  const key=[meta.stamp,meta.node,meta.text].join('|');if(logKeys.has(key))return;logKeys.add(key);
  const item=document.createElement('li');item.className='log '+logLevelClass(meta.level);item.dataset.key=key;item.dataset.level=meta.level;
  const time=document.createElement('time');time.textContent=logTime(meta.stamp);
  const node=document.createElement('b');node.textContent=String(meta.node||'');node.title=node.textContent;
  const text=document.createElement('span');text.textContent=String(meta.text||'')+(meta.repeat>0?'（此前 30 秒内又出现 '+meta.repeat+' 次）':'');
  item.append(time,node,text);item.hidden=$('#log-level').value==='warn'&&logLevelClass(meta.level)==='info';
  const list=$('#log-list');list.append(item);
  while(list.children.length>LOG_MAX){logKeys.delete(list.firstElementChild.dataset.key);list.firstElementChild.remove();}
  $('#log-empty').hidden=true;if(!$('#log-pause').checked)list.scrollTop=list.scrollHeight;
}
$('#log-level').onchange=()=>{const warn=$('#log-level').value==='warn';for(const item of $('#log-list').children)item.hidden=warn&&logLevelClass(item.dataset.level)==='info';};
$('#log-clear').onclick=()=>{$('#log-list').replaceChildren();logKeys.clear();$('#log-empty').hidden=false;};

// ---- camera
function videoState(active){$('#video-toggle').textContent=active?'停止视频':'开启视频';$('#video-state').textContent=active?'实时画面':'未预览';$('#video-state').className='badge '+(active?'':'neutral');$('#video-label').textContent=active?'LIVE · '+(videoSource==='front'?'前置相机':'云台相机'):'预览已关闭';$('#video-placeholder').hidden=active;$('#video-frame').hidden=!active;}
function stopVideo(){videoGeneration++;if(videoSocket){const old=videoSocket;videoSocket=null;old.close();}videoPending=null;clearTimeout(videoStale);videoState(false);}
async function paintVideo(){
  if(videoPainting)return;videoPainting=true;
  try{while(videoPending){const {data,generation}=videoPending;videoPending=null;let bitmap=null;
    try{bitmap=await createImageBitmap(new Blob([data],{type:'image/jpeg'}));if(generation!==videoGeneration||!videoSocket)continue;
      const frame=$('#video-frame');frame.width=bitmap.width;frame.height=bitmap.height;frame.getContext('2d').drawImage(bitmap,0,0);videoState(true);
      $('#frame-detail').textContent='新帧 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});
      clearTimeout(videoStale);videoStale=setTimeout(()=>{if(generation===videoGeneration){$('#video-label').textContent='等待新画面';$('#frame-detail').textContent='视频暂时中断';}},2000);
    }catch(error){if(generation===videoGeneration){toast('视频帧无效');stopVideo();}}finally{bitmap?.close();}}}
  finally{videoPainting=false;}
}
function startVideo(){
  videoGeneration++;const generation=videoGeneration,ws=socket(videoSource);videoSocket=ws;$('#video-placeholder strong').textContent='正在连接摄像头…';
  ws.onmessage=e=>{if(videoSocket!==ws)return;videoPending={data:e.data,generation};paintVideo();};
  ws.onclose=()=>{if(videoSocket===ws)stopVideo();};
}
$('#video-toggle').onclick=()=>{
  if(videoSocket){stopVideo();return;}
  if(!lastStatus.online){toast('老三离线，无法开启视频');return;}
  if(videoSource==='front'&&lastStatus.state?.front_camera?.state==='disabled'){toast('前置相机还没有配置');return;}
  startVideo();
};
for(const button of document.querySelectorAll('[data-source]'))button.onclick=()=>{
  if(button.dataset.source===videoSource)return;videoSource=button.dataset.source;
  for(const other of document.querySelectorAll('[data-source]'))other.setAttribute('aria-pressed',String(other===button));
  $('#camera-caption').textContent=videoSource==='front'?'前置 Orbbec 彩色相机':'云台 USB 摄像头';
  if(videoSocket){stopVideo();startVideo();}
};

// ---- page lifecycle
function stopAll(){stopped=true;stopVideo();clearTimeout(navRetry);if(navSocket){const old=navSocket;navSocket=null;old.close();}}
$('#estop').onclick=()=>api('control/estop').then(()=>toast('老三已锁定急停')).catch(error=>toast(error.message));
$('#logout').onclick=async()=>{const button=$('#logout');button.disabled=true;stopAll();try{await api('auth/logout');}catch(error){toast(error.message);}location.replace('/');};
document.addEventListener('visibilitychange',()=>{if(document.hidden){stopVideo();clearTimeout(navRetry);if(navSocket){const old=navSocket;navSocket=null;old.close();}}else if(!stopped)connectNav();});
window.addEventListener('pagehide',stopAll);
window.addEventListener('resize',()=>{dirty=true;});
(async()=>{
  try{const response=await fetch('/api/v1/auth/session');if(!response.ok){location.replace('/');return;}csrf=(await response.json()).csrf;}
  catch{toast('无法连接 Mini 服务');return;}
  resize();requestAnimationFrame(draw);poll();connectNav();setInterval(renderFacts,500);renderFacts();
})();
