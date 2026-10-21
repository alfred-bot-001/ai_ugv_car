"use strict";
const $=selector=>document.querySelector(selector);
let authStopped=false;
let csrf='',status={},videoSocket=null,audioSocket=null,controlSocket=null,audioContext=null,toastTimer=null;
let videoGeneration=0,pendingFrame=null,painting=false,videoStaleTimer=null;
let press=null,pressGeneration=0,controlSequence=0,challenge='',lastChallenge='',lease='',pressTimer=null;
const labels={available:'已连接',active:'采集中',degraded:'信号偏低',busy:'设备忙',fault:'故障',unknown:'未知',disabled:'未启用'};
function toast(message){$('#toast').textContent=message;$('#toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').hidden=true,6000);}
async function api(path,body={}){
  const response=await fetch('/api/v1/'+path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});
  if(response.status===401){closeMedia();if(!authStopped)location.replace('/');throw new Error('登录已失效');}
  if(!response.ok){const data=await response.json();throw new Error(typeof data.detail==='string'?data.detail:'请检查输入内容');}
  return response;
}
async function action(button,callback){if(button.disabled)return;button.disabled=true;button.dataset.busy='1';try{await callback();}catch(error){toast(error.message);}finally{button.dataset.busy='';button.disabled=false;}}
function metric(name,value,detail){$('#metric-'+name).textContent=value;$('#detail-'+name).textContent=detail;}
// 3S pack, same voltage bands as Waveshare's ugv_rpi page plus a full band for a resting ~12.2 V; no percentage without current data.
function batteryLevel(volts){if(!Number.isFinite(volts)||volts<=0)return null;return volts>=11.7?4:volts>=10.5?3:volts>=10?2:volts>=9.5?1:0;}
let batteryVolts=null;
function batteryText(raw,driving){const valid=Number.isFinite(raw)&&raw>0;if(!valid)batteryVolts=null;else if(!driving)batteryVolts=batteryVolts==null?raw:batteryVolts+.2*(raw-batteryVolts);const level=batteryLevel(batteryVolts);
  return {value:valid?raw.toFixed(2)+' V':'—',level,detail:level==null?(valid?'行驶中，停车后估算':'等待电压数据'):(level===0?'电量低，请尽快充电':'约 '+level+'/4 格')+' · 电压估算'+(driving?' · 行驶中保持':'')};}
function poseReviewText(review){
  if(!review||review.state==='disabled')return '本机审核采样未开启';
  const saved=Number.isSafeInteger(review.daily_saved)&&review.daily_saved>=0?review.daily_saved:'—';
  const limit=Number.isSafeInteger(review.daily_limit)&&review.daily_limit>0?review.daily_limit:50;
  const progress='今日已采集 '+saved+'/'+limit+' 张';
  if(review.state==='daily_limit')return '本机审核采样：'+progress+' · 已达上限，明日自动恢复（新加坡时间 00:00）';
  if(review.state==='paused')return '审核保存暂停：'+progress+' · '+(review.error||'等待恢复');
  if(review.state==='waiting_person')return '本机审核采样：'+progress+' · 未检测到人，暂不保存';
  if(review.state==='active')return '本机审核采样：'+progress+' · 仅有人时保存，至少间隔30秒';
  if(review.state==='waiting')return '本机审核采样：'+progress+' · 等待新画面';
  return '本机审核采样：'+progress+' · 等待读取采样状态';
}
function render(data){status=data;const online=data.online,state=data.state||{},system=state.system||{},board=state.board||{},telemetry=board.telemetry||{};
  const onlineBadge=$('#online-badge');onlineBadge.textContent=online?'老三在线':'老三离线';onlineBadge.className='badge '+(online?'':'warning');
  $('#sidebar-status').textContent=online?'设备连接正常':'等待设备连接';
  $('#connection-banner').className='notice '+(online?'good':'');
  $('#connection-banner').textContent=online?(board.wheels_ready&&board.gimbal_ready?'老三已就绪 · 拖动圆盘控制动作，松手停止。':'设备暂不可控 · '+(board.gate_reason||'等待硬件反馈正常后恢复控制。')):'老三尚未连接或数据已过期。Mini 主控台正常，设备操作暂停。';
  metric('camera',labels[state.camera?.state]||'未知',state.camera?.error||state.camera?.format||'未取得摄像头信息');
  metric('microphone',labels[state.microphone?.state]||'未知',state.microphone?.dbfs!=null?state.microphone.dbfs.toFixed(1)+' dBFS':state.microphone?.format||'等待麦克风状态');
  metric('servo',online&&board.state==='available'?'遥测正常':'未知',board.gimbal_ready&&board.wheels_ready?'可以控制':board.gate_reason||'等待硬件反馈');
  metric('temperature',system.temperature_c!=null?system.temperature_c.toFixed(1)+'°C':'—',system.load1!=null?'系统负载 '+system.load1.toFixed(2):'等待温度数据');
  metric('memory',system.memory_total?Math.round(system.memory_used/system.memory_total*100)+'%':'—',system.memory_total?(system.memory_used/2**30).toFixed(1)+' / '+(system.memory_total/2**30).toFixed(1)+' GB':'等待内存数据');
  const battery=batteryText(telemetry.v,press?.target==='wheels');metric('battery',battery.value,battery.detail);$('#metric-battery').classList[battery.level===0?'add':'remove']('low');
  $('#control-mode').textContent=board.wheels_ready&&board.gimbal_ready?'控制已验收':'观察模式';
  $('#fan').textContent=system.fan_rpm!=null?system.fan_rpm+' RPM':'未知';$('#throttled').textContent=system.throttled==='throttled=0x0'?'无欠压 / 限频':system.throttled||'未知';
  $('#uart').textContent=board.state==='available'?'收到新遥测 · '+board.age_seconds.toFixed(1)+'秒前':'无新遥测';$('#edge').textContent=(state.edge?.state==='active'?'姿态识别中':labels[state.edge?.state]||'未启用')+(state.edge?.inference_ms!=null?' · '+Math.round(state.edge.inference_ms)+' ms':'');const review=data.pose_review;if($('#pose-review-detail')){$('#pose-review-detail').textContent=poseReviewText(review);$('#pose-review-detail').title=review?.day?'统计日期 '+review.day+' · '+(review.timezone||'Asia/Singapore'):'';}
  $('#speaker-state').textContent=state.speaker?.state==='active'?'扬声器播放中':state.speaker?.state==='available'?'扬声器已识别':'扬声器未知';
  $('#audio-detail').textContent=state.microphone?.error||state.microphone?.format||'等待麦克风状态';
  $('#last-update').textContent=online?'数据更新 '+new Date(system.sampled_at*1000).toLocaleTimeString('zh-CN',{hour12:false}):'设备数据不可用';
  for(const target of ['wheels','gimbal']){const ready=online&&board[target+'_ready']&&!board.estopped,element=$('#stick-'+target);element.setAttribute('aria-disabled',String(!ready));$('#gate-'+target).textContent=board.estopped?'急停锁定':ready?'可以控制':!online?'设备离线':board.control_qualified?'等待硬件恢复':'硬件待检查';$('#gate-'+target).className='badge '+(ready?'':'warning');
    $('#feedback-'+target).textContent=target==='wheels'&&telemetry.L!=null?'左轮 '+telemetry.L.toFixed(3)+' / 右轮 '+telemetry.R.toFixed(3)+' · '+(ready?'已验收':board.gate_reason):ready?'拖动圆盘转动视角。':board.gate_reason||'设备离线，控制已锁定。';}
  for(const id of ['video-toggle','snapshot','listen','say','speech-stop'])if(!$('#'+id).dataset.busy)$('#'+id).disabled=!online;
  $('#transcribe').disabled=!online||!data.asr_ready||$('#transcribe').dataset.busy==='1';$('#transcribe').title=data.asr_ready?'采集 5 秒后在 Mini 本地转写':'尚未安装 Mini 本地转写模型';
  if(!online){closeMedia();releaseStick();}else if(!videoSocket)faceState('idle');
}
async function poll(){if(authStopped)return;try{const response=await fetch('/api/v1/status');if(authStopped)return;if(response.status===401){closeMedia();location.replace('/');return;}if(!response.ok)throw new Error('状态读取失败');const data=await response.json();if(authStopped)return;render(data);}catch(error){if(authStopped)return;render({online:false});}if(!authStopped)setTimeout(poll,1000);}
function socket(channel){if(authStopped)throw new Error('已退出登录');const ws=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/ws/'+channel,[channel==='video'?'console.video.v2':'console.v1']);ws.binaryType='arraybuffer';ws.onopen=()=>ws.send(JSON.stringify({csrf}));return ws;}
function videoState(active){$('#video-toggle').textContent=active?'停止视频':'开启视频';$('#video-state').textContent=active?'实时画面':'未预览';$('#video-state').className='badge '+(active?'':'neutral');$('#video-label').textContent=active?'LIVE · 老三视角':'预览已关闭';$('#video-placeholder').hidden=active;$('#video-frame').hidden=!active;}
function clearVideo(){const canvas=$('#video-frame');canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);pendingFrame=null;clearTimeout(videoStaleTimer);}
function idleFaceText(review){if(!review||review.state==='disabled')return '开启视频后识别家庭成员';if(review.state==='daily_limit')return '今日采样已达上限，明日新加坡时间 00:00 自动恢复';if(review.state==='paused')return '后台本机审核采样已暂停'+(review.error?'：'+review.error:'');return '后台本机审核采样继续运行';}
function faceState(state,faces=[],milliseconds=null){const badge=$('#face-state');badge.textContent={active:'家庭人脸识别',unavailable:'识别服务暂不可用',disabled:'人脸识别未启用',idle:'人脸预览已关闭',waiting:'等待新画面'}[state]||'等待新画面';badge.className='badge '+(state==='active'?'':state==='unavailable'?'warning':'neutral');$('#face-detail').textContent=state==='active'?(faces.length?faces.map(face=>face.label).join(' · '):'画面中未检测到人脸')+(milliseconds!=null?' · '+Math.round(milliseconds)+' ms':''):state==='unavailable'?'视频继续显示，正在重试识别':state==='idle'?idleFaceText(status.pose_review):'';}
function stopVideo(){videoGeneration++;if(videoSocket){const old=videoSocket;videoSocket=null;old.close();}clearVideo();videoState(false);faceState('idle');}
function drawFaces(context,faces,width,height){context.font='600 20px sans-serif';context.textBaseline='top';for(const face of faces){if(!Array.isArray(face.bbox)||face.bbox.length!==4||!face.bbox.every(Number.isFinite))continue;let [x1,y1,x2,y2]=face.bbox;x1=Math.max(0,Math.min(width,x1));y1=Math.max(0,Math.min(height,y1));x2=Math.max(x1,Math.min(width,x2));y2=Math.max(y1,Math.min(height,y2));if(x2<=x1||y2<=y1)continue;const color=face.known?'#c5f58a':'#ffd47c';context.strokeStyle=color;context.lineWidth=3;context.strokeRect(x1,y1,x2-x1,y2-y1);const label=String(face.label).slice(0,40),labelWidth=Math.min(width,context.measureText(label).width+16),left=Math.max(0,Math.min(x1,width-labelWidth)),top=y1>=30?y1-30:Math.min(height-30,y1);context.fillStyle='#132a23';context.fillRect(left,top,labelWidth,30);context.fillStyle=color;context.fillText(label,left+8,top+4,Math.max(1,labelWidth-16));}}
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
async function paintVideo(){if(painting)return;painting=true;try{while(pendingFrame){const {data,generation}=pendingFrame;pendingFrame=null;let bitmap=null;try{const bytes=new Uint8Array(data);if(bytes.length<5)throw new Error('视频帧格式无效');const length=new DataView(data).getUint32(0);if(length<1||length>32768||4+length>=bytes.length)throw new Error('视频帧信息无效');const meta=JSON.parse(new TextDecoder().decode(bytes.subarray(4,4+length)));bitmap=await createImageBitmap(new Blob([bytes.subarray(4+length)],{type:'image/jpeg'}));if(generation!==videoGeneration||!videoSocket)continue;const canvas=$('#video-frame'),context=canvas.getContext('2d');canvas.width=bitmap.width;canvas.height=bitmap.height;context.drawImage(bitmap,0,0);const faces=Array.isArray(meta.faces)?meta.faces.slice(0,16):[];drawPoses(context,meta.pose,bitmap.width,bitmap.height);drawFaces(context,faces,bitmap.width,bitmap.height);videoState(true);faceState(meta.recognition,faces,meta.inference_ms);$('#frame-detail').textContent='新帧 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});clearTimeout(videoStaleTimer);videoStaleTimer=setTimeout(()=>{if(generation!==videoGeneration)return;videoGeneration++;clearVideo();videoState(false);$('#video-toggle').textContent='停止视频';$('#video-label').textContent='等待新画面';$('#video-placeholder strong').textContent='视频暂时中断';faceState('waiting');},2000);}catch(error){if(generation===videoGeneration){toast(error.message);stopVideo();}}finally{bitmap?.close();}}}finally{painting=false;}}
function stopAudio(){if(audioSocket){audioSocket.close();audioSocket=null;}if(audioContext){audioContext.close();audioContext=null;}$('#listen').textContent='开启听音';$('.waveform').classList.remove('active');}
function closeMedia(){stopVideo();stopAudio();}
$('#video-toggle').onclick=()=>{if(videoSocket){stopVideo();return;}clearVideo();videoGeneration++;$('#video-placeholder strong').textContent='正在连接摄像头…';faceState('waiting');const ws=socket('video');videoSocket=ws;ws.onmessage=e=>{if(videoSocket!==ws)return;pendingFrame={data:e.data,generation:videoGeneration};paintVideo();};ws.onclose=()=>{if(videoSocket===ws)stopVideo();};};
$('#snapshot').onclick=()=>action($('#snapshot'),async()=>{const response=await api('snapshot');const url=URL.createObjectURL(await response.blob());const link=document.createElement('a');link.href=url;link.download='老三-'+Date.now()+'.jpg';link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);toast('新照片已下载');});
$('#fullscreen').onclick=()=>$('.video-stage').requestFullscreen?.();
$('#listen').onclick=()=>action($('#listen'),async()=>{if(audioSocket){stopAudio();return;}audioContext=new AudioContext();await audioContext.audioWorklet.addModule('/static/pcm-worklet.js');await audioContext.resume();const node=new AudioWorkletNode(audioContext,'pcm-player');node.connect(audioContext.destination);audioSocket=socket('audio');audioSocket.onmessage=e=>node.port.postMessage(e.data,[e.data]);audioSocket.onclose=()=>stopAudio();$('#listen').textContent='停止听音';$('.waveform').classList.add('active');});
$('#transcribe').onclick=()=>action($('#transcribe'),async()=>{$('#transcript').textContent='请在老三旁说话，采集 5 秒…';const response=await api('speech/transcribe');$('#transcript').textContent=(await response.json()).text||'未识别到清晰语音，请检查 M2 拾音。';});
$('#volume').oninput=()=>$('#volume-value').textContent=$('#volume').value+'%';
$('#say-form').onsubmit=e=>{e.preventDefault();action($('#say'),async()=>{$('#speech-detail').textContent='Mini 正在合成语音…';await api('speech/say',{text:$('#say-text').value,volume:Number($('#volume').value)/100});$('#speech-detail').textContent='老三已接收播放任务';});};
$('#speech-stop').onclick=()=>action($('#speech-stop'),async()=>{await api('control/stop');toast('已发送停止');});
function connectControl(){if(authStopped)return;controlSocket=socket('control');controlSocket.onmessage=e=>{const data=JSON.parse(e.data);if(data.error){toast(data.error);releaseStick();return;}if(data.challenge&&data.lease===lease){challenge=data.challenge;if(press&&lease&&challenge!==lastChallenge){lastChallenge=challenge;controlSocket.send(JSON.stringify({target:press.target,x:press.x,y:press.y,lease,challenge,sequence:++controlSequence}));}}};controlSocket.onclose=()=>{releaseStick();if(!authStopped)setTimeout(connectControl,1500);};}
function positionStick(element,event){const rect=element.getBoundingClientRect(),radius=rect.width/2-35;let x=(event.clientX-rect.left-rect.width/2)/radius,y=-(event.clientY-rect.top-rect.height/2)/radius;const scale=Math.max(1,Math.hypot(x,y));x/=scale;y/=scale;press.x=Math.abs(x)<.1?0:x;press.y=Math.abs(y)<.1?0:y;element.querySelector('.stick-knob').style.transform=`translate(${x*radius}px,${-y*radius}px)`;}
async function beginStick(element,event){if(element.getAttribute('aria-disabled')==='true'){toast(status.state?.board?.gate_reason||'等待设备恢复后控制');return;}if(press)return;event.preventDefault();const generation=++pressGeneration;press={target:element.dataset.target,x:0,y:0};if(event.pointerId!=null){element.setPointerCapture(event.pointerId);positionStick(element,event);}try{const result=await(await api('control/lease')).json();if(generation!==pressGeneration||!press){await api('control/stop');return;}lease=result.lease;lastChallenge='';pressTimer=setTimeout(releaseStick,1900);}catch(error){toast(error.message);releaseStick();}}
function releaseStick(){const had=!!press||!!lease;pressGeneration++;press=null;lease='';clearTimeout(pressTimer);for(const knob of document.querySelectorAll('.stick-knob'))knob.style.transform='translate(0,0)';if(had&&csrf)api('control/stop').catch(()=>{});}
for(const element of document.querySelectorAll('.joystick')){element.addEventListener('pointerdown',e=>beginStick(element,e));element.addEventListener('pointermove',e=>{if(press?.target===element.dataset.target)positionStick(element,e);});for(const kind of ['pointerup','pointercancel','lostpointercapture'])element.addEventListener(kind,releaseStick);element.addEventListener('keydown',async e=>{if(!['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)||e.repeat)return;await beginStick(element,e);if(press){press.x=e.key==='ArrowLeft'?-1:e.key==='ArrowRight'?1:0;press.y=e.key==='ArrowUp'?1:e.key==='ArrowDown'?-1:0;}});element.addEventListener('keyup',releaseStick);element.addEventListener('blur',releaseStick);}
for(const button of document.querySelectorAll('[data-stop]'))button.onclick=()=>{releaseStick();api('control/stop').then(()=>toast('停止指令已确认')).catch(error=>toast(error.message));};
$('#estop').onclick=()=>{releaseStick();api('control/estop').then(()=>toast('老三已锁定急停')).catch(error=>toast(error.message));};$('#reset-estop').onclick=()=>api('control/reset').then(()=>toast('停止锁已解除')).catch(error=>toast(error.message));
window.addEventListener('blur',releaseStick);document.addEventListener('visibilitychange',()=>{if(document.hidden){releaseStick();closeMedia();}});window.addEventListener('pagehide',()=>{releaseStick();closeMedia();controlSocket?.close();});
$('#logout').onclick=()=>action($('#logout'),async()=>{
  authStopped=true;releaseStick();closeMedia();controlSocket?.close();render({online:false});
  $('#connection-banner').textContent='正在退出登录…';
  try{await api('auth/logout');csrf='';location.replace('/');}
  catch(error){csrf='';const banner=$('#connection-banner');banner.textContent=error.message;
    const home=document.createElement('a');home.href='https://home.aimaker.vip/';home.textContent='前往家庭控制台确认退出';banner.append(' ',home);}
});
$('#password-open').onclick=()=>$('#password-dialog').showModal();$('#password-close').onclick=()=>$('#password-dialog').close();$('#password-form').onsubmit=async e=>{e.preventDefault();if($('#new-password').value!==$('#confirm-password').value){$('#password-error').textContent='两次新密码不一致';return;}try{await api('auth/password',{old_password:$('#old-password').value,new_password:$('#new-password').value});e.target.reset();closeMedia();location.replace('/');}catch(error){$('#password-error').textContent=error.message;}};
(async()=>{try{const response=await fetch('/api/v1/auth/session');if(authStopped)return;if(!response.ok){location.replace('/');return;}const data=await response.json();if(authStopped)return;csrf=data.csrf;connectControl();poll();}catch(error){toast('无法连接 Mini 服务');}})();
