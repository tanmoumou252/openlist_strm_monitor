请使用 Superpowers 工作流审查处理本次这个问题。
 
Please handle this issue using the Superpowers workflow.
 
流程要求：
Workflow requirements:
 
1. 使用 systematic-debugging 定位根因，先不要改代码。
   Use systematic-debugging to identify the root cause. Do not modify code yet.
 
2. 使用 writing-plans 输出修复计划，等我确认。
   Use writing-plans to create a fix plan and wait for my approval.
 
3. 我确认后，使用 executing-plans 按计划修复。
   After I approve the plan, use executing-plans to implement the fix according to the plan.
 
4. 修复完成后，使用 requesting-code-review / receiving-code-review 做审查报告。
   After the fix is complete, use requesting-code-review / receiving-code-review to produce a review report.
 
5. 最后使用 verification-before-completion 做交付前验证。
   Finally, use verification-before-completion to verify the work before delivery.
 
输出要求：
Output requirements:
 
- 每个阶段都要明确说明当前阶段。
  Clearly state the current stage at each step.
 
- 不要跳过计划直接改代码。
  Do not skip planning and directly modify code.
 
- 不要做无关重构。
  Do not perform unrelated refactoring.
 
- 所有修改都要说明原因。
  Explain the reason for every change.
 
- 最后给出：已完成 / 有风险 / 需要我决策。
  Finally provide one of the following conclusions: Completed / At risk / Needs my decision.

- 使用中文答复
  Reply in Chinese.