import os, re, requests, time, concurrent.futures

FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2RvbmciICYmIGNpdHk9Ilpob25nc2hhbiI%3D"
HEADERS = {"User-Agent": "Mozilla/5.0", "Cookie": os.environ.get("FOFA_COOKIE", "")}
SOURCE_IP_FILE, SOURCE_M3U_FILE, RTP_DIR = "source-ip.txt", "source-m3u.txt", "rtp"

def verify_geo(ip):
    try:
        url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
        res = requests.get(url, timeout=10).json()
        isp = (res.get("isp", "") + res.get("org", "")).lower()
        return "广东" in res.get("regionName", "") and any(kw in isp for kw in ["电信", "telecom", "chinanet"])
    except: return False

def check_status(ip_port):
    for path in ["/stat", "/status", "/status/"]:
        try:
            r = requests.get(f"http://{ip_port}{path}", timeout=4)
            if r.status_code == 200 and any(kw in r.text.lower() for kw in ["udpxy", "stat", "client", "active"]):
                return True
        except: continue
    return False

if __name__ == "__main__":
    print("📡 1. 抓取 FOFA...")
    try:
        r = requests.get(FOFA_URL, headers=HEADERS, timeout=15)
        raw_list = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
        unique_raw = sorted(list(set(raw_list)))
    except: unique_raw = []

    print(f"   找到 {len(unique_raw)} 个去重 IP，开始地理校验...")
    geo_ips = []
    for ip_port in unique_raw:
        if verify_geo(ip_port.split(":")[0]):
            print(f"   ✅ 广东电信: {ip_port}")
            geo_ips.append(ip_port)
        time.sleep(1.2)

    print(f"🔍 2. 校验 Web 接口...")
    online_ips = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
        futures = {ex.submit(check_status, ip): ip for ip in geo_ips}
        for f in concurrent.futures.as_completed(futures):
            if f.result(): online_ips.append(futures[f])

    if online_ips:
        online_ips = sorted(list(set(online_ips)))
        with open(SOURCE_IP_FILE, "w", encoding="utf-8") as f: f.write("\n".join(online_ips))
        
        rtp_path = os.path.join(RTP_DIR, "广东电信.txt")
        if os.path.exists(rtp_path):
            with open(rtp_path, encoding="utf-8") as f: rtps = [x.strip() for x in f if "," in x]
            m3u = []
            for ip in online_ips:
                for r in rtps:
                    name, r_url = r.split(",", 1)
                    p = "rtp" if "rtp://" in r_url else "udp"
                    m3u.append(f"{name},http://{ip}/{p}/{r_url.split('://')[1]}")
            with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f: f.write("\n".join(m3u))
            print(f"✅ 生成 {len(online_ips)} 个服务器，{len(m3u)} 条链接")
