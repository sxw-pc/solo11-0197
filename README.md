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

## 腔体环境收件

恢复探针作业前可收取离线环境监视器的成对读数。温度、台面温度和露点以十分之一摄氏度录入。问询按半开分钟窗进行。每条记录仅在配置的采样有效期内可用于判断，台面温度等于或低于露点时提示湿风险，过期或对同分钟有相反结论时标明未知。

```powershell
python -B -m wafer_retest --db env.db init
python -B -m wafer_retest --db env.db environment-record fixtures/environment-readings.json
python -B -m wafer_retest --db env.db environment-trace DRY-1 12 26 --max-age 4
python -B -m wafer_retest --db env.db audit
```

### 槽位交接与每盒环境凭证

同一准备腔的左右槽可在读数里加 `slot`（如 `LEFT`、`RIGHT`）；不带 `slot` 的旧收件格式和 `environment-trace CHAMBER ...` 整腔查询保持不变。`fixtures/environment-slots.json` 是一份可重复 `load` 的示例：12 分钟时左槽露点 16°C 干燥、右槽 18.5°C 凝露风险，15 分钟后两槽露点均降到 16°C，并附有两张双签槽位交接单。

```powershell
python -B -m wafer_retest --db slots.db init
python -B -m wafer_retest --db slots.db load fixtures/environment-slots.json
python -B -m wafer_retest --db slots.db batch-certificate PREP-SLOT 12 18 --max-age 3
python -B -m wafer_retest --db slots.db box-certificate PREP-SLOT BOX-LEFT 12 18 --max-age 3
```

收件命令 `slot-handover` 登记双签槽位交接单（`handover/chamber/slot/box/starts/ends/signers`），`dry-recovery` 登记恢复干燥签认（另带 `slot/box/starts/ends/signers`）。同号重投必须与原单完全一致，改动区间会按冲突退件，因此重投不能把一段恢复变长。

裁决口径：

- 只有整腔收件、没有可核验（双签）槽位归属时，`batch-certificate` 扫描全部生效槽位，任何凝露风险或环境缺口都使共批凭证保持 `PENDING`。
- 有双签交接后，`box-certificate` 只证明该盒自己的连续存放路径：监视器槽位、采样有效期内的分钟与盒子实际占用分钟必须逐分钟重合；归属短缺、一盒多槽或同槽重盒都标为待核。
- 经过凝露风险槽位的盒子移到干燥位置不会自动合格。只有满足以下全部条件的双签恢复签认才能清除末次风险：签认区间起点晚于最后一个风险分钟、整段每一分钟都有对应槽位的现有干燥读数支持、连续达到三分钟。未知或冲突分钟会打断连续性，不能把两段干燥拼成一次恢复。

`conditioning.certificate(db, chamber, box, start, end, max_age)` 与 `conditioning.batch_certificate(...)` 返回 `decision`（`CERTIFIED`/`PENDING`，并可直接与 `READY`/`HOLD`、`DRY`/`WET_RISK`/`UNKNOWN` 等既有状态字面量比较）、`segments`、`reasons`、`issues` 和恢复签认核验明细。

