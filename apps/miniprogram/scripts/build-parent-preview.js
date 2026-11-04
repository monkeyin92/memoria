const fs = require("node:fs");
const path = require("node:path");
const { renderPage, ROOT } = require("./mp-harness/render");
const { scenarioData } = require("./mp-harness/shoot");
const { PAGES } = require("./mp-harness/pages");

const OUTPUT = process.env.MP_PREVIEW_OUTPUT
  ? path.resolve(process.env.MP_PREVIEW_OUTPUT)
  : path.resolve(__dirname, "../../../docs/design/robot-ui-20261104/preview.html");
const VIEW = { width: 390, height: 844, safeTop: 47, safeBottom: 34 };
const assets = {};
const screens = {};
const flags = ["usageExpanded", "managementExpanded", "memberSettingsExpanded", "storiesExpanded", "diagExpanded"];

function portable(html) {
  return html.replace(/file:\/\/([^"<>\s]+)/g, (match, filename) => {
    const file = decodeURI(filename);
    if (!file.startsWith(`${ROOT}/`)) throw new Error(`Asset outside miniprogram: ${file}`);
    if (!assets[file]) {
      const mime = { ".svg": "image/svg+xml", ".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg" }[path.extname(file)];
      if (!mime) throw new Error(`Unsupported preview asset: ${file}`);
      assets[file] = `data:${mime};base64,${fs.readFileSync(file).toString("base64")}`;
    }
    return `memoria-asset:${file}`;
  });
}

function snapshot(slug, page, data, options = {}) {
  screens[slug] = portable(renderPage(page, data, { ...VIEW, eventHandlers: true, ...options }));
}

async function main() {
  process.env.TZ = "Asia/Shanghai";
  for (const slug of ["home", "memory", "companion", "device", "profile"]) {
    const scenario = PAGES.find((item) => item.slug === slug);
    const { data } = await scenarioData(scenario, VIEW);
    snapshot(slug, scenario.page, data);
    if (slug !== "device") continue;
    for (let mask = 0; mask < 2 ** flags.length; mask += 1) {
      const expanded = Object.fromEntries(flags.map((flag, index) => [flag, Boolean(mask & (1 << index))]));
      snapshot(`device-${mask}`, scenario.page, { ...data, ...expanded });
    }
    const quiet = await scenarioData({ ...scenario, actions: [["openLimitsSheet"]] }, VIEW);
    snapshot("device-quiet", scenario.page, quiet.data, { tabBar: false });
  }
  const payload = JSON.stringify({ screens, assets }).replace(/</g, "\\u003c");
  const html = `<!doctype html>
<html lang="zh">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="apple-mobile-web-app-title" content="Memoria 控制端预览"><title>Memoria · 设备与回顾</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#e7ebf3;color:#1a2440;font-family:-apple-system,"PingFang SC",sans-serif}
main{display:flex;align-items:center;justify-content:center;gap:64px;min-height:100vh;padding:40px}
aside{max-width:330px}h1{font-size:38px;line-height:1.3;letter-spacing:-1px;font-weight:600;margin:16px 0}p{font-size:14px;line-height:1.9;color:#515e78}small{letter-spacing:2px;font-size:12px}
nav{display:flex;flex-wrap:wrap;gap:8px;margin-top:28px}button{border:1px solid #bcc7de;background:transparent;border-radius:30px;color:#1a2440;padding:10px 18px;min-height:44px;cursor:pointer;font:inherit;font-size:13px}button[aria-pressed="true"]{background:#2455ef;color:#fff;border-color:#2455ef}
.phone{position:relative;flex-shrink:0;width:390px;height:844px;border-radius:36px;overflow:hidden;box-shadow:0 24px 70px #1a244026;background:#f2f4f8}iframe{border:0;width:390px;height:844px;display:block}.label{font-size:12px;color:#515e78;margin-top:24px}
#toast{position:absolute;bottom:114px;left:20px;right:20px;padding:12px 18px;background:#1a2440;color:white;font-size:13px;line-height:1.6;border-radius:14px;display:none;z-index:100}
@media(max-width:760px){main{padding:0;display:block}aside{max-width:none;padding:20px}h1{font-size:24px;margin:10px 0}aside p{margin:8px 0}nav{margin-top:12px}.label{margin-top:12px}.phone{margin:auto;border-radius:0;box-shadow:none}}
@media(max-width:389px){.phone{width:100%;overflow-x:auto}}
</style></head><body>
<main><aside><small>MEMORIA / DEVICE CONTROL</small><h1>状态清楚，<br>操作顺手。</h1><p>连接状态、用量与对话回顾各有位置。小程序负责控制和查看，所有设备共用一个默认声音。</p><nav aria-label="预览页面"><button data-screen="home">今天</button><button data-screen="memory">回顾</button><button data-screen="device">设备</button><button data-screen="profile">我的</button></nav><p class="label">由实际 WXML / WXSS 生成。底部导航、设备折叠项、默认声音说明与返回可点击。数据来自本地样例。保存和权限流程仍需微信验证。</p></aside>
<section class="phone" data-screen-label="小程序交互预览"><iframe title="Memoria 小程序预览" sandbox="allow-scripts"></iframe><div id="toast" role="status"></div></section></main>
<script>
const payload=${payload};
const frame=document.querySelector("iframe");
const tabSlugs=["home","memory","device","profile"];
const toggles={toggleUsage:0,toggleManagement:1,toggleMemberSettings:2,toggleStories:3,toggleDiag:4};
let screen="home",mask=0,toastTimer;
function show(next,keepScroll=false){
 screen=next;const scroll=keepScroll?lastScroll:0;lastScroll=0;
 let html=payload.screens[next==="device"?"device-"+mask:next];
 html=html.replace(/memoria-asset:([^"<>\\s]+)/g,(_,file)=>payload.assets[file]);
 const bridge='<script>document.addEventListener("click",function(event){const target=event.target.closest("[data-handler]");if(!target)return;event.preventDefault();parent.postMessage({type:"memoria-preview",handler:target.dataset.handler,index:target.dataset.index,scroll:window.scrollY},"*")});window.scrollTo(0,'+scroll+');<'+"/script>";
 frame.srcdoc=html.replace("</body>",bridge+"</body>");
 for(const button of document.querySelectorAll("[data-screen]"))button.setAttribute("aria-pressed",String(button.dataset.screen===(next.startsWith("device")?"device":next)));
}
let lastScroll=0;
for(const button of document.querySelectorAll("[data-screen]"))button.addEventListener("click",()=>show(button.dataset.screen));
window.addEventListener("message",event=>{
 if(event.source!==frame.contentWindow||event.data?.type!=="memoria-preview")return;
 const {handler,index,scroll}=event.data;lastScroll=scroll;
 if(handler==="onTapTab"){show(tabSlugs[Number(index)]||"home");return}
 if(Object.hasOwn(toggles,handler)){mask^=1<<toggles[handler];show("device",true);return}
 const links={openMemory:"memory",openProfile:"profile",openDevice:"device",openCompanion:"companion",goBack:"device",openLimitsSheet:"device-quiet",openNightSettings:"device-quiet",closeLimitsSheet:"device"};
 if(links[handler]){show(links[handler]);return}
 const toast=document.querySelector("#toast");toast.textContent="这是界面预览。此操作需要在微信小程序中完成。";toast.style.display="block";clearTimeout(toastTimer);toastTimer=setTimeout(()=>toast.style.display="none",2600);
});
show("home");
</script></body></html>`;
  fs.mkdirSync(path.dirname(OUTPUT), { recursive: true });
  fs.writeFileSync(OUTPUT, html);
  console.log(`${OUTPUT}: ${Object.keys(screens).length} screens, ${Object.keys(assets).length} local assets`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
