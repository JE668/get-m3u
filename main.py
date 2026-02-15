import os, re, requests, time, concurrent.futures, subprocess, tarfile
from datetime import datetime

# ===============================
# 1. 定向扫描配置 (根据你的 FOFA 结果整合)
# ===============================
# 我们将你提供的 IP 转化为 /16 或 /24 网段，缩小范围以提高速度
IP_SEGMENTS = [
    "106.111.0.0/16",
    "113.95.0.0/16",
    "116.30.0.0/16",
    "121.33.0.0/16",
    "14.145.0.0/16",
    "183.30.0.0/16",
    "183.31.0.0/16",
    "59.35.0.0/16",
    "61.146.0.0/16",
    "113.102.0.0/16"
]

# 整合有效端口
SCAN_PORTS = "4022,8000,8686,55555,54321,1024,10001,1024,500,8888,8889,8686,7788"

SCAN_TARGETS = ",".join(IP_SEGMENTS)

HEADERS = {"User-Agent": "Mozilla/5.0"}
RTP_SOURCES = [
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_4k.m3u",
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_hd.m3u"
]
RTP_FILE = os.path.join("rtp", "ChinaTelecom-Guangdong.txt")
SOURCE_IP_FILE, SOURCE_M3U_FILE, SOURCE_NONCHECK_FILE = "source-ip.txt", "source-m3u.txt", "source-m3u-noncheck.txt"

def log_section(name, icon="🔹"):
    print(f"\n{icon} {'='*15} {name} {'='*15}")

def setup_dismap():
    if os.path.exists("./dismap"): return True
    log_section("安装 Dismap 引擎", "🛠️")
    url = "https://github.com/zhzyker/dismap/releases/download/v0.3.8/dismap_0.3.8_linux_amd64.tar.gz"
    try:
        r = requests.get(url, stream=True)
        with open("dismap.tar.gz", "wb") as f: f.write(r.content)
        with tarfile.open("dismap.tar.gz", "r:gz") as tar: tar.extractall()
        os.chmod("dismap", 0o755)
        print("  ✅ Dismap 安装成功")
        return True
    except: return False

def run_dismap_scan():
    log_section("主动探测阶段 (Dismap)", "🚀")
    found_ips = []
    # 命令说明：-i 目标, -p 端口, --level 1 识别, --thread 1000 提高速度
    # GitHub Runner 性能不错，可以开到 1000 线程
    cmd = ["./dismap", "-i", SCAN_TARGETS, "-p", SCAN_PORTS, "--level", "1", "--thread", "1000", "--timeout", "2"]
    
    print(f"  📡 目标网段: {len(IP_SEGMENTS)} 个")
    print(f"  🔌 监控端口: {SCAN_PORTS}")
    
    try:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            if "[+]" in line:
                print(f"    {line.strip()}")
                match = re.search(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', line)
                if match: found_ips.append(match.group(1))
        process.wait()
    except Exception as e: print(f"  ❌ 扫描异常: {e}")
    return list(set(found_ips))

def update_rtp_template():
    log_section("同步 RTP 模板", "🔄")
    os.makedirs("rtp", exist_ok=True)
    unique_rtp = {}
    for url in RTP_SOURCES:
        try:
            r = requests.get(url, timeout=15)
            r.encoding = 'utf-8'
            if r.status_code == 200:
                lines = r.text.splitlines()
                for i in range(len(lines)):
                    if lines[i].startswith("#EXTINF"):
                        name = lines[i].split(',')[-1].strip()
                        for j in range(i+1, min(i+5, len(lines))):
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
        url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
        res = requests.get(url, timeout=10).json()
        if res.get("status") != "success": return False, f"{ip_port} | 查询受限"
        reg, city, isp = res.get("regionName","未知"), res.get("city","未知"), res.get("isp","未知")
        is_gd = "广东" in reg
        is_tel = any(kw in isp.lower() for kw in ["电信", "telecom", "chinanet"])
        # 统一输出格式: IP:端口 | 地区 | 运营商
        info = f"{ip_port} | {reg} - {city} | {isp}"
        return (is_gd and is_tel), info
    except: return False, f"{ip_port} | 网络异常"

if __name__ == "__main__":
    start_time = time.time()
    update_rtp_template()

    # 1. 获取资源 (Dismap)
    if setup_dismap():
        scanned = run_dismap_scan()
    else:
        scanned = []

    # 2. 地理校验
    log_section("地理归属地校验 (广东电信)", "🌍")
    geo_ips = []
    unique_raw = sorted(list(set(scanned)))
    total = len(unique_raw)
    
    for idx, ip_port in enumerate(unique_raw, 1):
        ok, desc = verify_geo(ip_port)
        status = "✅ 匹配" if ok else "⏭️ 跳过"
        print(f"  [{idx:02d}/{total:02d}] {status} | {desc}")
        if ok: geo_ips.append(ip_port)
        time.sleep(1.2)

    # 3. 归档
    log_section("数据归档与拼装", "💾")
    if geo_ips:
        geo_ips.sort()
        with open(SOURCE_IP_FILE, "w", encoding="utf-8") as f: f.write("\n".join(geo_ips))
        if os.path.exists(RTP_FILE):
            with open(RTP_FILE, encoding="utf-8") as f: rtps = [x.strip() for x in f if "," in x]
            m3u = [f"{r.split(',')[0]},http://{ip}/rtp/{r.split('://')[1]}" for ip in geo_ips for r in rtps]
            for fpath in [SOURCE_NONCHECK_FILE, SOURCE_M3U_FILE]:
                with open(fpath, "w", encoding="utf-8") as f: f.write("\n".join(m3u))
            print(f"✨ 报告: 在线 IP {len(geo_ips)} 个 | 播放链接 {len(m3u)} 条")
    else:
        print("❌ 终止: 本次扫描未发现符合条件的 udpxy 节点")
    
    print(f"\n⏱️ 总耗时: {round(time.time() - start_time, 2)}s")
