import os, subprocess, time, json, asyncio
import httpx
from datetime import datetime
from utils import live_print, write_summary, atomic_write

# ===============================
# 1. 配置区 (目录结构优化)
# ===============================
SOURCE_IP_FILE = "output/source-ip.txt"
SOURCE_M3U_FILE = "output/source-m3u.txt"
SOURCE_NONCHECK_FILE = "output/source-m3u-noncheck.txt"
LOG_FILE = "output/log.txt"
TRIGGER_COUNTER_FILE = "data/trigger_counter.txt"
RTP_FILE = "data/rtp/ChinaTelecom-Guangdong.txt"
SNAPSHOT_DIR = "data/.last_snapshot" # 变动比对快照目录

TARGET_REPO = "JE668/m3u-checker-max"
TARGET_WORKFLOW = "update.yml"
TARGET_BRANCH = "main"
TRIGGER_TOKEN=os.environ.get("PAT_TOKEN", "")

# 联动 iptv-api：get-m3u 完成后同时触发订阅源更新
IPTV_API_REPO = "JE668/iptv-api"
IPTV_API_WORKFLOW = "main.yml"
IPTV_API_BRANCH = "master"

# ===============================
# 3. 比对与联动逻辑
# ===============================
def has_data_changed(filename):
    live_print(f"::group::🕵️ 变动检测 - {filename}")
    if not os.path.exists(filename):
        live_print("::endgroup::"); return False
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
                live_print(" ℹ️ 结论: 无变动"); live_print("::endgroup::"); return False
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
            live_print(" ℹ️ 快照比对: 无变动"); live_print("::endgroup::"); return False
        live_print(" 🆕 快照比对: 有变动")

    # 更新快照
    with open(snapshot_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(current))

    live_print("::endgroup::"); return True

def get_trigger_status(changed):
    """每次运行成功均触发下游，不再计数等待。
    changed=True → 数据有变化，正常触发
    changed=False → 数据无变化，仍触发（保证下游同步）"""
    return True, 0, not changed  # should=True, count=0, is_forced=(无变化时为True)

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
            async with client.stream("GET", test_url, timeout=httpx.Timeout(connect=4, read=6)) as r:
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


def _trigger_workflow(repo, workflow, branch, token, max_retries=3):
    """触发 GitHub Actions workflow，失败自动重试（使用 httpx 同步客户端）"""
    for attempt in range(1, max_retries + 1):
        try:
            url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
            r = httpx.post(url, headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}, json={"ref": branch}, timeout=10)
            live_print(f"🎉 状态码: {r.status_code}")
            return
        except httpx.RequestError as e:
            if attempt < max_retries:
                wait = 3 * attempt
                live_print(f"⏳ 重试 ({attempt}/{max_retries})，{wait}s 后重试: {e}")
                time.sleep(wait)
            else:
                live_print(f"❌ 联动失败 (已重试 {max_retries} 次): {e}")

# ===============================
# 5. 运行主逻辑 (async)
# ===============================
async def main():
    changed = has_data_changed(SOURCE_IP_FILE)
    should_trigger, current_count, is_forced = get_trigger_status(changed)

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

            live_print(f"::group::🎬 抽样测速 (共 {len(ip_map)} 个独立 IP，async并发)")
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

            live_print("::endgroup::")

            # 写入元数据供下游 m3u-checker-max 使用
            if meta_data:
                atomic_write(SOURCE_META_FILE, json.dumps(meta_data, ensure_ascii=False, indent=2))
                live_print(f" 📝 服务器元数据已写入: {SOURCE_META_FILE} ({len(meta_data)} 台)")

            # ==========================================
            # 6. 重新拼装存活 IP 并写入 source-m3u.txt（标准 M3U 格式）
            # ==========================================
            live_print(f"::group::💾 纯净版数据重组与归档")

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

            live_print("::endgroup::")

    # ==========================================
    # 7. 联动处理
    # ==========================================
    live_print("\n⚖️ ========== 联动决策 ==========")
    if is_forced: live_print(f"🚨 强制触发")
    elif changed: live_print(f"✨ 更新触发")
    else: live_print(f"⏭️ 跳过 (计数: {current_count}/3)")

    # 写入 Job Summary
    write_summary("### 🎬 测速与联动摘要\n")
    write_summary("| 指标 | 数值 |")
    write_summary("|------|------|")
    write_summary(f"| 🧪 抽样 IP | {len(ip_map)} 个 |")
    write_summary(f"| ✅ 存活 IP | {len(valid_hostports)} 个 |")
    write_summary(f"| ❌ 无流 IP | {len(ip_map) - len(valid_hostports)} 个 |")
    if is_forced:
     write_summary("| ⚖️ 联动决策 | 🚨 强制触发 |")
    elif changed:
     write_summary("| ⚖️ 联动决策 | ✨ 更新触发 |")
    else:
     write_summary(f"| ⚖️ 联动决策 | ⏭️ 跳过 ({current_count}/3) |")

    if TRIGGER_TOKEN:
        # 触发 m3u-checker-max
        live_print(f"::group::🔗 远程联动: {TARGET_REPO}")
        _trigger_workflow(TARGET_REPO, TARGET_WORKFLOW, TARGET_BRANCH, TRIGGER_TOKEN)
        live_print("::endgroup::")

        # 同时触发 iptv-api（订阅源更新）
        live_print(f"::group::🔗 远程联动: {IPTV_API_REPO}")
        _trigger_workflow(IPTV_API_REPO, IPTV_API_WORKFLOW, IPTV_API_BRANCH, TRIGGER_TOKEN)
        live_print("::endgroup::")

if __name__ == "__main__":
    asyncio.run(main())

