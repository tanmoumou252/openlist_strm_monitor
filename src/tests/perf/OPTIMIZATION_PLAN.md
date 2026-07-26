# 极限优化方案

1. 循环外解析并缓存所有 A/B 根，最长根优先。
2. 每条 A/B 动态路径只 `resolve()` 一次，向下传递规范化结果。
3. 一条 SQL 全量读取 lineage 热字段，建立 `by_key/by_a_path/by_b_path` 字典，整个核对复用一个只读连接/事务。
4. 同一路径 `exists()` 最多一次。按父目录分组时，可用一次 `iterdir()` 构建名称集合，替代大量独立元数据查询，但必须用真实目录 A/B 测试。
5. 不在循环内输出日志，只累计计数和时间。
6. 新数据库直接冗余 `a_root_id,a_rel,b_root_id,b_rel,lineage_key,source_version,verified_version,state`，给 `(b_root_id,b_rel)`、`(a_root_id,a_rel)`、`(source_version,verified_version)` 建索引。热路径避免 JOIN。
7. 极致方案是版本化增量校验：只校验新增、文件事件、映射版本变化、上次失败的记录；全量核对移出启动关键路径，作为定期审计。
8. 只有 profile 证明剩余耗时为独立 I/O 等待后，才按根/父目录分区并行。每个进程使用独立 SQLite 只读连接。

禁止：字符串 startswith 替代路径包含；吞异常当有效；无失效机制永久跳过；线程间共享默认 sqlite3 连接；取消必要越界/符号链接检查。
