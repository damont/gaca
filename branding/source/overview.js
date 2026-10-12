// Contact sheet of the exported files (reads the real SVG/PNG output, not the sketches).
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');const fs=require('fs'),path=require('path');
const out=process.argv[2], dest=process.argv[3];
const S=f=>fs.readFileSync(path.join(out,'svg',f),'utf8');
const poses=[['in-flight','In flight','App icon'],['wings-high','Wings high','Launch, success'],['wings-level','Wings level','Headers, about'],['wings-down','Wings down','Empty, quiet']];
const cell=(svg,bg,label)=>`<div class="c"><div class="t" style="background:${bg}">${svg}</div><span>${label}</span></div>`;
const noteSvg=fs.readFileSync(path.join(out,'android/res/drawable/ic_notification.xml'),'utf8').match(/pathData="([^"]+)"/)[1];
let html=`<style>body{margin:0;padding:28px;background:#E3E8EE;font:13px system-ui;color:#282F35}h2{font:700 15px system-ui;margin:22px 0 10px}.r{display:flex;gap:14px;flex-wrap:wrap}.c{display:grid;gap:6px;justify-items:center}.t{width:150px;height:150px;border-radius:18px;display:grid;place-items:center}.t svg{width:110px;height:110px}.i svg{width:150px;height:150px;border-radius:34px}span{color:#5A6169;font-size:12px}</style>`;
html+=`<h2>App icon (In flight)</h2><div class="r i">`+cell(S('app-icon-light.svg'),'none','Light (primary)')+cell(S('app-icon-dark.svg'),'none','Dark')+
 cell(S('app-icon-light.svg').replace(/fill="#(?!F0F4F9)[0-9A-F]{6}"/gi,'fill="#FFFFFF"').replace('fill="#F0F4F9"','fill="#000"'),'none','iOS tinted (example)')+
 `<div class="c"><div class="t" style="background:#2D3C4C"><svg viewBox="0 0 24 24" style="width:48px;height:48px"><path fill="#fff" d="${noteSvg}"/></svg></div><span>Status bar (48px)</span></div>`+
 `<div class="c"><div class="t" style="background:#2D3C4C"><svg viewBox="0 0 24 24" style="width:24px;height:24px"><path fill="#fff" d="${noteSvg}"/></svg></div><span>Status bar (24px)</span></div></div>`;
for(const [n,l,u] of poses){html+=`<h2>${l} <span>· ${u}</span></h2><div class="r">`+cell(S(`gaca-${n}-on-light.svg`),'#F0F4F9','On light')+cell(S(`gaca-${n}-on-dark.svg`),'#2D3C4C','On dark')+cell(S(`gaca-${n}-mono.svg`),'#FFFFFF','One colour')+
 `<div class="c"><div class="t" style="background:#F0F4F9"><div style="width:36px;height:36px">${S(`gaca-${n}-on-light.svg`).replace('<svg','<svg style="width:36px;height:36px"')}</div></div><span>At 36px</span></div></div>`;}
(async()=>{const b=await chromium.launch({executablePath:process.env.CHROMIUM_PATH});const p=await b.newPage({viewport:{width:900,height:400},deviceScaleFactor:2});await p.setContent(html);await p.screenshot({path:dest,fullPage:true});await b.close();})();
