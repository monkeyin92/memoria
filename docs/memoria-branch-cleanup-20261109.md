# Memoria branch cleanup audit

审计日期为 2026-11-09。比较基准是远端 `origin/main`，当前提交为 `1e8f33f1`。本审计只删除已经确认没有未合并工作、且没有被 worktree 使用的本地分支。

## 已处理

`fix/r02-gates-tag-prefix` 已合并到 main。它的远端 tracking ref 已删除，独有提交只是 squash merge 前的同一补丁。本地分支已删除。

## 保留的本地分支

| 分支 | 原因 |
| --- | --- |
| `test/r02-stop-suite-record` | 已合并，但当前主工作树正签出该分支，包含未提交和未跟踪改动。切换分支会影响这些改动。 |
| `claude/dot-face-alive` | 有未合并提交，PR #493 仍开放，并有独立 worktree。 |
| `perf/speculative-speech-head-start` | 有未合并提交，PR #514 仍开放，并有独立 worktree。 |
| `t3/audit-branch-cleanup` | T3 当前 worktree 使用，等本线程结束后再清理。 |

本地 `main` 比 `origin/main` 落后一个提交，但当前工作树不在 `main`，本轮不做 fast-forward。

## 已清理的远端分支

以下分支的内容已合并或明显过时。收到明确确认后，远端 ref 已逐项删除，并用 `git ls-remote --heads origin` 复核均已消失。

- `test/r02-interrupt-backchannel-harness`。相关改动已进入 main。
- `cursor/u5-u7-three-sentences-564f`。内容已在 main。
- `deploy/20261103-card5-projection-range-fix-v2-ci-receipt`。内容已在 main。
- `deploy/20261103-card5-projection-range-fix-v3-ci-receipt`。内容已在 main。
- `deploy/20261029-plan-head-start-v1-ci-receipt`。补丁已在 main。

以下分支不能按过时分支删除。

- `release/20261101-asr-failover-v2` 有 release tag，且存在可能未被 squash 合并的独有提交，保留。
- `claude/mp-brand-pass` 有 5 个独有提交，没有 PR。
- `cursor/crisis-gen-guard-fallback-4000` 有 3 个独有提交，关联 PR #448 已关闭但未合并。

审计依据包括 `git branch -vv`、`git worktree list`、`git cherry origin/main`、`git rev-list`、远端 refs 和 GitHub PR 状态。`.dsh-project/` 仍按项目规则排除，不随本次清理删除。

## 2026-11-09 follow-up

用户要求提交、推送并清理本轮临时分支和 worktree 后，主工作树原位创建了 `fix/miniprogram-audit-memory-consent-20261109`，提交 `d9d2ade24c6b7d0009ef6d9c61201088e4dc8075` 已推送到同名远端分支。旧的 `test/r02-stop-suite-record` 本地分支已删除，远端同名 tracking ref 在清理前已是 `[gone]`。

以下 11 个已完成且干净的 T3 临时 worktree 和本地分支已删除：`t3/audit-branch-cleanup`、`t3/fix-device-status-resume`、`t3/fix-memory-correction`、`t3/fix-safety-alert-read-failure`、`t3/fix-self-use-memory-consent`、`t3/guardian-child-review-fixes`、`t3/miniprogram-auth-renewal-research`、`t3/miniprogram-login-cta-recovery`、`t3/review-memory-binding-consistency`、`t3/review-memory-consent-flow`、`t3/review-miniprogram-p1-fixes`。删除前的完整 T3 分支历史已保存在 `/private/tmp/memoria-t3-branches-20261109.bundle`，`git bundle verify` 通过。

保留了 `t3/fix-hardware-status-feedback`，因为它与当前固件提交存在文案差异；保留了开放 PR 的两个 `.claude/worktrees`、`/private/tmp/memoria-visual` 和主工作树。清理后主工作树仍有 50 项父任务已有的已修改或未跟踪路径。
