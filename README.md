# 晶圆探针复测数据采录台

量产电测操作员、针卡维护员和批次审核员可在同一份本地 SQLite 台账上登记离线探针 map、载台锚点、针卡资格与接触证据，冻结复测采样并发布按晶圆和裸片坐标索引的 WAT/电测图谱。样例和规则为合成数据。

## 运行

在本目录使用 Python 3.11 或更新版本。标准库包含全部运行依赖，可直接从源码运行。

```powershell
python -B -m wafer_retest --db ':memory:' demo
python -B -m unittest discover -s tests -v
```

文件型演示使用一个新数据库。`demo` 自动初始化，并在同一事务内提交整组示例操作。独立业务命令先执行 `init`。

```powershell
python -B -m wafer_retest --db demo.db demo
python -B -m wafer_retest --db demo.db audit
python -B -m wafer_retest --db demo.db journal-export | Set-Content -Encoding utf8 evidence.json
python -B -m wafer_retest --db rebuilt.db replay evidence.json
python -B -m wafer_retest --db rebuilt.db export LOT-P7 2
```

`demo` 给出镜像 `MX` 和旋转 `R90`，首发版本 `1`，迟到证据后的 `late_exportable: false` 与旧版本 `REVOKED`，以及替代复测后的版本 `2`。`audit` 返回 `MATCH`，新库返回 `REPLAYED`，两个库的最新版本号和摘要一致。`audit` 发现投影差异时返回 `MISMATCH` 和差异表名，退出码为 2。序号断裂、回执不符或命令失败也以 2 退出。

## 单步操作

```powershell
python -B -m wafer_retest --db ops.db init
python -B -m wafer_retest --db ops.db load fixtures/baseline.json
python -B -m wafer_retest --db ops.db project BASE-1
python -B -m wafer_retest --db ops.db contact PC-1 10 1
python -B -m wafer_retest --db ops.db load fixtures/retest.json
python -B -m wafer_retest --db ops.db plan PLAN-P7
python -B -m wafer_retest --db ops.db recompute LOT-P7
python -B -m wafer_retest --db ops.db publish LOT-P7
python -B -m wafer_retest --db ops.db export LOT-P7 1
python -B -m wafer_retest --db ops.db load fixtures/late-evidence.json
python -B -m wafer_retest --db ops.db status LOT-P7
python -B -m wafer_retest --db ops.db recompute LOT-P7
python -B -m wafer_retest --db ops.db load fixtures/replacement.json
python -B -m wafer_retest --db ops.db recompute LOT-P7
python -B -m wafer_retest --db ops.db publish LOT-P7
```

`load` 接受一组 `{"command":"map","args":{...}}` 操作，整组原子提交。`lot`、`map`、`anchors`、`card`、`qualify`、`evidence`、`dictionary`、`seal-dictionary`、`bind-dictionary`、`create-plan`、`transition-plan` 各自也接受一个文件，文件内容为相应的 `args` 对象。四份 `fixtures/*.json` 给出完整输入字段示例。命令和导出均使用 UTF-8 JSON。

## 换码收件 `bin-crosswalk`

新探针机把同一个 WAT 通过码拆成两段时，`bin-crosswalk`（实现 `wafer_retest/bin_crosswalk.py`，样例 `fixtures/bin-crosswalk.json`，回归 `tests/test_bin_crosswalk.py`）读取源/目标两份已封存字典和可选的旧机 map，产出可直接由原 `map`/`load` 命令采录的转换文件、固定换码表可行性、逐区间等价证明和没有等价新码的原始区间。工具只写指定的结果文件，不改动原 map、封存字典或发布记录。

```powershell
# 自包含请求（两份字典 + old_map + interval_readings）
python -B -m wafer_retest --db :memory: bin-crosswalk fixtures/bin-crosswalk.json `
  -o new-map.json --batch-output new-map.load.json --report xwalk-report.json `
  --fixed-table-output fixed-table.json --gaps-output gaps.json
python -B -m wafer_retest --db ops.db map new-map.json       # 原入库命令直接消费
python -B -m wafer_retest --db ops.db load new-map.load.json
# 也可分别给字典/map 文件；独立入口不需要 --db
python -B -m wafer_retest.bin_crosswalk -s old-dict.json -t new-dict.json -m old.json -o new.json
```

判定口径（端点与 `wafer_retest/dictionary.py` 一样按闭区间 `lo <= value <= hi`）：

- **等价必须档级和判档依据都相同**：同时比较 `grade` 与 `BIN_RULE`/`WAT_LIMIT`。因此源端 WAT 越界 FAIL 只能换到目标端同样判 `WAT_LIMIT` 的码，不能换到字典原生 `FAIL`（`BIN_RULE`）把“越界”偷换成“本档不合格”。
- **固定换码表**：同一 `(channel, old_code)` 只有在整个声明量程（规则的 `[lo,hi]`，无区间码为整条整数轴）内存在同一个等价新码时才允许烧一张固定表；否则 `fixed=false`、`new_code=null`、`reason=SPLIT_REQUIRED`，并在 `proof` 里给出逐区间证明。验收样例中 WAT `0` 在 `[90,110]` 全程 PASS/BIN_RULE，但 `[90,100]→L`、`[101,110]→H`，没有一个码覆盖全量程，故不能承诺固定换码。
- **逐笔转 map**：可按每笔实测整数选码。90、100→L，101、110→H；89→L、111→H，转换后目标字典仍判 `FAIL/WAT_LIMIT`，越界证据保留。每行保留实测 `value`、`old_code/source_code` 与 `source_dictionary(_version)`。未知旧码出 `UNKNOWN_OLD_CODE`（台账口径 `UNKNOWN_BIN`），无等价新码出 `NO_EQUIVALENT_NEW_CODE`，默认不放进交付 map（缺数只会 HOLD，不会默认放行）。
- **整数闭区间读数单**：同一个候选码必须对 `[lo,hi]` 内全部整数成立。`[100,101]` 因 100 只适用 L、101 只适用 H，没有公共码，结论为**待复测**；`[95,97]` 可整体核为 L。区间单只出核验结论（`map_written=false`），绝不虚构一个整数写进 map。完全没有等价码的区间按**待核** `NO_EQUIVALENT_NEW_CODE` 处理，并进入 `unmatched_intervals`（无界端点用 `null` 表示）。
- **发布影响**：全改 L 的朴素固定表会把基线 `fixtures/baseline.json` 中 101 那笔 WAT 误判成 `WAT_LIMIT` FAIL（该裸片已有 ET `9` FAIL，故批次 FAIL 总数仍为 1、仍 READY，但通道证据与摘要已不同）；若 101 落在原本通过的裸片上，全改 L 会把 FAIL 总数从 1 抬到 2，越过批次 `fail_limit=1` 触发 `FAIL_LIMIT` 挡住发布，而逐笔正确换码仍为 FAIL=1、READY。该成图差异由 `tests/test_bin_crosswalk.py` 用真实 `recompute` 锁定。

## 裁决规则

| 子系统 | 持久化与入口 | 裁决 |
| --- | --- | --- |
| 坐标对位 | `lots`、`wafers`、`dies`、`sessions`、`anchors`、`measurements`，`map`、`anchors`、`project` | 同一批次的晶圆共享设计掩膜。每轴减去 `origin_u/v`（默认 0），再按该轴整数步距求格点。对每个本地点选最高级锚点 `FIDUCIAL > STAGE > OPERATOR`，同级目标冲突即挂起。至少三个不共线点才能从八种旋转或镜像及平移中确定唯一精确解。离格、离掩膜、重复裸片通道及同落针同站点跨裸片的读数隔离。 |
| 针卡与接触 | `cards`、`qualifications`、`card_evidence`，`qualify`、`evidence`、`contact` | 按测量分钟匹配互不重叠的 `[starts, ends)` 资格，要求 `PASS`。证据优先级先看来源 `SIGNED > STATION > OPERATOR`，再看 `TOUCHDOWN > SITE > CARD`。同级相反结论使落针不可用。落针范围限会话及 touchdown，影响其中所有站点。无命中证据时合格读数可用。 |
| 采样与替换 | `sampling_plans`、`sampling_targets`，`create-plan`、`plan`、`transition-plan` | 每个晶圆的每个扇区先获一个名额，再补剩余容量。风险顺序为无完整读数、读数分歧、FAIL、MARGINAL、PASS，同风险按晶圆和坐标排序。冻结时保存完整风险快照；同批只允许一个有效计划。有效计划开始前的无计划读数可作基础。计划目标只能由指定计划在采集时窗 `[opens, closes)` 的完整读数组替换，封存后仍可导入时窗内采集的文件。 |
| 分档与释放 | `dictionaries`、`bin_rules`、`lot_dictionaries`、`assessments`、`releases`，`bind-dictionary`、`recompute`、`publish`、`export` | 数据字典封版后绑定批次，必须覆盖全部必需通道。已知原始 bin 按规则解释，带区间的 WAT 值越界提升为 FAIL，区间端点包含。未知 bin 使该次读数不完整。成组读数必须同会话、落针和站点，多个完整组只有逐通道档级相同才取最新。同一裸片取最差档，所有目标完成、无 HOLD、计划已封存且 FAIL 数未超过批次上限才可发布。 |

`recompute` 返回 `manifest`、`digest` 和逐读数 `trace`。图谱保留每颗裸片的晶圆、坐标、分档、道次、原始 bin、值、资格与胜出接触证据。未采用的读数及其原因可在 `trace` 查到。`export` 返回发布时冻结的图谱和 SHA-256 摘要。迟到证据或字典切换提升批次世代，旧版本立即失去导出资格。重算后如图谱内容和来源不变会恢复原版本；发生变化则撤回旧版本，待新图谱可发布时生成新版本号。撤回历史保留在 `status`。

## 恢复与审计

- `recompute LOT` 可从当前已接受的业务证据重建图谱和释放资格。同世代重算的图谱摘要相同；命令和原始记录使用事务，输入冲突会连同世代和来源条目一起回滚。
- `audit` 从按入账顺序保存的 JSON 命令参数重建空 SQLite，包括当时创建采样计划、重算与发布的时点。每步重算、发布的摘要及修订回执必须吻合，最后比较所有当前业务表和历史版本。它可发现源证据、分档或发布投影与归档指令不一致。
- `journal-export` 导出已接收的结构化来源和决定；`replay FILE` 在空库恢复这些命令及其结果。来源保留结构化内容，文件的原始空白与编码字节没有纳入证据签名。将日志文件保存到本地可独立保管的位置，便于台账丢失时重建。

首版坐标模型适用于正交等格距轴和整数平移；同批晶圆使用同一产品掩膜。项目范围是本地文件和台账，量产设备协议、外部 MES、数字签名核验及生产数据均在范围之外。
