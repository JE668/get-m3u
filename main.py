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

# 文件名配置
RTP_DIR = "rtp"
RTP_FILENAME = "ChinaTelecom-Guangdong.txt"
RTP_FILE = os.path.join(RTP_DIR, RTP_FILENAME)

SOURCE_IP_FILE = "source-ip.txt"
SOURCE_M3U_FILE = "source-m3u.txt"
SOURCE_NONCHECK_FILE = "source-m3u-noncheck.txt"

def log_section(name):
    print(f"\n{'='*20} {name} {'='*20}")

def update_rtp_template():
    log_section("0. 同步并更新 RTP 模板")
    os.makedirs(RTP_DIR, exist_ok=True)
    
    unique_rtp = {} # { "rtp://地址": "频道名" }
    
    for url in RTP_SOURCES:
        fname = url.split('/')[-1]
        try:
            print(f"📥 正在获取上游源: {fname}...")
            r = requests.get(url, timeout=15)
            r.encoding = 'utf-8'
            if r.status_code == 200:
                # 稳健的逐行解析算法
                lines = r.text.splitlines()
                count = 0
                for i in range(len(lines)):
                    line = lines[i].strip()
                    if line.startswith("#EXTINF"):
                        # 提取最后一个逗号后的内容作为频道名
                        try:
                            name = line.split(',')[-1].strip()
                            # 查找下一行非空的 URL
                            for j in range(i + 1, min(i + 5, len(lines))):
                                next_line = lines[j].strip()
                                if next_line.startswith("rtp://"):
                                    if next_line not in unique_rtp:
                                        unique_rtp[next_line] = name
                                        count += 1
                                    break
                        except: continue
                print(f"   ✅ 解析完成: 提取到 {count} 条频道")
        except Exception as e:
            print(f"   ❌ 同步失败 {fname}: {e}")

    if unique_rtp:
        # 将字典转换回 TXT 格式并写入
        print(f"💾 正在写入文件: {RTP_FILE}...")
        with open(RTP_FILE, "w", encoding="utf-8") as f:
            for r_url, name in unique_rtp.items():
                f.write(f"{name},{r_url}\n")
        print(f"📊 统计: RTP 模板已更新，总计 {len(unique_rtp)} 个独立频道")
    else:
        print(f"⚠️ 警告: 未能从线上获取到数据。")
        if os.path.exists(RTP_FILE):
            print(f"   ℹ️ 将继续使用本地现有的 {RTP_FILENAME}")
        else:
            print(f"   ❌ 错误: 本地也不存在 {RTP_FILENAME}，程序将无法拼装链接！")

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
    
    # 1. 优先更新 RTP 模板文件
    update_rtp_template()

    log_section("1. 抓取 FOFA 资源")
    try:
        r = requests.get(FOFA_URL, headers=HEADERS, timeout=15)
        raw_list = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
        unique_raw = sorted(list(set(raw_list)))
        print(f"🔎 FOFA 发现: 去重后 {len(unique_raw)} 个 IP")
    except Exception as e:
        print(f"❌ FOFA 抓取异常: {e}"); unique_raw = []

    log_section("2. 地理归属地校验 (广东电信)")
    geo_ips = []
    total = len(unique_raw)
    for idx, ip_port in enumerate(unique_raw, 1):
        host = ip_port.split(":")[0]
        ok, reason = verify_geo(host)
        if ok:
            print(f"   [{idx}/{total}] ✅ {ip_port} -> 匹配")
            geo_ips.append(ip_port)
        else:
            print(f"   [{idx}/{total}] ⏭️  {ip_port} -> 跳过 ({reason})")
        time.sleep(1.2)

    log_section("3. Web 接口在线检测")
    online_ips = []
    if geo_ips:
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
        with open(SOURCE_IP_FILE, "w", encoding="utf-8") as f: f.write("\n".join(online_ips))
        
        # 使用刚刚更新完的 RTP_FILE 进行拼装
        if os.path.exists(RTP_FILE):
            with open(RTP_FILE, encoding="utf-8") as f: 
                rtp_data = [x.strip() for x in f if "," in x]
            
            m3u_all = []
            for ip in online_ips:
                for r in rtp_data:
                    name, r_url = r.split(",", 1)
                    # 可选使用 /udp/或/rtp/ 路径
                    suffix = r_url.split("://")[1]
                    m3u_all.append(f"{name},http://{ip}/rtp/{suffix}")
            
            with open(SOURCE_NONCHECK_FILE, "w", encoding="utf-8") as f: f.write("\n".join(m3u_all))
            with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f: f.write("\n".join(m3u_all))
                
            print(f"\n✨ 最终结果:")
            print(f"   - 在线服务器: {len(online_ips)} 个")
            print(f"   - 拼装链接: {len(m3u_all)} ")
    
    print(f"\n⏱️  总耗时: {round(time.time() - start_total, 2)}s")
