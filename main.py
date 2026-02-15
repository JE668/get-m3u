import os, re, requests, time, concurrent.futures, subprocess, tarfile
from datetime import datetime

# ===============================
# 1. 配置区
# ===============================
# 将你提供的有效 IP 转化为精准的 /24 C段，扫描速度提升 256 倍
TARGET_C_SEGMENTS = [
    "106.111.127.0/24", "113.95.140.0/24", "116.30.197.0/24",
    "121.33.112.0/24", "14.145.163.0/24", "183.30.202.0/24",
    "183.31.11.0/24", "59.35.244.0/24", "61.146.190.0/24",
    "113.102.18.0/24"
]
SCAN_PORTS = "4022,8000,8686,55555,54321,1024,10001,8888,8889,55555,54321,5000"

FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2Rvbmci"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Cookie": os.environ.get("FOFA_COOKIE", "") 
}
RTP_FILE = os.path.join("rtp", "ChinaTelecom-Guangdong.txt")
SOURCE_IP_FILE, SOURCE_M3U_FILE, SOURCE_NONCHECK_FILE = "source-ip.txt", "source-m3u.txt", "source-m3u-noncheck.txt"

def log_section(name, icon="🔹"):
    print(f"\n{icon} {'='*15} {name} {'='*15}")

# ===============================
# 2. 资源获取模块
# ===============================

def scrape_fofa():
    """保底手段：FOFA 爬取"""
    if not HEADERS["Cookie"]: 
        print("  ⏭️  FOFA 跳过 | 未配置 Cookie")
        return []
    print("  📡 正在通过 FOFA 获取保底数据...")
    try:
        r = requests.get(FOFA_URL, headers=HEADERS, timeout=15)
        ips = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
        print(f"  ✅ FOFA 完成 | 抓取到 {len(ips)} 个 IP")
        return ips
    except: return []

def setup_dismap():
    """安装扫描引擎"""
    if os.path.exists("./dismap"): return True
    print("  📥 正在下载 Dismap 扫描引擎...")
    url = "https://github.com/zhzyker/dismap/releases/download/v0.3.8/dismap_0.3.8_linux_amd64.tar.gz"
    try:
        r = requests.get(url, timeout=30)
        with open("dismap.tar.gz", "wb") as f: f.write(r.content)
        with tarfile.open("dismap.tar.gz", "r:gz") as tar: tar.extractall()
        os.chmod("dismap", 0o755)
        return True
    except Exception as e:
        print(f"  ❌ 安装失败: {e}")
        return False

def run_dismap_scan():
    """精准 C 段扫描"""
    print("  🚀 启动定向 C 段扫描 (狙击模式)...")
    found_ips = []
    targets = ",".join(TARGET_C_SEGMENTS)
    # -i 目标, -p 端口, --level 1 识别, --thread 500
    cmd = ["./dismap", "-i", targets, "-p", SCAN_PORTS, "--level", "1", "--thread", "500", "--timeout", "2"]
    
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            if "[+]" in line:
                print(f"    {line.strip()}")
                match = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', line)
                if match: found_ips.append(match.group(1))
        process.wait()
    except: pass
    print(f"  ✅ 扫描结束 | 发现 {len(found_ips)} 个存活 udpxy")
    return found_ips

# ===============================
# 3. 校验与持久化模块
# ===============================

def update_rtp_template():
    log_section("同步并更新 RTP 模板", "🔄")
    os.makedirs("rtp", exist_ok=True)
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
                for i in range(len(lines)):
                    if lines[i].startswith("#EXTINF"):
                        name = lines[i].split(',')[-1].strip()
                        for j in range(i + 1, min(i + 5, len(lines))):
                            if lines[j].strip().startswith("rtp://"):
                                unique_rtp[lines[j].strip()] = name
                                break
        except: pass
    if unique_rtp:
        with open(RTP_FILE, "w", encoding="utf-8") as f:
            for url, name in unique_rtp.items(): f.write(f"{name},{url}\n")
        print(f"📊 统计: RTP 模板更新完毕 | 共 {len(unique_rtp)} 个频道")

def verify_geo(ip_port):
    try:
        ip = ip_port.split(":")[0]
        res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=10).json()
        if res.get("status") != "success": return False, f"{ip_port} | 接口限制"
        region, city, isp = res.get("regionName","未知"), res.get("city","未知"), res.get("isp","未知")
        is_gd = "广东" in region
        is_telecom = any(kw in isp.lower() for kw in ["电信", "telecom", "chinanet"])
        return (is_gd and is_telecom), f"{ip_port} | {region} - {city} | {isp}"
    except: return False, f"{ip_port} | 异常"

if __name__ == "__main__":
    start_time = time.time()
    update_rtp_template()

    # 第一阶段：混合抓取
    log_section("多模式资源抓取", "📡")
    fofa_ips = scrape_fofa()
    
    if setup_dismap():
        scanned_ips = run_dismap_scan()
    else:
        scanned_ips = []
    
    unique_raw = sorted(list(set(fofa_ips + scanned_ips)))
    print(f"\n📊 汇总结果: 发现 {len(unique_raw)} 个唯一 IP")

    # 第二阶段：地理过滤
    log_section("地理归属地校验 (广东电信)", "🌍")
    geo_ips = []
    for idx, ip in enumerate(unique_raw, 1):
        ok, desc = verify_geo(ip)
        print(f"  [{idx:02d}/{len(unique_raw):02d}] {'✅ 匹配' if ok else '⏭️ 跳过'} | {desc}")
        if ok: geo_ips.append(ip)
        time.sleep(1.2)

    # 第三阶段：持久化
    log_section("数据归档与拼装", "💾")
    if geo_ips:
        geo_ips.sort()
        with open(SOURCE_IP_FILE, "w", encoding="utf-8") as f: f.write("\n".join(geo_ips))
        if os.path.exists(RTP_FILE):
            with open(RTP_FILE, encoding="utf-8") as f: rtps = [x.strip() for x in f if "," in x]
            m3u = [f"{r.split(',')[0]},http://{ip}/rtp/{r.split('://')[1]}" for ip in geo_ips for r in rtps]
            for fpath in [SOURCE_NONCHECK_FILE, SOURCE_M3U_FILE]:
                with open(fpath, "w", encoding="utf-8") as f: f.write("\n".join(m3u))
            print(f"✨ 报告: 有效服务器 {len(geo_ips)} 个 | 播放链接 {len(m3u)} 条")
    else:
        print("❌ 终止: 未发现符合条件的节点")
    
    print(f"\n⏱️ 总耗时: {round(time.time() - start_time, 2)}s")
