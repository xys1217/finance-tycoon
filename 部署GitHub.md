# 部署到 GitHub Actions：5 步，约 10 分钟

代码侧**已经全部就绪**，只剩推仓库 + 开 Pages。
沙箱里没有 `gh` 登录、也没有 remote，这几步得在你自己机器上做。

---

## 第 1 步：建仓库

打开 <https://github.com/new>，建一个仓库（**不要**勾 README / .gitignore，
本地已经有内容了）。公开或私有都行：

- **公开**：Actions 分钟数不限
- **私有**：每月 2000 分钟，本 workflow 每天约 2 分钟，够用

建好后 GitHub 会给你一个地址，形如
`git@github.com:<用户名>/<仓库名>.git`

---

## 第 2 步：推送

**本次已经替你推好了。** 仓库地址：

```
https://github.com/xys1217/finance-tycoon
```

如果你以后换机器还要推送，注意：沙箱到 GitHub 的 **22 端口 SSH 会被墙 / 超时**，
需要让 SSH 走 **443 端口**。在本机 `~/.ssh/config` 里写：

```
Host github.com
    Hostname ssh.github.com
    Port 443
    User git
    IdentityFile ~/.ssh/id_ed25519
    StrictHostKeyChecking no
    IdentitiesOnly yes
```

然后再推：

```bash
cd <你的项目目录>
git remote add origin git@github.com:xys1217/finance-tycoon.git
git push -u origin main
```

> 仓库里已经 `.gitignore` 掉了 K 线缓存和日志，push 体积不大。
> `docs/`（Pages 部署源）和 `.github/`（workflow）**必须**推上去。

---

## 第 3 步：开启 Pages

仓库页面 → **Settings → Pages**

| 项 | 选 |
|---|---|
| Source | `Deploy from a branch` |
| Branch | `main` |
| Folder | **`/docs`** |

保存，等 1–2 分钟。之后访问：

```
https://<用户名>.github.io/<仓库名>/
```

就是当日信号页面 —— 不用起 server，不用下载，手机也能开。
**建议在手机上存个书签。**

---

## 第 4 步：手动触发一次（必做）

仓库页面 → **Actions** → 左侧 `每日 A 股交易信号` → 右边 `Run workflow`

这一步回答的是本方案**唯一的不确定项**：
**GitHub 的 runner 在境外，能不能连上腾讯 / 新浪 / 东财的行情接口。**

- **绿了** → 点进去看 Summary，会列出信号日、动作、调仓日、投入金额。
  再到 Pages 页面确认信号日期是今天。
- **红了** → Summary 会明写「行情数据源在此网络环境下不可用」，
  此时换方案（见文末「如果连不上」）。

workflow 里内置了连通性自检，连不上会显式 `exit 1`，
**不会**出现「任务绿了但其实什么都没跑」的情况。

---

## 第 5 步：之后就不用管了

北京时间**周一至周五 06:00 / 07:00 / 08:30** 各触发一次。

- 任一一次成功即可，后两次发现当天信号已生成会自动跳过
- 非交易日由脚本内的交易日历判断，自动跳过
- 排三次是因为 GitHub 官方说明 schedule 在高峰期会延迟，
  只排 08:30 一次可能错过 09:30 开盘

---

## 怎么确认它真的在跑

**不要只看 Actions 页面是不是绿的。**

本地定时任务那次事故的教训是：状态显示「已执行」、时间也往前走，
但什么都没发生。所以每天花 3 秒：

1. 打开 Pages 书签
2. 看一眼**信号日期**是不是今天

那才是唯一的真相。

---

## 如果连不上（境外 runner 访问国内行情源失败）

三个出路，按推荐顺序：

### 1. 自己的服务器 / NAS（最可靠）

任何一台 7×24 开机的 Linux 机器，拷过去挂 cron：

```bash
30 8 * * 1-5 /path/to/web/run_daily_signal.sh >> /path/to/logs/cron.log 2>&1
```

脚本路径是自动定位的，放哪都能跑。国内网络访问行情源没问题。

### 2. 自托管 runner

在一台国内机器上安装 [self-hosted runner](https://docs.github.com/en/actions/hosting-your-own-runners)，
然后把 workflow 里的 `runs-on: ubuntu-latest` 改成 `runs-on: self-hosted`。
代码和调度全都不用改。

### 3. 继续手动跑

每天早上执行一次：

```bash
cd /workspace/web && bash run_daily_signal.sh
```

约 10 秒（命中缓存）到 2 分钟（缓存空）。
只看信号的话，双击 `web/index_static.html` 就行，数据内嵌，不依赖任何服务。

---

## 已经内置的三道保险

| 保险 | 防什么 |
|---|---|
| 数据源连通性自检 | 连不上就 `exit 1` 明确报错，不假装成功 |
| 信号体检 | `stale=true` 就红；ETF 行情缺失超过 2 只也红 |
| 每日运行摘要 | Actions 页面列出信号日 / 动作 / 调仓日 / 投入金额 |

三道都是为了防同一件事：**静默失败**。
