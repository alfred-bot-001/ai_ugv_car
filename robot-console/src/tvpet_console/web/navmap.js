"use strict";
// Pure helpers for the map page (no DOM): packets, view transforms, colors and wording.
// The relay's map PNG is north up: its first row is the map's highest y.

const NAV_PALETTE=[[245,247,244],[32,52,45],[205,211,208]];
const NAV_MIN_SCALE=8,NAV_MAX_SCALE=600;
// Nav2 cruises at up to 0.10 m/s (bridge MAX_LINEAR 0.12); real runs average lower.
const NAV_AVERAGE_SPEED=0.08;
const NAV_OP_TEXT={mapping:'开始建图',save:'保存地图',localize:'加载地图定位',pose:'设置当前位置',goal:'导航去目标点',explore:'自动探索',patrol:'巡航'};
const NAV_OP_RUNNING={mapping:'正在切换到建图模式',save:'正在保存地图',localize:'正在加载地图',pose:'正在设置位置',goal:'正在导航去目标点',explore:'正在自动探索',patrol:'正在巡航'};

function parseNavPacket(buffer){
  const bytes=new Uint8Array(buffer);
  if(bytes.length<4)throw new Error('地图数据格式无效');
  const size=new DataView(buffer).getUint32(0);
  if(size<2||size>65536||4+size>bytes.length)throw new Error('地图数据格式无效');
  const meta=JSON.parse(new TextDecoder().decode(bytes.subarray(4,4+size)));
  if(!meta||typeof meta.kind!=='string')throw new Error('地图数据格式无效');
  return {meta,payload:bytes.subarray(4+size)};
}

// view.rot (radians, default 0) turns the map on screen counter-clockwise ("摆正").
function rotate(x,y,angle){const c=Math.cos(angle),s=Math.sin(angle);return [x*c-y*s,x*s+y*c];}
function worldToScreen(view,x,y){
  const [u,v]=rotate(x-view.cx,y-view.cy,view.rot||0);
  return [u*view.scale+view.width/2,view.height/2-v*view.scale];
}
function screenToWorld(view,sx,sy){
  const [dx,dy]=rotate((sx-view.width/2)/view.scale,(view.height/2-sy)/view.scale,-(view.rot||0));
  return [view.cx+dx,view.cy+dy];
}
// Move the view by a screen drag of (dx, dy) pixels.
function panBy(view,dx,dy){
  const [wx,wy]=rotate(dx/view.scale,-dy/view.scale,-(view.rot||0));
  return {...view,cx:view.cx-wx,cy:view.cy-wy};
}

function clampScale(scale){return Math.min(NAV_MAX_SCALE,Math.max(NAV_MIN_SCALE,scale));}

// Zoom keeping the world point under (sx, sy) fixed on screen.
function zoomAt(view,sx,sy,factor){
  const [wx,wy]=screenToWorld(view,sx,sy),scale=clampScale(view.scale*factor);
  const [dx,dy]=rotate((sx-view.width/2)/scale,(view.height/2-sy)/scale,-(view.rot||0));
  return {...view,scale,cx:wx-dx,cy:wy-dy};
}

function mapBounds(meta){
  const [ox,oy]=meta.origin,w=meta.width*meta.resolution,h=meta.height*meta.resolution;
  return {minX:ox,minY:oy,maxX:ox+w,maxY:oy+h};
}

function fitView(view,bounds,margin=24){
  const r=view.rot||0,bw=Math.max(bounds.maxX-bounds.minX,0.5),bh=Math.max(bounds.maxY-bounds.minY,0.5);
  const w=Math.abs(bw*Math.cos(r))+Math.abs(bh*Math.sin(r)),h=Math.abs(bw*Math.sin(r))+Math.abs(bh*Math.cos(r));
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
    const r=job.result||{};let legs='';
    if(job.op==='explore'&&Number.isFinite(r.legs))legs='第 '+(r.legs+1)+' 段'+(Number.isFinite(r.explored_m2)?' · 已探索 '+r.explored_m2+' m²':'')+' · ';
    if(job.op==='patrol'&&Number.isFinite(r.loop)&&r.loop>0)legs='第 '+r.loop+'/'+r.loops+' 圈 · 第 '+r.index+'/'+r.points+' 点 · 已到 '+r.reached+' 段 · ';
    return (NAV_OP_RUNNING[job.op]||job.op)+detail+'…'+(seconds!=null?'（'+legs+formatDuration(seconds)+'）':'');
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

// A map belongs to the container run that sent it; packets from older relays lack a session.
function sameSession(a,b){return !Number.isFinite(a)||!Number.isFinite(b)||a===b;}

// Distance in cells from (col, row) to the nearest wall pixel (opaque and dark) within
// maxCells, or Infinity. pixels is RGBA, row 0 on top, as the recolored map canvas holds.
function nearestWall(pixels,width,height,col,row,maxCells){
  let best=Infinity;
  for(let dy=-maxCells;dy<=maxCells;dy++){
    const y=row+dy;if(y<0||y>=height)continue;
    for(let dx=-maxCells;dx<=maxCells;dx++){
      const x=col+dx;if(x<0||x>=width)continue;
      const i=(y*width+x)*4;
      if(pixels[i+3]===255&&pixels[i]<128){const d=Math.hypot(dx,dy);if(d<best)best=d;}
    }
  }
  return best;
}

// Chinese for the log lines operators meet most; anything else stays as it is.
const LOG_TEXT=[
  [/^motion gate: front obstacle and turn space not verified/,'安全门：前方 30 cm 内有障碍，停止前进'],
  [/^motion gate: near wall: verified-clear turn only/,'安全门：靠近障碍，只允许原地慢转'],
  [/^motion gate: scan stale or too few valid front rays/,'安全门：激光数据过期或前方有效点太少，已停车'],
  [/^motion gate: laser odometry stale or invalid/,'安全门：激光里程计过期，已停车'],
  [/^Failed to make progress/,'导航：一段时间没有进展，放弃这个目标'],
  [/^Goal failed/,'导航：目标失败'],
  [/^Goal succeeded/,'导航：已到达目标'],
  [/^Begin navigating from current location/,'导航：开始前往目标'],
];
function logText(text){
  const value=String(text||'');
  for(const [pattern,chinese] of LOG_TEXT)if(pattern.test(value))return chinese;
  return value;
}

function formatDuration(seconds){return seconds<120?seconds+' 秒':Math.floor(seconds/60)+' 分 '+(seconds%60)+' 秒';}

// Exploration is allowed while mapping, with a live pose and nothing else running.
function exploreBlocker(s){
  const ready=navReadyText(s);if(ready)return ready;
  if(s.navd.mode!=='mapping')return '自动探索只能在建图时进行';
  if(!s.hasMap)return '还没有地图';
  if(!s.poseFresh)return '还没有老三的实时位置';
  return '';
}

// The wall direction (radians, in (-pi/4, pi/4]) most walls line up with. Each wall
// cell fits a local line through its neighbours (principal axis); straight neighbourhoods
// vote with angle*4 so walls at right angles agree, and the vote is averaged. Turning the
// view by minus this puts the walls level ("摆正"). cells are [col, row] with row up.
function dominantAngle(cells,radius=5){
  const occupied=new Set(cells.map(([c,r])=>c+','+r));let sx=0,sy=0;
  for(const [c,r] of cells){
    const near=[];
    for(let dx=-radius;dx<=radius;dx++)for(let dy=-radius;dy<=radius;dy++)
      if(dx*dx+dy*dy<=radius*radius&&occupied.has((c+dx)+','+(r+dy)))near.push([dx,dy]);
    if(near.length<4)continue;
    const mx=near.reduce((a,p)=>a+p[0],0)/near.length,my=near.reduce((a,p)=>a+p[1],0)/near.length;
    let xx=0,yy=0,xy=0;for(const [x,y] of near){xx+=(x-mx)**2;yy+=(y-my)**2;xy+=(x-mx)*(y-my);}
    const trace=xx+yy;if(trace<=0)continue;
    const straight=Math.hypot(xx-yy,2*xy)/trace;if(straight<0.6)continue;  // 1 for a line
    const theta=0.5*Math.atan2(2*xy,xx-yy);sx+=straight*Math.cos(4*theta);sy+=straight*Math.sin(4*theta);
  }
  return Math.hypot(sx,sy)<1e-9?0:Math.atan2(sy,sx)/4;
}

// ---- patrol routes
const ABORT_TEXT={cancelled:'已取消',battery:'电量低',failed:'没到达',stall_body:'车轮在转但车没动',localization_lost:'定位丢失',stall_wheel_left:'左轮转不动',stall_wheel_right:'右轮转不动',operator_cancel:'网页取消'};
function routeBlocker(s,count){
  if(!s.online)return '老三离线或地图数据未连接';
  if(!s.navd||!s.navd.online)return '导航服务未连接';
  if(s.navd.mode!=='localization')return '巡航路线要在已保存的地图上编辑';
  if(!s.hasMap)return '地图还没加载好';
  if(count>=20)return '最多 20 个巡航点';
  return '';
}
function patrolBlocker(s,count,dirty){
  const ready=navReadyText(s);if(ready)return ready;
  if(s.navd.mode!=='localization')return '巡航要在已保存的地图上进行';
  if(!count)return '先在地图上加巡航点';
  if(dirty)return '先保存路线';
  if(!s.poseFresh)return '先把老三的位置设好';
  return '';
}
function reportText(report){
  if(!report)return '还没有巡航记录';
  const date=new Date(report.modified*1000),p=n=>String(n).padStart(2,'0');
  const when=(date.getMonth()+1)+'月'+date.getDate()+'日 '+p(date.getHours())+':'+p(date.getMinutes());
  const legs=Number.isFinite(report.completed_legs)?report.completed_legs+'/'+report.planned_legs+' 段':'';
  const result=report.ok?'全部到达':'中止：'+(ABORT_TEXT[report.abort_reason]||report.abort_reason||'未知原因');
  return '最近一次：'+when+(report.map?' · '+report.map:'')+(legs?' · '+legs:'')+' · '+result;
}

// ---- can the car get there? Free cells (opaque, light) at least `clear` cells from any
// wall (opaque, dark), reached from start through such cells; unknown (transparent)
// blocks, as Nav2's planner (allow_unknown false) does. Returns whether goal is reachable
// and the reachable cell closest to it. Cells are [col, row], row 0 on top.
function reachCheck(pixels,width,height,start,goal,clear){
  const n=width*height,far=clear+1,wall=new Int16Array(n).fill(far),queue=new Int32Array(n);
  let head=0,tail=0;
  for(let i=0;i<n;i++)if(pixels[i*4+3]===255&&pixels[i*4]<128){wall[i]=0;queue[tail++]=i;}
  while(head<tail){
    const i=queue[head++],col=i%width,row=(i-col)/width,step=wall[i]+1;if(step>=far)continue;
    for(let dy=-1;dy<=1;dy++)for(let dx=-1;dx<=1;dx++){
      const c=col+dx,r=row+dy;if((dx||dy)&&c>=0&&r>=0&&c<width&&r<height&&wall[r*width+c]>step){wall[r*width+c]=step;queue[tail++]=r*width+c;}
    }
  }
  const passable=i=>pixels[i*4+3]===255&&pixels[i*4]>=128&&wall[i]>=clear;
  const s=start[1]*width+start[0],g=goal[1]*width+goal[0],seen=new Uint8Array(n);
  head=0;tail=0;queue[tail++]=s;seen[s]=1;
  let best=s,bestGap=Math.hypot(start[0]-goal[0],start[1]-goal[1]);
  while(head<tail){
    const i=queue[head++],col=i%width,row=(i-col)/width;
    if(i===g)return {reachable:true,cell:goal};
    const gap=Math.hypot(col-goal[0],row-goal[1]);if(gap<bestGap){best=i;bestGap=gap;}
    for(let dy=-1;dy<=1;dy++)for(let dx=-1;dx<=1;dx++){
      const c=col+dx,r=row+dy,j=r*width+c;
      if((dx||dy)&&c>=0&&r>=0&&c<width&&r<height&&!seen[j]&&passable(j)){seen[j]=1;queue[tail++]=j;}
    }
  }
  return {reachable:false,cell:[best%width,(best-best%width)/width]};
}

// World centre of image cell [col, row] (row 0 on top); the inverse of mapCell.
function cellCentre(meta,col,row){return [meta.origin[0]+(col+0.5)*meta.resolution,meta.origin[1]+(meta.height-row-0.5)*meta.resolution];}
