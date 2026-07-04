# Agent Instructions

`repair-audit-tracker` is a global opt-in skill for structured review reports, repair plans, progress tracking, unresolved issue tracking, and next-audit planning.

It is disabled by default.

Only activate it when the user explicitly says one of:

- Use repair-audit-tracker
- 开启审查追踪
- 开启修复审计
- 生成审查报告
- 生成修复计划
- 修复后给出审查报告
- 记录修复进度
- 下一轮审查
- #audit
- audit mode
- repair audit

Do not activate it for ordinary discussion, normal bug fixing, normal refactoring, normal implementation planning, file comparison, code review, or native plan mode unless the user explicitly requests audit/report tracking.

It is global and not WebUI-specific.

It must not replace or interfere with the native plan agent.

When active, follow the output format defined in the `repair-audit-tracker` skill.

When inactive, do not output:

```text
# Review Report
# Repair Plan
# Review Plan
# Unreviewed Areas