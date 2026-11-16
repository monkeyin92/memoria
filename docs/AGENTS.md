# docs — 文档聚合与验收证据层

## OVERVIEW

docs/ 是聚合文档生成管道与评测/验收证据收据层；9 个聚合 md 与 ~44 份 memory-evaluation 收据受 tests/test_documentation_budget.py 白名单管控。
<!-- score 9：文件数 750>20 ×3 + 直接子目录 7>5 ×2 + 自有配置(聚合 GROUPS+预算测试) ×1 + 代码引用中心度 364>20 ×3；独立领域 -->

## STRUCTURE

```
docs/
├── *.md                    # 8 个根聚合（唯一允许的根级 md）
├── memory-evaluation-*.json # ~44 份记忆评测收据（数据，非文档）
├── acceptance/             # 15 个 run 证据目录（79 文件）
├── design/                 # 4 个带日期 UI 快照目录（446 文件）
├── reviews/                # 3 个审查 stub + five-software 证据包（166 文件）
├── ops/  audit/  eval/     # 运行清单 / 审计 TSV / 决策模型评测
└── assets/                 # 3 个二进制设计素材（pill-face/robot-face）
```

## WHERE TO LOOK

| 需求 | 位置 |
|------|------|
| 最新验收结论 | docs/acceptance.md |
| 产品决策（顶部无日期章节优先） | docs/product.md |
| UI/固件设计记录 | docs/design.md |
| 合规与删除语义 | docs/compliance.md |
| 运行手册（发布/回滚/ASR/TTS） | docs/runbooks.md |
| 策略与 ADR | docs/strategy.md |
| 历史交接 | docs/handoff-archive.md |
| 记忆检索评测 | docs/eval.md + docs/memory-evaluation-*.json |
| 单次验收证据 | docs/acceptance/<run>/（入口 summary.json/capture.json/receipts.json） |
| 软件评审证据 | docs/reviews/five-software-20261111/（入口 receipt-index.json/decisions.tsv） |
| 线上运行清单 | docs/ops/runtime-inventory.md |

## CONVENTIONS

- 聚合生成：scripts/consolidate_docs.py，GROUPS 定义 8 组；默认 dry-run，--apply 写聚合、全局改写引用、删除源文件。输出已存在即拒绝重跑；compliance/runbooks/strategy 源目录已被删除，续写该主题需先建源文件并整体重建。
- 预算测试：只放行 ALLOWED_DOCS_FILES 的 9 个 md + `docs/memory-evaluation-*.json` 数据模式；新增文档类别必须同步改测试。
- 收据命名：`memory-evaluation-<extractor>-<model>-<suite>[-<label>]-<YYYYMMDD>.json`；可复现性靠文件内 adapter/config.note/dataset_sha256。
- 验收证据：run 目录 `run-YYYYMMDD-topic` 或 `<topic>-YYYYMMDD`；JSON 带 schema_version + sha256 来源收据；状态词 pass/fail/not_run_blocked/unknown；否定结论原样保留。
- UI 快照：`docs/design/<name>-YYYYMMDD/` 一次性迭代，按日期保留；由 apps/miniprogram/scripts/build-parent-preview.js、screenshot-parent-ui.js 生成。
- decisions.tsv 表头 `ts phase decision why evidence result`，append-only；中文叙述 + 英文标识符/键名。

## ANTI-PATTERNS

- 不手改 docs/ 根聚合 md（整体重建会覆盖）；不直接重跑 consolidator（输出存在即拒绝）。
- 不在 docs/ 下新增散落 md：预算测试只放行 9 个聚合 + memory-evaluation-*.json；话题文档进聚合源目录后整体重建。
- 不整读大文件：govulncheck.json 910KB、trivy.json 360KB、acceptance.md 4606 行、strategy.md 2975 行——流式或定向查询。
- 收据语义：semantic/candidate-* 收据是进程内补丁/临时环境开关的上界（见 config.note），不代表仓库代码；check_ingestion=false 不代表已验证摄入；dataset_sha256 不同的收据禁止比较。
- 不把嵌入式 OSV 记录当漏洞：只有 finding 对象算，且按 finding.osv 去重。
- 词义纪律：verified_empty 只证明"可删域无残留"，不得写成"已擦除/封存"。
- 审查 stub（key=value txt）状态为 desktop_review_only_not_approval，不是审批。
- 证据边界不改写：direct_real_device_verified=false、production_bridge_verified=false、soak fail 状态必须原样保留。
- 不手调 screenshots.json/preview.html——由小程序脚本重新生成。
- near-miss AUC<0.5 不代表可设检索阈值；不对未验证运行宣称通过。
