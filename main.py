import os, re, time, random, threading, io, asyncio, concurrent.futures
from collections import Counter
import httpx
import ip2region.util as ip2region_util
import ip2region.searcher as ip2region_searcher
from utils import live_print, write_summary, log_group_start, log_group_end, log_section, atomic_write

# --- 初始化离线 IP 归属地查询（ip2region xdb，零网络延迟） ---
_ip2region_searcher = None
def _get_ip2region():
    global _ip2region_searcher
    if _ip2region_searcher is None:
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ip2region.xdb")
        handle = io.open(db_path, "rb")
        header = ip2region_util.load_header(handle)
        version = ip2region_util.version_from_header(header)
        v_index = ip2region_util.load_vector_index(handle)
        _ip2region_searcher = ip2region_searcher.new_with_vector_index(version, db_path, v_index)
        handle.close()
    return _ip2region_searcher

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
# 2. 核心功能函数（工具函数已迁移至 utils.py）
# ===============================

# ===============================
# 3. 核心功能函数
# ===============================

def get_geo_info(ip, session=None):
    """查询 IP 归属地（离线 ip2region，零延迟无限速）"""
    try:
        region = _get_ip2region().search(ip)
        if not region:
            return False, "无归属数据"
        # ip2region v3 返回格式: "国家|省份|城市|ISP|iso-alpha2-Code"
        parts = region.split("|")
        province = parts[1] if len(parts) > 1 else "未知"
        city = parts[2] if len(parts) > 2 else "未知"
        isp = parts[3].lower() if len(parts) > 3 and parts[3] else "未知"
        is_gd = "广东" in province
        is_tel = any(k in isp for k in ["电信", "telecom", "chinanet"])
        isp_display = parts[3] if len(parts) > 3 and parts[3] else "未知"
        desc = f"{province}-{city} | {isp_display}"
        return (is_gd and is_tel), desc
    except Exception as e:
        return False, f"查询异常: {e}"

SAMPLE_IPS_PER_SEG = [1, 100, 200]  # 每个C段抽测3个IP
SAMPLE_GEO_THRESHOLD = 2              # 至少2个IP不合格才跳过（容忍1个误报）

def filter_segments(segments):
    """C段 预校验与清洗（多IP抽样，防止 .1 网关误判）。
    
    - 每段抽 SAMPLE_IPS_PER_SEG 个IP做归属地测试
    - 至少 SAMPLE_GEO_THRESHOLD 个IP不合格才跳过（容忍1个误报）
    - 不再永久写入黑名单文件（避免单个网关IP误判导致整段永久消失）
    """
    log_group_start("🛡️ C段 归属地预校验（多IP抽样）")
    blacklist = set()
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            blacklist = set([line.strip() for line in f if line.strip()])

    valid_segments, skipped_segments = [], []
    total = len(segments)
    blacklist_skip = 0
    live_print(f"📋 待检测: {total} 个 | 黑名单库: {len(blacklist)} 个 | 抽样: {len(SAMPLE_IPS_PER_SEG)} 个IP/段")

    for idx, seg in enumerate(segments, 1):
        if seg in blacklist:
            blacklist_skip += 1
            continue
        # 多IP抽样（.1/.100/.200），防止网关IP误判
        sample_details = []
        for offset in SAMPLE_IPS_PER_SEG:
            ip = f"{seg}.{offset}"
            is_valid, desc = get_geo_info(ip)
            sample_details.append((ip, is_valid, desc))

        # 统计不合格IP数，并构造详细日志
        fail_count = sum(1 for _, ok, _ in sample_details if not ok)
        ok_count = len(sample_details) - fail_count
        detail_lines = [f"{ip}: {('✅' if ok else '❌')} {desc}" for ip, ok, desc in sample_details]
        
        if fail_count >= SAMPLE_GEO_THRESHOLD:
            # 至少2个IP不合格才跳过（容忍1个误报）
            live_print(f"  [{idx}/{total}] ❌ 跳过: {seg}")
            for line in detail_lines:
                live_print(f"      {line}")
            skipped_segments.append(seg)
        else:
            # 至少1个IP合格即通过
            valid_segments.append(seg)
            live_print(f"  [{idx}/{total}] ✅ 通过: {seg} ({ok_count}/{len(SAMPLE_IPS_PER_SEG)} 合格)")
            for line in detail_lines:
                live_print(f"      {line}")

    live_print(f"📊 最终有效 C段: {len(valid_segments)} 个 (历史黑名单跳过: {blacklist_skip} 个, 本次临时跳过: {len(skipped_segments)} 个)")
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

# 扫描阶段超时配置（两阶段：连接快筛 + 读数据给足时间）
# connect=0.5s: 够快，0.5s内没完成TCP握手 → 真实不可达，直接放弃
# read=3.0s: 够慢，udpxy处理+网络RTT最多吃2-3s，给足缓冲不误杀
SCAN_CONNECT_TIMEOUT = 0.5
SCAN_READ_TIMEOUT = 3.0

# 增量验证超时（更短：已知的存活IP应该秒回）
INCR_CONNECT_TIMEOUT = 0.3
INCR_READ_TIMEOUT = 0.5


async def check_udpxy(ip_port, found_set=None, timeout=None, client=None):
    """HTTP 指纹探测（两阶段超时：connect快筛 + read给足时间）。

    timeout 为 None 时使用 SCAN_* 默认配置（扫描阶段）。
    传入 (connect_timeout, read_timeout) 元组时使用自定义值（增量验证等）。
    """
    ip = ip_port.split(":")[0]
    if found_set is not None and ip in found_set: return False, None
    if client is None: client = httpx.AsyncClient()

    # 解析超时配置
    if timeout is None:
        tm = httpx.Timeout(SCAN_READ_TIMEOUT, connect=SCAN_CONNECT_TIMEOUT, read=SCAN_READ_TIMEOUT)
    elif isinstance(timeout, tuple):
        tm = httpx.Timeout(timeout[1], connect=timeout[0], read=timeout[1])
    else:
        tm = httpx.Timeout(timeout)  # 兼容旧调用（数字→全局等分）

    try:
        r = await client.get(f"http://{ip_port}/status", timeout=tm, headers={"User-Agent":"Wget/1.14"})
        if r.status_code == 200 and any(kw in r.text.lower() for kw in ["udpxy", "stat", "client"]):
            if found_set is not None:
                found_set.add(ip)
            return True, ip_port
    except Exception:
        pass
    return False, None

async def run_native_scan(segments, ports, found_set=None):
    """统一扫描：持续任务流，结果随到随处理，不等慢任务 (async + httpx)"""
    log_group_start("🚀 启动扫描 (async + 持续任务流)")
    if not segments:
        live_print("⚠️ 无有效网段"); log_group_end(); return []

    scan_workers = int(os.environ.get("SCAN_WORKERS", "500"))

    # 复用外部 found_set（跨扫描共享，IP 命中后跳过其他端口）
    if found_set is None:
        found_set = set()
    sem = asyncio.Semaphore(scan_workers)

    # 端口优先级：高频端口排前面，更快命中
    port_list = [int(p) for p in ports]

    async def check_one(ip_port, timeout, client):
        async with sem:
            return await check_udpxy(ip_port, found_set, timeout, client)

    alive_ips = []
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_keepalive_connections=200, max_connections=1000),
        timeout=httpx.Timeout(connect=SCAN_CONNECT_TIMEOUT, read=SCAN_READ_TIMEOUT, write=1.5, pool=0.5),
    ) as client:
        # 增量验证：先快速验证上次的存活 IP（随完随处理）
        if os.path.exists(SOURCE_IP_FILE):
            with open(SOURCE_IP_FILE, "r", encoding="utf-8") as f:
                known_alive = [line.strip() for line in f if line.strip()]
            if known_alive:
                live_print(f"🔄 增量验证: {len(known_alive)} 个已知 IP (connect≤0.3s, read≤0.5s)...")
                still_alive = []
                tasks = [asyncio.create_task(check_one(ip, (INCR_CONNECT_TIMEOUT, INCR_READ_TIMEOUT), client)) for ip in known_alive]
                for coro in asyncio.as_completed(tasks):
                    ok, matched = await coro
                    if ok and matched:
                        still_alive.append(matched)
                        alive_ips.append(matched)
                live_print(f"✅ 已知存活验证: {len(still_alive)}/{len(known_alive)} 个")
                removed = len(known_alive) - len(still_alive)
                if removed > 0:
                    with open(SOURCE_IP_FILE, "w", encoding="utf-8") as f:
                        for ip in still_alive:
                            f.write(ip + "\n")
                    live_print(f"🧹 清理 {removed} 个失效 IP，SOURCE_IP_FILE 剩余 {len(still_alive)} 个")

        # 全量扫描：持续任务流，滚动窗口
        def _task_generator():
            for seg in segments:
                for i in range(1, 255):
                    ip = f"{seg}.{i}"
                    if ip in found_set:
                        continue
                    for port in port_list:
                        yield f"{ip}:{port}"

        total_tasks = len(segments) * 254 * len(port_list)
        live_print(f"🎯 全量扫描: 持续任务流 (并发: {scan_workers}, 预估任务: {total_tasks})")
        task_gen = _task_generator()
        completed = 0
        start_time = time.time()

        # 初始化：启动 scan_workers 个任务
        pending = set()
        for _ in range(scan_workers):
            try:
                ip_port = next(task_gen)
                pending.add(asyncio.create_task(check_one(ip_port, None, client)))
            except StopIteration:
                break

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                completed += 1
                ok, matched_ip = task.result()
                if ok and matched_ip:
                    alive_ips.append(matched_ip)
                    # 打印新命中的 IP 详情
                    ip = matched_ip.split(":")[0]
                    _, geo_desc = get_geo_info(ip)
                    live_print(f"    🎯 命中: {matched_ip} | {geo_desc}")

            # 补充新任务，维持并发数
            while len(pending) < scan_workers:
                try:
                    ip_port = next(task_gen)
                    pending.add(asyncio.create_task(check_one(ip_port, None, client)))
                except StopIteration:
                    break

            if completed % 5000 == 0:
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                found = len(set(alive_ips))
                msg = f" 📊 进度: {completed}/{total_tasks} | 发现: {found} | 命中IP: {len(found_set)}"
                if rate > 0:
                    remaining = (total_tasks - completed) / rate
                    msg += f" | 速度: {rate:.0f}/s | 预估剩余: {remaining:.0f}s"
                live_print(msg)

        live_print(f"✅ 扫描结束 | 总发现 {len(set(alive_ips))} 个")
        live_print(f"   📊 统计: 命中IP={len(found_set)} | 存活IP={len(set(alive_ips))}")

    alive_ips = list(set(alive_ips))
    log_group_end()
    return alive_ips

def scrape_fofa():
    """FOFA 抓取（含 Cookie 失效检测与降级提示，使用 httpx 同步客户端）"""
    log_group_start("📡 抓取 FOFA 资源")
    if not HEADERS["Cookie"]:
        live_print("⏭️ 未配置 Cookie，跳过。"); log_group_end(); return []
    try:
        r = httpx.get(FOFA_URL, headers=HEADERS, timeout=15)
        if "账号登录" in r.text or "login" in str(r.url).lower():
            live_print("❌ 错误: FOFA Cookie 已失效！请更新 secrets.FOFA_COOKIE")
            live_print("💡 提示: 在浏览器登录 fofa.info → F12 → Application → Cookies → 复制完整 Cookie 值")
            log_group_end(); return []
        if r.status_code == 403:
            live_print("❌ 错误: FOFA 返回 403 禁止访问，可能被限流或封禁")
            log_group_end(); return []

        raw_list = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
        if raw_list:
            counts = Counter(raw_list)
            live_print(f"✅ 获取 {len(raw_list)} 条记录")
            for ip in sorted(counts.keys()):
                live_print(f" - {ip:<21} ({counts[ip]}次)")
            log_group_end(); return list(counts.keys())
        else:
            live_print(f"⚠️ FOFA 页面解析成功但未提取到 IP，可能页面结构变化")
            log_group_end(); return []
    except httpx.TimeoutException:
        live_print("❌ FOFA 请求超时（15s），网络不稳定")
        log_group_end(); return []
    except httpx.RequestError as e:
        live_print(f"❌ FOFA 请求异常: {e}")
        log_group_end(); return []

_rtp_lock = threading.Lock()

def update_rtp_template():
    """RTP 模板下载（并发抓取两个源，线程安全）"""
    log_group_start("🔄 同步 RTP 模板")
    unique_rtp = {}

    def _download_single(url):
        """下载并解析单个 RTP 源（使用 httpx 同步客户端）"""
        local_rtp = {}
        try:
            r = httpx.get(url, timeout=15); r.encoding = 'utf-8'
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
                                    if rtp_url not in local_rtp or _channel_quality(name) > _channel_quality(local_rtp[rtp_url]):
                                        local_rtp[rtp_url] = name
                                    count += 1
                                    break
                        except (ValueError, IndexError):
                            continue
                live_print(f"  📥 {url.split('/')[-1]} | 解析 {count} 条")
        except httpx.RequestError:
            live_print(f"  ❌ 下载失败: {url}")
        return local_rtp

    # 并发下载两个 RTP 源，unique_rtp 写操作加锁保证线程安全
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futures = {ex.submit(_download_single, url): url for url in RTP_SOURCES}
        for future in concurrent.futures.as_completed(futures):
            local = future.result()
            with _rtp_lock:
                for rtp_url, name in local.items():
                    if rtp_url not in unique_rtp or _channel_quality(name) > _channel_quality(unique_rtp[rtp_url]):
                        unique_rtp[rtp_url] = name

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

# ===============================
# 4. 主程序入口
# ===============================
async def main():
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

    # 合并所有端口，一次性扫描（优化：取消端口分级）
    all_ports_set = set(int(p) for p in all_ports)
    sorted_ports = sorted(list(all_ports_set))
    live_print(f"📋 端口列表: {sorted_ports} ({len(sorted_ports)} 个)")

    # 共享 found_set
    shared_found = set()
    sips = await run_native_scan(valid_segs, sorted_ports, shared_found) if sorted_ports else []
    stats["scan_found"] = len(sips)
    live_print(f"📊 扫描汇总: 发现 {len(sips)} 个存活 IP | 命中IP集: {len(shared_found)}")

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
            log_group_end()

    # 4. 写入文件（标准 M3U 格式 + 原子化写入）
    if geo_ips:
        log_group_start("💾 数据归档 (output目录)")
        geo_ips.sort()

        # 写入 source-ip.txt（原子化）
        atomic_write(SOURCE_IP_FILE, "\n".join(geo_ips))
        live_print(f"  📝 {SOURCE_IP_FILE}")

        # 写入标准 M3U
        rtps = []
        if os.path.exists(RTP_FILE):
            with open(RTP_FILE, encoding="utf-8") as f:
                rtps = [x.strip() for x in f if "," in x]

        # 预计算 RTP 条目（避免每次循环内部重复 split）
        rtp_entries = []
        for r in rtps:
            try:
                name, rtp_url = r.split(",", 1)
                suffix = rtp_url.split("://")[1]  # "239.77.1.234:5146"
                rtp_entries.append((name, suffix))
            except (ValueError, IndexError):
                continue

        m3u_lines = ["#EXTM3U"]
        compat_lines = []
        for ip in geo_ips:
            for name, suffix in rtp_entries:
                full_url = f"http://{ip}/rtp/{suffix}"
                m3u_lines.append(f"#EXTINF:-1,{name}")
                m3u_lines.append(full_url)
                compat_lines.append(f"{name},{full_url}")

        atomic_write(SOURCE_M3U_FILE, "\n".join(m3u_lines))
        live_print(f"  📝 {SOURCE_M3U_FILE} (标准M3U)")
        atomic_write(SOURCE_NONCHECK_FILE, "\n".join(compat_lines))
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

if __name__ == "__main__":
    asyncio.run(main())
