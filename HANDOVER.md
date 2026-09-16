# 交接文档 / HANDOVER

> 生成时间：2026-09-13 13:40 (+08:00)　用途：换设备继续维护
> 仓库：`explore0-jpg/dlorder-cloud`（**私有**）　本地路径：`~/dlorder`
> 平台背景：温岭市第二中学食堂 ePay 点餐系统（mer_id `61266001`），本项目做"自动预演 + 周六统一抢订下周午晚餐"。

---

## 0. 当前状态快照（交接点）

- 本地 git：`main` 已全部推送到 origin（最新提交 `6c6ac51`）。
- 最近云端 runs：
  - `cloud-preview` 周五 21:00 北京：最近成功 ✅（2026-09-13 08:00Z 那次是手工触发）。
  - `cloud-grab` 每日 06:00 北京：最近几次"failure"，**属预期**——`notify.sh` 没配推送通道（BARK_KEY 未设）退出码 1 把 run 弄红，抢单逻辑本身正常。
  - `cloud-status`（新增，每 4h）：首次运行因 GitHub push 偶发 Internal Server Error 标红，已加 push 重试，待下一轮验证。
- **账号 token 交接时均处于失效态**（C900903，被外部登录顶号）：
  - self（梁明超）与 friend（毛佳豪）都需要重新绑一次（见 §5），否则周五预演/周六抢单会失败。
- 未完成（下一步）：多用户网页服务平台（已定方案未开工）、评分表填分授权、Bark 推送 key。

---

## 1. 架构总览

```
GitHub Actions（云端执行体，免费定时器）
 ├─ preview.yml   每周五 21:00（0 13 * * 5 UTC）→ cloud_order.py --preview
 │     算出下周每天每餐优选 → 写 .re/preview_cache.json / preview.md → git push
 ├─ grab.yml      每天 06:00（0 22 * * * UTC）+ workflow_dispatch
 │     → cloud_order.py [--date] [--dry-run]：周六统一扫 8 天窗口抢订
 ├─ status.yml    每 4h（0 20,0,4,8,12,16 * * *）→ cloud_order.py --status
 │     → 校验每个用户 token、抓窗口菜单+已订 → 写 .re/status.json → git push
 └─ 凭证来自 GitHub Secrets（见 §3）

本地脚本（~/dlorder，纯云端后不再依赖本地定时）
 ├─ cloud_order.py    云端无头抢餐主入口（--preview / --status / 默认抢单）
 ├─ auto_login.py     绑卡/重绑向导（SMS 验证码），支持 --user self|friend
 ├─ dish_score_table.py 拉某用户 N 天订单 → 荤/素按次数降序生成 Excel 评分表
 ├─ notify.sh         多通道推送：Bark(优先) > Server酱 > PushPlus；无通道退出码1
 ├─ epay.py/order.py/meal.py/history.py/dishes.py  点餐平台封装 + 评分 + 历史统计
 └─ users.cloud.json  每人配置：scores/blacklist/order_rules/delivery/prefer_overrides
```

数据流动：想改谁 → 改 `users.cloud.json` → commit+push → 下一次 Actions 拉最新 main 即生效。**手动预选/评分不在 GitHub 里改，改完要推送。**

---

## 2. 账号档案（非敏感部分）

| 用户 | 真实姓名 | 手机（收验证码） | 饭卡号 | 规则 |
|---|---|---|---|---|
| self | 梁明超 | 19330732884 | (config 未存，用手机短信) | 周六抢一窗，午+晚，min_score=1 |
| friend | 毛佳豪 | 13666836741 | 20241510 | 周六抢一窗，午+晚，min_score=0（等填分后改回1），use_history=false 不加历史权重 |

- 配置各字段含义：`grab_on_weekday=5`(仅周六) / `require_both_meals_weekday=[6]`(周日需午晚齐备) / `cancelled=[]`(退餐名单，永不重下)。
- 毛佳豪：180 天评分表已生成在旧设备 `/sdcard/Download/菜单评分表_毛佳豪_180天_全部.xlsx` 和 `_至少2次.xlsx`，等他填完分回来后导入 `scores`（填分后 min_score 调回 1）。

---

## 3. GitHub Secrets（凭证唯一存放地，不入文档/不入仓库）

| Secret | 含义 | 交接时状态 |
|---|---|---|
| CLOUD_UUID_SELF / CLOUD_TOKEN_SELF | self 的绑定凭证 | **已失效**，需重绑后重设 |
| CLOUD_UUID_FRIEND / CLOUD_TOKEN_FRIEND | friend 的绑定凭证 | **已失效**，需重绑后重设 |
| PUSHPLUS_TOKEN | PushPlus 推送（因实名要付费已弃用） | 保留占位 |
| BARK_KEY | iOS Bark 推送 key（**未创建**） | 待补 |
| SENDKEY | Server酱（**未创建**） | 待补 |
| （规划）REG_MASTER_KEY | 多用户方案的主密钥（未创建） | 二期 |

> ⚠️ 凭证（uuid/token）每次登录都换新，只有 GitHub Secrets 和旧设备 `.login_state.json` 有，**建议交接后重绑覆盖，别再依赖旧值**。任何新 token 都不要 commit。

---

## 4. 本地仓库说明

- `git remote origin` 用 https，`git config --global http.version HTTP/1.1`（绕 TLS/HTTP2 偶发失败）。
- push 偶发失败是 GitHub 端问题，重试即可；status.yml 已加重试。
- `users.json`（旧单用户）、`config.json`/`.login_state.json`/`*.log`、`.re/*`（除 preview/status 产物）都被 .gitignore 忽略；`users.cloud.json` 与 `.re/preview_cache.json|preview.md|status.json` 入库。
- 本地无 cron 依赖（`install_cron.sh`/run_grab.sh/run_preview.sh 是旧物），一切靠 Actions。

---

## 5. 新设备上恢复（照抄执行）

前置：Termux（或任何 Linux）安装 `git python3`，`pip install requests openpyxl cryptography`，装好 `gh` 并登录（`gh auth login`，让它能读写该私有仓库）。

```bash
git config --global http.version HTTP/1.1
gh auth login
git clone https://github.com/explore0-jpg/dlorder-cloud.git ~/dlorder && cd ~/dlorder
python3 -c "import cloud_order"          # 自检依赖齐全
gh secret list                            # 应能看到 §3 的条目
```

**重绑 self**（验证码短信发到 19330732884）：
```bash
python3 auto_login.py                              # 发短信，保存会话到 .login_state.json
python3 auto_login.py --code <收到的4位码> --gh     # 完成后自动更新 config.json 并同步 GitHub Secrets
```

**重绑 friend**（验证码到 13666836741，需毛佳豪本人）：
```bash
python3 auto_login.py --user friend \
  --percode 20241510 --realname 毛佳豪 --mobile 13666836741
python3 auto_login.py --user friend --code <4位码> --gh
```

**验证链路**（2026-09-19 是下个周六窗口）：
```bash
CLOUD_UUID_SELF=$(python3 -c "import json;print(json.load(open('config.json'))['client_uuid'])") \
CLOUD_TOKEN_SELF=$(python3 -c "import json;print(json.load(open('config.json'))['access_token'])") \
python3 cloud_order.py --date 20260919 --dry-run
```
（friend 凭证在 Secrets 里，本地跑 cloud 用不到；要本地跑 friend 需先从 Secrets 导出环境变量。）
也可以在 GitHub 手动触发：`gh workflow run grab.yml -f date=20260919 -f dry_run=true`，再 `gh run watch`。

**重跑一堆状态**：`gh workflow run status.yml`，等 1~2 分钟 `gh run watch`，确认 `status.json` 里两账号 token_ok=true 且已推送。

---

## 6. 常用操作速查

| 想做什么 | 命令 |
|---|---|
| 本地干跑某天 | `python3 cloud_order.py --date YYYYMMDD --dry-run` |
| 本地预演（不订） | `python3 cloud_order.py --preview` |
| 云端触发预演/抢单/状态 | `gh workflow run preview.yml` / `grab.yml -f dry_run=true` / `status.yml` |
| 看最近 runs | `gh run list --limit 8` |
| 看某次日志 | `gh run view <id> --log` |
| 生成某人评分表 | `python3 dish_score_table.py --user friend --days 180 --out ~/xx.xlsx` |
| 本地状态快照 | `python3 cloud_order.py --status` |
| 查域名/点餐 API | 见 `epay.py`（app.shop.* 接口，签名逻辑封装好了） |

---

## 7. 待办清单

1. **[紧急] 重绑 friend**（毛佳豪 13666836741），等她提供验证码。
2. **[配置] 补 BARK_KEY** 并在 `notify.sh` 验证（配好后 grab/preview 的 run 不再红）。
3. **[已完成] 多用户网页服务平台**：
   - `webapp/app.py`：注册（口令 `wlsdezx`）→ 登录 → 绑卡向导 → 评分/预选 → 管理员面板
   - 部署：`cd webapp && bash tunnel.sh`（Cloudflare Tunnel 免费公网）或 `bash run.sh`（本地）
   - 架构：VPS/Web 管人 + GitHub Actions 管单，凭证不入库
4. **[等待] 毛佳豪填好的评分表** → 导入 `scores`，min_score 0→1。
5. **[可选] 顶号自愈**：让登录界面引导用户自己重绑（已做进绑定向导规划）。

---

## 8. 安全与换机提醒

- 价值点不是代码而是：`users.cloud.json`（他人饭卡资料）+ GitHub Secrets（真实凭证）+ 口令 `wlsdezx`。换设备后：旧设备上的 `.login_state.json`、`config.json` 建议删除或妥善保管，别带入公共空间。
- 重绑会顶掉旧 token（单人单设备），所以"在哪里重绑都可以，但一台机器上一套凭证即可"。
- 多用户上线后：注册口令要能随时改、手机号打码显示、短信发送节流、会话过期、日志不落验证码。这些已在二期设计清单里。
- 如账号异常通知验证平台最新行为：登录即顶号 → 教学重点是"守住自己最后用的那台设备凭证"。