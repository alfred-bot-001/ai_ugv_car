"""Build a local-only, offline review gallery; never copy or upload source photos."""

# ruff: noqa: E501
# Embedded offline HTML/CSS/JS keeps its own compact formatting.

import argparse
import json
from datetime import datetime
from pathlib import Path

PAGE = """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'none'">
<title>老三 · 人脸审核相册</title>
<style>
:root{color-scheme:light;--ink:#153b31;--paper:#f3f0e6;--line:#ced5c6}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:"PingFang SC","Heiti SC",sans-serif}
header,main{max-width:1400px;margin:auto;padding:32px}header{border-bottom:1px solid var(--line)}
.eyebrow{letter-spacing:.18em;font-size:12px}h1{font-family:"Songti SC",serif;font-size:clamp(32px,5vw,58px);font-weight:600;margin:12px 0}
.summary{display:flex;align-items:end;justify-content:space-between;gap:24px;flex-wrap:wrap}.total{font-size:48px;font-family:Georgia,serif}.muted{color:#607468;font-size:13px;line-height:1.8}
.toolbar{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:28px}select,button{font:inherit;color:inherit;border:1px solid var(--line);border-radius:6px;background:#fffdf5;padding:12px;cursor:pointer}button:focus-visible,select:focus-visible{outline:3px solid #b27536;outline-offset:3px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(245px,1fr));gap:24px}.card{display:block;text-align:left;padding:0;overflow:hidden;border:0;box-shadow:0 1px 0 var(--line);background:#fffdf7}.card:hover{box-shadow:0 4px 18px #153b311a}.card img{width:100%;aspect-ratio:640/597;object-fit:contain;display:block;background:#132a23}.caption{padding:14px}.caption strong{display:block;font-size:15px;margin:5px 0}.caption time{font-size:12px;color:#607468}.caption p{margin:4px 0;font-size:12px;line-height:1.6;color:#607468}
dialog{border:0;border-radius:8px;background:#10271f;color:#faf6e9;width:min(1100px,96vw);max-height:96vh;padding:18px}dialog::backdrop{background:#071b15dd}dialog img{display:block;max-width:100%;max-height:70vh;margin:14px auto;object-fit:contain}dialog .bar{display:flex;align-items:center;justify-content:space-between;gap:12px}dialog button{background:#204336;color:white;border-color:#436354}.details{font-size:14px;line-height:1.7}.empty{padding:60px;text-align:center}footer{padding:40px 32px;color:#607468;font-size:12px;text-align:center}
@media(max-width:600px){header,main{padding:20px}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.caption{padding:10px}.caption strong{font-size:13px}dialog{padding:12px}.toolbar{gap:10px}}
</style>
<header><div class="eyebrow">老三 / LOCAL REVIEW</div><div class="summary"><div><h1>人脸审核相册</h1><div class="muted">仅保留检测到人脸的照片 · 本机离线查看 · 不上传</div></div><div><span class="total" id="total"></span> 张<div class="muted" id="generated"></div></div></div></header>
<main><div class="toolbar"><label>人物 <select id="person"><option value="">全部人物</option></select></label><label>日期 <select id="day"><option value="">全部日期</option></select></label><span id="count" class="muted"></span></div><div class="grid" id="grid"></div><p class="empty" id="empty" hidden>没有符合筛选条件的照片</p></main>
<footer>点击照片放大，使用 ← → 切换，Esc 关闭。名字与动作均为模型结果，供人工检查。此相册为生成时的本地快照。</footer>
<dialog id="viewer"><div class="bar"><span id="position"></span><button id="close" autofocus>关闭 ×</button></div><img id="large" alt="带人物与动作标注的审核照片"><div class="details" id="detail"></div><div class="bar"><button id="prev">← 上一张</button><button id="next">下一张 →</button></div></dialog>
<script type="application/json" id="data">__DATA__</script>
<script>
'use strict';
const payload=JSON.parse(document.getElementById('data').textContent),all=payload.photos;
const $=id=>document.getElementById(id);let filtered=all,index=0;
$('total').textContent=all.length;$('generated').textContent='生成于 '+payload.generated;
for(const [id,values] of [['person',[...new Set(all.flatMap(p=>p.names))].sort()],['day',[...new Set(all.map(p=>p.day))].sort().reverse()]])for(const value of values){const option=document.createElement('option');option.value=value;option.textContent=value;$(id).append(option);}
function source(photo){return './'+encodeURIComponent(photo.file);}
function show(i){index=(i+filtered.length)%filtered.length;const p=filtered[index];$('large').src=source(p);$('position').textContent=(index+1)+' / '+filtered.length;$('detail').textContent=p.time+' · '+p.names.join('、')+' · '+p.actions.join('；');if(!$('viewer').open)$('viewer').showModal();}
function render(){filtered=all.filter(p=>(!$('person').value||p.names.includes($('person').value))&&(!$('day').value||p.day===$('day').value));$('grid').replaceChildren();$('count').textContent='显示 '+filtered.length+' 张';$('empty').hidden=filtered.length>0;filtered.forEach((p,i)=>{const button=document.createElement('button');button.className='card';button.setAttribute('aria-label',p.time+' '+p.names.join('、'));const img=document.createElement('img');img.src=source(p);img.loading='lazy';img.alt='审核照片：'+p.names.join('、');const caption=document.createElement('div');caption.className='caption';const time=document.createElement('time');time.textContent=p.time;const names=document.createElement('strong');names.textContent=p.names.join('、');const actions=document.createElement('p');actions.textContent=p.actions.join('；')||'人体姿态无法判断';caption.append(time,names,actions);button.append(img,caption);button.onclick=()=>show(i);$('grid').append(button);});}
$('person').onchange=render;$('day').onchange=render;$('close').onclick=()=>$('viewer').close();$('prev').onclick=()=>show(index-1);$('next').onclick=()=>show(index+1);document.addEventListener('keydown',event=>{if(!$('viewer').open)return;if(event.key==='ArrowLeft'){event.preventDefault();show(index-1);}if(event.key==='ArrowRight'){event.preventDefault();show(index+1);}});render();
</script></html>"""


def build(directory: Path) -> Path:
    photos = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        record = json.loads(path.read_text())
        picture = path.with_suffix(".jpg")
        if (
            not picture.is_file()
            or record.get("recognition") != "active"
            or not record.get("faces")
        ):
            continue
        stamp = str(record.get("sampled_at", path.stem))
        photos.append(
            {
                "file": picture.name,
                "time": stamp[:19].replace("T", " "),
                "day": stamp[:10],
                "names": sorted({str(face.get("label", "未登记")) for face in record["faces"]}),
                "actions": [
                    f"{p.get('identity', '人物未确认')} · {p.get('posture_label', '无法判断')}"
                    f" · {p.get('action_label', '无明显动作')}"
                    for p in record.get("pose", {}).get("people", [])
                ],
            }
        )
    data = json.dumps(
        {"photos": photos, "generated": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")},
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    target = directory / "人脸审核相册.html"
    target.write_text(PAGE.replace("__DATA__", data))
    target.chmod(0o600)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成本机离线人脸审核相册")
    parser.add_argument("directory", type=Path)
    print(build(parser.parse_args().directory))
