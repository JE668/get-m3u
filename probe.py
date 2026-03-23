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
RTP_FILE = "data/rtp/ChinaTelecom-Guangdong.txt"  # 新增：用于重新拼装的模板路径

TARGET_REPO = "JE668/iptv-api"
TARGET_WORKFLOW = "main.yml"
TARGET_BRANCH = "master"
TRIGGER_TOKEN = os.environ.get("PAT_TOKEN", "")

def live_print(content): 
    print(content, flush=True)

# ===============================
# 2. 比对与联动逻辑
# ===============================
def has_data_changed(filename):
    live_print(f"::group::🕵️ 变动检测 - {filename}")
    if not os.path.exists(filename): return False
    with open(filename, 'r', encoding='utf-8') as f:
        current = sorted([l.strip() for l in f if l.strip()])
    try:
        # 与本地 Git HEAD (上次提交的版本) 对比
        cmd =['git', 'show', f'HEAD:{filename}']
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

# ===============================
# 3. 抽样测速逻辑
# ===============================
def fast_ip_probe(ip_port, url_list):
    """抽取最多3个频道测试是否能下载视频流"""
    for test_url in url_list[:3]:
        start = time.time()
        try:
            r = requests.get(test_url, stream=True, timeout=4)
            if r.status_code == 200:
                down = 0
                for chunk in r.iter_content(1024*64):
                    down += len(chunk)
                    if down >= 1024*128:  # 成功下载 128KB 数据
                        return True, ip_port, f"  🟢 [顺畅] {ip_port:<21} | {round(time.time()-start,2)}s"
                    if time.time() - start > 5: break
        except: continue
    return False, ip_port, f"  🔴 [无流] {ip_port:<21}"

# ===============================
# 4. 运行主逻辑
# ===============================
if __name__ == "__main__":
    changed = has_data_changed(SOURCE_IP_FILE)
    should_trigger, current_count, is_forced = get_trigger_status(changed)

    if os.path.exists(SOURCE_NONCHECK_FILE):
        with open(SOURCE_NONCHECK_FILE, encoding="utf-8") as f:
            lines =[l.strip() for l in f if "," in l]
        if lines:
            # 1. 归集要测试的 IP 和 URL
            ip_map, url_map = {}, {}
            for line in lines:
                try:
                    url = line.split(",", 1)[1]
                    ip = url.split("/")[2]
                    if ip not in ip_map: ip_map[ip] = []; url_map[ip] = []
                    ip_map[ip].append(line); url_map[ip].append(url)
                except: pass

            live_print(f"::group::🎬 抽样测速 (共 {len(ip_map)} 个独立 IP)")
            valid_ips, logs = set(),[]
            
            # 多线程测速 (提升到 50 线程)
            with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
                futures =[ex.submit(fast_ip_probe, ip, u) for ip, u in url_map.items()]
                for f in concurrent.futures.as_completed(futures):
                    ok, ip, msg = f.result()
                    live_print(msg)
                    logs.append(msg.strip())
                    if ok: valid_ips.add(ip)
            
            live_print("::endgroup::")
            
            # ==========================================
            # 5. 重新拼装存活 IP 并写入 source-m3u.txt
            # ==========================================
            live_print(f"::group::💾 纯净版数据重组与归档")
            
            # 先写日志
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.write(f"服务器抽测报告 | 时间: {datetime.now()}\n" + "\n".join(sorted(logs)))
            live_print(f"  📝 成功覆写日志: {LOG_FILE}")
            
            # 核心修改：读取 RTP 模板进行重新组装
            if valid_ips:
                if os.path.exists(RTP_FILE):
                    with open(RTP_FILE, encoding="utf-8") as f:
                        rtps = [x.strip() for x in f if "," in x]
                    
                    final_m3u_lines =[]
                    for ip in sorted(list(valid_ips)):
                        for r in rtps:
                            name, r_url = r.split(",", 1)
                            suffix = r_url.split("://")[1]
                            final_m3u_lines.append(f"{name},http://{ip}/rtp/{suffix}")
                    
                    # 覆写 source-m3u.txt
                    with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
                        f.write("\n".join(final_m3u_lines))
                        
                    live_print(f"  📝 成功重组并覆写纯净版: {SOURCE_M3U_FILE}")
                    live_print(f"✨ 测速结束: 存活 {len(valid_ips)} 个 IP | 生成 {len(final_m3u_lines)} 条纯净链接")
                else:
                    live_print(f"  ❌ 找不到 RTP 模板 {RTP_FILE}，无法重组！")
            else:
                # 如果没有存活IP，清空文件
                with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f:
                    f.write("")
                live_print(f"  📝 存活 IP 为 0，已清空 {SOURCE_M3U_FILE}")
                
            live_print("::endgroup::")

    # ==========================================
    # 6. 联动处理
    # ==========================================
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
