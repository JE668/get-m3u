import os, re, time, threading, io, asyncio, concurrent.futures, json
from datetime import datetime
from collections import Counter
import httpx
import ip2region.util as ip2region_util
import ip2region.searcher as ip2region_searcher
from utils import live_print, write_summary, log_section, atomic_write

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

# --- 端口动态管理 ---
PORT_STATS_FILE = "data/port-stats.json"

# 连续多少次零扫描后自动休眠端口（默认端口×2，更宽容）
MISSES_BEFORE_DEACTIVATE = 3
DEFAULT_PORT_MISSES_EXTRA = 3  # 默认端口额外容忍次数

# ===============================
# 2. 核心功能函数（工具函数已迁移至 utils.py）
# ===============================

# ===============================
# 3. 核心功能函数
# ===============================

def get_geo_info(ip):
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
    log_section("🛡️ C段 归属地预校验（多IP抽样）", "🔹")
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
    
    return valid_segments

# ===============================
# 2a. 端口动态管理（基于历史命中率自动休眠/激活）
# ===============================

def _load_port_stats():
    """加载端口命中率统计"""
    if os.path.exists(PORT_STATS_FILE):
        try:
            with open(PORT_STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"version": 1, "run_counter": 0, "last_run": "", "ports": {}}


def _save_port_stats(stats):
    """保存端口命中率统计"""
    with open(PORT_STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)
    live_print(f"  📊 端口统计已保存 ({sum(1 for p in stats['ports'].values() if p['active'])} active / {sum(1 for p in stats['ports'].values() if not p['active'])} 休眠)")


def _sync_discovery_to_stats(discovery_ports, stats, meta_from_ips):
    """同步 discovery.txt 端口到 port-stats.json，新端口给试用期"""
    now = datetime.utcnow().isoformat() + "Z"
    changed = False
    for p in discovery_ports:
        p_str = str(p)
        if p_str not in stats["ports"]:
            is_default = p_str in [str(x) for x in DEFAULT_PORTS]
            stats["ports"][p_str] = {
                "runs": 0,
                "hits": 0,
                "missed_streak": 0,
                "active": True,
                "first_seen": now,
                "source": "default" if is_default else "fofa"
            }
            changed = True
            if not is_default:
                live_print(f"  🆕 新端口 :{p_str} 加入扫描（来自FOFA发现）")

    # 从 source-ip.txt 统计复活：端口不在 discovery 但出现在 source-ip 中 → 加回来
    if meta_from_ips:
        for port_str in meta_from_ips:
            if port_str not in discovery_ports:
                # 这个端口被手动添加过或从历史数据来 → 复活
                p_str = str(port_str)
                if p_str not in stats["ports"]:
                    stats["ports"][p_str] = {
                        "runs": 0, "hits": 0, "missed_streak": 0,
                        "active": True, "first_seen": now,
                        "source": "source-ip-revival"
                    }
                    live_print(f"  ♻️ 端口 :{p_str} 复活（source-ip.txt 中存活）")
                    discovery_ports.append(p_str)
                    changed = True

    if changed:
        _save_port_stats(stats)
    return stats


def _filter_ports_by_stats(discovery_ports, stats):
    """根据统计过滤端口：只返回 active 端口，按命中率排序（高→低）"""
    default_set = set(str(x) for x in DEFAULT_PORTS)

    scored = []
    for p in discovery_ports:
        p_str = str(p)
        entry = stats["ports"].get(p_str, {})

        # 判定是否 active
        if entry.get("active", True):
            # active 端口：通过
            pass
        elif p_str in default_set:
            # 默认端口即使休眠也强制激活
            if not entry.get("active", True):
                entry["active"] = True
                entry["missed_streak"] = 0
                live_print(f"  ♻️ 默认端口 :{p_str} 强制复活")
        else:
            # 非默认休眠端口 → 跳过
            total_misses = entry.get("missed_streak", entry.get("runs", 0))
            if total_misses >= MISSES_BEFORE_DEACTIVATE:
                continue

        # 计算优先级分（越高越先扫）
        runs = entry.get("runs", 0)
        hits = entry.get("hits", 0)
        score = 0
        if runs > 0:
            hit_rate = hits / runs
            score = int(hit_rate * 100) + hits * 5  # 命中率优先，总命中次之
        elif p_str in default_set:
            score = 50  # 默认端口零历史也给中等优先级
        else:
            score = 30  # 新端口低优先级

        scored.append((score, p_str))

    # 按得分降序排列
    scored.sort(key=lambda x: (-x[0], x[1]))
    sorted_ports = [p for _, p in scored]

    if len(sorted_ports) < len(discovery_ports):
        dropped = len(discovery_ports) - len(sorted_ports)
        live_print(f"  🧹 端口过滤: {len(discovery_ports)}→{len(sorted_ports)} (休眠 {dropped} 个)")
    else:
        live_print(f"  ✅ 端口: {len(sorted_ports)} 个 (全部 active)")

    return sorted_ports


def _update_port_stats_after_scan(stats, scanned_ports, source_ip_file):
    """扫描后更新端口命中统计"""
    now = datetime.utcnow().isoformat() + "Z"
    stats["run_counter"] += 1
    stats["last_run"] = now

    # 读取本次 source-ip.txt 中的端口命中
    hit_ports = set()
    if os.path.exists(source_ip_file):
        with open(source_ip_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if ":" in line:
                    port = line.rsplit(":", 1)[1]
                    hit_ports.add(port)

    # 更新每个被扫描端口的统计
    deactivated = 0
    default_set = set(str(x) for x in DEFAULT_PORTS)
    for p_str in scanned_ports:
        entry = stats["ports"].setdefault(p_str, {
            "runs": 0, "hits": 0, "missed_streak": 0,
            "active": True, "first_seen": now,
            "source": "default" if p_str in default_set else "discovery"
        })

        entry["runs"] = entry.get("runs", 0) + 1

        if p_str in hit_ports:
            entry["hits"] = entry.get("hits", 0) + 1
            entry["missed_streak"] = 0
        else:
            entry["missed_streak"] = entry.get("missed_streak", 0) + 1

        # 休眠判定
        deactivate_threshold = MISSES_BEFORE_DEACTIVATE
        if p_str in default_set:
            deactivate_threshold += DEFAULT_PORT_MISSES_EXTRA

        if entry.get("active", True) and entry["missed_streak"] >= deactivate_threshold:
            entry["active"] = False
            deactivated += 1
            live_print(f"  💤 端口 :{p_str} 休眠（连续 {entry['missed_streak']} 次零命中）")
        elif not entry.get("active", True) and entry["missed_streak"] == 0:
            # 刚命中 → 自动复活
            entry["active"] = True
            live_print(f"  ♻️ 端口 :{p_str} 复活（本次命中）")

    _save_port_stats(stats)
    return deactivated


def update_discovery_database(new_ips):
    """更新发现库"""
    log_section("📂 更新发现库 (data/discovery.txt)", "🔹")
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

    # 未传入 client 时创建临时 client，函数结束前关闭
    _own_client = False
    if client is None:
        client = httpx.AsyncClient()
        _own_client = True

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
    finally:
        if _own_client:
            await client.aclose()
    return False, None

async def run_native_scan(segments, ports, found_set=None):
    """统一扫描：持续任务流，结果随到随处理，不等慢任务 (async + httpx)"""
    log_section("🚀 启动扫描 (async + 持续任务流)", "🔹")
    if not segments:
        live_print("⚠️ 无有效网段"); return []

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
    
    return alive_ips

def scrape_fofa():
    """FOFA 抓取（含 Cookie 失效检测与降级提示，使用 httpx 同步客户端）"""
    log_section("📡 抓取 FOFA 资源", "🔹")
    if not HEADERS["Cookie"]:
        live_print("⏭️ 未配置 Cookie，跳过。"); return []
    try:
        r = httpx.get(FOFA_URL, headers=HEADERS, timeout=15)
        if "账号登录" in r.text or "login" in str(r.url).lower():
            live_print("❌ 错误: FOFA Cookie 已失效！请更新 secrets.FOFA_COOKIE")
            live_print("💡 提示: 在浏览器登录 fofa.info → F12 → Application → Cookies → 复制完整 Cookie 值")
            return []
        if r.status_code == 403:
            live_print("❌ 错误: FOFA 返回 403 禁止访问，可能被限流或封禁")
            return []

        raw_list = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
        if raw_list:
            counts = Counter(raw_list)
            live_print(f"✅ 获取 {len(raw_list)} 条记录")
            for ip in sorted(counts.keys()):
                live_print(f" - {ip:<21} ({counts[ip]}次)")
            return list(counts.keys())
        else:
            live_print(f"⚠️ FOFA 页面解析成功但未提取到 IP，可能页面结构变化")
            return []
    except httpx.TimeoutException:
        live_print("❌ FOFA 请求超时（15s），网络不稳定")
        return []
    except httpx.RequestError as e:
        live_print(f"❌ FOFA 请求异常: {e}")
        return []

_rtp_lock = threading.Lock()

def update_rtp_template():
    """RTP 模板下载（并发抓取两个源，线程安全）"""
    log_section("🔄 同步 RTP 模板", "🔹")
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

    # ---- 端口动态管理（基于历史命中率过滤 + 排序） ----
    port_stats = _load_port_stats()
    # 分析 source-ip.txt 端口命中，用于复活检查
    live_ports = set()
    if os.path.exists(SOURCE_IP_FILE):
        with open(SOURCE_IP_FILE, "r", encoding="utf-8") as f:
            for line in f:
                if ":" in line.strip():
                    live_ports.add(line.strip().rsplit(":", 1)[1])
    # 将 discovery 新端口同步到 stats，同时检查复活
    port_stats = _sync_discovery_to_stats(all_ports, port_stats, live_ports)
    # 按统计过滤端口（只保留 active + 按命中率排序）
    sorted_ports = _filter_ports_by_stats(all_ports, port_stats)
    live_print(f"📋 端口扫描计划: {sorted_ports} ({len(sorted_ports)} 个 active)")

    # 共享 found_set
    shared_found = set()
    sips = await run_native_scan(valid_segs, sorted_ports, shared_found) if sorted_ports else []
    stats["scan_found"] = len(sips)
    live_print(f"📊 扫描汇总: 发现 {len(sips)} 个存活 IP | 命中IP集: {len(shared_found)}")

    # ---- 扫描后更新端口统计（在 source-ip 写入前记录 scanned_ports） ----
    scanned_ports = [str(p) for p in sorted_ports]

    unique_all = sorted(list(set(fips + sips)))

    # 3. 最终复核
    log_section("🌍 最终结果复核", "🔹")
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
    

    # 4. 写入文件（标准 M3U 格式 + 原子化写入）
    if geo_ips:
        log_section("💾 数据归档 (output目录)", "🔹")
        geo_ips.sort()

        # 写入 source-ip.txt（原子化）
        atomic_write(SOURCE_IP_FILE, "\n".join(geo_ips))
        live_print(f"  📝 {SOURCE_IP_FILE}")

        # 更新端口命中统计（基于本次 source-ip.txt）
        deactivated = _update_port_stats_after_scan(port_stats, scanned_ports, SOURCE_IP_FILE)
        if deactivated:
            stats["port_deactivated"] = deactivated

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
        
    else:
        live_print("\n❌ 本次运行未找到有效节点")

    # 5. 阶段摘要（管道视图 — 每一阶段输出即下一阶段输入）
    elapsed = round(time.time() - start_time, 2)
    deactivated = stats.get('port_deactivated', 0)
    m3u_count = stats.get('m3u_count', 0)
    rtp_count = stats.get('rtp_count', 0)
    review_total = stats['geo_pass'] + stats['geo_fail']
    scan_total = stats['scan_found']
    fofa_total = stats['fofa']
    fofa_only = max(0, review_total - scan_total)

    # ── Console 输出 ──
    log_section("源发现 — 阶段摘要", "📊")
    live_print(f"  源获取→端口扫描→归属复核→成品输出")
    live_print(f"")
    live_print(f"  ┌─ 阶段1: 源获取")
    live_print(f"  │  ├ FOFA 刮取 ............ {fofa_total:>4} 个原始IP")
    live_print(f"  │  ├ C段预过滤 ........... {stats['segments_valid']:>4} 个有效")
    live_print(f"  │  └ (黑名单跳过) ........ {stats.get('blacklist_skip', 0):>4} 个")
    live_print(f"  │")
    live_print(f"  ├─ 阶段2: 端口扫描")
    live_print(f"  │  ├ 存活发现 ............ {scan_total:>4} 个新IP")
    live_print(f"  │  ├ FOFA 旧IP复用 ........ {fofa_only:>4} 个")
    live_print(f"  │  ├ 待复核总数 ........... {review_total:>4} 个IP")
    live_print(f"  │  └ 端口休眠 ............. {deactivated:>4} 个")
    live_print(f"  │")
    live_print(f"  ├─ 阶段3: 归属复核")
    live_print(f"  │  ├ 复核通过 ............ {stats['geo_pass']:>4} 个")
    live_print(f"  │  └ 复核剔除 ............ {stats['geo_fail']:>4} 个")
    live_print(f"  │")
    live_print(f"  ├─ 阶段4: 成品输出")
    live_print(f"  │  ├ 有效服务器 .......... {len(geo_ips):>4} 个 (→ output/source-ip.txt)")
    live_print(f"  │  ├ RTP 频道 ............ {rtp_count:>4} 个")
    live_print(f"  │  ├ M3U 链接 ............ {m3u_count:>4} 条 (→ output/source-m3u.txt)")
    live_print(f"  │  └ 耗时 ............... {elapsed:>7.2f}s")
    live_print(f"  └──")

    # ── GitHub Actions Job Summary ──
    write_summary("### 📊 阶段摘要 — 源发现\n")
    write_summary(f"**源获取 → 端口扫描 → 归属复核 → 成品输出** | ⏱️ {elapsed}s\n\n")
    write_summary("| 阶段 | 指标 | 数值 |")
    write_summary("|------|------|------|")
    write_summary(f"| ① 源获取 | FOFA 刮取 | {fofa_total} 个原始IP |")
    write_summary(f"| ① 源获取 | C段预过滤 | {stats['segments_valid']} 个有效 ({stats['segments_total']}→{stats['segments_valid']}) |")
    write_summary(f"| ① 源获取 | 黑名单跳过 | {stats.get('blacklist_skip', 0)} 个 |")
    write_summary(f"| ② 端口扫描 | 新存活发现 | {scan_total} 个IP |")
    write_summary(f"| ② 端口扫描 | 端口休眠 | {deactivated} 个 |")
    write_summary(f"| ③ 归属复核 | 复核通过 | {stats['geo_pass']} 个 |")
    write_summary(f"| ③ 归属复核 | 复核剔除 | {stats['geo_fail']} 个 |")
    write_summary(f"| ④ 成品输出 | 有效服务器 | {len(geo_ips)} 个 |")
    write_summary(f"| ④ 成品输出 | RTP 频道 | {rtp_count} 个 |")
    write_summary(f"| ④ 成品输出 | M3U 总链接 | {m3u_count} 条 |")

    write_summary(f"\n> 💾 输出文件: `output/source-ip.txt` `output/source-m3u.txt` `output/source-m3u-noncheck.txt`")

if __name__ == "__main__":
    asyncio.run(main())
