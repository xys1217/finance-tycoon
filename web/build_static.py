# -*- coding: utf-8 -*-
"""把数据快照嵌入到 index.html —— 生成静态离线版本。

关键修复（此前四个模块全空白的根因）：
  数据脚本必须插在 <body> 之后、主脚本之前。
  主脚本里的四个 IIFE 是解析即执行的：
    - 模块1 直接读 window.__QUANT__
    - 模块2/3/4 调 fetch("/api/...") 拿数据
  如果数据脚本放在 </body> 前（之前的写法），它会在所有 IIFE 跑完之后才执行，
  于是 __QUANT__ 是 undefined、fetch 兜底还没装上，页面自然全空。

生成源固定用 index_src.html（干净模板），保证重复执行幂等、不会层层叠加。
"""
import json
import os
import re

WEB = "/workspace/web"


def L(name):
    p = f"{WEB}/api/{name}"
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception as e:
        print(f"  ! {name} 解析失败：{e}")
        return {}


sentiment = L("sentiment_status.json")
mainfund = L("mainfund.json")
quant = L("quant_partial.json")
analyze = L("analyze_cache.json")
signal = L("daily_signal.json")
paper    = L("paper.json")

src_p = f"{WEB}/index_src.html"
if not os.path.exists(src_p):
    raise SystemExit("缺少干净模板 index_src.html")
src = open(src_p, encoding="utf-8").read()

inject = f"""
<!--DATA-INJECT-START-->
<script>
/* 内嵌数据快照：必须早于下方主脚本执行 */
window.__SENTIMENT__ = {json.dumps(sentiment, ensure_ascii=False)};
window.__MAINFUND__  = {json.dumps(mainfund, ensure_ascii=False)};
window.__QUANT__     = {{ css: {json.dumps(quant.get('css', ''), ensure_ascii=False)},
                          html: {json.dumps(quant.get('html', ''), ensure_ascii=False)} }};
window.__ANALYZE__   = {json.dumps(analyze, ensure_ascii=False)};
window.__SIGNAL__    = {json.dumps(signal, ensure_ascii=False)};
window.__PAPER_SNAP__ = {json.dumps(paper, ensure_ascii=False)};

/* 离线兜底：把 /api/* 请求接到内嵌数据上 */
const _origFetch = window.fetch;
window.fetch = async (url, opt) => {{
  const u = String(url);
  // file:// 下不发起真请求：浏览器会直接抛
  // "URL scheme 'file' is not supported" 并往控制台刷一片红，
  // 虽然 catch 后用内嵌数据兜住了，但看着就像页面坏了。离线直接用快照。
  const offline = (location.protocol === "file:");
  if (!offline) {{
    try {{
      const r = await _origFetch(url, opt);
      if (r.ok) return r;
    }} catch (e) {{ /* 落到底下的内嵌数据兜底 */ }}
  }}
  {{
    let data;
    if (u.includes("/api/daily_signal")) data = window.__SIGNAL__;
    else if (u.includes("/api/positions")) data = (window.__SIGNAL__ && window.__SIGNAL__.positions) || {{}};
    else if (u.includes("/api/quant"))   data = window.__QUANT__;
    else if (u.includes("/api/sentiment")) data = window.__SENTIMENT__;
    else if (u.includes("/api/mainfund"))  data = window.__MAINFUND__;
    // 模拟盘故意不劫持：它需要实时行情与落盘，静态页给不了。
    // 让 /api/paper 自然失败，前端会走 POFF() 用 __PAPER_SNAP__ 渲染只读快照。
    else if (u.includes("/api/analyze")) {{
      const m = u.match(/code=(\\d{{6}})/);
      const c = m ? m[1] : "";
      data = (window.__ANALYZE__ && window.__ANALYZE__[c]) ||
             {{ error: "离线快照里没有 " + (c || "该股") + " 的分析。在线模式（server.py）可分析任意个股。" }};
    }}
    else throw e;
    // 假 Response 必须**同时**提供 json() 和 text()。
    // 只给 json() 时，前端 J() 里的 `await r.text()` 会抛
    // "r.text is not a function"，信号卡片直接显示「加载失败」——
    // 而页面其它部分照常渲染，于是「有内容、无 JS 错误」的检查全过，
    // 唯独当日信号是空的。2026-09-04 加 text() 才补上。
    return {{
      ok: true, status: 200,
      json: async () => data || {{}},
      text: async () => JSON.stringify(data || {{}})
    }};
  }}
}};
</script>
<!--DATA-INJECT-END-->"""

# 幂等：先剥掉旧的注入块，再插到 <body> 之后
src = re.sub(r"\n?<!--DATA-INJECT-START-->.*?<!--DATA-INJECT-END-->\n?", "", src, flags=re.S)
if "<body>" not in src:
    raise SystemExit("模板里找不到 <body>")
out = src.replace("<body>", "<body>" + inject, 1)

# 模块1 的注入逻辑：优先读内嵌数据，读不到再走 fetch（在线模式）
old = """try{
    const d = await J("/api/quant");
    document.getElementById("quantBox").innerHTML =
      `<style>${d.css}</style><div id="mod-quant">${d.html}</div>`;
  }catch(e){
    document.getElementById("quantBox").innerHTML =
      `<div class="empty">加载失败：${e}。请确认 server.py 正在运行。</div>`;
  }"""
new = """try{
    const d = await J("/api/quant");
    const css = d.css || "", htm = d.html || "";
    if(!htm) throw new Error("空片段");
    document.getElementById("quantBox").innerHTML =
      `<style>${css}</style><div id="mod-quant">${htm}</div>`;
  }catch(e){
    document.getElementById("quantBox").innerHTML =
      `<div class="empty">量化选股数据未生成：${e}</div>`;
  }"""
if old in out:
    out = out.replace(old, new)

for p in (f"{WEB}/index.html", f"{WEB}/index_static.html"):
    open(p, "w", encoding="utf-8").write(out)

print(f"已生成 index.html / index_static.html（{len(out)//1024} KB）")
print(f"  sentiment : {len(json.dumps(sentiment))//1024} KB / 共振 {len(sentiment.get('merged', []))} 只")
print(f"  mainfund  : {len(json.dumps(mainfund))//1024} KB / 龙虎榜 {mainfund.get('lhb',{}).get('n',0)} 条")
print(f"  quant     : {len(json.dumps(quant))//1024} KB / html {len(quant.get('html',''))} 字符")
print(f"  analyze   : {len(analyze)} 只预生成")
_sd = signal.get("sig_date","—"); _bd = signal.get("bar_date","—")
print(f"  signal    : {_sd}（bar {_bd}）个股 {len(signal.get('picks',[]))} 只 / ETF {len(signal.get('etfs',[]))} 只")
_np = len(paper.get("positions", {})); _nt = len(paper.get("trades", []))
print(f"  paper     : 持仓 {_np} 只 / 流水 {_nt} 笔")
# 顺序自检：数据脚本必须在主脚本之前。
# 锚点别写死整行——曾经写死 "const J = async u =>"，后来给 J 加了 opt 参数，
# 字符串对不上 → 自检恒 FAIL，等于这个检查自己失效了。用稳定前缀即可。
i_data = out.find("window.__SENTIMENT__")
i_main = out.find("const J = async")
if i_main < 0:
    i_main = out.find("async function pLoad")   # 退路：主逻辑里的取数函数
print(f"  顺序自检：数据 @{i_data} < 主脚本 @{i_main} → "
      f"{'OK' if 0 < i_data < i_main else 'FAIL'}")
