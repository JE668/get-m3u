import os, re, requests, time, concurrent.futures, random, threading, tempfile
from collections import Counter

# ===============================
# 1. 配置区 (目录结构优化版)
# ===============================
FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2Rvbmci&filter_type=last_month"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Cookie": os.environ.get("FOFA_COOKIE", "")
}

RTP_SOURCES = [
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_4k.m3u",
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_hd.m3u"
]

# --- 自动创建目录结构 ---
os.makedirs("data", exist_ok=True)
os.makedirs("data/rtp", exist_ok=True)
os.makedirs("output", exist_ok=True)

# --- 文件路径定义 ---
DISCOVERY_FILE = "data/discovery.txt"
BLACKLIST_FILE = "data/blacklist.txt"
RTP_FILE = "data/rtp/ChinaTelecom-Guangdong.txt"

SOURCE_IP_FILE = "output/source-ip.txt"
SOURCE_M3U_FILE = "output/source-m3u.txt"
SOURCE_NONCHECK_FILE = "output/source-m3u-noncheck.txt"

DEFAULT_PORTS = [4022, 8000, 8686, 55555, 54321, 1024, 10001, 8443, 8888]

# ===============================
# 2. 基础工具函数
# ===============================
SUMMARY_FILE = os.environ.get("GITHUB_STEP_SUMMARY", "")

def live_print(content):
 print(content, flush=True)

def write_summary(content):
 """写入 GitHub Actions Job Summary（Markdown 格式，仅 GitHub 环境生效）"""
 if SUMMARY_FILE:
  try:
   with open(SUMMARY_FILE, "a", encoding="utf-8") as f:
    f.write(content + "\n")
  except OSError:
   pass

def log_group_start(name):
 live_print(f"\n::group::{name}")

def log_group_end():
 live_print("\n::endgroup::")

def log_section(name, icon="🔹"):
 live_print(f"\n{icon} {'='*15} {name} {'='*15}")

# ===============================
# 3. 核心功能函数
# ===============================

def get_geo_info(ip, session=None):
    """查询 IP 归属地（含限速容错）"""
    s = session or requests
    for attempt in range(3):
        try:
            r = s.get(f"http://ip-api.com/json/{ip}?lang=zh-CN", timeout=5)
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                live_print(f"  ⏳ ip-api 限速，等待 {wait}s 后重试...")
                time.sleep(wait); continue
            res = r.json()
            if res.get("status") != "success": return False, "API查询失败"
            region, city, isp = res.get("regionName", "未知"), res.get("city", "未知"), res.get("isp", "未知").lower()
            is_gd = "广东" in region
            is_tel = any(k in isp for k in ["电信", "telecom", "chinanet"])
            return (is_gd and is_tel), f"{region}-{city} | {res.get('isp')}"
        except (requests.RequestException, ValueError) as e:
            return False, f"查询异常: {e}"
    return False, "ip-api 限速重试耗尽"

def filter_segments(segments):
    """C段 预校验与清洗"""
    log_group_start("🛡️ C段 归属地预校验")
    blacklist = set()
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            blacklist = set([line.strip() for line in f if line.strip()])

    valid_segments, new_black_segments = [], []
    total = len(segments)
    blacklist_skip = 0
    live_print(f"📋 待检测: {total} 个 | 黑名单库: {len(blacklist)} 个")

    for idx, seg in enumerate(segments, 1):
        if seg in blacklist:
            blacklist_skip += 1
            continue
        # 抽取 .1 进行测试
        is_valid, desc = get_geo_info(f"{seg}.1")
        if is_valid:
            live_print(f"  [{idx}/{total}] ✅ 通过: {seg} ({desc})")
            valid_segments.append(seg)
        else:
            live_print(f"  [{idx}/{total}] ❌ 拉黑: {seg} ({desc})")
            new_black_segments.append(seg)
            blacklist.add(seg)
        time.sleep(2.0)  # API 频率保护（ip-api 限45次/分钟，2s间隔≈30次/分钟）

    if new_black_segments:
        with open(BLACKLIST_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(new_black_segments) + "\n")
        live_print(f"💾 新增黑名单: {len(new_black_segments)} 个")

    live_print(f"📊 最终有效 C段: {len(valid_segments)} 个 (黑名单跳过: {blacklist_skip} 个)")
    log_group_end()
    return valid_segments

def update_discovery_database(new_ips):
    """更新发现库"""
    log_group_start("📂 更新发现库 (data/discovery.txt)")
    segs, ports = set(), set(str(p) for p in DEFAULT_PORTS)  # P3#7: 统一为 str 类型

    if os.path.exists(DISCOVERY_FILE):
        with open(DISCOVERY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if "|" in line:
                    p = line.strip().split("|")
                    if len(p) >= 2:
                        if p[0] == "SEG": segs.add(p[1])
                        if p[0] == "PORT": ports.add(str(p[1]))

    for ip_port in new_ips:
        try:
            ip, port = ip_port.split(":")
            segs.add(".".join(ip.split(".")[:3]))
            ports.add(str(port))
        except ValueError:
            continue

    # P2#6: 排序前过滤无效端口，防止 key=int 在脏数据上崩溃
    valid_port_set = {p for p in ports if p.isdigit()}
    sorted_segs, sorted_ports = sorted(list(segs)), sorted(list(valid_port_set), key=int)
    with open(DISCOVERY_FILE, "w", encoding="utf-8") as f:
        for s in sorted_segs: f.write(f"SEG|{s}\n")
        for p in sorted_ports: f.write(f"PORT|{p}\n")

    live_print(f"✅ 库同步 | C段: {len(sorted_segs)} | 端口: {len(sorted_ports)}")
    log_group_end()
    return sorted_segs, sorted_ports

def check_udpxy(ip_port, scanned_set, lock):
    """HTTP 指纹探测 (含 IP 熔断，线程安全)"""
    ip = ip_port.split(":")[0]
    with lock:
        if ip in scanned_set: return False, None
        scanned_set.add(ip)  # P1#2: 立即占位，防止同 IP 并发重复探测
    try:
        r = requests.get(f"http://{ip_port}/status", timeout=2, headers={"User-Agent":"Wget/1.14"})
        if r.status_code == 200 and any(kw in r.text.lower() for kw in ["udpxy", "stat", "client"]):
            return True, ip_port
    except requests.RequestException:
        pass
    return False, None

def run_native_scan(segments, ports):
    """全量矩阵扫描"""
    log_group_start("🚀 启动全量矩阵扫描")
    if not segments:
        live_print("⚠️ 无有效网段"); log_group_end(); return []

    scan_workers = int(os.environ.get("SCAN_WORKERS", "100"))
    tasks = [f"{s}.{i}:{p}" for s in segments for i in range(1, 255) for p in ports]
    random.shuffle(tasks)
    live_print(f"⚡ 任务规模: {len(tasks)} 次探测 (并发: {scan_workers})")

    found_ips = []
    scanned_ips_set = set()
    lock = threading.Lock()
    with concurrent.futures.ThreadPoolExecutor(max_workers=scan_workers) as ex:
        futures = {ex.submit(check_udpxy, ip, scanned_ips_set, lock): ip for ip in tasks}
        for i, f in enumerate(concurrent.futures.as_completed(futures)):
            ok, matched_ip = f.result()
            if ok:
                live_print(f"  🌟 [发现] {matched_ip}")
                found_ips.append(matched_ip)
            if (i+1)%5000==0:
                live_print(f"  📊 进度: {i+1}/{len(tasks)} | 存活: {len(found_ips)}")

    live_print(f"✅ 扫描结束 | 发现 {len(found_ips)} 个")
    log_group_end()
    return list(set(found_ips))

def scrape_fofa():
    """FOFA 抓取"""
    log_group_start("📡 抓取 FOFA 资源")
    if not HEADERS["Cookie"]:
        live_print("⏭️ 未配置 Cookie，跳过。"); log_group_end(); return []
    try:
        r = requests.get(FOFA_URL, headers=HEADERS, timeout=15)
        if "账号登录" in r.text:
            live_print("❌ 错误: FOFA Cookie 已失效！")
            log_group_end(); return []

        raw_list = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
        if raw_list:
            counts = Counter(raw_list)
            live_print(f"✅ 获取 {len(raw_list)} 条记录")
            for ip in sorted(counts.keys()):
                live_print(f"  - {ip:<21} ({counts[ip]}次)")
            log_group_end(); return list(counts.keys())
    except requests.RequestException as e:
        live_print(f"❌ FOFA 请求异常: {e}")
        log_group_end(); return []

def update_rtp_template():
    """RTP 模板下载"""
    log_group_start("🔄 同步 RTP 模板")
    unique_rtp = {}
    for url in RTP_SOURCES:
        try:
            r = requests.get(url, timeout=15); r.encoding = 'utf-8'
            if r.status_code == 200:
                lines = r.text.splitlines()
                count = 0
                for i in range(len(lines)):
                    if lines[i].startswith("#EXTINF"):
                        try:
                            name = lines[i].split(',')[-1].strip()
                            for j in range(i+1, min(i+5, len(lines))):
                                if lines[j].strip().startswith("rtp://"):
                                    rtp_url = lines[j].strip()
                                    # 保留质量更高的名称（4K > 超清 > 高清 > 标清）
                                    if rtp_url not in unique_rtp:
                                        unique_rtp[rtp_url] = name
                                    else:
                                        existing = unique_rtp[rtp_url]
                                        if _channel_quality(name) > _channel_quality(existing):
                                            unique_rtp[rtp_url] = name
                                    count += 1
                                    break
                        except (ValueError, IndexError):
                            continue
                live_print(f"  📥 {url.split('/')[-1]} | 解析 {count} 条")
        except requests.RequestException:
            live_print(f"  ❌ 下载失败: {url}")

    if unique_rtp:
        with open(RTP_FILE, "w", encoding="utf-8") as f:
            for url, name in unique_rtp.items(): f.write(f"{name},{url}\n")
    log_group_end()

def _channel_quality(name):
    """频道名称质量评分（用于去重时保留更高质量名称）"""
    name_lower = name.lower()
    if "4k" in name_lower or "超高清" in name: return 4
    if "超清" in name or "uhd" in name_lower: return 3
    if "高清" in name or "hd" in name_lower: return 2
    return 1

def _atomic_write(filepath, content):
    """原子化写入：先写临时文件再 rename，防止中途崩溃产生残缺文件"""
    dir_path = os.path.dirname(filepath) or '.'
    tmp = tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                       dir=dir_path, delete=False, suffix='.tmp')
    try:
        tmp.write(content)
        tmp.close()
        os.replace(tmp.name, filepath)
    except Exception:
        try: os.unlink(tmp.name)
        except OSError: pass
        raise

# ===============================
# 4. 主程序入口
# ===============================
if __name__ == "__main__":
    start_time = time.time()
    stats = {"fofa": 0, "segments_total": 0, "segments_valid": 0,
             "scan_tasks": 0, "scan_found": 0, "geo_pass": 0, "geo_fail": 0,
             "blacklist_skip": 0}

    # 1. 准备 RTP
    update_rtp_template()

    # 2. 抓取与扫描
    fips = scrape_fofa()
    stats["fofa"] = len(fips)
    all_segs, all_ports = update_discovery_database(fips)
    stats["segments_total"] = len(all_segs)
    valid_segs = filter_segments(all_segs)
    stats["segments_valid"] = len(valid_segs)

    # 端口分级：以 DEFAULT_PORTS 为基准，不再手工维护 HIGH_PORTS
    high_set = set(DEFAULT_PORTS)
    high_ports = [p for p in all_ports if int(p) in high_set]
    ext_ports = [p for p in all_ports if int(p) not in high_set]

    # 先扫高优先端口（高频端口命中率高，优先扫可快速出结果）
    sips_high = run_native_scan(valid_segs, high_ports) if high_ports else []
    # 扩展端口始终扫（避免漏扫只开放冷门端口的独立服务器）
    sips_ext = run_native_scan(valid_segs, ext_ports) if ext_ports else []
    sips = list(set(sips_high + sips_ext))
    stats["scan_found"] = len(sips)

    unique_all = sorted(list(set(fips + sips)))

    # 3. 最终复核
    log_group_start("🌍 最终结果复核")
    geo_ips = []
    for idx, ip in enumerate(unique_all, 1):
        ok, desc = get_geo_info(ip.split(":")[0])
        if ok:
            live_print(f"  [{idx:02d}/{len(unique_all):02d}] ✅ 有效 | {ip:<21} | {desc}")
            geo_ips.append(ip)
            stats["geo_pass"] += 1
        else:
            live_print(f"  [{idx:02d}/{len(unique_all):02d}] ⏭️ 剔除 | {ip:<21} | {desc}")
            stats["geo_fail"] += 1
        time.sleep(2.0)
    log_group_end()

    # 4. 写入文件（标准 M3U 格式 + 原子化写入）
    if geo_ips:
        log_group_start("💾 数据归档 (output目录)")
        geo_ips.sort()

        # 写入 source-ip.txt（原子化）
        _atomic_write(SOURCE_IP_FILE, "\n".join(geo_ips))
        live_print(f"  📝 {SOURCE_IP_FILE}")

        # 写入标准 M3U
        rtps = []
        if os.path.exists(RTP_FILE):
            with open(RTP_FILE, encoding="utf-8") as f:
                rtps = [x.strip() for x in f if "," in x]

        m3u_lines = ["#EXTM3U"]
        for ip in geo_ips:
            for r in rtps:
                try:
                    name, rtp_url = r.split(",", 1)
                    suffix = rtp_url.split("://")[1]
                    m3u_lines.append(f"#EXTINF:-1,{name}")
                    m3u_lines.append(f"http://{ip}/rtp/{suffix}")
                except (ValueError, IndexError):
                    continue

        # 同时输出兼容的自定义格式
        compat_lines = []
        for ip in geo_ips:
            for r in rtps:
                try:
                    cname = r.split(",")[0]
                    csuffix = r.split("://")[1]
                    compat_lines.append(f"{cname},http://{ip}/rtp/{csuffix}")
                except (ValueError, IndexError):
                    continue

        _atomic_write(SOURCE_M3U_FILE, "\n".join(m3u_lines))
        live_print(f"  📝 {SOURCE_M3U_FILE} (标准M3U)")
        _atomic_write(SOURCE_NONCHECK_FILE, "\n".join(compat_lines))
        live_print(f"  📝 {SOURCE_NONCHECK_FILE} (兼容格式)")

        stats["m3u_count"] = len(geo_ips) * len(rtps)
        stats["rtp_count"] = len(rtps)
        live_print(f"✨ 总结: {len(geo_ips)} 个服务器 | {len(rtps)} 个频道 | {stats['m3u_count']} 条链接")
        log_group_end()
    else:
        live_print("\n❌ 本次运行未找到有效节点")

    # 5. 运行统计摘要
    elapsed = round(time.time() - start_time, 2)
    log_section("运行统计", "📊")
    live_print(f"├── FOFA 获取: {stats['fofa']} 个 IP")
    live_print(f"├── C段过滤: {stats['segments_total']}→{stats['segments_valid']} 个有效")
    live_print(f"├── 矩阵扫描: {stats['scan_found']} 个存活")
    live_print(f"├── 归属复核: ✅{stats['geo_pass']} / ⏭️{stats['geo_fail']}")
    live_print(f"└── M3U 生成: {stats.get('m3u_count', 0)} 条")
    live_print(f"\n⏱️ 总耗时: {elapsed}s")

    # 6. 写入 GitHub Actions Job Summary
    write_summary("### 📊 运行统计摘要\n")
    write_summary("| 指标 | 数值 |")
    write_summary("|------|------|")
    write_summary(f"| 🛰️ FOFA 获取 | {stats['fofa']} 个 IP |")
    write_summary(f"| 🛡️ C段过滤 | {stats['segments_total']}→{stats['segments_valid']} 个有效 |")
    write_summary(f"| 🚀 矩阵扫描 | {stats['scan_found']} 个存活 |")
    write_summary(f"| 🌍 归属复核 | ✅ {stats['geo_pass']} / ⏭️ {stats['geo_fail']} |")
    write_summary(f"| 📺 M3U 生成 | {stats.get('m3u_count', 0)} 条 |")
    write_summary(f"| 🖥️ 有效服务器 | {len(geo_ips)} 个 |")
    write_summary(f"| 📺 RTP 频道 | {stats.get('rtp_count', 0)} 个 |")
    write_summary(f"| ⏱️ 总耗时 | {elapsed}s |")
