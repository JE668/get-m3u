import os, re, requests, time, concurrent.futures

FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2RvbmciICYmIGNpdHk9Ilpob25nc2hhbiI%3D"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Cookie": os.environ.get("FOFA_COOKIE", "")}
SOURCE_IP_FILE, SOURCE_M3U_FILE, RTP_DIR = "source-ip.txt", "source-m3u.txt", "rtp"

def verify_geo(ip):
    try:
        res = requests.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=10).json()
        isp = (res.get("isp", "") + res.get("org", "")).lower()
        return "广东" in res.get("regionName", "") and any(kw in isp for kw in ["电信", "telecom", "chinanet"])
    except: return False

def check_status(ip_port):
    for path in ["/stat", "/status"]:
        try:
            r = requests.get(f"http://{ip_port}{path}", timeout=4)
            if r.status_code == 200 and "udpxy" in r.text.lower(): return True
        except: continue
    return False

if __name__ == "__main__":
    print("📡 步骤1: 抓取并校验地理位置...")
    raw_ips = []
    try:
        r = requests.get(FOFA_URL, headers=HEADERS, timeout=15)
        raw_ips = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
    except: pass

    geo_ips = [ip for ip in raw_ips if verify_geo(ip.split(":")[0]) or (time.sleep(1.2) or False)]
    
    print("🔍 步骤2: 校验 Web 接口...")
    online_ips = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
        futures = {ex.submit(check_status, ip): ip for ip in geo_ips}
        online_ips = [futures[f] for f in concurrent.futures.as_completed(futures) if f.result()]

    if online_ips:
        online_ips.sort()
        with open(SOURCE_IP_FILE, "w") as f: f.write("\n".join(online_ips))
        
        rtp_path = os.path.join(RTP_DIR, "广东电信.txt")
        if os.path.exists(rtp_path):
            with open(rtp_path) as f: rtps = [x.strip() for x in f if "," in x]
            m3u = [f"{r.split(',')[0]},http://{ip}/{'rtp' if 'rtp://' in r else 'udp'}/{r.split('://')[1]}" for ip in online_ips for r in rtps]
            with open(SOURCE_M3U_FILE, "w") as f: f.write("\n".join(m3u))
            print(f"✅ 基础文件已生成，找到 {len(online_ips)} 个在线服务器")
