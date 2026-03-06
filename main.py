import os, re, requests, time, concurrent.futures, random
from datetime import datetime
from collections import Counter

# ===============================
# 1. 配置区 (目录结构优化版)
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

# --- 自动创建目录结构 ---
os.makedirs("data", exist_ok=True)
os.makedirs("data/rtp", exist_ok=True)
os.makedirs("output", exist_ok=True)

# --- 文件路径定义 ---
DISCOVERY_FILE = "data/discovery.txt"
BLACKLIST_FILE = "data/blacklist.txt"
RTP_FILE = "data/rtp/ChinaTelecom-Guangdong.txt"

SOURCE_IP_FILE = "output/source-ip.txt"
SOURCE_M3U_FILE = "output/source-m3u.txt"
SOURCE_NONCHECK_FILE = "output/source-m3u-noncheck.txt"

DEFAULT_PORTS = [4022, 8000, 8686, 55555, 54321, 1024, 10001, 8443, 8888]

# ===============================
# 2. 基础工具函数
# ===============================
def live_print(content): 
    print(content, flush=True)

def log_group_start(name): 
    live_print(f"\n::group::{name}")

def log_group_end(): 
    live_print("::endgroup::")

def log_section(name, icon="🔹"): 
    live_print(f"\n{icon} {'='*15} {name} {'='*15}")

# ===============================
# 3. 核心功能函数
# ===============================

def get_geo_info(ip):
    """查询 IP 归属地"""
    try:
        res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=5).json()
        if res.get("status") != "success": return False, "API查询失败"
        region, city, isp = res.get("regionName", "未知"), res.get("city", "未知"), res.get("isp", "未知").lower()
        is_gd = "广东" in region
        is_tel = any(k in isp for k in ["电信", "telecom", "chinanet"])
        return (is_gd and is_tel), f"{region}-{city} | {res.get('isp')}"
    except: return False, "网络异常"

def filter_segments(segments):
    """C段 预校验与清洗"""
    log_group_start("🛡️ C段 归属地预校验")
    blacklist = set()
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            blacklist = set([line.strip() for line in f if line.strip()])
    
    valid_segments, new_black_segments = [], []
    total = len(segments)
    live_print(f"📋 待检测: {total} 个 | 黑名单库: {len(blacklist)} 个")
    
    for idx, seg in enumerate(segments, 1):
        if seg in blacklist: continue
        # 抽取 .1 进行测试
        is_valid, desc = get_geo_info(f"{seg}.1")
        if is_valid:
            live_print(f"  [{idx}/{total}] ✅ 通过: {seg} ({desc})")
            valid_segments.append(seg)
        else:
            live_print(f"  [{idx}/{total}] ❌ 拉黑: {seg} ({desc})")
            new_black_segments.append(seg)
            blacklist.add(seg)
        time.sleep(1.5) # API 频率保护

    if new_black_segments:
        with open(BLACKLIST_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(new_black_segments) + "\n")
        live_print(f"💾 新增黑名单: {len(new_black_segments)} 个")

    live_print(f"📊 最终有效 C段: {len(valid_segments)} 个")
    log_group_end()
    return valid_segments

def update_discovery_database(new_ips):
    """更新发现库"""
    log_group_start("📂 更新发现库 (data/discovery.txt)")
    segs, ports = set(), set(map(str, DEFAULT_PORTS))
    
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
        
    live_print(f"✅ 库同步 | C段: {len(sorted_segs)} | 端口: {len(sorted_ports)}")
    log_group_end()
    return sorted_segs, sorted_ports

def check_udpxy(ip_port, scanned_set):
    """HTTP 指纹探测 (含 IP 熔断)"""
    ip = ip_port.split(":")[0]
    if ip in scanned_set: return False
    try:
        r = requests.get(f"http://{ip_port}/status", timeout=2, headers={"User-Agent":"Wget/1.14"})
        if r.status_code == 200 and any(kw in r.text.lower() for kw in ["udpxy", "stat", "client"]):
            scanned_set.add(ip)
            return True
    except: pass
    return False

def run_native_scan(segments, ports):
    """全量矩阵扫描"""
    log_group_start("🚀 启动全量矩阵扫描")
    if not segments: 
        live_print("⚠️ 无有效网段"); log_group_end(); return []
    
    tasks = [f"{s}.{i}:{p}" for s in segments for i in range(1, 255) for p in ports]
    random.shuffle(tasks)
    live_print(f"⚡ 任务规模: {len(tasks)} 次探测 (并发: 300)")
    
    found_ips = []
    scanned_ips_set = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=300) as ex:
        futures = {ex.submit(check_udpxy, ip, scanned_ips_set): ip for ip in tasks}
        for i, f in enumerate(concurrent.futures.as_completed(futures)):
            if f.result():
                ip = futures[f]
                live_print(f"    🌟 [发现] {ip}")
                found_ips.append(ip)
            if (i+1)%5000==0: 
                live_print(f"  📊 进度: {i+1}/{len(tasks)} | 存活: {len(found_ips)}")
    
    live_print(f"✅ 扫描结束 | 发现 {len(found_ips)} 个")
    log_group_end()
    return list(set(found_ips))

def scrape_fofa():
    """FOFA 抓取"""
    log_group_start("📡 抓取 FOFA 资源")
    if not HEADERS["Cookie"]: 
        live_print("⏭️  未配置 Cookie，跳过。"); log_group_end(); return []
    try:
        r = requests.get(FOFA_URL, headers=HEADERS, timeout=15)
        if "账号登录" in r.text:
            live_print("❌ 错误: FOFA Cookie 已失效！")
            log_group_end(); return []
            
        raw_list = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
        if raw_list:
            counts = Counter(raw_list)
            live_print(f"✅ 获取 {len(raw_list)} 条记录")
            for ip in sorted(counts.keys()): 
                live_print(f"  - {ip:<21} ({counts[ip]}次)")
            log_group_end(); return list(counts.keys())
    except: live_print("❌ FOFA 异常")
    log_group_end(); return []

def update_rtp_template():
    """RTP 模板下载"""
    log_group_start("🔄 同步 RTP 模板")
    unique_rtp = {}
    for url in RTP_SOURCES:
        try:
            r = requests.get(url, timeout=15); r.encoding = 'utf-8'
            if r.status_code == 200:
                lines = r.text.splitlines()
                count = 0
                for i in range(len(lines)):
                    if lines[i].startswith("#EXTINF"):
                        try:
                            name = lines[i].split(',')[-1].strip()
                            for j in range(i+1, min(i+5, len(lines))):
                                if lines[j].strip().startswith("rtp://"):
                                    if lines[j].strip() not in unique_rtp:
                                        unique_rtp[lines[j].strip()] = name
                                        count += 1
                                    break
                        except: continue
                live_print(f"  📥 {url.split('/')[-1]} | 解析 {count} 条")
        except: live_print(f"  ❌ 失败: {url}")
    
    if unique_rtp:
        with open(RTP_FILE, "w", encoding="utf-8") as f:
            for url, name in unique_rtp.items(): f.write(f"{name},{url}\n")
    log_group_end()

# ===============================
# 4. 主程序入口
# ===============================
if __name__ == "__main__":
    start_time = time.time()
    
    # 1. 准备 RTP
    update_rtp_template()
    
    # 2. 抓取与扫描
    fips = scrape_fofa()
    all_segs, all_ports = update_discovery_database(fips)
    valid_segs = filter_segments(all_segs)
    sips = run_native_scan(valid_segs, all_ports)
    
    unique_all = sorted(list(set(fips + sips)))
    
    # 3. 最终复核
    log_section("最终结果复核", "🌍")
    geo_ips = []
    for idx, ip in enumerate(unique_all, 1):
        ok, desc = get_geo_info(ip.split(":")[0])
        if ok:
            live_print(f"  [{idx:02d}/{len(unique_all):02d}] ✅ 有效 | {ip:<21} | {desc}")
            geo_ips.append(ip)
        else:
            live_print(f"  [{idx:02d}/{len(unique_all):02d}] ⏭️  剔除 | {ip:<21} | {desc}")
        time.sleep(1.0)
    log_group_end()

    # 4. 写入文件
    if geo_ips:
        log_group_start("💾 数据归档 (output目录)")
        geo_ips.sort()
        # 写入 source-ip.txt
        with open(SOURCE_IP_FILE, "w", encoding="utf-8") as f: 
            f.write("\n".join(geo_ips))
        live_print(f"  📝 {SOURCE_IP_FILE}")
        
        # 写入 M3U
        if os.path.exists(RTP_FILE):
            with open(RTP_FILE, encoding="utf-8") as f: 
                rtps = [x.strip() for x in f if "," in x]
            
            m3u = [f"{r.split(',')[0]},http://{ip}/rtp/{r.split('://')[1]}" for ip in geo_ips for r in rtps]
            
            for p in [SOURCE_NONCHECK_FILE, SOURCE_M3U_FILE]:
                with open(p, "w", encoding="utf-8") as f: 
                    f.write("\n".join(m3u))
                live_print(f"  📝 {p}")
                
            live_print(f"✨ 总结: {len(geo_ips)} 个服务器 | {len(m3u)} 条链接")
        log_group_end()
    else:
        live_print("\n❌ 本次运行未找到有效节点")
    
    live_print(f"\n⏱️ 总耗时: {round(time.time() - start_time, 2)}s")
