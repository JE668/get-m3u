import os, re, requests, time, concurrent.futures
from datetime import datetime

# ===============================
# 配置区
# ===============================
# 带城市筛选
# FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2RvbmciICYmIGNpdHk9Ilpob25nc2hhbiI%3D"

# 不带城市筛选
FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2Rvbmci"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cookie": os.environ.get("FOFA_COOKIE", "") 
}
RTP_SOURCES = [
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_4k.m3u",
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_hd.m3u"
]
SOURCE_IP_FILE, SOURCE_M3U_FILE, SOURCE_NONCHECK_FILE = "source-ip.txt", "source-m3u.txt", "source-m3u-noncheck.txt"
RTP_DIR, RTP_FILE = "rtp", os.path.join("rtp", "广东电信.txt")

def log_section(name):
    print(f"\n{'='*20} {name} {'='*20}")

def update_rtp_template():
    log_section("0. 同步并转换 RTP 模板")
    os.makedirs(RTP_DIR, exist_ok=True)
    unique_rtp = {}
    for url in RTP_SOURCES:
        fname = url.split('/')[-1]
        try:
            print(f"📥 正在下载上游 M3U: {fname}...")
            r = requests.get(url, timeout=15)
            r.encoding = 'utf-8'
            if r.status_code == 200:
                # 兼容 M3U 标签，提取最后一个逗号后的频道名
                matches = re.findall(r'#EXTINF:.*?,(.*?)\n(rtp://[\d\.:]+)', r.text)
                for name, r_url in matches:
                    if r_url.strip() not in unique_rtp:
                        unique_rtp[r_url.strip()] = name.strip()
                print(f"   ✅ 解析成功: 找到 {len(matches)} 条记录")
        except Exception as e:
            print(f"   ❌ 下载失败 {fname}: {e}")

    if unique_rtp:
        with open(RTP_FILE, "w", encoding="utf-8") as f:
            for r_url, name in unique_rtp.items():
                f.write(f"{name},{r_url}\n")
        print(f"📊 统计: RTP 模板转换完成，共 {len(unique_rtp)} 条独立频道")
    else:
        print("⚠️ 警告: 未能同步到数据，尝试使用本地缓存。")

def verify_geo(ip):
    try:
        url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
        res = requests.get(url, timeout=10).json()
        if res.get("status") != "success": return False, "API限制"
        region = res.get("regionName", "")
        isp = (res.get("isp", "") + res.get("org", "")).lower()
        is_gd = "广东" in region
        is_telecom = any(kw in isp for kw in ["电信", "telecom", "chinanet"])
        if is_gd and is_telecom: return True, "匹配"
        return False, f"地区:{region}/运营商:{res.get('isp','')}"
    except: return False, "网络异常"

def check_status(ip_port):
    for path in ["/stat", "/status", "/status/"]:
        try:
            r = requests.get(f"http://{ip_port}{path}", timeout=4)
            if r.status_code == 200 and any(kw in r.text.lower() for kw in ["udpxy", "stat", "client", "active"]):
                return True
        except: continue
    return False

if __name__ == "__main__":
    start_total = time.time()
    update_rtp_template()

    log_section("1. 抓取 FOFA 资源")
    try:
        r = requests.get(FOFA_URL, headers=HEADERS, timeout=15)
        raw_list = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
        unique_raw = sorted(list(set(raw_list)))
        print(f"🔎 FOFA 发现: 原始条目 {len(raw_list)} 个，去重后 {len(unique_raw)} 个 IP")
    except Exception as e:
        print(f"❌ FOFA 抓取异常: {e}"); unique_raw = []

    log_section("2. 地理归属地校验 (广东电信)")
    geo_ips = []
    total = len(unique_raw)
    for idx, ip_port in enumerate(unique_raw, 1):
        host = ip_port.split(":")[0]
        ok, reason = verify_geo(host)
        if ok:
            print(f"   [{idx}/{total}] ✅ {ip_port} -> 归属地匹配")
            geo_ips.append(ip_port)
        else:
            print(f"   [{idx}/{total}] ⏭️  {ip_port} -> 跳过 ({reason})")
        time.sleep(1.2)

    log_section("3. Web 接口在线检测")
    online_ips = []
    if geo_ips:
        print(f"🚀 启动并行检测 {len(geo_ips)} 个候选服务器...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
            futures = {ex.submit(check_status, ip): ip for ip in geo_ips}
            for f in concurrent.futures.as_completed(futures):
                ip_found = futures[f]
                if f.result():
                    print(f"   🟢 在线: {ip_found}")
                    online_ips.append(ip_found)
                else:
                    print(f"   🔴 离线: {ip_found}")

    if online_ips:
        online_ips.sort()
        # 1. 保存 IP 列表
        with open(SOURCE_IP_FILE, "w", encoding="utf-8") as f: f.write("\n".join(online_ips))
        
        # 2. 拼装 M3U 列表
        if os.path.exists(RTP_FILE):
            with open(RTP_FILE, encoding="utf-8") as f: 
                rtps = [x.strip() for x in f if "," in x]
            
            m3u_all = []
            for ip in online_ips:
                for r in rtps:
                    name, r_url = r.split(",", 1)
                    # 关键修改：强制提取 rtp:// 后的地址，并将路径统一为 /udp/
                    suffix = r_url.split("://")[1]
                    m3u_all.append(f"{name},http://{ip}/udp/{suffix}")
            
            # 同时生成两个文件
            with open(SOURCE_NONCHECK_FILE, "w", encoding="utf-8") as f: f.write("\n".join(m3u_all))
            with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f: f.write("\n".join(m3u_all))
                
            print(f"\n✨ 阶段总结:")
            print(f"   - 有效服务器: {len(online_ips)} 个")
            print(f"   - 拼装链接总数: {len(m3u_all)} 条 (已强制使用 /udp/ 路径)")
            print(f"   - 文件已生成: {SOURCE_IP_FILE}, {SOURCE_NONCHECK_FILE}")
    
    print(f"\n⏱️  任务总耗时: {round(time.time() - start_total, 2)}s")
