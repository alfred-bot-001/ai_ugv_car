"use strict";
// Pure helpers for the map page (no DOM): packets, view transforms, colors and wording.
// The relay's map PNG is north up: its first row is the map's highest y.

const NAV_PALETTE=[[245,247,244],[32,52,45],[205,211,208]];
const NAV_MIN_SCALE=8,NAV_MAX_SCALE=600;

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
