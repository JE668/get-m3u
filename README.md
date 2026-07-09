# get-m3u

全自动 IPTV（udpxy）源发现与质量探测流水线。在广东电信网络内发现可用的 udpxy 代理服务器，探测其流媒体带宽，并生成标准 M3U 播放列表，自动触发下游仓库更新。

## 工作原理

```
GitHub Actions（每 3 小时）
  ├─ main.py   源发现：FOFA 情报 + RTP 模板 → C 段扫描 → 离线归属地校验 → 异步 UDPXY 指纹探测
  └─ probe.py  质量探测：变动检测 → 异步带宽测速 → 重组纯净 M3U
        ↓（CI 统一触发）
  ├─ JE668/m3u-checker-max
  └─ JE668/iptv-api
```

## 目录结构

| 路径 | 说明 |
|------|------|
| `main.py` | 源发现主程序 |
| `probe.py` | 质量探测与数据重组 |
| `utils.py` | 公共工具（日志 / 原子写入） |
| `ip2region/` | 离线 IP 归属地查询库（vendored，非 pip 安装） |
| `data/` | 发现库、端口统计、RTP 模板、ip2region 数据库 |
| `output/` | 成品：`source-ip.txt` / `source-m3u.txt` / `source-m3u-noncheck.txt` / `source-meta.json` / `log.txt` |
| `.github/workflows/main.yml` | CI 调度与编排 |

## 配置（GitHub Secrets）

| Secret | 用途 |
|--------|------|
| `FOFA_COOKIE` | FOFA 情报平台登录 Cookie，用于抓取初始 IP 段。未配置则跳过 FOFA 刮取 |
| `PAT_TOKEN` | 具有 `workflow` 权限的 GitHub Personal Access Token，用于触发下游仓库 workflow |

## 本地运行

```bash
pip install -r requirements.txt
export FOFA_COOKIE="..."   # 可选
python main.py             # 源发现
python probe.py            # 质量探测
```

## 输出文件

- `output/source-ip.txt`：存活服务器清单（`ip:port`）
- `output/source-m3u.txt`：标准 M3U，仅包含测速有流的纯净链接
- `output/source-m3u-noncheck.txt`：兼容格式（未做带宽校验）
- `output/source-meta.json`：每台服务器的测速带宽（Mbps）
- `output/log.txt`：本次抽测明细日志

## 依赖说明

- 运行时依赖仅 `httpx`（`requirements.txt`）。
- `ip2region` 为 **vendored** 依赖（源码在 `./ip2region`，数据库为 `data/ip2region.xdb`）。**请勿单独升级 `ip2region.xdb` 而不同步升级 Python 代码**，否则返回结构变化可能导致解析错误。CI 会自动更新 `.xdb` 数据文件。

## 下游联动

源发现与测速完成后，由 `.github/workflows/main.yml` 通过 `gh` CLI **统一触发**下游仓库（避免重复触发）：

- `JE668/m3u-checker-max`（`update.yml`，分支 `main`）
- `JE668/iptv-api`（`main.yml`，分支 `master`）

## License

见 `LICENSE`。
