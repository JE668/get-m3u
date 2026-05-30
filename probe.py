import os, subprocess, time, concurrent.futures, tempfile
import requests
from datetime import datetime

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

TARGET_REPO = "JE668/iptv-api"
TARGET_WORKFLOW = "main.yml"
TARGET_BRANCH = "master"
TRIGGER_TOKEN = os.environ.get("PAT_TOKEN", "")

def live_print(content):
    print(content, flush=True)

# ===============================
# 2. 基础工具函数
# ===============================
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
    count = 0
    os.makedirs(os.path.dirname(TRIGGER_COUNTER_FILE), exist_ok=True)
    if os.path.exists(TRIGGER_COUNTER_FILE):
        try:
            with open(TRIGGER_COUNTER_FILE, 'r', encoding='utf-8') as f:
                count = int(f.read().strip())
        except (ValueError, OSError): pass

    forced = False
    if changed: count = 0; should = True
    else:
        count += 1
        if count >= 3: should = True; count = 0; forced = True
        else: should = False

    with open(TRIGGER_COUNTER_FILE, 'w', encoding='utf-8') as f: f.write(str(count))
    return should, count, forced

# ===============================
# 4. 抽样测速逻辑
# ===============================
def fast_ip_probe(host_port, url_list):
    """抽取最多3个频道测试是否能下载视频流，host_port 格式为 ip:port"""
    for test_url in url_list[:3]:
        start = time.time()
        try:
            r = requests.get(test_url, stream=True, timeout=4)
            if r.status_code == 200:
                down = 0
                for chunk in r.iter_content(1024*64):
                    down += len(chunk)
                    if down >= 1024*128: # 成功下载 128KB 数据
                        return True, host_port, f" 🟢 [顺畅] {host_port:<21} | {round(time.time()-start,2)}s"
                    if time.time() - start > 5: break
        except requests.RequestException: continue
    return False, host_port, f" 🔴 [无流] {host_port:<21}"

# ===============================
# 5. 运行主逻辑
# ===============================
if __name__ == "__main__":
    changed = has_data_changed(SOURCE_IP_FILE)
    should_trigger, current_count, is_forced = get_trigger_status(changed)

    if os.path.exists(SOURCE_NONCHECK_FILE):
        with open(SOURCE_NONCHECK_FILE, encoding="utf-8") as f:
            lines = [l.strip() for l in f if "," in l]
        if lines:
            # 1. 归集要测试的 IP:port 和 URL
            #    ip_map: 纯IP -> [ip:port1, ip:port2, ...]
            #    url_map: 纯IP -> [url1, url2, ...]
            ip_map, url_map = {}, {}
            for line in lines:
                try:
                    url = line.split(",", 1)[1]
                    host_port = url.split("/")[2]  # "ip:port"
                    ip_key = host_port.split(":")[0]  # 纯 IP 作为分组 key
                    if ip_key not in ip_map: ip_map[ip_key] = []; url_map[ip_key] = []
                    ip_map[ip_key].append(host_port)
                    url_map[ip_key].append(url)
                except (ValueError, IndexError): continue

            live_print(f"::group::🎬 抽样测速 (共 {len(ip_map)} 个独立 IP)")
            valid_hostports, logs = set(), []

            # 多线程测速 (并发数可配置)
            # 每个 ip:port 独立探测，通过后存回 ip:port（保留端口号）
            hostport_url_map = {}
            for ip_key, urls in url_map.items():
                for hp in ip_map[ip_key]:
                    hostport_url_map[hp] = urls

            probe_workers = int(os.environ.get("PROBE_WORKERS", "50"))
            with concurrent.futures.ThreadPoolExecutor(max_workers=probe_workers) as ex:
                futures = [ex.submit(fast_ip_probe, hp, urls) for hp, urls in hostport_url_map.items()]
                for f in concurrent.futures.as_completed(futures):
                    ok, hp, msg = f.result()
                    live_print(msg)
                    logs.append(msg.strip())
                    if ok: valid_hostports.add(hp)

            live_print("::endgroup::")

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
                    m3u_lines = ["#EXTM3U"]
                    for hp in sorted(list(valid_hostports)):
                        for r in rtps:
                            try:
                                name, r_url = r.split(",", 1)
                                suffix = r_url.split("://")[1]
                                m3u_lines.append(f"#EXTINF:-1,{name}")
                                m3u_lines.append(f"http://{hp}/rtp/{suffix}")
                            except (ValueError, IndexError): continue

                    _atomic_write(SOURCE_M3U_FILE, "\n".join(m3u_lines))

                    live_print(f" 📝 成功重组纯净版: {SOURCE_M3U_FILE} (标准M3U)")
                    live_print(f"✨ 测速结束: 存活 {len(valid_hostports)} 个 IP | 生成 {len(m3u_lines)-1} 条纯净链接")
                else:
                    live_print(f" ❌ 找不到 RTP 模板 {RTP_FILE}，无法重组！")
                    # 无 RTP 模板时写入空 M3U 头，避免下游使用过期数据
                    _atomic_write(SOURCE_M3U_FILE, "#EXTM3U\n")
                    live_print(f" 📝 已写入空 M3U 头: {SOURCE_M3U_FILE}")
            else:
                # 如果没有存活IP，清空文件
                _atomic_write(SOURCE_M3U_FILE, "")
                live_print(f" 📝 存活 IP 为 0，已清空 {SOURCE_M3U_FILE}")

            live_print("::endgroup::")

    # ==========================================
    # 7. 联动处理
    # ==========================================
    live_print("\n⚖️ ========== 联动决策 ==========")
    if is_forced: live_print(f"🚨 强制触发")
    elif changed: live_print(f"✨ 更新触发")
    else: live_print(f"⏭️ 跳过 (计数: {current_count}/3)")

    if should_trigger and TRIGGER_TOKEN:
        live_print(f"::group::🔗 远程联动: {TARGET_REPO}")
        try:
            url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/{TARGET_WORKFLOW}/dispatches"
            r = requests.post(url, headers={"Authorization": f"token {TRIGGER_TOKEN}", "Accept": "application/vnd.github.v3+json"}, json={"ref": TARGET_BRANCH}, timeout=10)
            live_print(f"🎉 状态码: {r.status_code}")
        except requests.RequestException as e:
            live_print(f"❌ 请求失败: {e}")
        live_print("::endgroup::")
