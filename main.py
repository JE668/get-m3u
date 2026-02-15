import os, re, requests, time, concurrent.futures, subprocess, tarfile
from datetime import datetime

# ===============================
# 1. 配置区
# ===============================
# 精准 C 段狙击
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

def setup_dismap():
    """Dismap 安装逻辑 - 使用更稳健的下载链接"""
    if os.path.exists("./dismap"): return True
    log_section("安装 Dismap 扫描引擎", "🛠️")
    
    # 重新校验后的下载地址
    url = "https://github.com/zhzyker/dismap/releases/download/v0.3.8/dismap_0.3.8_linux_amd64.tar.gz"
    try:
        print("  📥 正在尝试从 GitHub 下载 Dismap...")
        r = requests.get(url, stream=True, timeout=60, allow_redirects=True)
        if r.status_code == 200:
            with open("dismap.tar.gz", "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            if os.path.getsize("dismap.tar.gz") < 100000:
                print("  ❌ 下载文件过小，可能是错误的 HTML 页面。")
                return False

            with tarfile.open("dismap.tar.gz", "r:gz") as tar:
                tar.extractall()
            
            # 部分压缩包解压后可能在子目录，这里做个移动处理
            if not os.path.exists("./dismap") and os.path.exists("./dismap_0.3.8_linux_amd64/dismap"):
                os.rename("./dismap_0.3.8_linux_amd64/dismap", "./dismap")

            os.chmod("dismap", 0o755)
            print("  ✅ Dismap 安装成功")
            return True
        else:
            print(f"  ❌ 下载失败，HTTP 状态码: {r.status_code} (请检查仓库 releases 是否变动)")
            return False
    except Exception as e:
        print(f"  ❌ 安装异常: {e}")
        return False

def scrape_fofa():
    """抓取并详细输出 FOFA 资源"""
    log_section("抓取 FOFA 资源", "📡")
    if not HEADERS["Cookie"]: 
        print("  ⏭️  未配置 Cookie，跳过。")
        return []
    try:
        r = requests.get(FOFA_URL, headers=HEADERS, timeout=15)
        if "账号登录" in r.text:
            print("  ❌ FOFA Cookie 已失效，请及时更新 Secret！")
            return []
        
        # 提取所有匹配的 IP:端口
        raw_list = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
        
        if raw_list:
            print(f"  ✅ 成功获取原始数据 | 共找到 {len(raw_list)} 条记录")
            print("  📜 [详细抓取列表]:")
            unique_in_step = sorted(list(set(raw_list)))
            for ip in unique_in_step:
                # 统计该 IP 出现的次数
                count = raw_list.count(ip)
                print(f"    - {ip} (出现 {count} 次)")
            return unique_in_step
        else:
            print("  ⚠️  页面请求成功，但未匹配到任何 IP:端口。")
            return []
    except Exception as e:
        print(f"  ❌ FOFA 请求异常: {e}")
        return []

def run_dismap_scan():
    """定向扫描"""
    log_section("启动定向 C 段扫描", "🚀")
    found_ips = []
    targets = ",".join(TARGET_C_SEGMENTS)
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
    print(f"  ✅ 扫描结束 | 发现 {len(found_ips)} 个潜在节点")
    return list(set(found_ips))

# ===============================
# 3. 校验与处理模块
# ===============================

def update_rtp_template():
    log_section("同步 RTP 模板", "🔄")
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
                count = 0
                for i in range(len(lines)):
                    if lines[i].startswith("#EXTINF"):
                        name = lines[i].split(',')[-1].strip()
                        for j in range(i + 1, min(i + 5, len(lines))):
                            if lines[j].strip().startswith("rtp://"):
                                if lines[j].strip() not in unique_rtp:
                                    unique_rtp[lines[j].strip()] = name
                                    count += 1
                                break
                print(f"  📥 {url.split('/')[-1]} | 解析成功 | 提取 {count} 条")
        except: pass
    if unique_rtp:
        with open(RTP_FILE, "w", encoding="utf-8") as f:
            for url, name in unique_rtp.items(): f.write(f"{name},{url}\n")
        print(f"📊 统计: RTP 模板更新完毕 | 共 {len(unique_rtp)} 个频道")

def verify_geo(ip_port):
    """地理校验: 优化格式"""
    try:
        ip = ip_port.split(":")[0]
        res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=10).json()
        if res.get("status") != "success": return False, f"{ip_port} | 接口限制"
        reg, city, isp = res.get("regionName","未知"), res.get("city","未知"), res.get("isp","未知")
        is_gd = "广东" in reg
        is_tel = any(kw in isp.lower() for kw in ["电信", "telecom", "chinanet"])
        # 修正格式：IP:端口 | 地区 | 运营商
        info = f"{ip_port} | {reg} - {city} | {isp}"
        return (is_gd and is_tel), info
    except: return False, f"{ip_port} | 异常"

if __name__ == "__main__":
    start_time = time.time()
    update_rtp_template()

    # 1. 获取全量 IP
    fofa_ips = scrape_fofa()
    scanned_ips = []
    if setup_dismap():
        scanned_ips = run_dismap_scan()
    
    unique_raw = sorted(list(set(fofa_ips + scanned_ips)))
    print(f"\n📊 汇总统计: FOFA ({len(fofa_ips)}) + 扫描 ({len(scanned_ips)}) -> 去重后总计 {len(unique_raw)} 个 IP")

    # 2. 地理校验
    log_section("地理归属地校验 (广东电信)", "🌍")
    geo_ips = []
    for idx, ip_port in enumerate(unique_raw, 1):
        ok, desc = verify_geo(ip_port)
        status = "✅ 匹配" if ok else "⏭️ 跳过"
        print(f"  [{idx:02d}/{len(unique_raw):02d}] {status} | {desc}")
        if ok: geo_ips.append(ip_port)
        time.sleep(1.3) # API 频率限制保护

    # 3. 结果保存
    log_section("数据归档与拼装", "💾")
    if geo_ips:
        geo_ips.sort()
        with open(SOURCE_IP_FILE, "w", encoding="utf-8") as f: f.write("\n".join(geo_ips))
        if os.path.exists(RTP_FILE):
            with open(RTP_FILE, encoding="utf-8") as f: rtps = [x.strip() for x in f if "," in x]
            m3u = [f"{r.split(',')[0]},http://{ip}/rtp/{r.split('://')[1]}" for ip in geo_ips for r in rtps]
            for fpath in [SOURCE_NONCHECK_FILE, SOURCE_M3U_FILE]:
                with open(fpath, "w", encoding="utf-8") as f: f.write("\n".join(m3u))
            print(f"✨ 报告: 在线 IP {len(geo_ips)} 个 | 拼装链接 {len(m3u)} 条")
    else:
        print("❌ 终止: 未发现符合条件的节点")
    
    print(f"\n⏱️ 总耗时: {round(time.time() - start_time, 2)}s")
