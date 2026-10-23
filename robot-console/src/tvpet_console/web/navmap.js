"use strict";
// Pure helpers for the map page (no DOM): packets, view transforms, colors and wording.
// The relay's map PNG is north up: its first row is the map's highest y.

const NAV_PALETTE=[[245,247,244],[32,52,45],[205,211,208]];
const NAV_MIN_SCALE=8,NAV_MAX_SCALE=600;
// Nav2 cruises at up to 0.10 m/s (bridge MAX_LINEAR 0.12); real runs average lower.
const NAV_AVERAGE_SPEED=0.08;
const NAV_OP_TEXT={mapping:'开始建图',save:'保存地图',localize:'加载地图定位',pose:'设置当前位置',goal:'导航去目标点'};
const NAV_OP_RUNNING={mapping:'正在切换到建图模式',save:'正在保存地图',localize:'正在加载地图',pose:'正在设置位置',goal:'正在导航去目标点'};

function parseNavPacket(buffer){
  const bytes=new Uint8Array(buffer);
  if(bytes.length<4)throw new Error('地图数据格式无效');
  const size=new DataView(buffer).getUint32(0);
  if(size<2||size>65536||4+size>bytes.length)throw new Error('地图数据格式无效');
  const meta=JSON.parse(new TextDecoder().decode(bytes.subarray(4,4+size)));
  if(!meta||typeof meta.kind!=='string')throw new Error('地图数据格式无效');
  return {meta,payload:bytes.subarray(4+size)};
}

function worldToScreen(view,x,y){return [(x-view.cx)*view.scale+view.width/2,view.height/2-(y-view.cy)*view.scale];}
function screenToWorld(view,sx,sy){return [view.cx+(sx-view.width/2)/view.scale,view.cy-(sy-view.height/2)/view.scale];}

function clampScale(scale){return Math.min(NAV_MAX_SCALE,Math.max(NAV_MIN_SCALE,scale));}

// Zoom keeping the world point under (sx, sy) fixed on screen.
function zoomAt(view,sx,sy,factor){
  const [wx,wy]=screenToWorld(view,sx,sy),scale=clampScale(view.scale*factor);
  return {...view,scale,cx:wx-(sx-view.width/2)/scale,cy:wy+(sy-view.height/2)/scale};
}

function mapBounds(meta){
  const [ox,oy]=meta.origin,w=meta.width*meta.resolution,h=meta.height*meta.resolution;
  return {minX:ox,minY:oy,maxX:ox+w,maxY:oy+h};
}

function fitView(view,bounds,margin=24){
  const w=Math.max(bounds.maxX-bounds.minX,0.5),h=Math.max(bounds.maxY-bounds.minY,0.5);
  const scale=clampScale(Math.min((view.width-2*margin)/w,(view.height-2*margin)/h));
  return {...view,scale,cx:(bounds.minX+bounds.maxX)/2,cy:(bounds.minY+bounds.maxY)/2};
}

// Recolor the relay palette (free, wall, unknown) in RGBA pixel data, in place. Nearest
// palette entry, so a browser's small color-management shifts still map correctly.
function recolorMap(pixels,colors){
  for(let i=0;i<pixels.length;i+=4){
    let best=-1,bestDistance=48;
    for(let c=0;c<3;c++){
      const p=NAV_PALETTE[c],d=Math.abs(pixels[i]-p[0])+Math.abs(pixels[i+1]-p[1])+Math.abs(pixels[i+2]-p[2]);
      if(d<bestDistance){best=c;bestDistance=d;}
    }
    if(best>=0){const q=colors[best];pixels[i]=q[0];pixels[i+1]=q[1];pixels[i+2]=q[2];pixels[i+3]=q[3];}
  }
  return pixels;
}

function formatAge(seconds){
  if(!Number.isFinite(seconds)||seconds<0)return '—';
  if(seconds<2)return '刚刚';
  if(seconds<60)return Math.round(seconds)+' 秒前';
  if(seconds<3600)return Math.round(seconds/60)+' 分钟前';
  return Math.round(seconds/3600)+' 小时前';
}

function areaText(meta){
  if(!meta||!Number.isFinite(meta.free_cells)||!Number.isFinite(meta.resolution))return '—';
  return (meta.free_cells*meta.resolution*meta.resolution).toFixed(1)+' m²';
}

function healthText(state){
  if(!state)return {text:'等待导航数据',level:'neutral'};
  const health=state.health;
  if(!health)return {text:state.mode==='mapping'?'建图中 · 等待健康检查':'等待定位健康数据',level:'neutral'};
  const match=Number.isFinite(health.match)?' · 匹配 '+Math.round(health.match*100)+'%':'';
  if(health.healthy)return {text:(health.localization?'定位正常':'建图正常')+match,level:'good'};
  return {text:reasonText(String(health.reason||''))+match,level:'fault'};
}

function reasonText(reason){
  let m=reason.match(/scan matches the map only (\d+)%/);
  if(m)return '激光与地图只匹配 '+m[1]+'%';
  m=reason.match(/AMCL uncertainty ([\d.]+) m \/ ([\d.]+) rad/);
  if(m)return '定位不确定度过大（'+m[1]+' m / '+m[2]+' rad）';
  m=reason.match(/map->odom jumped ([\d.]+) m \/ ([\d.]+) rad/);
  if(m)return '定位突然跳变（'+m[1]+' m / '+m[2]+' rad）';
  return reason||'定位异常';
}

function faultText(fault){
  if(!fault||!fault.reason)return '';
  const reason=String(fault.reason);
  if(reason==='stall_body')return '车轮在转但车没动，已安全停车';
  if(reason==='localization_lost')return '定位丢失，已安全停车';
  if(reason==='operator_cancel')return '已在网页上取消导航并停车';
  const wheel=reason.match(/^stall_wheel_(left|right)$/);
  if(wheel)return (wheel[1]==='left'?'左':'右')+'轮转不动，已安全停车';
  return '安全停车：'+reason;
}

function modeText(state){
  if(!state)return '无导航数据';
  return state.mode==='mapping'?'建图中':state.mode==='localization'?'定位导航'+(state.map?' · '+state.map:''):'未知模式';
}

function logTime(stamp){
  if(!Number.isFinite(stamp))return '--:--:--';
  return new Date(stamp*1000).toLocaleTimeString('zh-CN',{hour12:false});
}

function logLevelClass(level){return {WARN:'warn',ERROR:'error',FATAL:'error'}[level]||'info';}

// 1-2-5 scale bar length (m) that spans at most maxPixels at the given px/m.
function scaleBar(scale,maxPixels=120){
  const raw=maxPixels/scale,power=10**Math.floor(Math.log10(raw));
  for(const step of [5,2,1])if(step*power<=raw)return step*power;
  return power;
}

// ---- map page controls

// The pose a press at (x0, y0) dragged to (x1, y1) describes; a short drag keeps `yaw`.
function arrowPose(x0,y0,x1,y1,yaw,minDrag){
  const dx=x1-x0,dy=y1-y0;
  return {x:x0,y:y0,yaw:Math.hypot(dx,dy)>=minDrag?Math.atan2(dy,dx):yaw};
}

// [column, row] of the north-up map image under world (x, y); null outside the map.
function mapCell(meta,x,y){
  const col=Math.floor((x-meta.origin[0])/meta.resolution),up=Math.floor((y-meta.origin[1])/meta.resolution);
  if(!(col>=0&&up>=0&&col<meta.width&&up<meta.height))return null;
  return [col,meta.height-1-up];
}

// Straight-line distance and a rough duration for the confirmation dialog.
function goalPlan(pose,goal){
  const distance=Math.hypot(goal.x-pose.x,goal.y-pose.y);
  return {distance,seconds:Math.max(5,Math.round((distance/NAV_AVERAGE_SPEED+6)/5)*5)};
}

function defaultMapName(date){
  const p=n=>String(n).padStart(2,'0');
  return 'room-'+date.getFullYear()+p(date.getMonth()+1)+p(date.getDate())+'-'+p(date.getHours())+p(date.getMinutes());
}

function mapNameValid(name){return /^[A-Za-z0-9_-]{1,64}$/.test(name);}

function containerText(navd){
  if(!navd)return {text:'等待 Pi 代理',level:'neutral'};
  if(!navd.online)return {text:'导航服务未连接',level:'fault'};
  const c=String(navd.container||'');
  if(c==='starting'||c==='running starting')return {text:'启动中',level:'neutral'};
  if(c==='running'||c==='running healthy')return {text:'运行中',level:'good'};
  if(c==='running unhealthy')return {text:'运行异常',level:'fault'};
  if(c==='missing')return {text:'未创建',level:'neutral'};
  if(c.startsWith('exited')||c.startsWith('created'))return {text:'已停止',level:'neutral'};
  return {text:c||'未知',level:'neutral'};
}

function containerRunning(navd){return !!navd&&navd.online&&/^running/.test(String(navd.container||''));}

function navdModeText(navd){
  if(!navd||!navd.online)return '—';
  if(navd.mode==='mapping')return '建图';
  if(navd.mode==='localization')return navd.map?'定位 · '+navd.map:'定位';
  return '只有里程计（未建图）';
}

function jobText(job,now){
  if(!job)return '还没有任务';
  const detail=job.detail&&job.detail.map?' '+job.detail.map:'';
  if(job.state==='running'){
    const seconds=Number.isFinite(now)&&Number.isFinite(job.started)?Math.max(0,Math.round(now-job.started)):null;
    return (NAV_OP_RUNNING[job.op]||job.op)+detail+'…'+(seconds!=null?'（'+seconds+' 秒）':'');
  }
  const result=job.message||{ok:'完成',failed:'失败',cancelled:'已取消'}[job.state]||job.state;
  const age=Number.isFinite(now)&&Number.isFinite(job.finished)?'（'+formatAge(now-job.finished)+'）':'';
  return (NAV_OP_TEXT[job.op]||job.op)+'：'+result+age;
}

function mapOptionText(map){
  if(map.error)return map.name+' · 无法读取';
  const date=new Date(map.modified*1000),p=n=>String(n).padStart(2,'0');
  const when=(date.getMonth()+1)+'月'+date.getDate()+'日 '+p(date.getHours())+':'+p(date.getMinutes());
  return map.name+' · '+(Number.isFinite(map.area_m2)?map.area_m2.toFixed(1)+' m² · ':'')+when;
}

// ---- map context menu: why an item is unavailable ('' when it can be used)
function navBusyText(navd){const job=navd&&navd.job;return job&&job.state==='running'?'「'+(NAV_OP_TEXT[job.op]||job.op)+'」还没完成':'';}
function navReadyText(s){
  if(!s.online)return '老三离线或地图数据未连接';
  if(!s.navd||!s.navd.online)return '导航服务未连接';
  if(!containerRunning(s.navd))return '导航容器没有运行';
  return navBusyText(s.navd);
}
function poseBlocker(s){
  const ready=navReadyText(s);if(ready)return ready;
  if(s.navd.mode!=='localization')return '建图时不用设置位置';
  if(!s.hasMap||!s.relay)return '地图还没加载好';
  return '';
}
function goalBlocker(s){
  const ready=navReadyText(s);if(ready)return ready;
  if(!s.hasMap)return '还没有地图';
  if(!s.poseFresh)return s.navd.mode==='localization'?'先把老三的位置设好':'还没有老三的实时位置';
  return '';
}
