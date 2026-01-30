import os, subprocess, json, time, concurrent.futures, requests
from datetime import datetime

SOURCE_M3U_FILE, LOG_FILE = "source-m3u.txt", "log.txt"
TARGET_REPO, TRIGGER_TOKEN = "JE668/iptv-api", os.environ.get("PAT_TOKEN", "")

def probe_stream(line):
    name, url = line.split(",", 1)
    start = time.time()
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", "-i", url]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, timeout=8)
        elapsed = round(time.time() - start, 2)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
            if v:
                res_str = f"{v.get('width')}x{v.get('height')}"
                bitrate = f"{round(int(data.get('format', {}).get('bit_rate', 0))/1024/1024, 2)}Mbps"
                return True, line, f"[{name}] {url} | 成功 | 延迟:{elapsed}s | 分辨率:{res_str} | 码率:{bitrate}"
        return False, line, f"[{name}] {url} | 失败 | 无有效视频流"
    except:
        return False, line, f"[{name}] {url} | 失败 | 探测超时"

if __name__ == "__main__":
    if not os.path.exists(SOURCE_M3U_FILE): exit()
    with open(SOURCE_M3U_FILE) as f: lines = [l.strip() for l in f if "," in l]

    print(f"🎬 开始 FFMPEG 深度探测 {len(lines)} 条链接...")
    valid_lines, logs = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(probe_stream, l) for l in lines]
        for f in concurrent.futures.as_completed(futures):
            success, line, log_msg = f.result()
            logs.append(log_msg)
            if success: valid_lines.append(line)

    with open(LOG_FILE, "w") as f: f.write(f"探测时间: {datetime.now()}\n" + "\n".join(sorted(logs)))
    with open(SOURCE_M3U_FILE, "w") as f: f.write("\n".join(sorted(valid_lines)))

    print("⬆️ 推送数据并触发联动...")
    os.system("git config --global user.name 'bot' && git config --global user.email 'bot@noreply.com'")
    os.system(f"git add source-ip.txt {SOURCE_M3U_FILE} {LOG_FILE} && git commit -m 'Update' && git push origin main")

    if TRIGGER_TOKEN:
        url = f"https://api.github.com/repos/{TARGET_REPO}/actions/workflows/main.yml/dispatches"
        requests.post(url, headers={"Authorization": f"token {TRIGGER_TOKEN}"}, json={"ref": "main"})
