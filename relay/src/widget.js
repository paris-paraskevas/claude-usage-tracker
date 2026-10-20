// MCP Apps pool widget (blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps).
//
// get_team_overview links this ui:// resource via _meta.ui.resourceUri and returns
// structuredContent; MCP-Apps hosts (claude.ai, Claude Desktop) render it in a sandboxed
// iframe, and non-UI hosts fall back to the text content. In-chat render on a non-partner
// custom server is UNVERIFIED (ext-apps#671), so the data bridge accepts several payload
// shapes (MCP Apps `ui/notifications/tool-result`, plus older/OpenAI-style fields).
//
// Security: every value that reaches innerHTML goes through esc() (account names, devices,
// currency codes, dates — all of it is teammate-supplied data), and the message listener only
// accepts messages from the parent frame with a recognisable payload shape.

export const POOL_UI_URI = "ui://claude-usage-tracker/pool";

export const POOL_WIDGET_HTML = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Team account pool</title>
<style>
 :root,:root[data-theme=light]{color-scheme:light;--bg:#fbf8f3;--fg:#2a251f;--muted:#6b645a;--line:#e4ddd2;--track:#e9e2d7;--accent:#8f6212;--hot:#b0412a;--warn:#9a6a12;--ok:#3a7a4e;--none:#cfc7ba}
 @media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#1c1712;--fg:#e8e2d6;--muted:#9a948a;--line:#2f2a23;--track:#2f2a23;--accent:#cda24e;--hot:#e07a60;--warn:#cda24e;--ok:#6cae80;--none:#4a443c}}
 :root[data-theme=dark]{color-scheme:dark;--bg:#1c1712;--fg:#e8e2d6;--muted:#9a948a;--line:#2f2a23;--track:#2f2a23;--accent:#cda24e;--hot:#e07a60;--warn:#cda24e;--ok:#6cae80;--none:#4a443c}
 html,body{margin:0;background:transparent;font:13px system-ui,'Segoe UI',sans-serif}
 #wrap{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:12px;padding:14px 16px;max-width:860px;box-sizing:border-box}
 .ttl{color:var(--accent);font-weight:600;font-size:15px;margin:0}
 .sub{color:var(--muted);font-size:11px}
 .hot{color:var(--hot)}
 #hd{margin-bottom:8px}
 .row{border-top:1px solid var(--line);padding:10px 0}
 .row.first{border-top:none}
 .top{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
 .nm{min-width:200px;display:flex;flex-direction:column;overflow-wrap:anywhere}
 .w{min-width:150px}
 .wc{font-size:11px;color:var(--muted);margin-bottom:3px}
 .bar{background:var(--track);border-radius:4px;height:8px;overflow:hidden}
 .bar i{display:block;height:100%;background:var(--none)}
 .bar i.ok{background:var(--ok)} .bar i.warn{background:var(--warn)} .bar i.hot{background:var(--hot)}
 .sp{margin-left:auto;text-align:right;display:flex;flex-direction:column}
 ul{list-style:none;margin:0;padding:0}
</style></head><body>
<div id="wrap" aria-live="polite"><div id="hd"></div><div id="cards" class="sub">Waiting for data from the host…</div></div>
<script>
(function(){
  "use strict";
  var rendered=false, INIT_ID="cut-pool-init";
  function esc(s){ return (s==null?"":String(s)).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[c];}); }
  function num(v){ var n=(typeof v==="number")?v:(typeof v==="string"&&v!==""?Number(v):NaN); return isFinite(n)?n:null; }
  function money(v,cur){ var n=num(v); return n==null?"—":esc((cur?cur+" ":"")+n.toFixed(2)); }
  function bar(label,longLabel,p){
    var n=num(p), pct=n==null?"no data":Math.round(n)+"%";
    var lvl=n==null?"":(n>=80?"hot":(n>=60?"warn":"ok"));
    var w=n==null?0:Math.max(0,Math.min(100,n));
    var aria=n==null?"aria-valuetext='no data'":"aria-valuenow='"+Math.round(w)+"' aria-valuetext='"+esc(pct+(n>=80?", near limit":""))+"'";
    return "<div class='w'><div class='wc' aria-hidden='true'>"+esc(label)+" · "+esc(pct)+(n!=null&&n>=80?" · near limit":"")+"</div>"+
      "<div class='bar' role='progressbar' aria-label='"+esc(longLabel)+"' aria-valuemin='0' aria-valuemax='100' "+aria+">"+
      "<i class='"+lvl+"' style='width:"+w+"%'></i></div></div>";
  }
  function cur(d){ var a=Array.isArray(d.accounts)?d.accounts:[]; for(var i=0;i<a.length;i++){ var c=(((a[i]||{}).account||{}).extra||{}).currency; if(typeof c==="string"&&c)return c; } return ""; }
  // Org spend: one total when every account bills in one currency, else one figure per
  // currency (never summed across currencies). Older payloads only carry org_spend.
  function orgSpend(k,d){
    var by=(k.org_spend_by_currency&&typeof k.org_spend_by_currency==="object")?k.org_spend_by_currency:null;
    if(num(k.org_spend)!=null)return money(k.org_spend,typeof k.org_currency==="string"?(k.org_currency||cur(d)):cur(d));
    if(by){ var parts=Object.keys(by).filter(function(c){return num(by[c])!=null;}).map(function(c){return money(by[c],c);});
      if(parts.length)return parts.join(" · "); }
    return "—";
  }
  function shape(x){ return (x&&typeof x==="object"&&!Array.isArray(x)&&(Array.isArray(x.accounts)||(x.kpis&&typeof x.kpis==="object")))?x:null; }
  function render(d){
    d=shape(d); if(!d)return;
    rendered=true;
    var k=(d.kpis&&typeof d.kpis==="object")?d.kpis:{};
    var n=num(k.account_count)||0, near=Array.isArray(k.near)?k.near.length:0;
    document.getElementById("hd").innerHTML="<h2 class='ttl'>Team account pool</h2>"+
      "<div class='sub'>"+esc(d.today||"")+" · "+esc(d.tz||"")+" · org spend this month <b>"+orgSpend(k,d)+"</b> across "+n+" account"+(n===1?"":"s")+
      (near?" · <span class='hot'>"+near+" near a limit</span>":"")+"</div>"+
      "<div class='sub'>Sorted by 5-hour usage, most headroom first.</div>";
    var pool=(Array.isArray(d.accounts)?d.accounts:[]).filter(function(m){return m&&typeof m==="object";}).slice().sort(function(a,b){
      var pa=num((a.account||{}).fh_pct), pb=num((b.account||{}).fh_pct); return (pa==null?1e9:pa)-(pb==null?1e9:pb); });
    var box=document.getElementById("cards");
    box.className="";
    if(!pool.length){ box.innerHTML="<div class='sub'>No accounts in the pool yet — a teammate adds one by signing in to the tracker with it.</div>"; return; }
    box.innerHTML="<ul>"+pool.map(function(m,i){
      var r=(m.account&&typeof m.account==="object")?m.account:{}, e=(r.extra&&typeof r.extra==="object")?r.extra:{}, lu=(m.last_used&&typeof m.last_used==="object")?m.last_used:{};
      var at=num(lu.ts)||num(r.ts); var seen=at?new Date(at*1000).toLocaleString():"no data";
      var meta="last used "+esc(seen)+(lu.device?" · "+esc(lu.device):"")+(lu.by?" · by "+esc(lu.by):"");
      var capPct=num(e.pct);
      return "<li class='row"+(i===0?" first":"")+"'><div class='top'>"+
        "<div class='nm'><b>"+esc(m.name||m.acct||"")+"</b><span class='sub'>"+meta+"</span></div>"+
        bar("5h","5-hour usage",r.fh_pct)+bar("weekly","Weekly usage",r.sd_pct)+
        "<div class='sp'><b>"+(num(m.month_spend)!=null?money(m.month_spend,e.currency):"—")+"</b>"+
        "<span class='sub'>spent since the 1st"+(e.enabled&&capPct!=null?" · "+Math.round(capPct)+"% of cap":"")+"</span></div>"+
        "</div></li>";
    }).join("")+"</ul>";
  }
  function applyHost(ctx){
    if(ctx&&(ctx.theme==="light"||ctx.theme==="dark"))document.documentElement.setAttribute("data-theme",ctx.theme);
  }
  function post(m){ try{ if(window.parent&&window.parent!==window)window.parent.postMessage(m,"*"); }catch(e){} }
  function tryData(){
    try{ if(window.openai&&shape(window.openai.toolOutput)){ render(window.openai.toolOutput); return true; } }catch(e){}
    try{ if(window.openai&&shape(window.openai.structuredContent)){ render(window.openai.structuredContent); return true; } }catch(e){}
    try{ if(shape(window.__mcpToolOutput)){ render(window.__mcpToolOutput); return true; } }catch(e){}
    return false;
  }
  window.addEventListener("message", function(ev){
    // Only the embedding host may talk to us; anything else (other frames, popups) is ignored.
    if(ev.source!==window.parent)return;
    var d=ev.data; if(!d||typeof d!=="object")return;
    if(d.jsonrpc==="2.0"&&d.id===INIT_ID){
      if(d.result&&typeof d.result==="object")applyHost(d.result.hostContext);
      post({jsonrpc:"2.0",method:"ui/notifications/initialized"});
      return;
    }
    var p=(d.params&&typeof d.params==="object")?d.params:{};
    if(d.method==="ui/notifications/host-context-changed"){ applyHost(p); return; }
    var payload=shape(p.structuredContent)||shape(d.structuredContent)||shape(d.toolOutput)||
      shape(d.result&&typeof d.result==="object"?d.result.structuredContent:null)||shape(d);
    if(payload)render(payload);
  });
  window.render=render;
  // MCP Apps handshake, plus the older ready pings some hosts still listen for.
  post({jsonrpc:"2.0",id:INIT_ID,method:"ui/initialize",params:{appInfo:{name:"claude-usage-tracker-pool",version:"0.2.0"},appCapabilities:{},protocolVersion:"2025-06-18"}});
  post({type:"ui/ready"});
  post({type:"openai:ready"});
  if(!tryData()){ setTimeout(tryData,300); setTimeout(tryData,1200); }
  setTimeout(function(){
    if(rendered)return;
    var box=document.getElementById("cards");
    box.className="sub";
    box.textContent="No data received from the host. The tool's text result has the same numbers.";
  },10000);
})();
</script></body></html>`;
