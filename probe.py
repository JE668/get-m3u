import os, subprocess, time, concurrent.futures, requests
from datetime import datetime

# ===============================
# 1. 配置区 (目录结构优化)
# ===============================
SOURCE_IP_FILE = "output/source-ip.txt"
SOURCE_M3U_FILE = "output/source-m3u.txt"
SOURCE_NONCHECK_FILE = "output/source-m3u-noncheck.txt"
LOG_FILE = "output/log.txt"
TRIGGER_COUNTER_FILE = "data/trigger_counter.txt"

TARGET_REPO = "JE668/iptv-api"
TARGET_WORKFLOW = "main.yml"
TARGET_BRANCH = "master"
TRIGGER_TOKEN = os.environ.get("PAT_TOKEN", "")

def live_print(content): print(content, flush=True)

def has_data_changed(filename):
    live_print(f"::group::🕵️ 变动检测 - {filename}")
    if not os.path.exists(filename): return False
    with open(filename, 'r', encoding='utf-8') as f:
        current = sorted([l.strip() for l in f if l.strip()])
    try:
        cmd = ['git', 'show', f'HEAD:{filename}']
        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        if res.returncode == 0:
            old = sorted([l.strip() for l in res.stdout.splitlines() if l.strip()])
            live_print(f"  📊 历史: {len(old)}行 | 当前: {len(current)}行")
            if current == old:
                live_print("  ℹ️ 结论: 无变动"); live_print("::endgroup::"); return False
            live_print("  🆕 结论: 有变动")
        else: live_print("  🆕 结论: 首次文件")
    except: live_print("  ⚠️ 比对异常")
    live_print("::endgroup::"); return True

def get_trigger_status(changed):
    count = 0
    # 确保 data 目录存在
    os.makedirs(os.path.dirname(TRIGGER_COUNTER_FILE), exist_ok=True)
    
    if os.path.exists(TRIGGER_COUNTER_FILE):
        try:
            with open(TRIGGER_COUNTER_FILE, 'r', encoding='utf-8') as f: count = int(f.read().strip())
        except: pass
    
    forced = False
    if changed: count = 0; should = True
    else:
        count += 1
        if count >= 3: should = True; count = 0; forced = True
        else: should = False
    
    with open(TRIGGER_COUNTER_FILE, 'w', encoding='utf-8') as f: f.write(str(count))
    return should, count, forced

def fast_ip_probe(ip_port, url_list):
    for test_url in url_list[:3]:
        start = time.time()
        try:
            r = requests.get(test_url, stream=True, timeout=4)
            if r.status_code == 200:
                down = 0
                for chunk in r.iter_content(1024*64):
                    down += len(chunk)
                    if down >= 1024*128:
                        return True, ip_port, f"  🟢 [顺畅] {ip_port:<21} | {round(time.time()-start,2)}s"
                    if time.time() - start > 5: break
        except: continue
    return False, ip_port, f"  🔴 [无流] {ip_port:<21}"

if __name__ == "__main__":
    changed = has_data_changed(SOURCE_IP_FILE)
    should_trigger, current_count, is_forced = get_trigger_status(changed)

    if os.path.exists(SOURCE_NONCHECK_FILE):
        with open(SOURCE_NONCHECK_FILE, encoding="utf-8") as f:
            lines = [l.strip() for l in f if "," in l]
        if lines:
            ip_map, url_map = {}, {}
            for line in lines:
                try:
                    url = line.split(",", 1)[1]
                    ip = url.split("/")[2]
                    if ip not in ip_map: ip_map[ip] = []; url_map[ip] = []
                    ip_map[ip].append(line); url_map[ip].append(url)
                except: pass

            live_print(f"::group::🎬 抽样测速 ({len(ip_map)} IP)")
            valid_ips, logs = set(), []
            with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
                futures = [ex.submit(fast_ip_probe, ip, u) for ip, u in url_map.items()]
                for f in concurrent.futures.as_completed(futures):
                    ok, ip, msg = f.result()
                    live_print(msg); logs.append(msg.strip())
                    if ok: valid_ips.add(ip)
            
            valid_lines = []
            for ip in valid_ips: valid_lines.extend(ip_map[ip])
            
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"Report: {datetime.now()}\n" + "\n".join(sorted(logs)))
            with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(sorted(valid_lines)))
            live_print(f"✅ 测速结束: 存活 {len(valid_ips)} IP"); live_print("::endgroup::")

    live_print("\n⚖️  ========== 联动决策 ==========")
    if is_forced: live_print(f"🚨 强制触发")
    elif changed: live_print(f"✨ 更新触发")
    else: live_print(f"⏭️  跳过 (计数: {current_count}/3)")

    if should_trigger and TRIGGER_TOKEN:
        live_print(f"::group::🔗 远程联动: {TARGET_REPO}")
        try:
            url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/{TARGET_WORKFLOW}/dispatches"
            r = requests.post(url, headers={"Authorization": f"token {TRIGGER_TOKEN}", "Accept": "application/vnd.github.v3+json"}, json={"ref": TARGET_BRANCH}, timeout=10)
            live_print(f"🎉 状态码: {r.status_code}")
        except: live_print("❌ 请求失败")
        live_print("::endgroup::")
