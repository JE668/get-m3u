import os, re, requests, time, concurrent.futures
from datetime import datetime
from collections import Counter

# ===============================
# 1. 配置区
# ===============================
FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2Rvbmci"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Cookie": os.environ.get("FOFA_COOKIE", "") 
}
RTP_SOURCES = [
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_4k.m3u",
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_hd.m3u"
]
DISCOVERY_FILE = "discovery.txt"
SOURCE_IP_FILE = "source-ip.txt"
SOURCE_M3U_FILE = "source-m3u.txt"
SOURCE_NONCHECK_FILE = "source-m3u-noncheck.txt"
RTP_FILE = os.path.join("rtp", "ChinaTelecom-Guangdong.txt")

# 实时日志工具
def live_print(content):
    print(content, flush=True)

def log_group_start(name):
    live_print(f"\n::group::{name}")

def log_group_end():
    live_print("::endgroup::")

# ===============================
# 2. 发现库与模板管理
# ===============================

def update_rtp_template():
    log_group_start("🔄 同步并更新 RTP 模板")
    os.makedirs("rtp", exist_ok=True)
    unique_rtp = {}
    for url in RTP_SOURCES:
        try:
            r = requests.get(url, timeout=15)
            r.encoding = 'utf-8'
            if r.status_code == 200:
                lines = r.text.splitlines()
                count = 0
                for i in range(len(lines)):
                    if lines[i].startswith("#EXTINF"):
                        name = lines[i].split(',')[-1].strip()
                        for j in range(i+1, min(i+5, len(lines))):
                            if lines[j].strip().startswith("rtp://"):
                                rurl = lines[j].strip()
                                if rurl not in unique_rtp:
                                    unique_rtp[rurl] = name
                                    count += 1
                                break
                live_print(f"  📥 {url.split('/')[-1]} | 提取 {count} 条频道")
        except: live_print(f"  ❌ 同步失败: {url.split('/')[-1]}")
    if unique_rtp:
        with open(RTP_FILE, "w", encoding="utf-8") as f:
            for url, name in unique_rtp.items(): f.write(f"{name},{url}\n")
        live_print(f"📊 统计: RTP 模板更新完毕 | 共 {len(unique_rtp)} 个频道")
    log_group_end()

def update_discovery_database(new_ips):
    log_group_start("📂 更新发现库 (discovery.txt)")
    segs, ports = set(), set()
    if os.path.exists(DISCOVERY_FILE):
        with open(DISCOVERY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if "|" in line:
                    p = line.strip().split("|")
                    if p[0] == "SEG": segs.add(p[1])
                    if p[0] == "PORT": ports.add(p[1])

    for ip_port in new_ips:
        try:
            ip, port = ip_port.split(":")
            segs.add(".".join(ip.split(".")[:3]))
            ports.add(port)
        except: continue

    sorted_segs, sorted_ports = sorted(list(segs)), sorted(list(ports), key=int)
    with open(DISCOVERY_FILE, "w", encoding="utf-8") as f:
        for s in sorted_segs: f.write(f"SEG|{s}\n")
        for p in sorted_ports: f.write(f"PORT|{p}\n")
    
    live_print(f"✅ 库同步完成 | 累计网段: {len(sorted_segs)} | 累计端口: {len(sorted_ports)}")
    log_group_end()
    return sorted_segs, sorted_ports

# ===============================
# 3. 资源抓取与主动扫描
# ===============================

def scrape_fofa():
    log_group_start("📡 抓取 FOFA 资源")
    if not HEADERS["Cookie"]:
        live_print("⏭️  未配置 FOFA_COOKIE，跳过。")
        log_group_end(); return []
    try:
        r = requests.get(FOFA_URL, headers=HEADERS, timeout=15)
        if "账号登录" in r.text or "登录后可见" in r.text:
            live_print("❌ 错误: FOFA Cookie 已失效！")
            log_group_end(); return []
        raw_list = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
        if raw_list:
            counts = Counter(raw_list)
            live_print(f"✅ FOFA 原始条目: {len(raw_list)} 个")
            for ip in sorted(counts.keys()):
                live_print(f"  - {ip:<25} (出现 {counts[ip]} 次)")
            log_group_end(); return list(counts.keys())
    except: live_print("❌ FOFA 抓取异常")
    log_group_end(); return []

def check_udpxy(ip_port):
    for path in ["/stat", "/status"]:
        try:
            r = requests.get(f"http://{ip_port}{path}", timeout=2, headers={"User-Agent":"Wget/1.14"})
            if r.status_code == 200 and any(kw in r.text.lower() for kw in ["udpxy", "stat", "client"]):
                return True
        except: continue
    return False

def run_native_scan(segs, ports):
    log_group_start("🚀 启动矩阵扫描 (实时发现)")
    if not segs or not ports:
        live_print("⚠️ 库为空，跳过扫描。"); log_group_end(); return []
    
    tasks = [f"{s}.{i}:{p}" for s in segs for i in range(1, 255) for p in ports]
    found = []
    live_print(f"⚡ 任务规模: {len(tasks)} 次探测...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=250) as ex:
        futures = {ex.submit(check_udpxy, ip): ip for ip in tasks}
        for f in concurrent.futures.as_completed(futures):
            if f.result():
                res = futures[f]
                live_print(f"  🌟 发现目标: {res}")
                found.append(res)
    live_print(f"✅ 扫描结束 | 新发现 {len(found)} 个节点")
    log_group_end(); return found

def verify_geo(ip_port):
    try:
        ip = ip_port.split(":")[0]
        res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=10).json()
        if res.get("status") != "success": return False, f"{ip_port:<21} | 查询限制"
        reg, city, isp = res.get("regionName","未知"), res.get("city","未知"), res.get("isp","未知")
        is_gd = "广东" in reg
        is_tel = any(kw in isp.lower() for kw in ["电信", "telecom", "chinanet"])
        return (is_gd and is_tel), f"{ip_port:<21} | {reg}-{city} | {isp}"
    except: return False, f"{ip_port:<21} | 异常"

# ===============================
# 4. 主程序入口
# ===============================
if __name__ == "__main__":
    start_total = time.time()
    update_rtp_template()
    f_ips = scrape_fofa()
    segs, ports = update_discovery_database(f_ips)
    s_ips = run_native_scan(segs, ports)
    
    unique_all = sorted(list(set(f_ips + s_ips)))
    
    log_group_start("🌍 地理归属地校验 (广东电信)")
    geo_ips = []
    for idx, ip in enumerate(unique_all, 1):
        ok, desc = verify_geo(ip)
        status = "✅ 匹配" if ok else "⏭️ 跳过"
        live_print(f"  [{idx:02d}/{len(unique_all):02d}] {status} | {desc}")
        if ok: geo_ips.append(ip)
        time.sleep(1.2)
    log_group_end()

    if geo_ips:
        log_group_start("💾 数据归档与拼装")
        geo_ips.sort()
        with open(SOURCE_IP_FILE, "w", encoding="utf-8") as f: f.write("\n".join(geo_ips))
        if os.path.exists(RTP_FILE):
            with open(RTP_FILE, encoding="utf-8") as f: rtps = [x.strip() for x in f if "," in x]
            m3u = [f"{r.split(',')[0]},http://{ip}/rtp/{r.split('://')[1]}" for ip in geo_ips for r in rtps]
            for p in [SOURCE_NONCHECK_FILE, SOURCE_M3U_FILE]:
                with open(p, "w", encoding="utf-8") as f: f.write("\n".join(m3u))
            live_print(f"✨ 报告: 服务器 {len(geo_ips)} 个 | 链接 {len(m3u)} 条")
        log_group_end()
    
    live_print(f"\n⏱️ 总耗时: {round(time.time() - start_total, 2)}s")
