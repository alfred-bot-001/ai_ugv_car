"use strict";
// Front Orbbec depth frames (both pages): "!I" metadata length, JSON with distances in
// mm, then a colorized JPEG (blue near, red far, black = no reading).

function parseDepthPacket(buffer){
  const bytes=new Uint8Array(buffer);
  if(bytes.length<6)throw new Error('深度帧格式无效');
  const size=new DataView(buffer).getUint32(0);
  if(size<2||size>4096||4+size>=bytes.length)throw new Error('深度帧格式无效');
  const meta=JSON.parse(new TextDecoder().decode(bytes.subarray(4,4+size)));
  if(!meta||typeof meta!=='object')throw new Error('深度帧格式无效');
  return {meta,jpeg:bytes.subarray(4+size)};
}

function metres(mm){return Number.isFinite(mm)&&mm>0?(mm/1000).toFixed(2)+' m':'测不到';}

function depthText(meta){
  const parts=['中心 '+metres(meta.centre_mm),'最近 '+metres(meta.nearest_mm)];
  if(Number.isFinite(meta.valid))parts.push('有效 '+Math.round(meta.valid*100)+'%');
  return parts.join(' · ');
}

function depthScaleText(meta){
  const near=Number.isFinite(meta?.near_mm)?meta.near_mm:300,far=Number.isFinite(meta?.far_mm)?meta.far_mm:4000;
  return [(near/1000).toFixed(1)+' m',(far/1000).toFixed(0)+' m'];
}
