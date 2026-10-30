# bridge/：桥接实验代码（草稿 v4，未封存，2026-10-19 晚）

对应 `docs/prereg/2026-10-18_bridge_prereg_DRAFT.md` v4（四臂）。v4 之前的代码快照：`docs/_snapshots/bridge_code.pre_2026-10-19_v4/`（身份 100cc6bf…，60/60）。**预登记封存前，不在任何样本题上运行。**
已封存的测量代码 `d1/` 只导入、不复制、不修改；d1 代码身份（d1/*.py 文件名加字节的 sha256）必须等于
`ce9fe5e4a6282975e696eb2ecdd7be113d7b1464a5acee1d087bee52109593c3`，驱动、评测与判读启动时都会核对，不一致就停。

| 文件 | 作用 |
|---|---|
| `prompts.py` | 四个臂的文本 I0、B1、G、N0、B0，与草案 §2 逐字一致 |
| `agent.py` | `BridgeAgent`，继承 mini-swe-agent 2.1.0 的 DefaultAgent：附加各臂文本；逐轮记录显示的预算数与计数器；在 `step()` 内部（容器还活着的时候）为提交、到限、出错打快照；记录先触发的是步数上限还是美元上限；支出停止线（按每次响应的 API 用量累计，含格式错误轮） |
| `model_log.py` | `LoggedLitellmModel`：逐次调用记录 `response.model` 与原始 `modelVersion`（经 litellm 的 logger_fn）、时间、响应 id、usage、成本；**计价保护**（v4 §7.3）：对照 `pricing_registry.json` 判定计价状态，非 OK 时成本记未知、记临时预留、抛 PricingUnavailable |
| `outcome.py` | 七类终止原因、每类的 S/H 取法，以及 `score()`：由 judge 裁决算 S、H、W，未知向下传递（§4）；盲交启发式 `blind_submit()`（只作描述） |
| `snapshot.py` | `DockerSnapshotter` 在台式机上用已封存的 F（`d1/extract.py`，F-v6）取补丁；`LocalSnapshotter` 只给离线测试用 |
| `spend.py` | 全局支出账本（加文件锁）：已定价支出、临时预留、停止线、计价暂停；干跑与正式账本分开；作者对账清除暂停的命令行 |
| `run_one.py` | 跑单条轨迹。核心 `run_trajectory()` 与 Docker 无关；`main()` 在台式机上接 DockerEnvironment 与真模型。写 `record.json`、`traj.json`、`calls.json`、`submission.patch`、`F_end.patch` |
| `evaluate.py` | 评测：把提交补丁与截止时的 F 交给已封存的 `d1/judge.py`，算 S、H、W，原子写 `result.json`（带身份字段） |
| `batch.py` | 批量驱动：按哈希顺序交错各臂；ENG_FAIL 至多重跑 1 次并留历史；账本；按身份续跑；支出停止线；计价暂停；连续 3 次 ENG_FAIL 暂停；周账本上限；不拉镜像；正式批次要求登记表已确认 |
| `analysis_rules.py` | v4 §4–§6：S/H 与 W 分开的最不利填补；共用重抽样的边际区间；R-a；Q1（过半/不到一半）；Q2（正/负，带符号检验保护）；W 跨口径稳健性；严格版；R-c、R-d 三档；熔断与保守附表；措辞义务 flags；R-e 全组合 |
| `analyze.py` | 判读入口：读 `result.json`，调 `decide()`；每个分支允许的措辞（CLAIMS）；各差值包络（ΔW 主比较 BATS−Silent）；描述统计（含计价失败、资源中止、未运行、盲交、W 未知率）；版本分层；漂移检查（`--public-entry`） |
| `pricing_registry.json` | 价目登记表（**不计入代码身份**，sha256 另记进每条轨迹与账本）；正式批次要求 `confirmed_by_author: true` |
| `tools/probe_pricing.py` | 干跑前的最小计价验证：1 次真实调用，打印服务端版本串、原始 modelVersion、用量、计价状态，并记入干跑账本（离线已用假 API 测过两种情形） |
| `tools/pull_bridge.py`、`tools/delete_images.py` | 方案 B 的下载与删除（作者 2026-10-20 批准）：新磁盘停止线（D 盘剩余 + 虚拟盘内可复用 − 预留 ≥ 100 GB；WSL 剩余 − 预留 ≥ 80 GB），新增压缩 ≤ 35 GB；限流每 30 分钟重试；删除只按清单、不强制、有容器运行或碰到桥接镜像即拒绝。测试 `tests/test_tools.py` 6/6 |
| F-b1（`snapshot.py`、`run_one.py`、`evaluate.py`、`analyze.py`；`tools/check_fb1_docker.py`） | 次要敏感性产物（作者 2026-10-20 确认）：F-v6 先取并原样保留；只在 F-v6 因残留进程无效时终止进程并重取；只去掉新增的非 UTF-8 文件；只在 F-v6 不可用时评测，记 `H_b1`、`W_b1`；判读另出 `F_b1_sensitivity`。测试 `tests/test_fb1.py` 5/5 |
| `tools/make_tasks_meta.py` | 从本地缓存的 Verified 生成 `--tasks-meta` 并与 d1 的 base_commits 交叉核对（不计入代码身份） |
| `identity.py` | 代码身份（bridge 与 d1）、原子写、账本、受检的 d1 导入（d1 与 bridge 都有 `batch.py`，所以 d1 不长期留在 `sys.path` 上，也不往 `d1/__pycache__` 写字节码） |
| `config_public_gemini3flash.json` | 从公开轨迹里冻结下来的 system 模板、instance 模板、model 和 environment 配置 |

**bridge 代码身份** = bridge/ 顶层 `*.py` 与 `*.json` 的文件名加字节的 sha256（不含 tests/、tools/、本 README 和 `pricing_registry.json`）。它包含 `analyze.py`，
所以封存后改任何 bridge 文件都会改变哈希；判读时用 `--expect-bridge-code` 传入运行时记录的封存值。

## 一条轨迹的数据流（台式机）
1. `batch.py` 按顺序取一槽（题 × 臂），核对支出线、周账本、本地镜像 Id；以子进程用 mini 的虚拟环境跑 `run_one.py`。
2. `run_one.py`：BridgeAgent 运行；停下的那一刻在 `step()` 内取 F；销毁容器；分类；写 `record.json`。
3. `evaluate.py`（在驱动进程内，用装了 swebench 5.0.2 的 Python）：从落盘产物重新分类并核对 sha256；按需要判提交补丁
   （S）与 F（H 与 W），同一字节只判一次；写 `result.json`。
4. ENG_FAIL（非资源中止）且是第一次：整个目录移入 `attempts/`，立即重跑一次；但若支出账本处于计价暂停，或已连续 3 次 ENG_FAIL，整批先停下，续跑时再重跑。
5. 全部结束后在 Mac 上：`python3 -I bridge/analyze.py --results OUT --selection docs/prereg/2026-10-18_bridge_selection_DRAFT.json --expect-bridge-code SHA --public-entry data/d1_census/raw/20260217_mini-v2.0.0_gemini-3-flash-high.json --out report.json`。

**S/H/W 取法**（`outcome.score`）：SUB_NONEMPTY：S=H=判提交补丁，W=判 F；SUB_EMPTY：S=H=0，W=判 F；CAP_STEP_DIFF、
CAP_COST、FORMAT_EXIT：S=0，H=W=判 F；CAP_STEP_NODIFF：S=H=W=0；ENG_FAIL：全未知。F 取不到（不支持或快照无效）：
用到 F 的 H 与 W 记未知，S 照常；judge 返回未知或抛异常：只有那一个裁决记未知。

## 已验证（Mac 上的离线测试，不联网、不用 Docker、不调 API；2026-10-19 晚，v4 草稿）
**离线测试通过不等于真实环境验收完成**；真实验收见 prereg v4 §7.5 的干跑清单。

| 命令 | 结果 | 覆盖 |
|---|---|---|
| `python3 -I bridge/tests/test_analysis_rules.py` | 15/15 | v4 规则层反例：两问同时成立；S 正 H 负的反转；总收益接近零；两层反向；未知集中在 BATS 撞限轨迹且各臂不同；单臂超熔断；零事件只走精确界；少事件不判过半；三臂与一臂未运行；W 只在 H 口径成立与 W 熔断；严格版只报告；Q2 的符号检验保护；R-e 全组合且无“很少”档；Bonferroni 选项 |
| `python3 -I bridge/tests/test_analyze.py` | 14/14 | 从 result.json 到报告：两问同时成立的措辞；反转措辞限定评分规则；H 下为负但 W 不成立；接近零；W 未知单独计；缺失槽位计未知、熔断、保守附表；未知集中在撞限轨迹；计价失败与资源中止计数且分母不变；漂移检查；身份不符即停；版本变化按版本分层；登记表中途被改即标记；三臂 |
| `python3 -I bridge/tests/test_evaluate.py` | 14/14 | 原 13 项 + 盲交启发式 |
| `python3 -I bridge/tests/test_batch.py` | 21/21 | 原 11 项 + 连续 ENG_FAIL 暂停、作废连续失败段后不丢槽位、成功后清零、计价暂停在开跑前生效、干跑与正式账本分开、评测异常后只补评测不重跑 agent、移动中途崩溃只计一次、本地对账（一致/重复/金额不符/孤立/漏记/预留未结清/总额不符；有差异即停，结清后续跑） |
| `uv run --no-project --with mini-swe-agent==2.1.0 python bridge/tests/test_bridge_agent.py` | 9/9 | 未改 |
| `uv run --no-project --with mini-swe-agent==2.1.0 python bridge/tests/test_end_to_end.py` | 12/12 | 原 6 项（未定价版本串改走 legacy 路径保留）+ 已登记版本定价并保留原始 modelVersion、未登记版本不按 0 计、缺 usage 不按 0 计、litellm 静默按基础价定价的 `-0930` 被拦下、只改登记表即可让 litellm 不认识的替代版本继续（代码身份不变）、批量计价暂停→清除对账→只重跑 1 次 |

⟦2026-10-20 补记 6：A、B 复查后修了 B 的两处，测试共 138 个，Mac 与台式机全部通过；代码身份 d1da6e7a…，见补遗草稿 §12⟧ ⟦2026-10-20 补记 5：闭环复核 A、B 修复后，测试共 136 个，Mac 与台式机全部通过；代码身份 371c9ec8…，见补遗草稿 §11⟧ ⟦2026-10-20 补记 4：Codex 全景审核与 Gemini G2 后，测试共 134 个，Mac 与台式机全部通过；代码身份 98cd3cca…，见补遗草稿 §10⟧ ⟦2026-10-20 补记 3：测试函数共 127 个，Mac 与台式机全部通过；运行中修了两处，并新增 2 个测试；台式机真实 Docker 的 B1 检查（`tools/check_b1_docker.py`）通过。代码身份 1868093c…，见补遗草稿 §9⟧ ⟦2026-10-20 补记 2：测试函数共 125 个（106 个已通过；故障路径审核后新增 19 个，**写好但尚未运行**，2026-10-21 14 点后运行）⟧ ⟦2026-10-20 补记 1：合计 106/106，各文件计数见 `docs/prereg/2026-10-20_bridge_addendum1_DRAFT.md` §4⟧ 合计 85/85（基线 60/60 中，test_analysis_rules 的 9 项与 test_analyze 的 12 项按 v3 语义写成，已按 v4 改写；其余 39 项原样保留并通过）。
d1 代码身份重算仍为 ce9fe5e4…；测试不往 d1/ 写任何文件（`test_d1_untouched`）。

## v4 处理表：README 原列的 18 处歧义
“待作者”只标会改变研究问题、评分对象、科学判据、预算或样本范围的事项。

| # | 歧义 | v3 时的文本/代码 | 处理决定 | 修改位置 | 验证证据 | 待作者？ |
|---|---|---|---|---|---|---|
| 1 | 运行顺序 | 只写“同一题各臂按哈希交错” | 维持字面实现并写进预登记：题按 ORDER 摘要升序；臂序 = 摘要 mod 4! 取规范臂序的全排列 | prereg §3；batch.py 不变 | test_batch 顺序两项 | 否 |
| 2 | ENG_FAIL 重跑时机 | 未写 | 立即重跑（保持交错）；计价暂停与连续失败熔断时先停，续跑再重跑 | prereg §3、§7.3–7.4；batch.py | test_batch、test_end_to_end 批量暂停 | 否 |
| 3 | ΔW 用哪对臂 | 文本 Number−Silent，R-b 用 BATS−Silent | 主比较 BATS−Silent，Number−Silent 另报（作者指令） | prereg §6.5；analysis_rules.w_robustness；analyze.diffs | test_analysis_rules W 两项；test_analyze | 否（作者已定） |
| 4 | 有未知时“同号”怎么判 | 点估计同号、按所有填补 | 改为区间判据 + 符号检验；W 单独自由填补；只作稳健性标签，不进 R-e | prereg §4 规则 5(b)、§6.5 | test_analyze W 未知项 | 否 |
| 5 | 严格版的边界与前提 | 闭区间、无 R-a 前提 | 闭区间；只在 R-a 达到门槛时计算；只报告、低功效 | prereg §6.6；analysis_rules.strict_report | test_analysis_rules 严格版 | 阈值属 C7 候选 |
| 6 | W 在提交轨迹上取什么 | 提交时的 F；ENG_FAIL 的 W 未写 | 写进预登记：提交那一刻的 F（F-v6，含 patch.txt）；ENG_FAIL 的 W 未知 | prereg §4 | test_evaluate S/H/W 表 | 否 |
| 7 | FORMAT_EXIT 在 2.1.0 不会出现 | 分类保留 | 保留，预期为 0，出现即作工程检查 | prereg §4 表 | — | 否 |
| 8 | 纯空白提交 | outcome 记 SUB_EMPTY，与 d1 judge 约定不同 | 采用 SUB_EMPTY（S = 0）：不改动任何文件，与空提交等价；写进预登记 | prereg §4 | test_evaluate | 否 |
| 9 | 轨迹中途触发支出线 | ENG_FAIL + resource_stop，不重跑 | 维持；写明资源中止后的报告方式；不做子样本分析 | prereg §7.2 | test_batch、test_end_to_end | 否（停止线金额属 C8） |
| 10 | 支出累计口径 | 按 usage 含格式错误轮 | 维持；另加临时预留（未定价调用不按 0 计） | spend.py、agent.py | test_end_to_end 计价项 | 否 |
| 11 | 价目表与定价用的模型名 | 未入价目表 → 成本 0 → 全部 ENG_FAIL 且账本漏记 | 价目登记表 + 计价状态 + 计价暂停 + 对账；不改 `cost_tracking`；litellm 认不出的新串需作者另行处理 | model_log.py、spend.py、batch.py、pricing_registry.json；prereg §7.3；`docs/d1_bridge_litellm_pricing_check_2026-10-19.md` | test_end_to_end 6 项 | **登记表的价格要作者按 Google 价目页确认**；出现新版本串时由作者决定 |
| 12 | 原始版本串 | litellm 去掉 “@” 之后的部分 | 经 logger_fn 另存原始 `modelVersion` 与 `usageMetadata` | model_log.py；evaluate 身份字段 | test_end_to_end（假 API 调 logger_fn）；**真实取值待干跑** | 否 |
| 13 | judge 周账本 | 未定 | 桥接实验单独一个周账本文件，上限沿用 d1/week.py | prereg §7.5；batch 账本记录路径 | batch_start 记录 week_state | 否 |
| 14 | 续跑遇到身份不一致 | 移入 attempts 并计入 2 次上限 | ⟦补记 1、补记 2 改为：**停批**，不移动、不重跑；开跑前扫描整个目录；跨身份续跑只能另建目录，用 `--prior-out` 续跑（作者 2026-10-20 确认）⟧；登记表移出代码身份，避免补登版本串引发全量重跑 | identity.py、batch.py | test_batch 身份项 | 否（作者已确认） |
| 15 | 版本分层 | 只给描述 | 维持：规则在全样本判读，按版本分层只作描述；结论限定为所列版本的混合效应 | prereg §3；analyze.versions（含原始版本串） | test_analyze 版本项 | 否 |
| 16 | 熔断时的 R-e | 表里没有这一行 | 加“熔断”行：只出工程报告，附保守附表（不作结论） | prereg §4 规则 6、§6.8；analysis_rules | test_analysis_rules、test_analyze 熔断项 | 否 |
| 17 | 盲交率与漂移检查未实现 | 未实现 | 盲交：命令文本启发式写死（只作描述）；漂移：用公开逐题文件算合成 A 并逐题对比（只报告） | outcome.blind_submit；analyze.drift_check；prereg §4 | test_evaluate 盲交；test_analyze 漂移 | 否 |
| 18 | 题面与基准提交 | 没有生成 | `tools/make_tasks_meta.py`：本地缓存数据（不联网）+ 与 d1 base_commits 交叉核对；sha256 记进批量账本 | tools/；batch.py | build() 3 项检查；**台式机上尚未运行** | 否 |

## 独立审核后的修复（2026-10-19 晚，审核记录 `docs/reviews/2026-10-19_claude_bridge_v4_review.md`）
- 登记新版本串只改登记表：`model_log.register_with_litellm` 把登记价交给 litellm（审核第 1 条）。
- 连续失败段可作废：`batch.py --void-streak`（第 2 条）。
- R-a 措辞改为“通过筛查判据”（第 3 条）；Q1 精确界路径两界平分 α（第 5 条）；C5 推荐改为 Bonferroni（边界模拟，第 6 条）。
- 登记表只增不改的检查、正式轨迹要求登记表已确认（第 7 条）。
- 支出账本原子写；移动目录先记 pending；评测异常只补评测（第 8 条）。
- CLAIMS 补 R-c、R-d；文本与代码的小处对齐（第 9 条）。

## 作者反馈后的小修（2026-10-19 夜）
- 错误率表述：97.5% 区间每个方向单侧 1.25%；两问四个方向合计，Bonferroni 名义上界 5%；百分位 bootstrap 的覆盖只是近似（prereg §6.1）。
- 符号检验写明为辅助保守门槛，不是加权效应的检验；补了层间反向的模拟（prereg §6.4、§5.3）。
- 续跑本地对账：`spend.reconcile` 加上按调用键记录的流水；batch 在开始时和每槽之后对账，有差异即停。
- $1 是临时记账预留，不是未知调用费用的上界；停止线是软件停止线，不保证账单不超出。

## v4 新增、需要作者定的（详见 prereg 状态总表）
- C5（Q1/Q2 是否再做 Bonferroni）、C6（符号检验保护）、C11（二进制补丁与后台进程的桥接版提取规则 F-b1，代码**未实现**）。其余候选阈值沿用 v3 的值。

## 还要在台式机上用 Docker 与真 API 干跑确认（开发题 5 道 × 4 臂；见 prereg v4 §7.5）
- 计价：真实 `response.model` 与原始 `modelVersion`、是否命中登记表、逐次成本与 Google 账单对账（含思考 token）。
- 未知来源：二进制补丁、后台进程、判分失败、F 不支持率。
- 快照时容器存活；`--pull never`；`docker top` 保活进程名；镜像 Id 一致。
- 真 LitellmModel 的 FormatError 路径；API 重试用尽；judge 判 bridge 产物的完整路径与耗时；两个虚拟环境分开；账本文件锁跨子进程生效；`make_tasks_meta.py` 在台式机上生成并核对。
