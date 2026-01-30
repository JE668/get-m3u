import os, subprocess, json, time, concurrent.futures, requests
from datetime import datetime

SOURCE_M3U_FILE, LOG_FILE = "source-m3u.txt", "log.txt"
TARGET_REPO, TRIGGER_TOKEN = "JE668/iptv-api", os.environ.get("PAT_TOKEN", "")

def probe_stream(line):
    if "," not in line: return False, line, "无效行"
    name, url = line.split(",", 1)
    start = time.time()
    # 增加 ffmpeg 参数以提高成功率
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", "-i", url]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
        elapsed = round(time.time() - start, 2)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
            if v:
                res_str = f"{v.get('width')}x{v.get('height')}"
                br_raw = data.get('format', {}).get('bit_rate', 0)
                bitrate = f"{round(int(br_raw)/1024/1024, 2)}Mbps" if br_raw else "N/A"
                return True, line, f"[{name}] {url} | 成功 | 延迟:{elapsed}s | 分辨率:{res_str} | 码率:{bitrate}"
        return False, line, f"[{name}] {url} | 失败 | 无视频流"
    except Exception as e:
        return False, line, f"[{name}] {url} | 失败 | 探测超时或异常"

if __name__ == "__main__":
    print("🎬 脚本 probe.py 开始运行...")
    if not os.path.exists(SOURCE_M3U_FILE):
        print(f"❌ 错误: 找不到 {SOURCE_M3U_FILE}，探测终止")
        exit()

    with open(SOURCE_M3U_FILE, encoding="utf-8") as f: 
        lines = [l.strip() for l in f if "," in l]

    if not lines:
        print("⚠️ 警告: source-m3u.txt 内容为空，无需探测")
        exit()

    print(f"🎬 开始 FFMPEG 深度探测 {len(lines)} 条链接...")
    valid_lines, logs = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(probe_stream, l) for l in lines]
        for f in concurrent.futures.as_completed(futures):
            success, line, log_msg = f.result()
            logs.append(log_msg)
            if success: 
                valid_lines.append(line)
                print(f"   🟢 有效: {log_msg.split('|')[0]}")

    # 保存结果
    with open(LOG_FILE, "w", encoding="utf-8") as f: 
        f.write(f"探测时间: {datetime.now()}\n" + "\n".join(sorted(logs)))
    
    with open(SOURCE_M3U_FILE, "w", encoding="utf-8") as f: 
        f.write("\n".join(sorted(valid_lines)))

    print(f"✅ 探测完成，保留有效链接: {len(valid_lines)} 条")

    # 只有在有数据的情况下才推送和联动
    if valid_lines:
        print("⬆️ 推送数据并触发联动...")
        os.system("git config --global user.name 'github-actions[bot]' && git config --global user.email 'github-actions[bot]@users.noreply.github.com'")
        os.system(f"git add source-ip.txt {SOURCE_M3U_FILE} {LOG_FILE}")
        os.system("git commit -m 'Auto update validated IPTV source' || echo 'No changes'")
        os.system("git push origin main")

        if TRIGGER_TOKEN:
            dispatch_url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/main.yml/dispatches"
            r = requests.post(dispatch_url, headers={"Authorization": f"token {TRIGGER_TOKEN}"}, json={"ref": "main"})
            print(f"🚀 联动信号发送结果: {r.status_code}")
