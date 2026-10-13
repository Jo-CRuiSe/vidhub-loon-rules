# Vidhub 的 Loon 直连规则自动更新

GitHub Actions 每天北京时间 **10:23** 扫描 `vidhub3.top`，发现新域名时提交更新。支持在 Actions 页面手动运行。已包含首次核查得到的 51 条规则，上线前即可使用。

仓库：[Jo-CRuiSe/vidhub-loon-rules](https://github.com/Jo-CRuiSe/vidhub-loon-rules)。

## 建立仓库并启用

1. 在 GitHub 创建一个空的 **公开仓库**，例如 `vidhub-loon-rules`，便于 Loon 通过 Raw 地址订阅。
2. 解压项目，将**本目录里面的文件**上传到仓库根目录，包含隐藏目录 `.github/workflows/`。不要把整个目录套在仓库内，也不要只上传 ZIP。工作流必须位于默认分支。
3. 打开 **Actions → Update Vidhub Loon rules → Run workflow**，执行首次检查。下载运行产物 `vidhub-scan-report` 查看结果。
4. 如果自动提交显示权限错误，检查 **Settings → Actions → General → Workflow permissions**，允许工作流读写。工作流使用 GitHub 内置 `GITHUB_TOKEN`，不需要填写个人 Token；分支保护也可能阻止直接提交。

也可以在本目录使用 Git 上传（替换仓库地址）：

```sh
git init -b main
git add .
git commit -m "Add daily Vidhub Loon rule updater"
git remote add origin https://github.com/Jo-CRuiSe/vidhub-loon-rules.git
git push -u origin main
```

## 在 Loon 订阅

将下面一行加入现有配置的 `[Remote Rule]`，本仓库地址如下：

```ini
https://raw.githubusercontent.com/Jo-CRuiSe/vidhub-loon-rules/main/rules/Vidhub.list, policy=DIRECT, tag=Vidhub直连, enabled=true
```

使用自动分流/规则模式。首次添加后刷新规则，之后按 Loon 的规则订阅刷新机制获取更新；GitHub 提交更新不会直接把新规则推到手机。其他本地或插件规则可能优先于订阅规则，请检查请求记录实际命中的策略。

`rules/Vidhub.list` 是**不带策略**的订阅文件，策略由上面的 `policy=DIRECT` 指定。`rules/Vidhub-DIRECT.txt` 是带 `DIRECT` 的本地规则片段，可粘贴到 `[Rule]` 段靠前位置。两者都不是完整 Loon 配置。

## 每天检查什么

- 读取首页资源，并选择 8 部固定样本和 12 部按日期轮换的首页影片。
- 读取每部影片的各条播放线路，抽查首集和最新集；每次最多 160 个播放页。
- 读取站内嵌入播放器中的 JSON 配置，提取视频及图片域名，兼容旧版 `player_*` 配置。
- 每个视频 CDN 读取一个 m3u8 样本，最多跟随 3 层播放清单，提取清单引用的分片、字幕、密钥和跳转域名。**不下载视频分片、不执行网站脚本**。
- 新域名使用完整主机名 `DOMAIN` 规则，已有的 20 个 CDN 后缀规则保持不变；历史域名累计保留，不会因某一天的样本未出现就删除。
- 主站请求失败、返回验证页，或播放配置成功率不足 50% 时退出失败，保留既有规则。CDN 清单返回 403 时，保留播放器确认的域名并在报告中记下问题。
- 只有规则或域名登记表变化时提交；每次运行的时间、请求异常和扫描统计写入 Actions 摘要及产物，避免无变化时每天提交。

规则使用共享 CDN/图片域名，因此其他网站访问同一域名时，也会匹配直连。该扫描是逐日抽样，不能保证覆盖所有影片、线路、未来域名、分片 HTTP 重定向或运行时 JS 动态构造的地址。

## 参数与本地运行

修改 `config.json`：`base_url` 可改为新的站点地址；`seed_movies` 是固定影片 ID；其他字段限制影片数、播放页数、清单数和并发数。默认并发为 2，使用请求超时、重试和体积上限。

需要 Python 3.12 和 curl，Python 无第三方依赖：

```sh
python3 -m unittest discover -s tests -v
python3 scripts/update_rules.py
```

仅从已有登记表重新生成文件：

```sh
python3 scripts/update_rules.py --render-only
```

GitHub 托管运行器的网络环境与手机不同。Cloudflare 验证、站点换域名或解析结构变化可能让任务失败，应以 Actions 的实际运行结果为准。当前项目已本地测试；远端检查结果见 [Actions](https://github.com/Jo-CRuiSe/vidhub-loon-rules/actions)。

GitHub 定时任务可能延迟；公开仓库连续 60 天无活动时，GitHub 会暂停定时工作流，可从 Actions 页面重新启用。这里没有添加空提交来维持活跃。

参考：[GitHub schedule 文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)、[Loon 订阅规则](https://nsloon.app/docs/Rule/sub_rule/)、[Loon 规则优先级](https://nsloon.app/docs/Rule/)。
