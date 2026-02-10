import os, re, requests, time, concurrent.futures
from datetime import datetime

# ===============================
# 配置区
# ===============================
# 带城市筛选
# FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2RvbmciICYmIGNpdHk9Ilpob25nc2hhbiI%3D"

# 不带城市筛选
FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2RvbmciICYmIGNpdHk9Ilpob25nc2hhbiI="
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Cookie": os.environ.get("FOFA_COOKIE", "") 
}
RTP_SOURCES = [
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_4k.m3u",
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_hd.m3u"
]

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
    unique_rtp = {}
    for url in RTP_SOURCES:
        fname = url.split('/')[-1]
        try:
            print(f"📥 正在获取上游源: {fname}...")
            r = requests.get(url, timeout=15)
            r.encoding = 'utf-8'
            if r.status_code == 200:
                lines = r.text.splitlines()
                count = 0
                for i in range(len(lines)):
                    line = lines[i].strip()
                    if line.startswith("#EXTINF"):
                        try:
                            name = line.split(',')[-1].strip()
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
        with open(RTP_FILE, "w", encoding="utf-8") as f:
            for r_url, name in unique_rtp.items():
                f.write(f"{name},{r_url}\n")
        print(f"📊 统计: RTP 模板已更新，总计 {len(unique_rtp)} 个独立频道")
    else:
        if os.path.exists(RTP_FILE):
            print(f"   ℹ️ 使用本地缓存 {RTP_FILENAME}")

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
    unique_raw = []
    try:
        if not HEADERS["Cookie"]:
            print("❌ 错误: 未配置 FOFA_COOKIE 环境变量！")
        
        r = requests.get(FOFA_URL, headers=HEADERS, timeout=15)
        
        # --- 增加 Cookie 失效检测逻辑 ---
        if r.status_code == 401:
            print("❌ 错误: FOFA 提示未经授权 (401)。请检查 Cookie 是否填写正确！")
        elif "账号登录" in r.text or "登录后可见" in r.text or "Account Login" in r.text:
            print("❌ 警告: FOFA Cookie 已失效或已过期，当前获取的是登录页面！")
        elif r.status_code != 200:
            print(f"❌ 错误: FOFA 响应异常，状态码: {r.status_code}")
        else:
            raw_list = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
            if not raw_list:
                if "如果您看到此页面，说明您的请求频率过快" in r.text:
                    print("⚠️ 提醒: FOFA 提示请求频率过快，IP 已被临时限制。")
                else:
                    print("⚠️ 提醒: FOFA 响应成功但未找到 IP。请确认 Cookie 是否有效，或搜索条件是否有结果。")
            else:
                unique_raw = sorted(list(set(raw_list)))
                print(f"🔎 FOFA 发现: 去重后 {len(unique_raw)} 个 IP")
                
    except Exception as e:
        print(f"❌ FOFA 抓取异常: {e}")

    log_section("2. 地理归属地校验 (广东电信)")
    geo_ips = []
    if unique_raw:
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
    else:
        print("⏭️  由于未获取到原始 IP，跳过归属地校验。")

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
        if os.path.exists(RTP_FILE):
            with open(RTP_FILE, encoding="utf-8") as f: 
                rtp_data = [x.strip() for x in f if "," in x]
            m3u_all = []
            for ip in online_ips:
                for r in rtp_data:
                    name, r_url = r.split(",", 1)
                    suffix = r_url.split("://")[1]
                    m3u_all.append(f"{name},http://{ip}/rtp/{suffix}")
            
            with open(SOURCE_NONCHECK_FILE, "w", encoding="utf-8") as f: f.write("\n".join(m3u_all))
            with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f: f.write("\n".join(m3u_all))
            print(f"\n✨ 最终结果: 在线服务器 {len(online_ips)} 个，拼装链接 {len(m3u_all)} 条")
    else:
        print("❌ 流程中断: 无在线 UDPXY 接口。")
    
    print(f"\n⏱️  总耗时: {round(time.time() - start_total, 2)}s")
