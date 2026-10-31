#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wi-Fi 实时流量看板（macOS）v1.3.0
--------------------------------
用途：一眼看清本机无线网卡的上传 / 下载流量都去了哪些地址、哪些程序。
特性：
  · 原生独立窗口（pywebview/WKWebView）；未安装 pywebview 时自动回退浏览器模式
  · 进程显示为真实应用名（如 Electron → WorkBuddy），而非框架名
  · 连接标注 内网 / 外网；外网 IP 仅显示地址（不做在线国家/地区解析）
原理：调用系统自带 nettop 持续采样，按「远端 IP + 应用」聚合每秒速率；
      内置本地 HTTP 服务绑定随机空闲端口（无端口冲突），窗口/浏览器访问该地址。
依赖：Python 3 标准库 + macOS 自带 /usr/bin/nettop；
      可选 pip3 install pywebview 启用原生窗口。

启动：
    python3 wifi_monitor.py
停止：关闭窗口，或终端按 Ctrl+C
"""

import subprocess
import re
import json
import time
import threading
import http.server
import socketserver
import sys

APP_VERSION = "1.3.0"
POLL = 2  # nettop -L 的样本个数（默认 -s 1 秒/样本）；速率按真实耗时 dt 计算，与此值无关

latest = {"ts": 0, "total_rx_kbps": 0.0, "total_tx_kbps": 0.0, "talkers": []}
lock = threading.Lock()
last_cumul = {}          # key -> 上次采样时的累计字节（保留旧连接的旧值，不随单轮空结果清空）
last_seen_ts = {}        # key -> 最近一次见到该连接的时间（用于定期清理长期不活跃的保留项）
last_poll_ts = 0.0
have_baseline = False    # 首次采到非空连接后置 True，之后永不回退（不能靠 last_cumul 是否为空判断）


# ------------------------- IP 内外网判定 -------------------------
def is_private(ip):
    if ip in ("*", "*.*") or ip == "":
        return True
    if ip.startswith("fe80") or ip.startswith("::1"):
        return True
    if ip.startswith("fc") or ip.startswith("fd"):     # IPv6 唯一本地地址 ULA (fc00::/7)
        return True
    if ip.startswith("127") or ip.startswith("169.254"):
        return True
    if ip.startswith("10."):
        return True
    if ip.startswith("192.168."):
        return True
    if ip.startswith("172."):
        try:
            if 16 <= int(ip.split(".")[1]) <= 31:
                return True
        except ValueError:
            pass
    return False


# （已移除在线 GeoIP 国家/地区解析：用户决定不做该特性，外网 IP 仅显示地址）


# ------------------------- 进程 → 真实应用名 -------------------------
def resolve_apps(pids):
    """批量把 PID 解析成真实应用名（从 ps 命令行提取 .app 包名）。"""
    if not pids:
        return {}
    try:
        out = subprocess.run(
            ["ps", "-o", "pid=,command=", "-p", ",".join(pids)],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return {}
    res = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, cmd = line.partition(" ")
        m = re.search(r"/([^/]+)\.app/", cmd)
        if m:
            res[pid] = m.group(1)
        else:
            b = cmd.rsplit("/", 1)[-1].split()[0] if cmd else ""
            if b:                                   # 不写 "?"：否则会挡住 nettop 短名兜底
                res[pid] = b
    return res


# 系统守护进程 → 中文友好名
# 这些不是某个 .app，而是 macOS 自带的系统服务，无法通过 .app 反查,
# 用映射表直接给出可读含义,让看板一眼能懂。
SYSTEM_PROC = {
    "apsd": "苹果推送服务",
    "rapportd": "设备连续互通",
    "mDNSResponder": "Bonjour/mDNS 服务",
    "netbiosd": "NetBIOS 服务",
    "awdd": "Apple 无线诊断",
    "bird": "iCloud 同步",
    "cloudd": "iCloud 守护",
    "nsurlsessiond": "系统网络会话",
    "networkd": "网络守护",
    "trustd": "证书信任守护",
    "configd": "系统配置守护",
    "sharingd": "AirDrop/共享守护",
    "bluetoothd": "蓝牙守护",
    "locationd": "定位服务",
    "symptomsd": "网络诊断",
    "sandbox-cli": "WorkBuddy 沙箱CLI",
    "WeType": "微信输入法",
}


def friendly(name):
    """把 nettop/ps 给的进程名串映射成可读名（系统守护进程→中文，其余原样）。"""
    if not name:
        return name
    tokens = [t.strip() for t in name.split(",") if t.strip()]
    seen, res = set(), []
    for tk in tokens:
        disp = SYSTEM_PROC.get(tk, tk)
        if disp not in seen:
            seen.add(disp)
            res.append(disp)
    return ", ".join(res)


# ------------------------- 解析 nettop 输出 -------------------------
def parse_remote(col0):
    if "<->" not in col0:
        return None
    remote = col0.split("<->")[1].strip().rstrip(",")
    if remote in ("*", "*.*"):
        return None
    if remote.startswith("["):                      # IPv6 字面量 [addr]:port
        end = remote.find("]")
        inner = remote[1:end] if end != -1 else remote[1:]
        return inner.split("%")[0]                  # 剥掉 %zone（如 fe80::1%en0）
    if "%" in remote:                                # 作用域标识 addr%zone
        return remote.split("%")[0]
    # IPv6（>=2 个冒号，如 2001:db8::1）直接返回，避免把末段误当地址端口截断
    if remote.count(":") >= 2:
        return remote
    if ":" in remote:                                # IPv4 ip:port
        return remote.rsplit(":", 1)[0]
    return remote


def poll_once():
    global last_poll_ts, have_baseline
    now = time.time()
    dt = (now - last_poll_ts) if last_poll_ts else POLL
    if dt <= 0:
        dt = POLL
    last_poll_ts = now
    try:
        r = subprocess.run(
            ["nettop", "-n", "-L", str(POLL), "-t", "wifi",
             "-J", "bytes_in,bytes_out,interface,state"],
            capture_output=True, text=True, timeout=POLL + 10,
        )
    except Exception as e:
        with lock:
            latest.update({"ts": int(time.time()), "total_rx_kbps": 0.0,
                           "total_tx_kbps": 0.0})
            latest["talkers"] = [{"ip": "nettop 调用失败", "proc": str(e),
                                  "loc": "", "rx": 0, "tx": 0, "total": 0, "err": True}]
        return
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        err = err[-1] if err else ("退出码 %d" % r.returncode)
        with lock:
            latest.update({"ts": int(time.time()), "total_rx_kbps": 0.0,
                           "total_tx_kbps": 0.0})
            latest["talkers"] = [{"ip": "nettop 出错", "proc": err[:300],
                                  "loc": "", "rx": 0, "tx": 0, "total": 0, "err": True}]
        return
    out = r.stdout

    cur = {}
    proc_of = {}        # conn_key -> (pid, shortname)
    cur_proc_pid = None
    cur_proc_name = None

    for line in out.splitlines():
        if not line.strip():
            continue
        p = [c.strip() for c in line.split(",")]
        # 找到含 <-> 的字段作为连接行（nettop 列序不固定，不假设在第 0 列）
        conn_field = next((c for c in p if "<->" in c), None)
        if conn_field is None:
            # 进程汇总行（形如 process.pid，不含 <->）：定位带 .pid 的字段
            for c in p:
                m = re.search(r"\.(\d+)$", c)
                if m:
                    cur_proc_name = c
                    cur_proc_pid = m.group(1)
                    break
            continue
        # 连接行：其余字段自适应识别（纯数字=字节数），不依赖 nettop 列序
        rest = [c for c in p if c is not conn_field]
        nums = [c for c in rest if re.fullmatch(r"\d+", c)]
        if len(nums) < 2:
            continue
        try:
            bi_v = int(nums[0])
            bo_v = int(nums[1])
        except ValueError:
            continue
        ip = parse_remote(conn_field)
        if ip is None:
            continue
        prev = cur.get(conn_field, (0, 0))
        cur[conn_field] = (max(prev[0], bi_v), max(prev[1], bo_v))
        proc_of[conn_field] = (cur_proc_pid, cur_proc_name)

    agg = {}            # ip -> {rx, tx, pids:set}
    # 首次出现（两次轮询之间新建）的连接：nettop 的计数是该连接建立以来的累计字节，
    # 全部落在本次窗口内，按全额计入速率（否则 DNS/短请求永远贡献 0）。
    # 仅首轮（have_baseline 为 False）不这么做——避免把启动前就存在的老连接的历史流量算成突发。
    # last_cumul 不清空：连接漏采一轮再出现时，仍按旧值做差（漏采窗口的流量归入本轮，
    # 而不是把整个连接生命周期的累计误算成一次突发）。
    for key, (bi, bo) in cur.items():
        if not have_baseline:
            pbi, pbo = bi, bo
        else:
            pbi, pbo = last_cumul.get(key, (0, 0))
        d_rx = max(0, bi - pbi)
        d_tx = max(0, bo - pbo)
        ip = parse_remote(key)
        if ip is None:
            continue
        pid, _ = proc_of.get(key, (None, None))
        a = agg.setdefault(ip, {"rx": 0, "tx": 0, "pids": set()})
        a["rx"] += d_rx
        a["tx"] += d_tx
        if pid:
            a["pids"].add(pid)

    # 基线更新：不清空，增量覆盖；长期不见的连接定期清理，防止字典无限增长
    now_keep = time.time()
    for k, v in cur.items():
        last_cumul[k] = v
        last_seen_ts[k] = now_keep
    if len(last_seen_ts) > 4096:
        for k in [k for k, t in last_seen_ts.items() if now_keep - t > 600]:
            last_cumul.pop(k, None)
            last_seen_ts.pop(k, None)
    if cur:
        have_baseline = True   # 采到过非空结果才算有基线；空结果不能重置基线（否则整窗数据归零）

    # 解析真实应用名
    all_pids = set()
    for a in agg.values():
        all_pids |= a["pids"]
    apps = resolve_apps(list(all_pids))
    # 同时收集 nettop 短名作为兜底
    short_of = {}
    for key, (pid, name) in proc_of.items():
        if pid and name:
            short_of[pid] = name.rsplit(".", 1)[0]

    talkers = []
    for ip, a in agg.items():
        names = set()
        for pid in a["pids"]:
            nm = apps.get(pid) or short_of.get(pid)
            if nm:
                names.add(nm)
        proc = ",".join(sorted(names))[:200]
        rx_k = a["rx"] / dt / 1024.0
        tx_k = a["tx"] / dt / 1024.0
        proc = friendly(proc) or "—"
        talkers.append({
            "ip": ip,
            "proc": proc or "—",
            "rx": round(rx_k, 1),
            "tx": round(tx_k, 1),
            "total": round(rx_k + tx_k, 1),
        })
    talkers.sort(key=lambda x: -x["total"])

    total_rx = sum(t["rx"] for t in talkers)
    total_tx = sum(t["tx"] for t in talkers)

    with lock:
        latest.update({
            "ts": int(time.time()),
            "total_rx_kbps": round(total_rx, 1),
            "total_tx_kbps": round(total_tx, 1),
            "talkers": talkers[:15],
        })


def sampler_loop():
    while True:
        poll_once()
        time.sleep(1)   # 退避：nettop 报错瞬间退出时避免 while True 空转打满 CPU


# ------------------------- 前端页面 -------------------------
PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WI-FI TRAFFIC // MATRIX</title>
<style>
  :root{--bg:#000;--card:#050b05;--fg:#00ff41;--sub:#1f9d4d;--dim:#0e5a2a;
        --rx:#00ff41;--tx:#ffb000;--line:rgba(0,255,65,.22);--err:#ff0033}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:13px/1.6 "SF Mono",Menlo,Consolas,"Courier New",monospace;
       text-shadow:0 0 4px rgba(0,255,65,.3)}
  #rain{position:fixed;top:0;left:0;z-index:0;opacity:.26}
  body::before{content:"";position:fixed;top:0;left:0;right:0;bottom:0;z-index:3;pointer-events:none;
       background:radial-gradient(ellipse at center,transparent 55%,rgba(0,0,0,.7) 100%)}
  body::after{content:"";position:fixed;top:0;left:0;right:0;bottom:0;z-index:3;pointer-events:none;
       background:repeating-linear-gradient(0deg,rgba(0,0,0,.20) 0 1px,transparent 1px 3px)}
  header,main{position:relative;z-index:2}
  header{padding:16px 22px;border-bottom:1px solid var(--line);
         display:flex;align-items:center;gap:26px;flex-wrap:wrap;
         background:rgba(0,4,0,.74)}
  h1{font-size:16px;margin:0;font-weight:700;letter-spacing:2px;color:#c8ffd4;
     text-shadow:0 0 10px rgba(0,255,65,.8)}
  h1 .cn{font-size:11px;color:var(--sub);letter-spacing:4px;display:block;font-weight:400}
  h1 .ver{color:var(--fg);opacity:.7;letter-spacing:1px}
  .tot{display:flex;gap:18px}
  .tot div{background:var(--card);border:1px solid var(--line);
           border-radius:2px;padding:8px 16px;min-width:140px;
           box-shadow:inset 0 0 14px rgba(0,255,65,.10)}
  .tot .k{font-size:11px;color:var(--sub);letter-spacing:1px}
  .tot .v{font-size:22px;font-weight:700}
  .rx-c{color:var(--rx)} .tx-c{color:var(--tx)}
  main{padding:16px 26px 34px;width:100%}
  .legend{font-size:12px;color:var(--sub);margin:4px 0 14px}
  .dot{display:inline-block;width:10px;height:10px;border-radius:1px;
       margin:0 4px 0 14px;vertical-align:middle}
  .dot.r{background:var(--rx);box-shadow:0 0 7px var(--rx)}
  .dot.t{background:var(--tx);box-shadow:0 0 7px var(--tx)}
  select{background:#000;color:var(--fg);border:1px solid var(--line);
         border-radius:2px;font:inherit;font-size:12px;padding:2px 4px}
  .cpall{font-size:12px;padding:3px 12px;background:transparent;color:var(--fg);
         border:1px solid var(--fg);border-radius:2px;margin-left:6px;font-family:inherit;
         box-shadow:0 0 8px rgba(0,255,65,.25)}
  .cpall:hover{background:rgba(0,255,65,.14);box-shadow:0 0 16px rgba(0,255,65,.55)}
  .row{display:grid;grid-template-columns:minmax(340px,460px) 1fr;gap:14px;align-items:center;
       padding:9px 4px;border-bottom:1px solid rgba(0,255,65,.10)}
  .meta{overflow:hidden}
  .meta .p{font-weight:700;word-break:break-word;line-height:1.45;
           text-shadow:0 0 7px rgba(0,255,65,.55)}
  .meta .i{font-size:12px;color:var(--sub);display:flex;align-items:center;gap:6px;
           white-space:nowrap;overflow:hidden}
  .meta .i .ip{overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
  .tag{display:inline-block;font-size:10px;padding:0 6px;border-radius:2px;
       margin-left:8px;vertical-align:middle;border:1px solid}
  .tag.lan{color:var(--fg);border-color:var(--fg);background:rgba(0,255,65,.08)}
  .tag.wan{color:var(--tx);border-color:var(--tx);background:rgba(255,176,0,.08);
           text-shadow:0 0 5px rgba(255,176,0,.5)}
  .cp{margin-left:8px;font-size:11px;line-height:1;padding:2px 8px;background:transparent;
      color:var(--sub);border:1px solid var(--dim);border-radius:2px;flex:none;font-family:inherit}
  .cp:hover{color:var(--fg);border-color:var(--fg)}
  .bars{position:relative;height:36px}
  .bar{position:absolute;height:13px;border-radius:1px;left:0;transition:width .4s}
  .bar.rx{top:2px;background:linear-gradient(90deg,rgba(0,255,65,.22),var(--rx));
          box-shadow:0 0 9px rgba(0,255,65,.5)}
  .bar.tx{top:20px;background:linear-gradient(90deg,rgba(255,176,0,.18),var(--tx));
          box-shadow:0 0 9px rgba(255,176,0,.45)}
  .lab{position:absolute;font-size:11px}
  .lab.rx{top:2px;right:0;color:var(--rx)} .lab.tx{top:20px;right:0;color:var(--tx)}
  #ts{font-size:12px;color:var(--sub);margin-left:auto}
  .empty{color:var(--sub);padding:36px;text-align:center;letter-spacing:3px}
  .row-err .p{color:var(--err);text-shadow:0 0 7px rgba(255,0,51,.7)}
  .row-err .i{color:var(--err)}
</style></head>
<body>
<canvas id="rain"></canvas>
<header>
  <h1>WI-FI TRAFFIC MONITOR<span class="cn">无 线 流 量 监 视 · THE MATRIX HAS YOU · <span class="ver">{{VER}}</span></span></h1>
  <div class="tot">
    <div><div class="k">&#9660; IN / 下载</div><div class="v rx-c" id="trx">0</div></div>
    <div><div class="k">&#9650; OUT / 上传</div><div class="v tx-c" id="ttx">0</div></div>
  </div>
  <span id="ts"></span>
</header>
<main>
  <div class="legend">
    <span class="dot r"></span>下载
    <span class="dot t"></span>上传
    &nbsp;（条形长度 = 真实速率 ÷ 满格参考值；满格
    <select id="ref">
      <option value="256">256 KB/s</option>
      <option value="1024">1 MB/s</option>
      <option value="2048" selected>2 MB/s</option>
      <option value="5120">5 MB/s</option>
      <option value="10240">10 MB/s</option>
      <option value="51200">50 MB/s</option>
    </select>，超出则封顶变白）
    &nbsp;&nbsp;<label><input type="checkbox" id="showIdle"> 显示空闲连接（0 KB/s）</label>
    &nbsp;&nbsp;<button id="copyAll" class="cpall">[ 复制全部外网IP ]</button>
  </div>
  <div id="list"><div class="empty">WAKE UP, NEO... 采集中</div></div>
</main>
<script>
/* ---------- Matrix code rain（低频绘制，纯装饰，不影响数据） ---------- */
(function(){
  var cv=document.getElementById('rain'),cx=cv.getContext('2d');
  var FS=14,cols=0,drops=[];
  var glyphs='\u30A2\u30A4\u30A6\u30A8\u30AA\u30AB\u30AD\u30AF\u30B1\u30B3\u30B5\u30B7\u30B9\u30BB\u30BD'+
             '\u30BF\u30C1\u30C4\u30C6\u30C8\u30CA\u30CB\u30CC\u30CD\u30CE0123456789<>*+=-';
  function rs(){cv.width=innerWidth;cv.height=innerHeight;
    cols=Math.ceil(cv.width/FS);
    drops=[];for(var i=0;i<cols;i++)drops[i]=Math.floor(Math.random()*-60);}
  rs();addEventListener('resize',rs);
  setInterval(function(){
    cx.fillStyle='rgba(0,0,0,0.08)';cx.fillRect(0,0,cv.width,cv.height);
    cx.font=FS+'px monospace';
    for(var i=0;i<cols;i++){
      var ch=glyphs.charAt(Math.floor(Math.random()*glyphs.length));
      var y=drops[i]*FS;
      cx.fillStyle=Math.random()<0.04?'#d8ffe2':'rgba(0,255,65,0.6)';
      cx.fillText(ch,i*FS,y);
      if(y>cv.height&&Math.random()>0.975)drops[i]=0;
      drops[i]+=1;
    }
  },66);
})();
/* ---------- 数据逻辑 ---------- */
function fmt(n){return n>=1024?(n/1024).toFixed(1)+' MB/s':n.toFixed(1)+' KB/s';}
function esc(s){s=s==null?'':String(s);return s.replace(/[&<>]/g,function(c){return c==='&'?'&amp;':c==='<'?'&lt;':'&gt;';});}
function tag(t){
  if(t.scope==='lan') return '<span class="tag lan">内网</span>';
  if(t.scope==='wan') return '<span class="tag wan">外网</span>';
  return '';
}
var REF=2048;
var lastData=null;
document.getElementById('ref').addEventListener('change',function(e){REF=+e.target.value;});
function flash(btn,msg){ if(!btn)return; var old=btn.textContent; btn.textContent=msg; setTimeout(function(){btn.textContent=old;},1200); }
function fallback(txt,ok,fail){
  try{var ta=document.createElement('textarea');ta.value=txt;ta.style.position='fixed';ta.style.top='-1000px';
      document.body.appendChild(ta);ta.focus();ta.select();
      var r=document.execCommand('copy');document.body.removeChild(ta);r?ok():fail();}
  catch(e){fail();}
}
function copyText(txt,btn){
  function ok(){flash(btn,'已复制 ✓');} function fail(){flash(btn,'复制失败');}
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(txt).then(ok,function(){fallback(txt,ok,fail);});
  } else { fallback(txt,ok,fail); }
}
document.getElementById('list').addEventListener('click',function(e){
  var b=e.target.closest?e.target.closest('.cp'):null;
  if(b&&b.getAttribute('data-ip')){ copyText(b.getAttribute('data-ip'),b); }
});
document.getElementById('copyAll').addEventListener('click',function(){
  if(!lastData)return;
  var ips=lastData.talkers.filter(function(t){return t.scope==='wan';}).map(function(t){return t.ip;});
  if(!ips.length){flash(this,'无外网IP');return;}
  copyText(ips.join('\n'),this);
});
function loop(){
  fetch('/api').then(function(r){return r.json();}).then(function(d){
    lastData=d;
    document.getElementById('trx').textContent=fmt(d.total_rx_kbps);
    document.getElementById('ttx').textContent=fmt(d.total_tx_kbps);
    document.getElementById('ts').textContent='更新于 '+new Date(d.ts*1000).toLocaleTimeString();
    var showIdle=document.getElementById('showIdle').checked;
    var rows=showIdle?d.talkers:d.talkers.filter(function(t){return t.total>0||t.err;});
    var el=document.getElementById('list');
    if(!rows.length){el.innerHTML='<div class="empty">当前无活跃流量（所有连接均空闲）</div>';return;}
    el.innerHTML=rows.map(function(t){
      var rv=t.rx/REF*100, tv=t.tx/REF*100;
      var rOver=rv>=100, tOver=tv>=100;
      var rw=Math.min(100,rv).toFixed(1), tw=Math.min(100,tv).toFixed(1);
      var loc='';
      var cls=t.err?' row-err':'';
      var cpBtn=(t.scope==='wan'&&/^[0-9a-fA-F:.]+$/.test(t.ip))?'<button class="cp" data-ip="'+esc(t.ip)+'">[复制]</button>':'';
      return '<div class="row'+cls+'"><div class="meta"><div class="p">'+esc(t.proc||'—')+tag(t)+'</div><div class="i"><span class="ip">'+esc(t.ip)+loc+'</span>'+cpBtn+'</div></div><div class="bars"><div class="bar rx" style="width:'+rw+'%;'+(rOver?'background:#eaffee;box-shadow:0 0 12px #fff':'')+'"></div><div class="lab rx">'+fmt(t.rx)+'</div><div class="bar tx" style="width:'+tw+'%;'+(tOver?'background:#ffe9c8;box-shadow:0 0 12px #fff':'')+'"></div><div class="lab tx">'+fmt(t.tx)+'</div></div></div>';
    }).join('');
  }).catch(function(){});
  setTimeout(loop,2500);
}
loop();
</script>
</body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api"):
            with lock:
                snap = dict(latest)
            # 内网/外网标签：纯本地判定，绝不发起网络请求 -> 接口瞬时返回
            for t in snap["talkers"]:
                if t.get("err"):
                    t["scope"] = ""
                    t["loc"] = ""
                    continue
                ip = t["ip"]
                t["scope"] = "lan" if is_private(ip) else "wan"
                t["loc"] = ""
            snap["version"] = APP_VERSION
            body = json.dumps(snap).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(PAGE.replace("{{VER}}", "v" + APP_VERSION).encode())

    def log_message(self, *a):
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _serve_browser(url):
    """浏览器模式（无 pywebview 或原生窗口启动失败时的兑底）。"""
    import webbrowser
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"Wi-Fi 流量看板（浏览器模式）→ {url}   （Ctrl+C 退出）")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n已停止。")
        sys.exit(0)


def main():
    threading.Thread(target=sampler_loop, daemon=True).start()
    # 端口 0 = 由系统分配空闲端口，彻底避免端口占用冲突
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        import webview                                   # pywebview（可选依赖）
    except ImportError:
        print("提示：未安装 pywebview，回退浏览器模式。要原生独立窗口请先执行： pip3 install pywebview")
        _serve_browser(url)
    try:
        webview.create_window("Wi-Fi 流量看板 // MATRIX", url=url,
                              width=1100, height=760,
                              background_color="#000000")
        print(f"Wi-Fi 流量看板（独立窗口）已启动 → {url}   （关闭窗口即退出）")
        webview.start()                                  # 阻塞至窗口关闭
    except Exception as e:
        # pywebview 能 import 但窗口启动失败（WebView 初始化异常等）→ 兑底浏览器模式
        print(f"原生窗口启动失败（{e}），回退浏览器模式。")
        _serve_browser(url)
    print("\n已停止。")


if __name__ == "__main__":
    main()
