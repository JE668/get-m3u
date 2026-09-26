import os, re, time, threading, io, asyncio, concurrent.futures, json
import atexit
import hashlib
import ipaddress
import requests
from datetime import datetime, timezone
from collections import Counter
import httpx
import ip2region.util as ip2region_util
import ip2region.searcher as ip2region_searcher
from utils import live_print, write_summary, log_section, atomic_write, parse_rtp_entries, build_m3u, build_compat
from utils.crawler import crawl_segments, segments_from_ip_list
from probe import load_source_scores, update_source_score, filter_by_score

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

def _release_ip2region():
    """释放 ip2region searcher 资源（程序退出时自动调用）"""
    global _ip2region_searcher
    if _ip2region_searcher:
        try:
            _ip2region_searcher.close()
        except Exception:
            pass
        _ip2region_searcher = None

atexit.register(_release_ip2region)

# ===============================
# 1. 配置区 (目录结构优化版)
# ===============================
FOFA_URL = "https://fofa.info/result?qbase64=IlVEUFhZIiAmJiBjb3VudHJ5PSJDTiIgJiYgcmVnaW9uPSJHdWFuZ2Rvbmci&filter_type=last_month"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Cookie": os.environ.get("FOFA_COOKIE", "")
}

RTP_SOURCES = [
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_all.m3u",
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
IP_SIGNATURE_FILE = "output/.ip_signature"

DEFAULT_PORTS = [4022, 8000, 8686, 55555, 54321, 1024, 10001, 8443, 8888]

# --- 端口动态管理 ---
PORT_STATS_FILE = "data/port-stats.json"

# m3u-checker-max 反馈数据 URL（通过 GitHub Raw 访问）
FEEDBACK_URL = os.environ.get(
    "FEEDBACK_URL",
    "https://raw.githubusercontent.com/JE668/m3u-checker-max/main/output/feedback.json"
)

# 连续多少次零扫描后自动休眠端口（默认端口×2，更宽容）
MISSES_BEFORE_DEACTIVATE = 3
DEFAULT_PORT_MISSES_EXTRA = 3  # 默认端口额外容忍次数

# C段验证缓存（避免每次重新验证所有 segment）
# 命名注意：这是 geo 归属地验证缓存，与 utils/crawler.py 的 segments_cache.json
# （爬虫结果缓存）是两个不同文件，勿混淆
SEGMENT_CACHE_FILE = os.path.join(os.path.dirname(DISCOVERY_FILE), "segment_geo_cache.json")
SEGMENT_CACHE_TTL = 7 * 24 * 3600  # 7 天缓存

# ===============================
# 核心功能函数
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

def _load_segment_cache():
    """加载已验证的 C 段缓存"""
    if not os.path.exists(SEGMENT_CACHE_FILE):
        return {}
    try:
        with open(SEGMENT_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 过滤过期条目
        now = time.time()
        return {k: v for k, v in data.items() if now - v.get("ts", 0) < SEGMENT_CACHE_TTL}
    except (json.JSONDecodeError, OSError):
        return {}

def _save_segment_cache(cache):
    """保存已验证的 C 段缓存"""
    try:
        os.makedirs(os.path.dirname(SEGMENT_CACHE_FILE), exist_ok=True)
        with open(SEGMENT_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        live_print(f"⚠️ 段缓存写入失败: {e}")

def filter_segments(segments):
    """C段 预校验与清洗（多IP抽样，防止 .1 网关误判）。
    
    - 每段抽 SAMPLE_IPS_PER_SEG 个IP做归属地测试
    - 至少 SAMPLE_GEO_THRESHOLD 个IP不合格才跳过（容忍1个误报）
    - 不再永久写入黑名单文件（避免单个网关IP误判导致整段永久消失）
    - 已验证过的 segment 使用 7 天缓存，避免重复验证
    """
    log_section("🛡️ C段 归属地预校验（多IP抽样）", "🔹")
    blacklist = set()
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
            blacklist = set([line.strip() for line in f if line.strip()])

    # 加载已验证缓存
    segment_cache = _load_segment_cache()
    cached_valid = [seg for seg in segments if seg in segment_cache and segment_cache[seg].get("valid")]
    cached_invalid = [seg for seg in segments if seg in segment_cache and not segment_cache[seg].get("valid")]
    
    # 只验证未缓存的 segment
    to_verify = [seg for seg in segments if seg not in segment_cache]
    
    valid_segments = list(cached_valid)
    skipped_segments = list(cached_invalid)
    blacklist_skip = 0
    
    total = len(segments)
    live_print(f"📋 待检测: {total} 个 | 缓存命中: {len(cached_valid)} 个 | 黑名单库: {len(blacklist)} 个")
    live_print(f"   本次需验证: {len(to_verify)} 个 | 已验证无效: {len(cached_invalid)} 个")
    
    now_ts = time.time()
    
    for idx, seg in enumerate(to_verify, 1):
        if seg in blacklist:
            blacklist_skip += 1
            segment_cache[seg] = {"valid": False, "reason": "blacklist", "ts": now_ts}
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
            live_print(f"  [{idx}/{len(to_verify)}] ❌ 跳过: {seg}")
            for line in detail_lines:
                live_print(f"      {line}")
            skipped_segments.append(seg)
            segment_cache[seg] = {"valid": False, "reason": "geo_fail", "ts": now_ts}
        else:
            # 至少1个IP合格即通过
            valid_segments.append(seg)
            live_print(f"  [{idx}/{len(to_verify)}] ✅ 通过: {seg} ({ok_count}/{len(SAMPLE_IPS_PER_SEG)} 合格)")
            for line in detail_lines:
                live_print(f"      {line}")
            segment_cache[seg] = {"valid": True, "reason": "geo_pass", "ts": now_ts}

    # 保存缓存
    _save_segment_cache(segment_cache)

    live_print(f"📊 最终有效 C段: {len(valid_segments)} 个 (缓存命中: {len(cached_valid)} 个, 本次新验证: {len(valid_segments) - len(cached_valid)} 个, 历史黑名单跳过: {blacklist_skip} 个, 本次临时跳过: {len(skipped_segments) - len(cached_invalid)} 个)")

    return valid_segments, blacklist_skip


# ===============================
# 2b. 扫描配额轮换（避免每次全量扫 100 万+ 任务）
# ===============================
#
# 策略：
# - 含已知存活 IP 的段（生产中段）→ 每轮必扫（增量验证 + 同段新端口探测）
# - 其余段按「未扫描优先 → 最久未扫优先 → 历史命中高优先」排序，取配额 N 个
# - 配额默认 80 段/轮：283 段约 4 轮（12h）全覆盖；0 = 不限制（退回旧行为）

SCAN_STATE_FILE = "data/segment_scan_state.json"
SCAN_SEGMENT_QUOTA = int(os.environ.get("SCAN_SEGMENT_QUOTA", "80"))


def _load_scan_state():
    """加载段扫描状态 {seg: {"last_scanned": ts, "hits": n, "runs": n}}"""
    if not os.path.exists(SCAN_STATE_FILE):
        return {}
    try:
        with open(SCAN_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_scan_state(state):
    try:
        with open(SCAN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except OSError as e:
        live_print(f"⚠️ 扫描状态写入失败: {e}")


def select_scan_segments(valid_segs, known_alive_ips=None, dead_ips=None):
    """从有效段中按轮换策略选出本轮要扫描的段。

    - dead_ips（下游 checker 反馈的全挂服务器）所在段排到轮换队列尾部：
      不拉黑（可能是临时故障），只降低本轮被选中的优先级，形成
      检测 → 反馈 → 降权 → 复活的闭环。
    返回 (selected_segs, stats_line)
    """
    dead_segs = set()
    for ip_port in (dead_ips or []):
        dead_segs.add(ip_port.split(":")[0].rsplit(".", 1)[0])

    if SCAN_SEGMENT_QUOTA <= 0 or len(valid_segs) <= SCAN_SEGMENT_QUOTA:
        if dead_segs:
            selected = [s for s in valid_segs if s not in dead_segs] + \
                       [s for s in valid_segs if s in dead_segs]
            return selected, f"全量模式: {len(valid_segs)} 段 ({len(dead_segs)} 个全挂段已排尾部)"
        return list(valid_segs), f"全量模式: {len(valid_segs)} 段 (配额={SCAN_SEGMENT_QUOTA or '∞'})"

    state = _load_scan_state()
    now_ts = time.time()

    # 生产中段（含已知存活 IP）每轮必扫
    hot_segs = set()
    for ip_port in (known_alive_ips or []):
        seg = ip_port.split(":")[0].rsplit(".", 1)[0]
        if seg in valid_segs and seg not in dead_segs:
            hot_segs.add(seg)

    # 其余段按优先级排序：未扫过的最优先，然后按 last_scanned 升序，同档 hits 降序；
    # dead_ips 所在段排最后
    rest = [s for s in valid_segs if s not in hot_segs]
    rest.sort(key=lambda s: (s in dead_segs,
                             state.get(s, {}).get("last_scanned", 0.0),
                             -state.get(s, {}).get("hits", 0)))

    quota_left = max(0, SCAN_SEGMENT_QUOTA - len(hot_segs))
    selected = sorted(hot_segs) + rest[:quota_left]

    live_print(f"🎯 扫描配额: {len(selected)}/{len(valid_segs)} 段 "
               f"(生产中段 {len(hot_segs)} 必扫 + 轮换 {min(quota_left, len(rest))} 段, "
               f"全挂降权 {len(dead_segs)} 段, "
               f"剩余 {len(rest) - min(quota_left, len(rest))} 段下轮)")
    # 标记本轮扫描时间（实际命中数在扫描后由 update_scan_state 补记）
    for seg in selected:
        entry = state.setdefault(seg, {"last_scanned": 0.0, "hits": 0, "runs": 0})
        entry["last_scanned"] = now_ts
        entry["runs"] = entry.get("runs", 0) + 1
    _save_scan_state(state)
    return selected, None


def update_scan_state(scanned_segs, found_ips):
    """扫描结束后更新各段命中数"""
    if not scanned_segs:
        return
    state = _load_scan_state()
    for ip_port in found_ips:
        seg = ip_port.split(":")[0].rsplit(".", 1)[0]
        if seg in state:
            state[seg]["hits"] = state[seg].get("hits", 0) + 1
    _save_scan_state(state)

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
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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


def compute_ip_signature(ips: list) -> str:
    """计算IP列表签名"""
    sig = hashlib.sha256()
    for ip in sorted(ips):
        sig.update(ip.encode())
    return sig.hexdigest()

def load_previous_ip_signature() -> str:
    if os.path.exists(IP_SIGNATURE_FILE):
        try:
            with open(IP_SIGNATURE_FILE) as f:
                return f.read().strip()
        except OSError:
            pass
    return None

def save_ip_signature(sig: str):
    try:
        os.makedirs(os.path.dirname(IP_SIGNATURE_FILE), exist_ok=True)
        with open(IP_SIGNATURE_FILE, 'w') as f:
            f.write(sig)
    except OSError:
        pass

def load_external_feedback() -> dict:
    """加载 m3u-checker-max 的反馈数据，用于优化源优先级"""
    try:
        r = requests.get(FEEDBACK_URL, timeout=10)
        if r.status_code == 200:
            feedback = json.loads(r.text)
            version = feedback.pop("_version", 1)
            live_print(f"📥 已加载下游反馈数据: {feedback.get('total_channels', 0)} 个有效频道 (v{version})")
            return feedback
    except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
        live_print(f"⚠️ 下游反馈不可用: {e}")
    return {}


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
SCAN_CONNECT_TIMEOUT = float(os.environ.get("SCAN_CONNECT_TIMEOUT", "0.5"))
SCAN_READ_TIMEOUT = float(os.environ.get("SCAN_READ_TIMEOUT", "3.0"))

# 增量验证超时（更短：已知的存活IP应该秒回）
INCR_CONNECT_TIMEOUT = float(os.environ.get("INCR_CONNECT_TIMEOUT", "0.3"))
INCR_READ_TIMEOUT = float(os.environ.get("INCR_READ_TIMEOUT", "0.5"))


async def check_udpxy(ip_port, found_set=None, timeout=None, client=None):
    """HTTP 指纹探测（两阶段超时：connect快筛 + read给足时间）。

    timeout 为 None 时使用 SCAN_* 默认配置（扫描阶段）。
    传入 (connect_timeout, read_timeout) 元组时使用自定义值（增量验证等）。

    .. deprecated::
        不传 client 参数时会创建临时 httpx.AsyncClient()，
        应始终传入外部 client 以避免重复创建开销。
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
        if r.status_code == 200 and "udpxy" in r.text.lower():
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
    MAX_PENDING = scan_workers * 2  # 最大并发任务数上限（滚动窗口 × 2）

    # 复用外部 found_set（跨扫描共享，IP 命中后跳过其他端口）
    if found_set is None:
        found_set = set()
    sem = asyncio.Semaphore(scan_workers)

    # 端口优先级：高频端口排前面，更快命中
    port_list = [int(p) for p in ports]

    # 加载质量评分，跳过评分过低的源
    scores = load_source_scores()
    min_score = float(os.environ.get("MIN_SOURCE_SCORE", "10.0"))
    high_score_ports = filter_by_score(scores, min_score) if scores else set()

    async def check_one(ip_port, timeout, client):
        async with sem:
            return await check_udpxy(ip_port, found_set, timeout, client)

    alive_ips = []
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_keepalive_connections=200, max_connections=1000),
        timeout=httpx.Timeout(connect=SCAN_CONNECT_TIMEOUT, read=SCAN_READ_TIMEOUT, write=1.5, pool=0.5),
    ) as client:
        # 增量验证：不再串行阻塞，而是作为高优任务混入全量任务流前端
        # （旧实现单独 await as_completed，500 并发只用了 33 个槽位）
        incr_tasks = []
        known_alive = []
        if os.path.exists(SOURCE_IP_FILE):
            with open(SOURCE_IP_FILE, "r", encoding="utf-8") as f:
                known_alive = [line.strip() for line in f if line.strip()]
        if known_alive:
            live_print(f"🔄 增量验证: {len(known_alive)} 个已知 IP 混入任务流 (connect≤0.3s, read≤0.5s)")
            incr_tasks = [asyncio.create_task(
                check_one(ip, (INCR_CONNECT_TIMEOUT, INCR_READ_TIMEOUT), client)
            ) for ip in known_alive]

        # 全量扫描：持续任务流，滚动窗口
        def _task_generator():
            for seg in segments:
                for i in range(1, 255):
                    ip = f"{seg}.{i}"
                    if ip in found_set:
                        continue
                    for port in port_list:
                        ip_port = f"{ip}:{port}"
                        if scores and ip_port not in high_score_ports and ip_port in scores:
                            continue  # 跳过低评分端口
                        yield ip_port

        total_tasks = len(segments) * 254 * len(port_list)
        live_print(f"🎯 全量扫描: 持续任务流 (并发: {scan_workers}, 预估任务: {total_tasks})")
        task_gen = _task_generator()
        completed = 0
        incr_done = 0
        start_time = time.time()

        # 初始化：增量验证任务优先进入 pending
        pending = set(incr_tasks)
        while len(pending) < min(scan_workers, MAX_PENDING):
            try:
                ip_port = next(task_gen)
                pending.add(asyncio.create_task(check_one(ip_port, None, client)))
            except StopIteration:
                break

        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                ok, matched_ip = task.result()
                if task in incr_tasks:
                    # 增量任务单独计数，不进扫描进度
                    incr_done += 1
                    if ok and matched_ip:
                        alive_ips.append(matched_ip)
                    if incr_done == len(incr_tasks):
                        live_print(f"✅ 已知存活验证完成: {incr_done}/{len(known_alive)} 个已处理")
                    continue
                completed += 1
                if ok and matched_ip:
                    alive_ips.append(matched_ip)
                    live_print(f"    🎯 命中: {matched_ip}")

            # 补充新任务，维持并发数
            while len(pending) < min(scan_workers, MAX_PENDING):
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

        scan_elapsed = round(time.time() - start_time, 2)
        live_print(f"✅ 扫描结束 | 总发现 {len(set(alive_ips))} 个")
        live_print(f"   📊 统计: 命中IP={len(found_set)} | 存活IP={len(set(alive_ips))} | 扫描耗时 {scan_elapsed:.2f}s")

    alive_ips = list(set(alive_ips))
    
    return alive_ips, scan_elapsed


def _is_invalid_ip(ip):
    """检查 IP 是否为内网/回环/保留地址"""
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return True

def scrape_fofa():
    """FOFA 抓取（含 Cookie 失效检测与降级提示，使用 httpx 同步客户端）"""
    log_section("📡 抓取 FOFA 资源", "🔹")
    if not HEADERS["Cookie"]:
        live_print("⏭️ 未配置 Cookie，跳过。"); return []
    try:
        r = httpx.get(FOFA_URL, headers=HEADERS, timeout=15)
        if r.status_code == 429:
            live_print("⚠️ FOFA 请求过于频繁 (HTTP 429)，将被限流")
            live_print("💡 提示: 等待 10-15 分钟后重试，或使用爬虫模式")
            return []
        if "账号登录" in r.text or "login" in str(r.url).lower():
            live_print("❌ 错误: FOFA Cookie 已失效！请更新 secrets.FOFA_COOKIE")
            live_print("💡 提示: 在浏览器登录 fofa.info → F12 → Application → Cookies → 复制完整 Cookie 值")
            return []
        if r.status_code == 403:
            live_print("❌ 错误: FOFA 返回 403 禁止访问，可能被限流或封禁")
            return []

        raw_list = re.findall(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}:\d+)', r.text)
        raw_list = [ip for ip in raw_list if not _is_invalid_ip(ip.split(':')[0])]
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

def _review_geo(unique_all):
    """最终复核：逐 IP 归属地校验。

    返回 (geo_ips, geo_pass, geo_fail, lines)，供 main() 以 to_thread 调用，
    避免同步 geo 查询阻塞事件循环。
    """
    geo_ips, lines = [], []
    geo_pass = geo_fail = 0
    total = len(unique_all)
    for idx, ip in enumerate(unique_all, 1):
        ok, desc = get_geo_info(ip.split(":")[0])
        if ok:
            lines.append(f"  [{idx:02d}/{total:02d}] ✅ 有效 | {ip:<21} | {desc}")
            geo_ips.append(ip)
            geo_pass += 1
        else:
            lines.append(f"  [{idx:02d}/{total:02d}] ⏭️ 剔除 | {ip:<21} | {desc}")
            geo_fail += 1
    return geo_ips, geo_pass, geo_fail, lines

# ===============================
# 4. 主程序入口
# ===============================
async def main():
    start_time = time.time()
    live_print("🚀 main.py 启动...")
    stats = {"fofa": 0, "segments_total": 0, "segments_valid": 0,
             "scan_tasks": 0, "scan_found": 0, "geo_pass": 0, "geo_fail": 0,
             "blacklist_skip": 0}

    # 1. 准备 RTP（同步阻塞 I/O 移至线程，避免卡住事件循环）
    await asyncio.to_thread(update_rtp_template)
    live_print("✅ RTP 模板更新完成")

    # 2. 抓取与扫描（同步阻塞调用均 offload 到线程）
    fips = await asyncio.to_thread(scrape_fofa)
    stats["fofa"] = len(fips)
    live_print(f"📡 FOFA 返回: {len(fips)} 条记录")

    # 始终加载现有发现库（包含历史 C-segment 和端口）
    all_segs, all_ports = await asyncio.to_thread(update_discovery_database, fips)
    stats["segments_total"] = len(all_segs)
    live_print(f"📊 发现库加载: {len(all_segs)} 个 segment, {len(all_ports)} 个端口")
    
    # FOFA 结果为空时，尝试爬虫补充
    if not fips:
        live_print("⚠️ FOFA 返回 0 条结果（cookie 可能已过期），尝试爬虫补充")
        crawler_segments = await asyncio.to_thread(crawl_segments)
        if crawler_segments:
            new_segs = [s for s in crawler_segments if s not in all_segs]
            if new_segs:
                live_print(f"🕷️ 爬虫补充 {len(new_segs)} 个新 segment")
                all_segs.extend(new_segs)
                # 更新发现库
                with open(DISCOVERY_FILE, "w", encoding="utf-8") as f:
                    for s in sorted(all_segs):
                        f.write(f"SEG|{s}\n")
                    for p in sorted(all_ports, key=int):
                        f.write(f"PORT|{p}\n")
        else:
            live_print("⚠️ 爬虫也失败，将使用历史发现库继续扫描")
    
    # 检查是否有任何 segment 可扫描
    if not all_segs:
        live_print("❌ 发现库为空且爬虫无结果，无法扫描")
        return
    
    live_print(f"🔍 开始验证 {len(all_segs)} 个 segment...")
    valid_segs, blacklist_skip = await asyncio.to_thread(filter_segments, all_segs)
    stats["segments_valid"] = len(valid_segs)
    stats["blacklist_skip"] = blacklist_skip
    live_print(f"✅ 验证完成: {len(valid_segs)} 个有效 segment")
    
    if not valid_segs:
        live_print("❌ 无有效 segment，无法扫描")
        return

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

    # ---- 扫描配额轮换：生产中段必扫 + 其余段按优先级取配额 ----
    # 提前加载下游反馈：dead_ips 对应的段会被排入轮换尾部
    feedback = await asyncio.to_thread(load_external_feedback)
    dead_ips = feedback.get("dead_ips", [])
    if dead_ips:
        live_print(f"📥 下游反馈全挂服务器: {len(dead_ips)} 个 (其 C 段降权)")

    known_alive = []
    if os.path.exists(SOURCE_IP_FILE):
        with open(SOURCE_IP_FILE, "r", encoding="utf-8") as f:
            known_alive = [line.strip() for line in f if line.strip()]
    scan_segs, quota_msg = select_scan_segments(valid_segs, known_alive, dead_ips=dead_ips)
    if quota_msg:
        live_print(f"🎯 {quota_msg}")
    stats["segments_scanned"] = len(scan_segs)

    # 共享 found_set
    shared_found = set()
    live_print(f"🚀 准备扫描: {len(scan_segs)} 段 × 254 IP × {len(sorted_ports)} 端口 = {len(scan_segs)*254*len(sorted_ports):,} 任务")
    if sorted_ports and scan_segs:
        live_print(f"🔍 开始扫描...")
        sips, scan_seconds = await run_native_scan(scan_segs, sorted_ports, shared_found)
        stats["scan_seconds"] = scan_seconds
        update_scan_state(scan_segs, sips)
    else:
        sips = []
        live_print("⚠️ 无 active 端口或无有效段，跳过扫描")
    stats["scan_found"] = len(sips)
    live_print(f"📊 扫描完成: 发现 {len(sips)} 个新 IP | 总命中: {len(shared_found)} | 耗时: {stats.get('scan_seconds', 0):.1f}s")

    # ---- 扫描后更新端口统计（在 source-ip 写入前记录 scanned_ports） ----
    scanned_ports = [str(p) for p in sorted_ports]

    # 合并 FOFA 结果和扫描结果
    unique_all = sorted(list(set(fips + sips)))
    live_print(f"📋 合并结果: FOFA={len(fips)} + 扫描={len(sips)} = 总计 {len(unique_all)} 个唯一 IP")

    # 下游反馈排序：server_scores 按 host:port 聚合存活率/带宽（上方已加载）
    server_scores = feedback.get("server_scores", {})
    if server_scores:
        def _fb_key(ip_port):
            s = server_scores.get(ip_port, {})
            # 按 best_bw 优先，其次存活数；无记录者排最后（返回 0）
            return (s.get("best_bw", 0), s.get("alive", 0))
        unique_all.sort(key=_fb_key, reverse=True)
        live_print(f"📥 反馈排序: {len(server_scores)} 台服务器评分已应用")

    # IP 列表签名检测：如果 IP 列表未变化，跳过归属复核使用缓存
    current_sig = compute_ip_signature(unique_all)
    previous_sig = load_previous_ip_signature()

    if current_sig == previous_sig and os.path.exists(SOURCE_IP_FILE):
        live_print("📋 IP 列表未变化，跳过归属复核（使用缓存结果）")
        with open(SOURCE_IP_FILE, "r", encoding="utf-8") as f:
            geo_ips = [line.strip() for line in f if line.strip()]
        gp = len(geo_ips)
        gf = 0
        review_lines = [f"  📋 缓存结果: {gp} 个有效 IP (IP 列表未变化)"]
        for line in review_lines:
            live_print(line)
        save_ip_signature(current_sig)
    else:
        save_ip_signature(current_sig)
        # 3. 最终复核（同步 geo 查询 offload 到线程，避免阻塞事件循环）
        log_section("🌍 最终结果复核", "🔹")
        geo_ips, gp, gf, review_lines = await asyncio.to_thread(_review_geo, unique_all)
        for line in review_lines:
            live_print(line)

    stats["geo_pass"], stats["geo_fail"] = gp, gf
    

    # 4. 写入文件（标准 M3U 格式 + 原子化写入）
    if geo_ips:
        log_section("💾 数据归档 (output目录)", "🔹")
        geo_ips.sort()

        # 写入 source-ip.txt（原子化，offload 到线程避免阻塞事件循环）
        await asyncio.to_thread(atomic_write, SOURCE_IP_FILE, "\n".join(geo_ips))
        live_print(f"  📝 {SOURCE_IP_FILE}")

        # 更新端口命中统计（基于本次 source-ip.txt）
        deactivated = _update_port_stats_after_scan(port_stats, scanned_ports, SOURCE_IP_FILE)
        if deactivated:
            stats["port_deactivated"] = deactivated

        # 写入标准 M3U（RTP 解析与拼接改用 utils 公共函数）
        rtp_entries = parse_rtp_entries(RTP_FILE)
        m3u_lines = build_m3u(rtp_entries, geo_ips)
        compat_lines = build_compat(rtp_entries, geo_ips)

        await asyncio.to_thread(atomic_write, SOURCE_M3U_FILE, "\n".join(m3u_lines))
        live_print(f"  📝 {SOURCE_M3U_FILE} (标准M3U)")
        await asyncio.to_thread(atomic_write, SOURCE_NONCHECK_FILE, "\n".join(compat_lines))
        live_print(f"  📝 {SOURCE_NONCHECK_FILE} (兼容格式)")

        stats["m3u_count"] = len(geo_ips) * len(rtp_entries)
        stats["rtp_count"] = len(rtp_entries)
        live_print(f"✨ 总结: {len(geo_ips)} 个服务器 | {len(rtp_entries)} 个频道 | {stats['m3u_count']} 条链接")
        
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
    live_print(f"  │  ├ 扫描耗时 ............. {stats.get('scan_seconds', 0):>7.2f}s")
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
    write_summary(f"| ② 端口扫描 | 扫描耗时 | {stats.get('scan_seconds', 0)}s |")
    write_summary(f"| ② 端口扫描 | 端口休眠 | {deactivated} 个 |")
    write_summary(f"| ③ 归属复核 | 复核通过 | {stats['geo_pass']} 个 |")
    write_summary(f"| ③ 归属复核 | 复核剔除 | {stats['geo_fail']} 个 |")
    write_summary(f"| ④ 成品输出 | 有效服务器 | {len(geo_ips)} 个 |")
    write_summary(f"| ④ 成品输出 | RTP 频道 | {rtp_count} 个 |")
    write_summary(f"| ④ 成品输出 | M3U 总链接 | {m3u_count} 条 |")

    write_summary(f"\n> 💾 输出文件: `output/source-ip.txt` `output/source-m3u.txt` `output/source-m3u-noncheck.txt`")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        live_print(f"❌ main.py 异常退出: {e}")
        traceback.print_exc()
        # 写入错误日志，确保 CI 能看到
        with open("output/error.txt", "w", encoding="utf-8") as f:
            f.write(f"Error: {e}\n")
            traceback.print_exc(file=f)
        raise
