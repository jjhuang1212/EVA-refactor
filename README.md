# EVA

企业关系交叉穿透、访谈录音转写与纠错、尽调报告辅助生成。用户通过企微客服（或本地终端、模拟器）和一个自主 agent 对话，agent 用工具读文件、转写、查企业数据、联网搜索、在沙箱里跑 Python，并交付文件。

## 环境

| 项 | 要求 |
|---|---|
| Python | 3.10（conda 环境 `py310`），不要用 3.11+ 的特性 |
| 依赖 | `pip install -r requirements.txt`；跑测试用 `pip install -r requirements-dev.txt`（含前者） |
| LibreOffice | 解析老式 doc/xls/ppt 用，`soffice` 在 `PATH` 里即可，代码自己找。开发机验证过 26.8；服务器在 `eva-parse` 镜像里，宿主不装 |
| Tesseract | 图片和扫描页 OCR 用，在 `PATH` 里即可，需带 `chi_sim`、`eng` 语言包。开发机验证过 5.5；服务器同在 `eva-parse` 镜像里 |
| Pango（GTK 运行库） | 只有 Windows 开发机需要：`backend: local` 时报告 skill 在本机出 PDF，WeasyPrint 要加载 Pango、GLib 等 DLL，缺了 `save_pdf` 报"本机缺少 WeasyPrint 依赖的系统库"，模型会改出 Word。装法见下。服务器沙箱镜像自带，宿主不装 |
| SQL Server | 会话日志、账本、文件登记 |
| MinIO | 原始文件、解析缓存、调用记录 |
| Redis | 只有企微和模拟器需要，终端和评测不需要 |
| Docker | 只有沙箱后端用 `docker` 时需要（服务器）。开发机默认 `backend: local` |

凭证全部走环境变量，写在项目根的 `.env`（已被 git 忽略），不要写进代码、日志或文档。仓库里的 [.env.example](.env.example) 是模板，列出了全部变量名和各自的用途，不含真实值。首次配置：

```powershell
Copy-Item .env.example .env   # 然后在 .env 里填值
```

需要的变量：

- 数据库：`DB_SERVER`、`EVA_DB_USER`、`EVA_DB_PASS`、`EVA_DB_NAME`
- 旧库只读账号（可选，只有 `legacy_import` 导入旧数据时用）：`LEGACY_DB_USER`、`LEGACY_DB_PASS`
- 对象存储：`MINIO_ENDPOINT`、`MINIO_ACCESS_KEY`、`MINIO_SECRET_KEY`
- Redis：`EVA_REDIS_HOST`、`EVA_REDIS_PORT`、`EVA_REDIS_DB`，可选 `EVA_REDIS_PASSWORD`
- 模型：`QWEN_API_KEY`、`QWEN_BASE_URL`、`DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`
- 录音转写：`ASR_API_KEY`、`ASR_BASE_URL`
- 联网搜索：`ZHIPU_API_KEY`、`ZHIPU_BASE_URL`
- 企业数据：`QCC_API_KEY`
- 企微（只有真实企微服务需要）：`WECOM_CORP_ID`、`WECOM_KF_SECRET`、`WECOM_CALLBACK_TOKEN`、`WECOM_ENCODING_AES_KEY`
- EVA测评系统登录密码：`EVA_DEVTOOLS_PASSWORD`

缺哪个变量，启动时会报 `CONFIG_INVALID`（数据库、对象存储、Redis 缺变量报 `STORAGE_ERROR`），并列出缺的名字。新增或删除环境变量时同步改 `.env.example`。

Windows 上输出中文乱码时先设 `PYTHONIOENCODING=utf-8`。本机的 LibreOffice、Tesseract 不在默认 `PATH` 时，先把它们的安装目录（LibreOffice 是 `program` 子目录）加进 `PATH`。

```powershell
conda activate py310
$env:PYTHONIOENCODING = "utf-8"
```

Windows 上要出 PDF，按 [WeasyPrint 官方文档](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#windows)装 Pango：

```powershell
# 1. 装 MSYS2（https://www.msys2.org），在 MSYS2 MINGW64 终端里(本例装在D:\DownLoadTools\MSYS2)：
pacman -S mingw-w64-x86_64-pango mingw-w64-x86_64-gdk-pixbuf2 mingw-w64-x86_64-fontconfig

# 2. 配置用户环境变量（Windows PowerShell，普通窗口即可，改完重开终端）
[Environment]::SetEnvironmentVariable("WEASYPRINT_DLL_DIRECTORIES", "D:\DownLoadTools\MSYS2\mingw64\bin", "User")
[Environment]::SetEnvironmentVariable("FONTCONFIG_FILE", "D:\DownLoadTools\MSYS2\mingw64\etc\fonts\fonts.conf", "User")

# 3. 新开 PowerShell，激活环境后验证（不报错就行）
conda activate py310
python -c "import weasyprint; print('ok')"

# 4. 实际生成 PDF（务必在可写目录，如桌面，别在 system32 下跑）
cd $env:USERPROFILE\Desktop
python -c "from weasyprint import HTML; HTML(string='<h1>测试</h1><p>Hello</p>').write_pdf('test.pdf'); print('pdf ok')"
```

配置都在 `configs/`，阈值、配额、预算、模型和单价不写在代码里。

## 启动

三个入口用的是同一套 agent 核心，区别只在渠道。**都会真调模型，产生费用。**

### 终端（console）

```powershell
python -m eva.app console
```

读一行输入当作用户消息，打印工具调用过程和回复。

- `/upload <文件路径>`：上传文件，之后在消息里说明要做什么
- `/exit` 或 `/quit`：退出
- 回复期间按 Ctrl+C 是取消当前轮（在步骤边界收尾），等输入时按是退出

日志只写文件（目录见 `configs/logging.yaml`），不和对话界面混在一起。

### 企微客服（真实服务）

```powershell
python -m eva.app wecom
```

起一个 Flask 回调服务（地址和端口见 `configs/wecom.yaml`，路径 `/wechat/callback`），消息处理在主线程。需要 Redis 和企微的四个变量。企微硬限制：用户发消息后 48 小时内可回复，且每个窗口最多 5 条，所以长回复会合并成文件。

### 企微模拟器

```powershell
python -m tools.wecom_sim
```

浏览器打开 <http://127.0.0.1:5001>。真的部分：企微 Worker、Redis 事件队列和游标、agent 核心、模型、数据库、MinIO、工具。模拟的部分：企微的收消息、素材下载和发消息，不验签，不走回调。队列用单独的前缀 `eva_sim`，不会被真实服务取走。只监听本机，没有鉴权。模拟用户的渠道用户 ID 固定以 `sim_` 开头（前缀页面上固定显示，后缀可手写或随机生成），同一个 ID 在同一个客服号下始终是同一个租户，换客服号是另一个租户；不带 `sim_` 的 ID 一律被拒，所以碰不到真实用户。模拟器按企微的 5 条配额计，所以用来验证窗口和投递行为；要多轮长对话，用终端或评测更方便。页面上的对话在内存里，重启后从 `channel_messages` 表还原（渠道实际收发的消息，含文件，回复下的统计行除外）；这张表只用来重建对话和排障，主流程不读它。

## 可观测：回放一个会话

每次模型调用（含重试的每一次）都会留一条调用记录，存在 MinIO，开关是 `configs/model.yaml` 的 `call_record`（默认开）。回放工具读这些记录，把一个会话的每一轮、每次模型请求和回复、思考、工具往返、费用打出来。只读，不花钱。

```powershell
python -m devtools.replay <tenant_id> <session_id>
python -m devtools.replay <tenant_id> <session_id> --full   # 正文不截断，并打印 system prompt 和 tools 全文
```

- `tenant_id` 和 `session_id`：评测结果里有，租户编号以用途开头：终端 `dev_`、评测 `eval_`、模拟器 `sim_`、真实企微用户 `usr_`，企微的租户见日志。
- 不经模型调用层的调用（录音转写、智谱搜索、企查查）没有调用记录，只在账本里有费用，回放里会标注，不算缺失。
- 排查提示词或流程问题时，先看模型每一步实际收到了什么、回了什么，再改。
- 调用记录里的提示词按内容哈希存放，结果里带 `code_version`（git 短哈希，有未提交改动时带 `+dirty`），可以追到当时的代码。

## 评测：改完提示词或流程，一条命令看结果

评测用的是和终端同一个 agent loop，不 mock 模型，不开旁路，所以结果反映真实行为。**每次运行都会产生费用**，跑完按账本汇总。

### 案例

一个案例是 `devtools/eval/cases/<名字>/` 目录：`case.yaml` 加样本文件。只有 `case.yaml` 进 git，样本被忽略（可能含客户资料）。

```yaml
message: 请转录并整理这段访谈录音，信息见我发的信息文件。
files:                      # 相对案例目录，必须存在
  - 段静访谈信息.md
  - 段静_财务负责人.mp3
asserts:                    # 可选，写了才判
  reply_contains: [聚石]    # 最终回复包含这些字符串
  max_steps: 40             # 总步数上限
  max_cost_cny: 1.0         # 总费用上限（元）
  max_questions: 1          # agent 中途最多可以向用户提问几次；0 表示这个案例不该提问
```

案例里不写 agent 提问时该怎么答（问法会随提示词改动而变，写死的回答对不上）。agent 中途提问时怎么办，由运行时选：**不交互**，这次运行就此停下，判为不通过（命令行只有这一种）；**人工回答**（网页），问题出现在评测页的进度里，你回答后运行继续，等太久（`answer_timeout_seconds`，在 `devtools/web/config.yaml`）则判不通过。**模型扮演用户**（网页）：由模型按提示词和案例里可选的 `persona`（补充信息：用户是谁、手上有什么事实，没写的它会说不清楚）替你回答，最多回答 `max_sim_rounds` 次（都在 `devtools/eval/config.yaml`，默认提示词也在那里，开跑时可以只对这一批改写）。模拟用户的调用经同一个模型调用层，用途记 `user_sim`，挂在它自己的会话上：不占 agent 会话的预算，也不算进案例的 `max_cost_cny`，结果里单列「模拟用户费用」。实际用的模拟用户提示词也进输入版本号。提问次数记在结果里，用 `max_questions` 判。

新增案例：建目录，放样本，写 `case.yaml`，然后直接运行。

### 运行

```powershell
python -m devtools.eval devtools/eval/cases/my-case
python -m devtools.eval devtools/eval/cases/my-case --runs 3 --label after-v2 --tenant demo
python -m devtools.eval devtools/eval/cases/案例A devtools/eval/cases/案例B --runs 2   # 多个案例按顺序跑，每个各跑 2 次
```

- `--runs N`：同一案例跑 N 次，每次一个新会话。
- `--label 标签`：给这组运行起名，之后按标签对比。只能含字母、数字、`_`、`-`，最长 40 个字符。
- `--tenant 名字`：评测租户的渠道用户 ID 是 `eval_<名字>`，同名解析到同一个租户；不给就按时间生成 `eval_YYYYMMDD-HHMMSS`。评测租户的编号带 `eval_` 前缀，指不到真实租户。

输出每次运行是否通过、结束原因、步数、账本里按用途和模型分项的费用、每条断言、多轮对话的每一轮、交付的文件。全部通过时退出码为 0。

**租户同时是缓存范围**：同一租户内，相同内容的文件复用解析和转写缓存。要在同一份转写上比较两版提示词，两组都用同一个 `--tenant`；要强制重新转写，换一个名字或不给。

每次运行的详情写在 `devtools/eval/out/<案例>/<run_id>/`（`result.json` 和交付的文件，被 git 忽略）。出问题时用 `result.json` 里的 `tenant_id` 和 `session_id` 去回放。

### 对比两组

```powershell
python -m devtools.eval.compare my-case before-v2 after-v2
```

只读 `result.json`，不重跑、不花钱。列出两组的运行次数、通过率、步数和费用的最小/中位/最大、结束原因、代码版本，以及每条断言的通过次数，通过率变了的标 `← 变化`。两组代码版本相同时会提示：差别只来自模型每次的随机性。

典型流程：

1. 改之前，用 `--label before --tenant t1` 跑 3 次。
2. 改提示词或流程。
3. 用 `--label after --tenant t1` 跑 3 次（同一租户，复用转写缓存，只比较改动的影响）。
4. `python -m devtools.eval.compare <案例> before after`。

### 限制与注意事项

- **花钱**：每次运行都真调模型。案例里的 `max_cost_cny` 在运行中就盯着：每隔 `devtools/eval/config.yaml` 里的 `cost_poll_seconds` 查一次已结算金额，超了就让这一次在步骤边界停下（只停这一次，后面的案例照常跑），这一次判不通过。停在步骤边界，实际花费可能略超上限；读不到账本也会停下这一次并记错误码。每个会话另有预算上限，在 `configs/model.yaml`，到上限会停。
- **单次不代表稳定**：模型每次输出不同，判断改动好坏要用 `--runs 3` 以上再对比。
- **只评整体**：评的是从用户消息到交付的全过程，不评单个工具。评测走终端渠道，企微的窗口和条数限制不在评测范围，那部分用模拟器验证。
- **样本不进库**：样本被 git 忽略，换机器要手动拷。
- **判定只有三种**：回复包含指定词、步数上限、费用上限。回复内容对不对仍要人看回复和交付文件。
- **改了就生效的范围**：`configs/system_prompt.md`、`skills/`、`configs/*.yaml` 改完，下一次运行就会读到，不用重启。`eva/` 下的 Python 代码，命令行评测每次是新进程，网页服务要重启。
- **运行期间不要改上面这些文件**：提示词每个 turn 都重读，同一次运行前后可能用了不同版本。
- **代码版本只是参考**：`code_version` 带 `+dirty` 时，只说明有未提交改动，两次都带 `+dirty` 不代表内容相同。
- **同时开别的服务**：评测可以和终端、企微、模拟器同时开（评测租户带 `eval_` 前缀，不用 Redis），但共用同一批模型接口的限流，沙箱并发上限（`configs/sandbox.yaml`）是每个进程各算各的。

## EVA测评系统

回放、评测、对比、标注和改提示词都能在网页上做，和命令行用的是同一套代码。

```powershell
$env:EVA_DEVTOOLS_PASSWORD = "<自己定一个>"
python -m devtools.web
```

浏览器打开 <http://127.0.0.1:5002>，用上面的密码登录。默认只听本机（见 `devtools/web/config.yaml`），没设密码变量会拒绝启动；登录有效期、连续输错的锁定时间、三个列表的每页条数（`tenants_limit`、`sessions_limit`、`results_limit`）也在这个配置里。页面不依赖外部库，没有构建步骤，可以离线用，深浅两套主题（左下角图标切换，没选过时跟随系统）。

| 页面 | 做什么 |
|---|---|
| 会话 | 按用途（全部、真实用户、模拟器、终端、评测）、租户前缀或搜索词找租户，进会话列表，再进回放：每轮、每次模型请求和回复、思考、工具往返、费用；记录缺失、失败尝试和提示词变化会标出来。回放默认新→旧（最新一轮在最上），可一键切正序；支持按关键词和事件类型查找、一键折叠/展开回合。除评测外的租户只读，页面上有标注 |
| 评测 | 列出案例，可新建案例（样本文件、判定词、步数、费用和提问次数上限）；多选案例，设每个案例跑几次、租户名、标签和 agent 提问时的处理方式（不交互、人工回答或模型扮演用户）后开跑；进度用服务端推送实时更新（不用轮询），需要时在进度里回答 agent 的提问，可取消；可设「本次覆盖」（见下） |
| 结果 | 按案例看运行记录；详情页有断言、最终回复、费用分项、交付文件下载、回放链接；可标人工判定，可把这次用的那一版输入认可成命名版本 |
| 对比 | 选案例和两个标签（自动列出该案例所有已用过的标签，点一下填入），看通过率、步数和费用范围、断言变化，以及两组输入（提示词、skill、配置）的差异；没有可对比的数据时会引导去评测页 |
| 版本 | 认可过的版本，及它名下的人工标注统计 |
| 文件 | 直接改工作区里的 `configs/system_prompt.md`、`configs/` 顶层的 `.yaml` 和 `skills/` 下的文本文件；保存前可展开「改动预览」看逐行差异（绿增红删） |
| 帮助 | 页面内的使用说明：几个常用词，和"看对话""验证提示词改动""打分与认可版本"三条常用流程；其余页面顶部也各有一句提示 |

几个页面交互的细节：

- **回放顺序**：默认新→旧（回合级倒序，最新一轮在最上，但每轮内部仍是"用户提问 → 模型回复"的正序）。点顶部按钮可在倒序（新→旧）和正序（旧→新）间反复切换，选择记在 URL 参数里，刷新保持、链接可分享。关键节点卡片跟随排序方向，点击跳转照常可用。
- **回放查找与折叠**：顶部搜索框按关键词实时过滤，旁边的类型下拉可只看某类事件（用户消息、模型回复、工具结果、闸门、错误、调用等），显示命中数；「全部折叠 / 全部展开」一键收起或铺开所有回合。命中被折叠的内容时对应回合会自动展开。这些只影响显示，不影响耗时、最终回复等判定。
- **列表分页**：租户、会话、结果三个列表都翻页，底部有「上一页 / 第 N 页 / 下一页」，每页条数见 `devtools/web/config.yaml`。数据不足一页时不显示分页控件。
- **进度推送**：评测进度用 SSE（`GET /api/runs/stream`）实时推送，状态变化才推一条，任务结束自动断开，换页时关闭连接、断线由浏览器自动重连，比定时轮询更即时也更省请求。

**评测任务**同一时刻只允许一个，已有任务在跑时再开会被拒绝。取消只影响"不再开始新的一次"，进行中的这次在步骤边界收尾。评测接口 `POST /api/runs` 的请求体是 `{"cases": ["案例A", "案例B"], "runs": 2, "tenant": "可选", "label": "可选"}`，案例按列表顺序跑，每个各跑 `runs` 次；有一个案例不存在就整体不跑。

**本次覆盖**：开跑时可以只对这一批运行改思考和系统提示词，不碰磁盘上的文件，请求体里的 `overrides` 字段：`think_first_steps`（每个回合的前几步让模型思考，0 是不主动要求）、`think_after_tools`（只有上一步调用了这些工具里的任一个，这一步才接着思考，空列表是不启用）、`max_steps`（每个回合最多几步，到了就收尾）、`system_prompt`（整段系统提示词，skill 清单照常附上）。覆盖项并进输入版本号，所以对比页会把它当作一处输入差异显示；运行结果里记着用了什么。运行详情页的「采用到真配置」把覆盖写回 `configs/loop.yaml` 和 `configs/system_prompt.md`（只改对应的那几行，保留注释和换行风格）；这两个文件在那次运行之后被改过就拒绝。备选模型 `deepseek-flash` 的思考无法开关，切到它时思考覆盖不起作用。

**改文件**的规则：

- 保存时要带打开时看到的内容哈希。磁盘上的文件在这之后被改过（比如在 IDE 里），保存会被拒绝并提示重新读取，你在页面上改的内容不会丢，可以先复制出来。
- 评测在跑时不能保存，因为评测每个 turn 都重读这些文件，中途改会让同一次运行前后用了不同版本。
- 保留原文件的换行风格和 BOM；`.yaml` 保存前先检查语法，有误不写入。
- 新文件只能建在 `skills/` 下；不是 UTF-8 文本的文件、符号链接和白名单之外的路径不显示也不能改。
- 改的是真实工作区，没有撤销。改之前想留底就先 git 提交，或先把这一版在结果页认可成命名版本。
- 每次评测运行会存下当时的这些文件，运行期间被改动会被判不通过，对比页能看到两组输入的差异。

## 测试

```powershell
python -m pytest tests/ -v
```

`tests/` 被 git 忽略，只在本机。

## 目录

| 目录 | 内容 |
|---|---|
| `eva/` | 核心：循环、模型调用层、工具、hook、存储、沙箱、解析、渠道 |
| `eva/app.py` | 装配层，`python -m eva.app` 的入口 |
| `configs/` | 全部配置和系统提示词 |
| `skills/` | agent 按需加载的 skill |
| `tools/wecom_sim/` | 企微模拟器 |
| `devtools/` | 回放、评测和 EVA 测评系统后端，开发用，可整体拆走，核心不依赖它 |
| `sandbox_image/` | 沙箱镜像构建 |
