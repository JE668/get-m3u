import os, re, requests, time, concurrent.futures
from datetime import datetime

# ===============================
# 配置区
# ===============================
# 搜索关键词
SEARCH_KEYWORD = "广东电信"

# 免登录搜索引擎配置 (Tonkiang)
TONKIANG_URL = "https://tonkiang.us/?i=" + SEARCH_KEYWORD

# FOFA 配置 (保留作为备选，若Cookie失效会自动跳过)
# 带城市筛选
# FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2RvbmciICYmIGNpdHk9Ilpob25nc2hhbiI%3D"

# 不带城市筛选
FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2Rvbmci&filter_type=last_month"
# FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2RvbmciICYmIGNpdHk9Ilpob25nc2hhbiI="
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cookie": os.environ.get("FOFA_COOKIE", ""),
    "Referer": "https://tonkiang.us/"
}

RTP_SOURCES = [
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_4k.m3u",
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_hd.m3u"
]
RTP_FILE = os.path.join("rtp", "ChinaTelecom-Guangdong.txt")
SOURCE_IP_FILE, SOURCE_M3U_FILE, SOURCE_NONCHECK_FILE = "source-ip.txt", "source-m3u.txt", "source-m3u-noncheck.txt"

def log_section(name, icon="🔹"):
    print(f"\n{icon} {'='*15} {name} {'='*15}")

def update_rtp_template():
    log_section("同步并更新 RTP 模板", "🔄")
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
                        for j in range(i + 1, min(i + 5, len(lines))):
                            if lines[j].strip().startswith("rtp://"):
                                if lines[j].strip() not in unique_rtp:
                                    unique_rtp[lines[j].strip()] = name
                                    count += 1
                                break
                print(f"  📥 {url.split('/')[-1]} | 解析成功 | 提取 {count} 条")
        except: print(f"  ❌ 同步失败: {url.split('/')[-1]}")
    if unique_rtp:
        with open(RTP_FILE, "w", encoding="utf-8") as f:
            for url, name in unique_rtp.items(): f.write(f"{name},{url}\n")
        print(f"📊 统计: RTP 模板更新完毕 | 共 {len(unique_rtp)} 个频道")

def scrape_tonkiang():
    """免登录从 Tonkiang 爬取 IP"""
    log_section("从 Tonkiang 检索资源 (免登录)", "🔍")
    found_ips = []
    try:
        r = requests.get(TONKIANG_URL, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            # 匹配 IP:端口 格式
            found_ips = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
            print(f"  ✅ Tonkiang 响应成功 | 提取到 {len(found_ips)} 个潜在 IP")
        else:
            print(f"  ❌ Tonkiang 访问失败 | 状态码: {r.status_code}")
    except Exception as e:
        print(f"  ❌ Tonkiang 异常: {e}")
    return found_ips

def scrape_fofa():
    """从 FOFA 爬取 IP"""
    if not HEADERS["Cookie"]:
        return []
    log_section("从 FOFA 检索资源", "📡")
    found_ips = []
    try:
        r = requests.get(FOFA_URL, headers=HEADERS, timeout=15)
        if "账号登录" in r.text or "登录后可见" in r.text:
            print("  ⚠️ FOFA Cookie 已失效，跳过此源。")
            return []
        found_ips = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
        print(f"  ✅ FOFA 响应成功 | 提取到 {len(found_ips)} 个潜在 IP")
    except:
        print("  ❌ FOFA 访问异常")
    return found_ips

def verify_geo(ip_port):
    try:
        ip = ip_port.split(":")[0]
        url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
        res = requests.get(url, timeout=10).json()
        if res.get("status") != "success": return False, f"{ip_port} | 查询失败"
        region, city, isp = res.get("regionName","未知"), res.get("city","未知"), res.get("isp","未知")
        is_gd = "广东" in region
        is_telecom = any(kw in isp.lower() for kw in ["电信", "telecom", "chinanet"])
        info = f"{ip_port} | {region} - {city} | {isp}"
        return (is_gd and is_telecom), info
    except: return False, f"{ip_port} | 网络异常"

def check_status(ip_port):
    for path in ["/stat", "/status"]:
        try:
            r = requests.get(f"http://{ip_port}{path}", timeout=4)
            if r.status_code == 200 and any(kw in r.text.lower() for kw in ["udpxy", "stat", "client"]):
                return True
        except: continue
    return False

if __name__ == "__main__":
    start_time = time.time()
    update_rtp_template()

    # 1. 汇总多源数据
    ips_tonkiang = scrape_tonkiang()
    ips_fofa = scrape_fofa()
    unique_raw = sorted(list(set(ips_tonkiang + ips_fofa)))
    print(f"\n📊 资源汇总: 总共获取到 {len(unique_raw)} 个唯一 IP")

    # 2. 地理校验
    log_section("地理归属地校验 (广东电信)", "🌍")
    geo_ips = []
    total = len(unique_raw)
    for idx, ip_port in enumerate(unique_raw, 1):
        ok, desc = verify_geo(ip_port)
        status = "✅ 匹配" if ok else "⏭️ 跳过"
        print(f"  [{idx:02d}/{total:02d}] {status} | {desc}")
        if ok: geo_ips.append(ip_port)
        time.sleep(1.2)

    # 3. Web 状态探测
    log_section("Web 接口在线检测 (UDPXY)", "🔍")
    online_ips = []
    if geo_ips:
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
            futures = {ex.submit(check_status, ip): ip for ip in geo_ips}
            for f in concurrent.futures.as_completed(futures):
                ip = futures[f]
                if f.result():
                    print(f"  🟢 在线 | {ip}"); online_ips.append(ip)
                else: print(f"  🔴 离线 | {ip}")

    # 4. 数据保存
    log_section("数据归档与拼装", "💾")
    if online_ips:
        online_ips.sort()
        with open(SOURCE_IP_FILE, "w", encoding="utf-8") as f: f.write("\n".join(online_ips))
        if os.path.exists(RTP_FILE):
            with open(RTP_FILE, encoding="utf-8") as f: rtps = [x.strip() for x in f if "," in x]
            m3u = [f"{r.split(',')[0]},http://{ip}/rtp/{r.split('://')[1]}" for ip in online_ips for r in rtps]
            for fpath in [SOURCE_NONCHECK_FILE, SOURCE_M3U_FILE]:
                with open(fpath, "w", encoding="utf-8") as f: f.write("\n".join(m3u))
            print(f"✨ 报告: 有效服务器 {len(online_ips)} 个 | 播放链接 {len(m3u)} 条")
    else: print("❌ 终止: 未发现可用在线接口")
    print(f"\n⏱️ 总耗时: {round(time.time() - start_time, 2)}s")
