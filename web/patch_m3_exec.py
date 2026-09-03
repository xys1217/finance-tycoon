# -*- coding: utf-8 -*-
"""给模块3 插入「第零道闸门：可执行性」面板。"""
src = open("/workspace/web/index_src.html", encoding="utf-8").read()

PANEL = """  <div class="panel">
    <h3>第零道闸门：可执行性（20 万本金能不能买）</h3>
    <table class="tbl"><tbody>
      <tr><td>最新价</td><td><b>${ex.price} 元</b></td>
          <td>股价上限</td><td>≤ ${ex.max_price} 元 ${ex.price<=ex.max_price
              ?'<span class="up">✓</span>':'<span class="down">✕ 超限</span>'}</td></tr>
      <tr><td>1 手金额</td><td><b>${ex.lot_amount.toLocaleString()}</b> 元（100 股）</td>
          <td>占本金</td><td>${(ex.lot_amount/200000*100).toFixed(1)}%</td></tr>
      <tr><td>单只预算 6.5%</td><td>${ex.budget.toLocaleString()} 元</td>
          <td>可买</td><td><b class="${ex.hands>0?'up':'down'}">${ex.hands} 手</b>
              ${ex.hands>0?`（约 ${ex.cost.toLocaleString()} 元，占 ${ex.pos_pct}%）`:''}</td></tr>
    </tbody></table>
    ${ex.ok?`<div class="note">可执行：按单只 6.5% 仓位上限可建 <b>${ex.hands} 手</b>。</div>`
      :`<div class="note" style="color:#ef5350"><b>不可执行</b>：${ex.reason}。
        20 万本金下这只票买不出合规仓位，因子分再高也不进组合。</div>`}
  </div>

"""

old = """  <div class="panel">
    <h3>三道不追高闸门</h3>"""
assert old in src, "找不到三道闸门面板锚点"
src = src.replace(old, PANEL + old, 1)

old2 = "  const f=d.factors, g=d.gates, s=d.sentiment, fu=d.fund, nw=d.news;"
new2 = (old2 + '\n  const ex=d.exec||{price:f.price,max_price:120,lot_amount:(f.price||0)*100,'
        'budget:13000,hands:0,cost:0,pos_pct:0,ok:true,reason:null};')
assert old2 in src, "找不到解构锚点"
src = src.replace(old2, new2, 1)

open("/workspace/web/index_src.html", "w", encoding="utf-8").write(src)
print("已插入第零道闸门面板")
