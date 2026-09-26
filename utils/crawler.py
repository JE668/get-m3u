"""
从公开 M3U 列表提取 C-segment，用于扩展扫描范围。
无需 FOFA cookie，零成本获取新扫描目标。

原理：
1. 从公开 M3U 源抓取 HTTP 直播地址
2. 提取 IP 地址
3. 按 C-segment (/24) 分组
4. 返回这些 segment 供扫描器使用
"""

import re
import ipaddress
import requests
from typing import Set, Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils import live_print

# ── 公开 M3U 源列表 ──
PUBLIC_M3U_SOURCES = [
    # 国内电信 IPTV 公开列表
    "https://iptv-org.github.io/iptv/countries/cn.m3u",
    "https://iptv-org.github.io/iptv/languages/zho.m3u",
    "https://iptv-org.github.io/iptv/categories/news.m3u",
    "https://iptv-org.github.io/iptv/categories/sports.m3u",
    "https://iptv-org.github.io/iptv/categories/kids.m3u",
    # 国内 IPTV 社区维护
    "https://raw.githubusercontent.com/tukuaa/iptv/master/tv/m3u/iptv.m3u",
    "https://raw.githubusercontent.com/qist/iptv/master/tv.m3u",
    "https://raw.githubusercontent.com/evils0t/iptv/master/m3u/iptv.m3u",
    # Tzwcard 系列（与 get-m3u 相同源）
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_4k.m3u",
    "https://raw.githubusercontent.com/Tzwcard/ChinaTelecom-GuangdongIPTV-RTP-List/refs/heads/master/GuangdongIPTV_rtp_hd.m3u",
]

# ── 额外可配置的 M3U 源（从环境变量或配置文件读取） ──
EXTRA_M3U_URLS = []

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

# ── IP 提取正则 ──
HTTP_IP_PATTERN = re.compile(
    r'http://(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
    r'(?::(\d+))?/'
)

# ── 缓存文件 ──
SEGMENT_CACHE_FILE = "data/segments_cache.json"
CACHE_TTL = 6 * 3600  # 6小时缓存


def is_valid_public_ip(ip: str) -> bool:
    """检查 IP 是否为有效的公网地址"""
    try:
        addr = ipaddress.ip_address(ip)
        return (
            not addr.is_private and
            not addr.is_loopback and
            not addr.is_link_local and
            not addr.is_reserved
        )
    except ValueError:
        return False


def extract_ips_from_m3u(content: str) -> List[Tuple[str, str]]:
    """从 M3U 内容中提取 (ip, port) 对"""
    results = []
    for match in HTTP_IP_PATTERN.finditer(content):
        ip = match.group(1)
        port = match.group(2) or "80"
        if is_valid_public_ip(ip):
            results.append((ip, port))
    return results


def ip_to_segment(ip: str) -> str:
    """将 IP 转换为 C-segment (/24)"""
    parts = ip.split('.')
    return f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"


def fetch_single_m3u(url: str, timeout: int = 15) -> Tuple[str, List[Tuple[str, str]]]:
    """抓取单个 M3U 并提取 IP 列表"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code == 200:
            ips = extract_ips_from_m3u(r.text)
            return url, ips
        else:
            return url, []
    except (requests.RequestException, Exception):
        return url, []


def fetch_all_m3u_sources(urls: List[str] = None, max_workers: int = 10) -> Dict[str, List[Tuple[str, str]]]:
    """并行抓取所有 M3U 源"""
    if urls is None:
        urls = PUBLIC_M3U_SOURCES + EXTRA_M3U_URLS
    
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_single_m3u, url): url for url in urls}
        for future in as_completed(futures):
            url, ips = future.result()
            results[url] = ips
            if ips:
                live_print(f"  📡 {url}: {len(ips)} 个 IP")
    
    return results


def build_segment_index(ip_results: Dict[str, List[Tuple[str, str]]]) -> Dict[str, List[Tuple[str, str]]]:
    """
    构建 segment 索引：{segment: [(ip, port), ...]}
    
    每个 segment 记录所有已知的存活 IP 和端口，
    用于优先扫描这些 segment 的其他端口。
    """
    segment_index = {}
    
    for url, ips in ip_results.items():
        for ip, port in ips:
            seg = ip_to_segment(ip)
            if seg not in segment_index:
                segment_index[seg] = []
            # 避免重复添加
            if (ip, port) not in segment_index[seg]:
                segment_index[seg].append((ip, port))
    
    return segment_index


def get_priority_segments(segment_index: Dict[str, List[Tuple[str, str]]], 
                           min_ips: int = 3) -> List[str]:
    """
    获取优先扫描的 segment 列表
    
    选择标准：
    - segment 中至少有 min_ips 个已知存活 IP
    - 按 IP 数量降序排列
    """
    filtered = {
        seg: ips 
        for seg, ips in segment_index.items() 
        if len(ips) >= min_ips
    }
    
    # 按 IP 数量降序排列
    sorted_segments = sorted(filtered.keys(), 
                             key=lambda seg: len(filtered[seg]), 
                             reverse=True)
    
    return sorted_segments


def save_segment_cache(segment_index: Dict[str, List[Tuple[str, str]]]) -> None:
    """保存 segment 索引到缓存文件"""
    import json
    import os
    import time
    
    try:
        os.makedirs(os.path.dirname(SEGMENT_CACHE_FILE), exist_ok=True)
        cache = {
            "_timestamp": time.time(),
            "segments": {seg: [(ip, port) for ip, port in ips] 
                        for seg, ips in segment_index.items()}
        }
        with open(SEGMENT_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except (OSError, TypeError):
        pass


def load_segment_cache(max_age: float = None) -> Dict[str, List[Tuple[str, str]]]:
    """从缓存文件加载 segment 索引"""
    import json
    import os
    import time
    
    if max_age is None:
        max_age = CACHE_TTL
    
    try:
        if os.path.exists(SEGMENT_CACHE_FILE):
            with open(SEGMENT_CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            age = time.time() - cache.get("_timestamp", 0)
            if age < max_age:
                return {seg: [(ip, port) for ip, port in ips] 
                       for seg, ips in cache.get("segments", {}).items()}
    except (json.JSONDecodeError, OSError):
        pass
    
    return {}


def crawl_segments(use_cache: bool = True) -> List[str]:
    """
    主入口：从公开 M3U 爬取 segment 列表
    
    返回：优先扫描的 segment 列表
    """
    # 尝试加载缓存
    if use_cache:
        cached = load_segment_cache()
        if cached:
            live_print(f"📦 从缓存加载 {len(cached)} 个 segment")
            segments = get_priority_segments(cached)
            if segments:
                return segments
    
    # 爬取新的数据
    live_print("🕷️ 从公开 M3U 爬取 segment...")
    ip_results = fetch_all_m3u_sources()
    
    # 构建索引
    segment_index = build_segment_index(ip_results)
    live_print(f"📊 共发现 {len(segment_index)} 个 segment，"
               f"覆盖 {sum(len(v) for v in segment_index.values())} 个 IP")
    
    # 保存缓存
    save_segment_cache(segment_index)
    
    # 获取优先 segment
    segments = get_priority_segments(segment_index)
    live_print(f"🎯 优先扫描 {len(segments)} 个高价值 segment")
    
    return segments


# ── 额外：从 IP 列表提取 segment（供其他模块调用） ──
def segments_from_ip_list(ips: List[str]) -> List[str]:
    """从 IP 列表提取唯一的 C-segment"""
    segments = set()
    for ip in ips:
        if is_valid_public_ip(ip):
            segments.add(ip_to_segment(ip))
    return list(segments)
