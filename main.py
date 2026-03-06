import os, re, requests, time, concurrent.futures, random
from datetime import datetime
from collections import Counter

# ===============================
# 1. 配置区
# ===============================
# 如果想手动增加网段，直接在 discovery.txt 中按格式添加即可
DISCOVERY_FILE = "discovery.txt"
SOURCE_IP_FILE = "source-ip.txt"
SOURCE_M3U_FILE = "source-m3u.txt"
SOURCE_NONCHECK_FILE = "source-m3u-noncheck.txt"
RTP_FILE = os.path.join("rtp", "ChinaTelecom-Guangdong.txt")

FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2Rvbmci"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Cookie": os.environ.get("FOFA_COOKIE", "") 
}
# 默认端口，如果 discovery.txt 为空则使用这些
DEFAULT_PORTS = [4022, 8000, 8686, 55555, 54321, 1024, 10001, 8443, 8888]

# ===============================
# 2. 基础函数 (移到最上方)
# ===============================
def log_section(name, icon="🔹"):
    print(f"\n{icon} {'='*15} {name} {'='*15}", flush=True)

def live_print(content):
    print(content, flush=True)

# ===============================
# 3. 资源管理与扫描模块
# ===============================

def update_discovery_database(new_ips):
    log_section("更新发现库 (discovery.txt)", "📂")
    segs, ports = set(), set(DEFAULT_PORTS)
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
    live_print(f"✅ 库同步 | 累计 C 段: {len(sorted_segs)} | 累计端口: {len(sorted_ports)}")
    return sorted_segs, sorted_ports

def check_udpxy(ip_port):
    """指纹识别"""
    for path in ["/stat", "/status"]:
        try:
            r = requests.get(f"http://{ip_port}{path}", timeout=2, headers={"User-Agent":"Wget/1.14"})
            if r.status_code == 200 and any(kw in r.text.lower() for kw in ["udpxy", "stat", "client"]):
                return True
        except: continue
    return False

def run_native_scan(segments, ports):
    log_section("🚀 启动全量矩阵扫描", "⚡")
    tasks = [f"{s}.{i}:{p}" for s in segments for i in range(1, 255) for p in ports]
    random.shuffle(tasks)
    
    live_print(f"⚡ 任务规模: {len(tasks)} 次探测 (并发: 300)")
    found_ips = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=300) as ex:
        futures = {ex.submit(check_udpxy, ip): ip for ip in tasks}
        for i, f in enumerate(concurrent.futures.as_completed(futures)):
            if f.result():
                ip = futures[f]
                live_print(f"    🌟 [发现目标] {ip}")
                found_ips.append(ip)
            if (i + 1) % 5000 == 0:
                live_print(f"  📊 进度: {i+1}/{len(tasks)} | 当前存活: {len(found_ips)}")
                
    live_print(f"✅ 扫描结束 | 发现 {len(found_ips)} 个有效节点")
    return list(set(found_ips))

# ===============================
# 4. 辅助模块
# ===============================

def update_rtp_template():
    log_section("同步并更新 RTP 模板", "🔄")
    unique_rtp = {}
    sources = [
        "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_4k.m3u",
        "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_hd.m3u"
    ]
    for url in sources:
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
                                if lines[j].strip() not in unique_rtp:
                                    unique_rtp[lines[j].strip()] = name
                                    count += 1
                                break
                live_print(f"  📥 {url.split('/')[-1]} | 解析 {count} 条")
        except: live_print(f"  ❌ 同步失败: {url}")
    if unique_rtp:
        with open(RTP_FILE, "w", encoding="utf-8") as f:
            for url, name in unique_rtp.items(): f.write(f"{name},{url}\n")
    log_group_end()

def verify_geo(ip_port):
    try:
        ip = ip_port.split(":")[0]
        res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=10).json()
        if res.get("status") != "success": return False, f"{ip_port:<21} | API限制"
        reg, city, isp = res.get("regionName","未知"), res.get("city","未知"), res.get("isp","未知")
        is_gd, is_tel = "广东" in reg, any(kw in isp.lower() for kw in ["电信", "telecom", "chinanet"])
        return (is_gd and is_tel), f"{ip_port:<21} | {reg}-{city} | {isp}"
    except: return False, f"{ip_port:<21} | 异常"

# ===============================
# 5. 主入口
# ===============================
if __name__ == "__main__":
    start_time = time.time()
    update_rtp_template()

    # FOFA 抓取
    log_section("抓取 FOFA 资源", "📡")
    try:
        r = requests.get(FOFA_URL, headers=HEADERS, timeout=15)
        raw_list = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
        print(f"  ✅ 发现 {len(raw_list)} 条记录")
    except: raw_list = []

    # 发现库更新与全网段扫描
    segs, ports = update_discovery_database(raw_list)
    s_ips = run_native_scan(segs, ports)
    
    unique_all = sorted(list(set(raw_list + s_ips)))
    
    # 归属地校验
    log_section("地理归属地校验 (广东电信)", "🌍")
    geo_ips = []
    for idx, ip in enumerate(unique_all, 1):
        ok, desc = verify_geo(ip)
        if ok:
            live_print(f"  [{idx:02d}/{len(unique_all):02d}] ✅ 匹配 | {desc}")
            geo_ips.append(ip)
        time.sleep(1.0)

    # 保存
    log_section("数据归档与拼装", "💾")
    if geo_ips:
        geo_ips.sort()
        with open(SOURCE_IP_FILE, "w", encoding="utf-8") as f: f.write("\n".join(geo_ips))
        if os.path.exists(RTP_FILE):
            with open(RTP_FILE, encoding="utf-8") as f: rtps = [x.strip() for x in f if "," in x]
            m3u = [f"{r.split(',')[0]},http://{ip}/rtp/{r.split('://')[1]}" for ip in geo_ips for r in rtps]
            for fpath in [SOURCE_NONCHECK_FILE, SOURCE_M3U_FILE]:
                with open(fpath, "w", encoding="utf-8") as f: f.write("\n".join(m3u))
            live_print(f"✨ 最终结果: 有效服务器 {len(geo_ips)} 个 | 拼装链接 {len(m3u)} 条")
    
    print(f"\n⏱️ 总耗时: {round(time.time() - start_time, 2)}s")
