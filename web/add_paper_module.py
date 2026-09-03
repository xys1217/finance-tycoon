# -*- coding: utf-8 -*-
"""给 index_src.html 插入「模块5 模拟盘」。

模块 5 依赖后端（实时行情 + 账户读写），静态离线版只能看快照，
所以渲染时会先探测 /api/paper，失败则给出启动引导而不是假装能交易。
"""

# 注意：本脚本是一次性注入器，其内容已固化进 index_src.html。
# 后续修复直接改 index_src.html（唯一改动入口），不要重跑本脚本，
# 否则会把「跟单说明动态回填」等后来的修复覆盖回硬编码文案。

SRC = "/workspace/web/index_src.html"
s = open(SRC, encoding="utf-8").read()

if 'id="m5"' in s:
    raise SystemExit("模块5 已存在，跳过")

# ─────────────────────────── 1. CSS ───────────────────────────
CSS = """
/* ── 模块5 模拟盘 ── */
.pform{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin-top:12px}
.pform label{font-size:12px;color:#8b9199;display:flex;align-items:center;gap:5px}
.pform input,.pform select{background:#1a1f27;border:1px solid #2a3038;color:#e8eaed;
  border-radius:8px;padding:8px 10px;font-size:13px;outline:none}
.pform input:focus,.pform select:focus{border-color:#1a73e8}
.pform input[type=number]{width:130px}
.btn{border:0;border-radius:8px;padding:9px 16px;font-size:13px;cursor:pointer;color:#fff;
  background:linear-gradient(100deg,#1a73e8,#42a5f5)}
.btn:hover{filter:brightness(1.1)}
.btn.red{background:linear-gradient(100deg,#e53935,#ef5350)}
.btn.ghost{background:#232a33;border:1px solid #333b45}
.btn:disabled{opacity:.5;cursor:wait}
.btn-xs{padding:3px 9px;font-size:11.5px;border-radius:6px}
.qbox{background:#1a1f27;border:1px solid #262d36;border-radius:10px;padding:13px 15px;margin-top:12px}
.qbox.bad{border-left:3px solid #ef5350}
.qhead{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:9px}
.qhead .nm{font-size:16px;color:#fff;font-weight:600}
.qhead .px{font-size:22px;font-weight:700}
.qmeta{display:flex;gap:7px;flex-wrap:wrap;font-size:11.5px;color:#8b9199}
.qmeta span{background:#141920;padding:3px 8px;border-radius:6px}
.qerr{color:#ef5350;font-size:12.5px;margin-top:9px;line-height:1.6}
.seg{display:inline-flex;background:#141920;border-radius:7px;padding:2px}
.seg button{background:transparent;border:0;color:#8b9199;padding:6px 12px;font-size:12px;
  cursor:pointer;border-radius:5px}
.seg button.on{background:#1a73e8;color:#fff}
.chart{width:100%;height:220px;background:#141920;border-radius:9px;overflow:hidden}
.pnote{font-size:12px;color:#8b9199;margin-top:9px;line-height:1.7}
.bar{height:26px;background:#1a1f27;border-radius:6px;position:relative;overflow:hidden;margin:7px 0}
.bar i{position:absolute;left:0;top:0;bottom:0;border-radius:6px;display:block}
.bar span{position:absolute;right:10px;top:4px;font-size:12px;color:#e8eaed}
"""

s = s.replace("</style>", CSS + "</style>", 1)

# ─────────────────────────── 2. tab ───────────────────────────
s = s.replace(
    '  <div class="tab" data-mod="m4"><span class="n">04</span>主力动向</div>',
    '  <div class="tab" data-mod="m4"><span class="n">04</span>主力动向</div>\n'
    '  <div class="tab" data-mod="m5"><span class="n">05</span>模拟盘</div>', 1)

# ─────────────────────────── 3. 模块 HTML ───────────────────────────
M5 = '''
<!-- ══════════ 模块 5 模拟盘 ══════════ -->
<div class="mod" id="m5">
  <div class="mhd">模拟盘 · 20 万本金随时买卖</div>
  <div class="msub">成本口径与回测<b>完全一致</b>：整手 100 股/份 · 佣金 0.025%（最低 5 元）·
  滑点 0.1% · 卖出印花税 0.05%（ETF 免）· 剔除科创板/北交所/B股 · 个股股价 ≤ 120 元。
  按<b>实时价</b>撮合，盘中每刷新一次就变。</div>

  <div class="kpis" id="pKpi"></div>
  <div id="pAlert"></div>

  <div class="panel">
    <h3>下单</h3>
    <div class="search">
      <input id="pCode" placeholder="输入 6 位代码，如 601872（股票）或 512040（ETF）" maxlength="6">
      <button id="pQuote">查价</button>
    </div>
    <div class="chips" id="pChips"></div>
    <div id="pQuoteBox"></div>
  </div>

  <div class="panel">
    <h3>持仓明细</h3>
    <div class="scroll"><table class="tbl" id="pPos"></table></div>
    <div class="pnote" id="pSector"></div>
  </div>

  <div class="panel">
    <h3>净值曲线 vs 沪深300</h3>
    <div id="pChart"></div>
    <div class="pnote" id="pChartNote"></div>
  </div>

  <div class="panel">
    <h3>板块分布</h3>
    <div id="pSectorBar"></div>
  </div>

  <div class="panel">
    <h3>交易流水</h3>
    <div class="scroll"><table class="tbl" id="pTrades"></table></div>
  </div>

  <div class="panel">
    <h3>v5 定案组合 · 一键跟单</h3>
    <div class="pnote" id="pPlanNote">读取当日信号…</div>
    <div class="pform">
      <button class="btn" id="pPlan">一键跟单建仓</button>
      <button class="btn ghost" id="pReset">清空账户重置</button>
      <button class="btn ghost" id="pRefresh">刷新行情</button>
    </div>
    <div id="pPlanLog"></div>
  </div>
</div>
'''

anchor = '\n<div class="foot">'
assert anchor in s, "找不到 foot 锚点"
s = s.replace(anchor, "\n" + M5 + anchor, 1)

# ─────────────────────────── 4. JS ───────────────────────────
JS = r'''
/* ── 模块5：模拟盘（需后端；离线只显示引导）── */
const PN = (n, d=2) => (n==null||isNaN(n)) ? "—" : Number(n).toLocaleString("zh-CN",
  {minimumFractionDigits:d, maximumFractionDigits:d});
const PS = n => (n==null||isNaN(n)) ? "—" : (n>0?"+"+Number(n).toFixed(2):Number(n).toFixed(2));
const PCLS = n => n>0?"up":(n<0?"down":"");
const POFF = async () => {
  document.getElementById("pKpi").innerHTML =
    `<div class="kpi"><label>账户状态</label><b class="r" style="font-size:15px">未连接后端</b></div>
     <div class="kpi"><label>本金</label><b>200,000</b></div>`;
  document.getElementById("pAlert").innerHTML =
    `<div class="alert"><b>模拟盘需要后端服务才能交易。</b>
     在 <code>web/</code> 目录执行 <code>python3 server.py 8899</code>，
     再打开 <code>http://127.0.0.1:8899/?tab=m5</code>。<br>
     原因是下单要取<b>实时行情</b>、账户要落盘保存，静态 HTML 做不到这两件事 ——
     与其给你一个假装在交易的界面，不如直说。</div>`;
  ["pPos","pTrades","pChart","pSector","pSectorBar","pQuoteBox","pChips"].forEach(id=>{
    const e=document.getElementById(id); if(e) e.innerHTML="";
  });
};

async function pLoad(){
  let d;
  try{ d = await J("/api/paper"); }
  catch(e){ return POFF(); }
  if(d.error){ document.getElementById("pAlert").innerHTML =
    `<div class="alert">后端报错：${d.error}</div>`; return; }
  pRender(d);
}

function pRender(d){
  // KPI
  document.getElementById("pKpi").innerHTML=`
    <div class="kpi"><label>总权益</label><b>${PN(d.equity,0)}</b></div>
    <div class="kpi"><label>总盈亏</label><b class="${PCLS(d.total_pnl)}">${PS(d.total_pnl)}
      <span style="font-size:13px">(${PS(d.total_pnl_pct)}%)</span></b></div>
    <div class="kpi"><label>浮动盈亏</label><b class="${PCLS(d.float_pnl)}">${PS(d.float_pnl)}</b></div>
    <div class="kpi"><label>已实现盈亏</label><b class="${PCLS(d.realized_pnl)}">${PS(d.realized_pnl)}</b></div>
    <div class="kpi"><label>可用现金</label><b>${PN(d.cash,0)}
      <span style="font-size:12px;color:#8b9199">（${d.cash_weight}%）</span></b></div>
    <div class="kpi"><label>持仓市值</label><b>${PN(d.market_value,0)}</b></div>
    <div class="kpi"><label>超额 vs 沪深300</label>
      <b class="${PCLS(d.alpha)}">${d.alpha==null?"—":PS(d.alpha)+"pp"}</b></div>`;

  // 风险提醒
  const al=d.alerts||[];
  document.getElementById("pAlert").innerHTML = al.length ? al.map(a=>{
    const c = a.level==="high"?"#ef5350":(a.level==="mid"?"#ff9800":"#8b9199");
    return `<div class="note" style="border-left-color:${c};margin-bottom:8px">
      <b style="color:${c}">${a.level==="high"?"⚠ 高风险":a.level==="mid"?"⚡ 注意":"ℹ 提示"}</b>
      ${a.code?`· ${a.code} `:""}${a.name} —— ${a.msg}</div>`;
  }).join("") : `<div class="note" style="border-left-color:#4caf50">
      ✅ 当前无风险提醒（单只仓位、行业集中度、止损线、现金垫均在阈值内）</div>`;

  // 持仓
  const pos=d.positions||[];
  document.getElementById("pPos").innerHTML = pos.length ? `
    <thead><tr><th>代码</th><th>名称</th><th>板块</th><th>数量</th><th>成本价</th><th>现价</th>
      <th>今日</th><th>市值</th><th>浮动盈亏</th><th>操作</th></tr></thead>
    <tbody>${pos.map(p=>`<tr>
      <td><b>${p.code}</b></td><td>${p.name}</td>
      <td><span class="tag t-em">${p.sector}</span></td>
      <td>${p.shares.toLocaleString()}</td>
      <td>${PN(p.avg_cost,3)}</td><td><b>${PN(p.price,3)}</b></td>
      <td class="${PCLS(p.pct_today)}">${PS(p.pct_today)}%</td>
      <td>${PN(p.market_value,0)}</td>
      <td class="${PCLS(p.pnl)}"><b>${PS(p.pnl)}</b> (${PS(p.pnl_pct)}%)</td>
      <td><button class="btn red btn-xs" data-sell="${p.code}">全卖</button>
          <button class="btn ghost btn-xs" data-half="${p.code}">卖半</button></td></tr>`).join("")}</tbody>`
    : `<tbody><tr><td class="empty">空仓。点下方「一键跟单建仓」按 v5 定案配比买入，
       或在上面输入代码自己下单。</td></tr></tbody>`;

  document.querySelectorAll("[data-sell]").forEach(b=>b.onclick=()=>pSell(b.dataset.sell,1));
  document.querySelectorAll("[data-half]").forEach(b=>b.onclick=()=>pSell(b.dataset.half,0.5));

  // 板块
  const secs=d.sectors||[];
  document.getElementById("pSectorBar").innerHTML = secs.length ? secs.map(x=>`
    <div style="display:flex;align-items:center;gap:10px;margin:6px 0">
      <div style="width:76px;font-size:12.5px;color:#cfd4da">${x.sector}</div>
      <div class="bar" style="flex:1;margin:0">
        <i style="width:${Math.min(x.weight,100)}%;background:linear-gradient(90deg,#1a73e8,#42a5f5)"></i>
        <span>${PN(x.value,0)} 元 · ${x.weight}%</span></div></div>`).join("")
    : `<div class="pnote">空仓，无板块分布。</div>`;

  // 净值曲线
  pChart(d.snapshots||[], d);

  // 流水
  const tr=d.trades||[];
  document.getElementById("pTrades").innerHTML = tr.length ? `
    <thead><tr><th>时间</th><th>方向</th><th>代码</th><th>名称</th><th>数量</th>
      <th>成交价</th><th>金额</th><th>佣金</th><th>印花税</th><th>已实现盈亏</th></tr></thead>
    <tbody>${tr.map(t=>`<tr>
      <td style="font-size:11.5px;color:#8b9199">${t.ts}</td>
      <td><b class="${t.side==="买入"?"up":"down"}">${t.side}</b></td>
      <td>${t.code}</td><td>${t.name}</td><td>${t.shares.toLocaleString()}</td>
      <td>${PN(t.price,3)}</td><td>${PN(t.amount,2)}</td>
      <td>${PN(t.fee,2)}</td><td>${PN(t.tax,2)}</td>
      <td class="${PCLS(t.pnl)}">${t.pnl==null?"—":PS(t.pnl)+" ("+PS(t.pnl_pct)+"%)"}</td></tr>`).join("")}</tbody>`
    : `<tbody><tr><td class="empty">暂无交易记录</td></tr></tbody>`;

  // 快捷代码 chip
  const hot=[...new Set([...(d.plan||[]).map(x=>x.code),
                         ...(d.positions||[]).map(x=>x.code)])];
  document.getElementById("pChips").innerHTML =
    hot.map(c=>`<span class="chip" data-p="${c}">${c}</span>`).join("");
  document.querySelectorAll("#pChips .chip").forEach(c=>
    c.onclick=()=>{document.getElementById("pCode").value=c.dataset.p; pQuote();});
}

function pChart(sn, d){
  const box=document.getElementById("pChart"), note=document.getElementById("pChartNote");
  if(sn.length<2){
    // 快照不足：退回「今日账户 vs 沪深300」对比，第一天也有东西看
    const me=d.total_pnl_pct||0, base=(d.hs300&&d.hs300.pct)||0;
    const w=v=>Math.min(Math.abs(v)/Math.max(Math.abs(me),Math.abs(base),0.5)*46,48);
    box.innerHTML=`<div class="chart" style="height:auto;padding:18px">
      <div style="font-size:12.5px;color:#8b9199;margin-bottom:12px">
        账户今日 ${PS(me)}%　·　沪深300 今日 ${PS(base)}%　·
        超额 ${PS((me-base).toFixed?0:0)}${PS(me-base)}pp</div>
      ${[["我的账户",me,"#ff9800"],["沪深300",base,"#42a5f5"]].map(([nm,v,c])=>`
        <div style="display:flex;align-items:center;gap:10px;margin:8px 0">
          <div style="width:70px;font-size:12.5px;color:#cfd4da">${nm}</div>
          <div class="bar" style="flex:1;margin:0;height:22px">
            <i style="width:${w(v)}%;left:${v>=0?50:50-w(v)}%;background:${c}"></i>
            <span class="${PCLS(v)}">${PS(v)}%</span></div></div>`).join("")}
      <div style="text-align:center;font-size:11.5px;color:#6b7280;margin-top:10px">
        — 0 —</div></div>`;
    note.innerHTML=`净值曲线需要跨交易日积累：建仓日起每天自动记一次快照（含沪深300 点位），
      攒够 2 天就能画出真实曲线。上面先用<b>今日涨跌对比</b>顶着。`;
    return;
  }
  // 折线：账户 vs 基准，都归一化到建仓日 = 0
  const W=880,H=220,P=34;
  const e0=sn[0].equity, b0=sn[0].hs300;
  const rs=sn.map(x=>({d:x.date, a:(x.equity/e0-1)*100,
                       b:(b0&&x.hs300)?(x.hs300/b0-1)*100:null}));
  const all=rs.flatMap(r=>[r.a,r.b]).filter(v=>v!=null);
  let lo=Math.min(...all,0), hi=Math.max(...all,0);
  const pad=(hi-lo)*0.15||0.5; lo-=pad; hi+=pad;
  const X=i=>P+i*(W-2*P)/Math.max(rs.length-1,1);
  const Y=v=>H-P-(v-lo)/(hi-lo)*(H-2*P);
  const line=(k,c)=>rs.map((r,i)=>`${i?"L":"M"}${X(i).toFixed(1)},${Y(r[k]).toFixed(1)}`).join(" ");
  box.innerHTML=`<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    ${[0,.5,1].map(t=>{const v=lo+(hi-lo)*t;
      return `<line x1="${P}" x2="${W-P}" y1="${Y(v)}" y2="${Y(v)}"
        stroke="#262d36" stroke-width="1"/>
        <text x="${P-6}" y="${Y(v)+4}" fill="#6b7280" font-size="10"
          text-anchor="end">${v.toFixed(1)}%</text>`;}).join("")}
    <path d="${line("b","")}" fill="none" stroke="#42a5f5" stroke-width="1.8"
      stroke-dasharray="5 3" opacity=".85"/>
    <path d="${line("a","")}" fill="none" stroke="#ff9800" stroke-width="2.2"/>
    ${rs.map((r,i)=>`<circle cx="${X(i)}" cy="${Y(r.a)}" r="2.6" fill="#ff9800"/>`).join("")}
    ${rs.map((r,i)=>i%Math.ceil(rs.length/6)===0||i===rs.length-1
      ?`<text x="${X(i)}" y="${H-10}" fill="#6b7280" font-size="9.5"
         text-anchor="middle">${r.d.slice(5)}</text>`:"").join("")}
  </svg>
  <div style="display:flex;gap:16px;font-size:12px;margin-top:8px;padding-left:6px">
    <span><i style="display:inline-block;width:16px;height:2.5px;background:#ff9800;
      vertical-align:middle;margin-right:5px"></i>我的账户</span>
    <span><i style="display:inline-block;width:16px;height:2.5px;background:#42a5f5;
      vertical-align:middle;margin-right:5px"></i>沪深300</span>
  </div>`;
  note.innerHTML=`建仓日 ${sn[0].date}（沪深300 ${PN(b0,2)}）起算，两条线都归零到同一起点。
    ${sn.length} 个交易日快照，每次下单会自动补记当天。`;
}

/* ── 查价 + 下单 ── */
async function pQuote(){
  const code=document.getElementById("pCode").value.trim(), box=document.getElementById("pQuoteBox");
  if(!/^\d{6}$/.test(code)){ box.innerHTML=`<div class="qerr">请输入 6 位数字代码</div>`; return; }
  box.innerHTML=`<div class="qbox">查价中…</div>`;
  let q; try{ q=await J("/api/paper/quote?code="+code); }
  catch(e){ box.innerHTML=`<div class="qbox bad"><div class="qerr">查价失败：${e}（需要后端服务）</div></div>`; return; }
  if(q.error&&q.price==null){ box.innerHTML=`<div class="qbox bad"><div class="qerr">${q.error}</div></div>`; return; }

  const held=(window.__PAPER__&&window.__PAPER__.positions||{})[code];
  box.innerHTML=`<div class="qbox ${q.ok?"":"bad"}">
    <div class="qhead">
      <span class="nm">${q.name||code} <span style="font-size:12px;color:#8b9199">${code}</span></span>
      <span class="px ${PCLS(q.pct)}">${PN(q.price,3)}</span>
      <span class="${PCLS(q.pct)}" style="font-size:13px">${PS(q.pct)}%</span>
      ${q.etf?`<span class="tag t-em">ETF · 免印花税</span>`:`<span class="tag t-page">股票</span>`}
      ${q.trading?`<span class="tag t-xq">盘中实时</span>`:`<span class="tag t-news">非交易时段·收盘价</span>`}
    </div>
    <div class="qmeta">
      <span>昨收 ${PN(q.prev_close,3)}</span>
      <span>涨停 ${PN(q.limit_up,3)}</span>
      <span>跌停 ${PN(q.limit_down,3)}</span>
      <span>买入价（含滑点）<b>${PN(q.buy_price,4)}</b></span>
      <span>1 手成本 <b>${PN(q.lot_cost,2)}</b> 元</span>
      ${held?`<span>已持有 <b>${held.shares}</b> 份 @ ${PN(held.avg_cost,3)}</span>`:""}
    </div>
    ${q.error?`<div class="qerr">⛔ ${q.error}</div>`:`
    <div class="pform">
      <div class="seg"><button id="sgBuy" class="on">买入</button><button id="sgSell">卖出</button></div>
      <div class="seg"><button id="smSh" class="on">按数量</button><button id="smAmt">按金额</button></div>
      <input type="number" id="pVal" placeholder="按数量：整手" value="100" step="100" min="100">
      <button class="btn" id="pGo">提交</button>
      <span style="font-size:11.5px;color:#6b7280">数量须为 100 的整数倍；按金额则自动向下取整到整手</span>
    </div>
    <div id="pMsg"></div>`}
  </div>`;

  const bb=document.getElementById("sgBuy"), sb=document.getElementById("sgSell"),
        sh=document.getElementById("smSh"), am=document.getElementById("smAmt"),
        vi=document.getElementById("pVal");
  let side="buy", mode="shares";
  const sync=()=>{
    bb.classList.toggle("on",side==="buy"); sb.classList.toggle("on",side==="sell");
    sh.classList.toggle("on",mode==="shares"); am.classList.toggle("on",mode==="amount");
    vi.step = mode==="shares"?"100":"0.01";
    vi.placeholder = mode==="shares"?"手数 ×100，如 500":"金额（元），如 8000";
    document.getElementById("pGo").className = "btn"+(side==="sell"?" red":"");
    document.getElementById("pGo").textContent = (side==="buy"?"买入 ":"卖出 ")+(q.name||code);
  };
  bb.onclick=()=>{side="buy";sync();}; sb.onclick=()=>{side="sell";sync();};
  sh.onclick=()=>{mode="shares";vi.value=100;sync();};
  am.onclick=()=>{mode="amount";vi.value=8000;sync();};
  sync();
  document.getElementById("pGo").onclick=async()=>{
    const btn=document.getElementById("pGo"), msg=document.getElementById("pMsg");
    btn.disabled=true; msg.innerHTML=`<div class="pnote">提交中…</div>`;
    try{
      const r=await J("/api/paper/order",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({code, side, mode, value:parseFloat(vi.value)||0})});
      msg.innerHTML=`<div class="${r.ok?"note":"qerr"}" style="margin-top:10px">${r.msg||r.error||""}</div>`;
      if(r.state){ window.__PAPER__=r.state; pRender(r.state); }
    }catch(e){ msg.innerHTML=`<div class="qerr">下单失败：${e}</div>`; }
    btn.disabled=false;
  };
}

async function pSell(code, ratio){
  const st=window.__PAPER__; if(!st) return;
  const p=(st.positions||[]).find(x=>x.code===code); if(!p) return;
  let n=Math.floor(p.shares*ratio/100)*100;
  if(n<100){ alert("不足 1 手，无法卖出"); return; }
  if(!confirm(`确定卖出 ${p.name} ${n} 份？\n当前现价 ${PN(p.price,3)}，成本 ${PN(p.avg_cost,3)}`)) return;
  const r=await J("/api/paper/order",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({code, side:"sell", mode:"shares", value:n})});
  if(r.state){ window.__PAPER__=r.state; pRender(r.state); }
  else alert(r.msg||"卖出失败");
}

document.getElementById("pQuote").addEventListener("click",pQuote);
document.getElementById("pCode").addEventListener("keydown",e=>{if(e.key==="Enter")pQuote();});
document.getElementById("pRefresh").addEventListener("click",pLoad);
document.getElementById("pPlan").addEventListener("click",async()=>{
  if(!confirm("按 v5 定案组合一键建仓？\n将按实时价买入 17 只标的（防御仓 26% + 热点仓 4% + ETF 腿 58%），留约 12% 现金。")) return;
  const b=document.getElementById("pPlan"); b.disabled=true; b.textContent="建仓中…";
  try{
    const r=await J("/api/paper/plan",{method:"POST"});
    const ok=(r.log||[]).filter(x=>x.ok).length;
    document.getElementById("pPlanLog").innerHTML=`
      <div class="note" style="margin-top:12px">建仓完成：<b>${ok}/${(r.log||[]).length}</b> 只成功。
        ${(r.log||[]).filter(x=>!x.ok).map(x=>`<br>· ${x.code} ${x.name}：${x.msg}`).join("")}</div>`;
    if(r.state){ window.__PAPER__=r.state; pRender(r.state); }
  }catch(e){ document.getElementById("pPlanLog").innerHTML=`<div class="qerr">${e}</div>`; }
  b.disabled=false; b.textContent="一键跟单建仓";
});
document.getElementById("pReset").addEventListener("click",async()=>{
  if(!confirm("清空账户，恢复 200,000 元现金？所有持仓与流水将被删除，不可恢复。")) return;
  const r=await J("/api/paper/reset",{method:"POST"});
  if(r.state){ window.__PAPER__=r.state; pRender(r.state);
    document.getElementById("pPlanLog").innerHTML=`<div class="note">${r.msg}</div>`; }
});
pLoad();
'''

anchor2 = "\n</script>"
i = s.rfind(anchor2)
assert i > 0
s = s[:i] + "\n" + JS + s[i:]

open(SRC, "w", encoding="utf-8").write(s)
print("已插入模块5")
