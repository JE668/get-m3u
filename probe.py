import os, subprocess, time, json, asyncio
import httpx
from datetime import datetime
from utils import live_print, write_summary, atomic_write, log_section

# ===============================
# 1. 配置区 (目录结构优化)
# ===============================
SOURCE_IP_FILE = "output/source-ip.txt"
SOURCE_M3U_FILE = "output/source-m3u.txt"
SOURCE_NONCHECK_FILE = "output/source-m3u-noncheck.txt"
LOG_FILE = "output/log.txt"
RTP_FILE = "data/rtp/ChinaTelecom-Guangdong.txt"
SNAPSHOT_DIR = "data/.last_snapshot" # 变动比对快照目录

# 下游仓库联动触发已统一移至 .github/workflows/main.yml（通过 gh CLI 触发），
# 避免与 Python 内触发重复，并集中错误处理与 Job Summary 汇报。
# 下游仓库：JE668/m3u-checker-max (update.yml) / JE668/iptv-api (main.yml)

# ===============================
# 3. 比对与联动逻辑
# ===============================
def has_data_changed(filename):
    live_print(f"━━━ 🕵️ 变动检测 ━━━━━━━━━━━━━━━━━━━━━━━━━━  📂 {filename}")
    if not os.path.exists(filename): return False
    with open(filename, 'r', encoding='utf-8') as f:
        current = sorted([l.strip() for l in f if l.strip()])

    # 策略1: 与本地 Git HEAD 对比
    try:
        cmd = ['git', 'show', f'HEAD:{filename}']
        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        if res.returncode == 0:
            old = sorted([l.strip() for l in res.stdout.splitlines() if l.strip()])
            live_print(f" 📊 历史: {len(old)}行 | 当前: {len(current)}行")
            if current == old:
                live_print(" ℹ️ 结论: 无变动"); return False
            live_print(" 🆕 结论: 有变动")
        else:
            live_print(" 🆕 结论: 无 Git 历史（首次或 shallow clone）")
    except (OSError, subprocess.SubprocessError) as e:
        live_print(f" ⚠️ Git 比对异常: {e}，回退到快照比对")

    # 策略2: 快照比对（shallow clone 容错）
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    snapshot_path = os.path.join(SNAPSHOT_DIR, os.path.basename(filename))
    if os.path.exists(snapshot_path):
        with open(snapshot_path, 'r', encoding='utf-8') as f:
            snap = sorted([l.strip() for l in f if l.strip()])
        if current == snap:
            live_print(" ℹ️ 快照比对: 无变动"); return False
        live_print(" 🆕 快照比对: 有变动")

    # 更新快照
    with open(snapshot_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(current))
    return True

# ===============================
# 4. 抽样测速逻辑（量化版：512KB + 带宽计算）
# ===============================
PROBE_DOWNLOAD_TARGET = 512 * 1024  # 512KB，比原128KB多4倍数据量，支持带宽计算
PROBE_TIMEOUT_PER_URL = 6           # 单URL最多6秒（原5秒）
SOURCE_META_FILE = "output/source-meta.json"

async def async_fast_ip_probe(client, host_port, url_list):
    """
    异步测试IP:port的流质量（同IP的多个URL并发测试）
    返回: (is_alive, host_port, bandwidth_mbps, log_message)
    """
    # 同IP的多个URL并发测试（最多3个）
    async def _probe_single_url(test_url):
        start = time.time()
        try:
            async with client.stream("GET", test_url, timeout=httpx.Timeout(10, connect=4, read=6)) as r:
                if r.status_code == 200:
                    down = 0
                    async for chunk in r.aiter_bytes(chunk_size=64*1024):
                        down += len(chunk)
                        if down >= PROBE_DOWNLOAD_TARGET:
                            elapsed = time.time() - start
                            bw = round(down * 8 / elapsed / 1_000_000, 1)
                            return True, bw
                        if time.time() - start > PROBE_TIMEOUT_PER_URL:
                            break
                    # 下载不足但拿到了一些数据
                    elapsed = time.time() - start
                    bw = round(down * 8 / elapsed / 1_000_000, 1) if down > 4096 else 0
                    if bw > 0:
                        return True, bw
        except (httpx.RequestError, httpx.TimeoutException):
            pass
        return False, 0.0
    
    # 并发测试最多3个URL
    tasks = [_probe_single_url(url) for url in url_list[:3]]
    results = await asyncio.gather(*tasks)
    
    # 取最佳结果
    best_bw = 0
    for is_alive, bw in results:
        if is_alive and bw > best_bw:
            best_bw = bw
    
    if best_bw > 0:
        return True, host_port, best_bw, f" 🟢 [存活] {host_port:<21} | {best_bw:.1f}Mbps"
    elif any(r[0] for r in results):
        return True, host_port, best_bw, f" 🟡 [弱流] {host_port:<21} | {best_bw:.1f}Mbps"
    else:
        return False, host_port, 0.0, f" 🔴 [无流] {host_port:<21}"


# ===============================
# 5. 运行主逻辑 (async)
# ===============================
async def main():
    start_time = time.time()
    changed = has_data_changed(SOURCE_IP_FILE)

    # 预初始化，确保即使数据为空也有定义，防止 summary 阶段 NameError
    ip_map, url_map = {}, {}
    valid_hostports = set()

    if os.path.exists(SOURCE_NONCHECK_FILE):
        with open(SOURCE_NONCHECK_FILE, encoding="utf-8") as f:
            lines = [l.strip() for l in f if "," in l]

        if lines:
            # 1. 归集要测试的 IP:port 和 URL
            ip_map, url_map = {}, {}
            for line in lines:
                try:
                    url = line.split(",", 1)[1]
                    host_port = url.split("/")[2]
                    ip_key = host_port.split(":")[0]
                    if ip_key not in ip_map: ip_map[ip_key] = []; url_map[ip_key] = []
                    ip_map[ip_key].append(host_port)
                    url_map[ip_key].append(url)
                except (ValueError, IndexError): continue

            live_print(f"━━━ 🎬 抽样测速 ━━━━━━━━━━━━━━━━━━━━━━━━  🌐 {len(ip_map)} IP (async)")
            valid_hostports, logs = set(), []
            meta_data = {}

            # 构建 IP -> [(host_port, url_list), ...] 映射（同IP多端口并发）
            ip_to_hostports = {}
            for ip_key, urls in url_map.items():
                ip_to_hostports[ip_key] = [(hp, urls) for hp in ip_map[ip_key]]

            # 异步并发测速（同IP多端口并发 + 提前满足）
            probe_workers = int(os.environ.get("PROBE_WORKERS", "50"))
            sem = asyncio.Semaphore(probe_workers)
            ip_found = set()  # 已找到有效端口的 IP，跳过剩余端口
            
            async with httpx.AsyncClient(
                limits=httpx.Limits(max_keepalive_connections=300, max_connections=1000),
                timeout=httpx.Timeout(connect=4, read=6, write=5, pool=2)
            ) as client:
                async def bounded_probe(hp, urls):
                    async with sem:
                        return await async_fast_ip_probe(client, hp, urls)
                
                # 滚动窗口并发（同IP多端口并发，任意端口成功后跳过该IP剩余端口）
                pending = set()
                all_ips = list(ip_to_hostports.items())  # [(ip, [(hp, urls), ...]), ...]
                ip_idx = 0
                hp_idx_per_ip = {}  # ip -> 当前测到第几个端口
                
                # 初始化：每个 IP 先测第一个端口
                for ip, hps in all_ips[:probe_workers]:
                    if hps:
                        hp, urls = hps[0]
                        pending.add(asyncio.create_task(bounded_probe(hp, urls)))
                        hp_idx_per_ip[ip] = 1
                ip_idx = min(probe_workers, len(all_ips))
                
                while pending:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        ok, hp, bw, msg = task.result()
                        ip = hp.split(":")[0]
                        live_print(msg)
                        logs.append(msg.strip())
                        if ok:
                            valid_hostports.add(hp)
                            meta_data[hp] = {"bandwidth_mbps": bw}
                            ip_found.add(ip)  # 该 IP 已找到有效端口
                    
                    # 补充新任务（跳过已成功的 IP）
                    while len(pending) < probe_workers and ip_idx < len(all_ips):
                        ip, hps = all_ips[ip_idx]
                        # 如果该 IP 已成功，跳过剩余端口
                        if ip in ip_found:
                            ip_idx += 1
                            continue
                        # 测试该 IP 的下一个端口（如果还有）
                        hp_idx = hp_idx_per_ip.get(ip, 0)
                        if hp_idx < len(hps):
                            hp, urls = hps[hp_idx]
                            pending.add(asyncio.create_task(bounded_probe(hp, urls)))
                            hp_idx_per_ip[ip] = hp_idx + 1
                        else:
                            # 该 IP 所有端口都测完了，下一个 IP
                            ip_idx += 1

            # 写入元数据供下游 m3u-checker-max 使用
            if meta_data:
                atomic_write(SOURCE_META_FILE, json.dumps(meta_data, ensure_ascii=False, indent=2))
                live_print(f" 📝 服务器元数据已写入: {SOURCE_META_FILE} ({len(meta_data)} 台)")

            # ==========================================
            # 6. 重新拼装存活 IP 并写入 source-m3u.txt（标准 M3U 格式）
            # ==========================================
            live_print(f"━━━ 💾 数据重组与归档 ━━━━━━━━━━━━━━━━━━━━━")

            # 先写日志
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"服务器抽测报告 | 时间: {datetime.now()}\n" + "\n".join(sorted(logs)))
            live_print(f" 📝 成功覆写日志: {LOG_FILE}")

            # 读取 RTP 模板进行重新组装
            if valid_hostports:
                if os.path.exists(RTP_FILE):
                    with open(RTP_FILE, encoding="utf-8") as f:
                        rtps = [x.strip() for x in f if "," in x]

                    # 标准 M3U 格式输出（使用 ip:port 拼接，保留端口号）
                    m3u_lines = []

                    # 预计算 RTP 条目（避免内层循环重复 split）
                    rtp_entries = []
                    for r in rtps:
                        try:
                            name, r_url = r.split(",", 1)
                            suffix = r_url.split("://")[1]
                            rtp_entries.append((name, suffix))
                        except (ValueError, IndexError):
                            continue

                    m3u_lines.append("#EXTM3U")
                    for hp in sorted(valid_hostports):
                        for name, suffix in rtp_entries:
                            m3u_lines.append(f"#EXTINF:-1,{name}")
                            m3u_lines.append(f"http://{hp}/rtp/{suffix}")

                    atomic_write(SOURCE_M3U_FILE, "\n".join(m3u_lines))

                    live_print(f" 📝 成功重组纯净版: {SOURCE_M3U_FILE} (标准M3U)")
                    live_print(f"✨ 测速结束: 存活 {len(valid_hostports)} 个 IP | 生成 {len(m3u_lines)-1} 条纯净链接")
                else:
                    live_print(f" ❌ 找不到 RTP 模板 {RTP_FILE}，无法重组！")
                    # 无 RTP 模板时写入空 M3U 头，避免下游使用过期数据
                    atomic_write(SOURCE_M3U_FILE, "#EXTM3U\n")
                    live_print(f" 📝 已写入空 M3U 头: {SOURCE_M3U_FILE}")
            else:
                # 如果没有存活IP，清空文件
                atomic_write(SOURCE_M3U_FILE, "")
                live_print(f" 📝 存活 IP 为 0，已清空 {SOURCE_M3U_FILE}")

    # ==========================================
    # 7. 数据变动小结
    #    （下游仓库联动触发已统一移至 .github/workflows/main.yml，
    #     此处仅汇报本次数据是否相较上次提交有变动）
    # ==========================================
    live_print("\n⚖️ ========== 数据变动 ==========")
    live_print(f"📌 source-ip.txt 相对上次提交: {'🆕 有变动' if changed else 'ℹ️ 无变动'}")
    live_print("🔗 下游触发(m3u-checker-max / iptv-api)由 CI 统一处理")

    elapsed = round(time.time() - start_time, 2)
    out_of_ip_count = len(ip_map) - len(valid_hostports)

    log_section("测速 — 阶段摘要", "🎬")
    live_print(f"  源发现结果 → 抽样测速 → 数据变动")
    live_print(f"")
    live_print(f"  ┌─ 阶段: 测速结果")
    live_print(f"  │  ├ 上游有效服务器 ..... {len(ip_map):>4} 个 (来自 source-ip.txt)")
    live_print(f"  │  ├ 有流响应 .......... {len(valid_hostports):>4} 个")
    live_print(f"  │  └ 无流/失败 ......... {out_of_ip_count:>4} 个")
    live_print(f"  │")
    live_print(f"  └─ 阶段: 数据变动")
    live_print(f"     ├ 本次数据: {'🆕 有变动' if changed else 'ℹ️ 无变动'}")
    live_print(f"     └ 耗时 ............. {elapsed:>7.2f}s")
    live_print(f"")

    # ── Job Summary ──
    write_summary("### 🎬 阶段摘要 — 测速\n")
    write_summary("| 阶段 | 指标 | 数值 |")
    write_summary("|------|------|------|")
    write_summary(f"| ① 测速 | 待测服务器 | {len(ip_map)} 个 |")
    write_summary(f"| ① 测速 | 有流响应 | {len(valid_hostports)} 个 |")
    write_summary(f"| ① 测速 | 无流/失败 | {out_of_ip_count} 个 |")
    write_summary(f"| ② 数据变动 | source-ip | {'🆕 有变动' if changed else 'ℹ️ 无变动'} |")

    write_summary(f"\n> ⏱️ 总耗时: {elapsed}s")
    write_summary(f"\n> 🔗 下游触发(m3u-checker-max / iptv-api)由 CI 统一处理")

    live_print("\n✅ 测速完成，下游联动由 GitHub Actions 统一触发。")

if __name__ == "__main__":
    asyncio.run(main())

