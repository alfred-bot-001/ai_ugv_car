"use strict";
// Live video shared by the dashboard and the map page: the gimbal camera's v2 frame
// (metadata length, JSON with faces and poses, then the JPEG), the overlays drawn on
// it, and the light switches. Pure functions only; each page owns its sockets, the
// state of its buttons and the status it polls.

// A console.video.v2 frame: "!I" metadata length, JSON (faces, pose, recognition,
// inference_ms), then the JPEG.
function parseVideoPacket(buffer){
  const bytes=new Uint8Array(buffer);
  if(bytes.length<5)throw new Error('视频帧格式无效');
  const length=new DataView(buffer).getUint32(0);
  if(length<1||length>32768||4+length>=bytes.length)throw new Error('视频帧信息无效');
  const meta=JSON.parse(new TextDecoder().decode(bytes.subarray(4,4+length)));
  if(!meta||typeof meta!=='object')throw new Error('视频帧信息无效');
  return {meta,jpeg:bytes.subarray(4+length)};
}

// Face boxes with their labels; known family members in green, strangers in amber.
function drawFaces(context,faces,width,height){
  context.font='600 20px sans-serif';context.textBaseline='top';
  for(const face of faces){
    if(!Array.isArray(face.bbox)||face.bbox.length!==4||!face.bbox.every(Number.isFinite))continue;
    let [x1,y1,x2,y2]=face.bbox;
    x1=Math.max(0,Math.min(width,x1));y1=Math.max(0,Math.min(height,y1));x2=Math.max(x1,Math.min(width,x2));y2=Math.max(y1,Math.min(height,y2));
    if(x2<=x1||y2<=y1)continue;
    const color=face.known?'#c5f58a':'#ffd47c';
    context.strokeStyle=color;context.lineWidth=3;context.strokeRect(x1,y1,x2-x1,y2-y1);
    const label=String(face.label).slice(0,40),labelWidth=Math.min(width,context.measureText(label).width+16),left=Math.max(0,Math.min(x1,width-labelWidth)),top=y1>=30?y1-30:Math.min(height-30,y1);
    context.fillStyle='#132a23';context.fillRect(left,top,labelWidth,30);context.fillStyle=color;context.fillText(label,left+8,top+4,Math.max(1,labelWidth-16));
  }
}

// Pose skeletons (33 MediaPipe keypoints, normalised) for up to three people.
function drawPoses(context,pose,width,height){
  const links=[[11,12],[11,13],[13,15],[12,14],[14,16],[11,23],[12,24],[23,24],[23,25],[25,27],[24,26],[26,28],[27,29],[29,31],[28,30],[30,32]];
  for(const person of (pose?.people||[]).slice(0,3)){
    const points=person.keypoints;if(!Array.isArray(points)||points.length!==33)continue;
    const visible=i=>points[i]?.length===4&&points[i].every(Number.isFinite)&&points[i][3]>=.6;
    context.strokeStyle='#40e0d0';context.fillStyle='#ffd47c';context.lineWidth=3;
    for(const [a,b] of links)if(visible(a)&&visible(b)){context.beginPath();context.moveTo(points[a][0]*width,points[a][1]*height);context.lineTo(points[b][0]*width,points[b][1]*height);context.stroke();}
    for(let i=0;i<33;i++)if(visible(i)){context.beginPath();context.arc(points[i][0]*width,points[i][1]*height,3,0,Math.PI*2);context.fill();}
    const text=`#${person.track_id} ${person.identity||'人物未确认'} · ${person.posture_label} · ${person.action_label}`;
    const y=height-28-32*(pose.people.indexOf(person));context.font='18px sans-serif';context.textBaseline='top';context.fillStyle='#132a23';context.fillRect(0,y,width,28);context.fillStyle='#ffd47c';context.fillText(text,8,y+4,width-16);
  }
}

// Lights: one channel per click, flipped from the last known state (unknown counts as
// off). `pending` holds the reply the browser just got until the polled status (1 Hz,
// one more hop behind) has had time to catch up, so a fresh click never flickers back.
const LIGHT_HOLD=3000;
function lightMessage(name,lights){return {[name]:lights?.[name]!==true};}
function lightState(name,lights,pending,now){const held=pending?.[name];return held&&now<held.until?held.on:lights?.[name]===true;}
