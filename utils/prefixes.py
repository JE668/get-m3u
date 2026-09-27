"""APNIC delegated 库 → 广东电信 /24 段枚举

FOFA 免费额度枯竭后的另一条发现通道：
- APNIC 每日发布亚太地区 IP 分配权威清单（delegated-apnic-latest）
- 下载全部 CN IPv4 块，用本地 ip2region.xdb 离线逐 /24 判定归属
- 命中「广东 + 电信/CHINANET」的段即候选扫描对象

优点：免费、无额度、覆盖全量注册分配空间（理论上 FOFA 能查到的 udpxy
主机必然落在这些段内）。缺点：段里不保证有 udpxy 服务，依赖扫描验证。

产出缓存到 data/apnic_segments.json（24h TTL）。
"""
import ipaddress
import json
import os
import urllib.request
from typing import List, Optional, Set


APNIC_URL = os.environ.get(
    "APNIC_DELEGATED_URL",
    "https://ftp.apnic.net/stats/apnic/delegated-apnic-latest",
)
CACHE_FILE = "data/apnic_segments.json"
CACHE_TTL = 24 * 3600  # 分配表每天一版，24h 缓存足够

# 每个分配块最多展开的 /24 数（防个别大块拖慢扫描队列）
MAX_24_PER_BLOCK = 64


def _load_cache() -> Optional[List[str]]:
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            import time
            if time.time() - data.get("ts", 0) < CACHE_TTL:
                return data.get("segments", [])
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return None


def _save_cache(segments: List[str]) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            import time
            json.dump({"ts": time.time(), "segments": segments}, f)
    except OSError:
        pass


def enumerate_gd_chinanet_segments(geo_searcher=None) -> List[str]:
    """枚举 APNIC 中国 IPv4 块中归属为 广东+电信 的 /24 段列表。

    geo_searcher: 可选，传入复用 main.py 已加载的 ip2region searcher
    （未传则临时加载一次，供独立使用/测试）。
    """
    from utils import live_print

    cached = _load_cache()
    if cached is not None:
        live_print(f"📦 APNIC 段缓存命中: {len(cached)} 段 (24h 内)")
        return cached

    if geo_searcher is None:
        from utils import __name__ as _u  # noqa
        # 惰性：独立使用时临时加载 ip2region
        import io as _io
        import ip2region.util as ip2region_util
        import ip2region.searcher as ip2region_searcher
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "data", "ip2region.xdb")
        db_path = os.path.normpath(db_path)
        handle = _io.open(db_path, "rb")
        header = ip2region_util.load_header(handle)
        version = ip2region_util.version_from_header(header)
        v_index = ip2region_util.load_vector_index(handle)
        geo_searcher = ip2region_searcher.new_with_vector_index(version, db_path, v_index)
        handle.close()

    live_print(f"🌐 APNIC delegated 下载: {APNIC_URL}")
    data = urllib.request.urlopen(APNIC_URL, timeout=30).read().decode("utf-8", "ignore")

    blocks = []
    for line in data.splitlines():
        if not line or line.startswith('#') or '|' not in line:
            continue
        p = line.split('|')
        if len(p) >= 6 and p[1] == 'CN' and p[2] == 'ipv4':
            try:
                count = int(p[4])
                if count >= 256:  # 至少含一个 /24
                    blocks.append((ipaddress.IPv4Address(p[3]), count))
            except ValueError:
                continue

    gd_segs: Set[str] = set()
    for start, count in blocks:
        n24 = min(count // 256, MAX_24_PER_BLOCK)
        for i in range(n24):
            seg_ip = start + i * 256 + 1
            seg = '.'.join(str(seg_ip).split('.')[:3])
            try:
                region = geo_searcher.search(str(seg_ip))
            except Exception:
                continue
            if region and ('广东' in region) and any(k in region for k in ('电信', 'CHINANET', 'chinanet')):
                gd_segs.add(seg)

    result = sorted(gd_segs)
    live_print(f"🗺️ APNIC 枚举: {len(blocks)} 个 CN 块 → 广东电信段 {len(result)} 个")
    _save_cache(result)
    return result


if __name__ == "__main__":
    segs = enumerate_gd_chinanet_segments()
    print(f"共 {len(segs)} 段，示例: {segs[:10]}")
