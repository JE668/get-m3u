import os, re, requests, time, concurrent.futures

# 配置
FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2RvbmciICYmIGNpdHk9Ilpob25nc2hhbiI%3D"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Cookie": os.environ.get("FOFA_COOKIE", "")}
SOURCE_IP_FILE, SOURCE_M3U_FILE, RTP_DIR = "source-ip.txt", "source-m3u.txt", "rtp"

def verify_geo(ip):
    try:
        url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
        res = requests.get(url, timeout=10).json()
        isp = (res.get("isp", "") + res.get("org", "")).lower()
        is_gd = "广东" in res.get("regionName", "")
        is_telecom = any(kw in isp for kw in ["电信", "telecom", "chinanet"])
        return is_gd and is_telecom
    except: return False

def check_status(ip_port):
    for path in ["/stat", "/status", "/status/"]:
        try:
            r = requests.get(f"http://{ip_port}{path}", timeout=4)
            if r.status_code == 200:
                if any(kw in r.text.lower() for kw in ["udpxy", "stat", "client", "active"]):
                    return True
        except: continue
    return False

if __name__ == "__main__":
    print("📡 步骤1: 抓取 FOFA 原始数据...")
    raw_list = []
    try:
        r = requests.get(FOFA_URL, headers=HEADERS, timeout=15)
        raw_list = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
    except Exception as e:
        print(f"❌ FOFA 请求异常: {e}")

    # --- 改进点：立即去重 ---
    unique_raw = sorted(list(set(raw_list)))
    print(f"   找到 {len(raw_list)} 个条目，去重后剩余 {len(unique_raw)} 个 IP，开始地理校验...")
    
    geo_ips = []
    for ip_port in unique_raw:
        if verify_geo(ip_port.split(":")[0]):
            print(f"   ✅ 归属地匹配: {ip_port}")
            geo_ips.append(ip_port)
        time.sleep(1.2) # API 保护

    print(f"🔍 步骤2: 校验 Web 接口 (候选: {len(geo_ips)} 个)...")
    online_ips = []
    if geo_ips:
        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as ex:
            futures = {ex.submit(check_status, ip): ip for ip in geo_ips}
            for f in concurrent.futures.as_completed(futures):
                if f.result():
                    ip_found = futures[f]
                    print(f"   🟢 接口在线: {ip_found}")
                    online_ips.append(ip_found)

    # --- 改进点：确保文件写入逻辑清晰 ---
    if online_ips:
        online_ips = sorted(list(set(online_ips))) # 二次去重
        with open(SOURCE_IP_FILE, "w", encoding="utf-8") as f: 
            f.write("\n".join(online_ips))
        print(f"📝 已生成 {SOURCE_IP_FILE}")

        # 检查 RTP 模板
        rtp_path = os.path.join(RTP_DIR, "广东电信.txt")
        if os.path.exists(rtp_path):
            try:
                with open(rtp_path, encoding="utf-8") as f: 
                    rtps = [x.strip() for x in f if "," in x]
                
                m3u_lines = []
                for ip in online_ips:
                    for r in rtps:
                        name, rtp_url = r.split(",", 1)
                        proto = "rtp" if "rtp://" in rtp_url else "udp"
                        suffix = rtp_url.split("://")[1]
                        m3u_lines.append(f"{name},http://{ip}/{proto}/{suffix}")
                
                if m3u_lines:
                    with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f: 
                        f.write("\n".join(m3u_lines))
                    print(f"✅ 基础文件已生成，找到 {len(online_ips)} 个在线服务器，拼装 {len(m3u_lines)} 条链接")
                else:
                    print("⚠️ 警告: RTP 模板内容解析为空")
            except Exception as e:
                print(f"❌ 解析 RTP 模板时出错: {e}")
        else:
            print(f"❌ 错误: 找不到模板文件 {rtp_path}，无法生成 source-m3u.txt")
    else:
        print("❌ 流程中断: 没有发现任何在线的 UDPXY 接口")
