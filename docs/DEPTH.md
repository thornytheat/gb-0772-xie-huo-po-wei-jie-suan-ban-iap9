# 深度登记 卸货泊位结算板

- 规模：中型
- 核心规则：先将跨传感器的同车同泊位占用取并集，再按到达时有效预约和本地自然日收费，保留逐单差价。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 停靠与预约采信 | 检查原结构、重复及同号冲突 | `GET /api/cases/dirty/intake` | `app/source/csvscan.py` `app/curb/detections.py` | `tests/test_intake.py` |
| M2 车牌匿名关联 | 用车辆代号关联到达时的预约版本 | `GET /api/cases/dirty/identity` | `app/source/plates.py` `app/curb/bookings.py` | `tests/test_identity.py` |
| M3 占用区间重建 | 查并集、去重分钟与争议格 | `GET /api/cases/dirty/occupancy` | `app/curb/merge.py` `app/curb/grid.py` | `tests/test_occupancy.py` |
| M4 首免与升级价计算 | 选择一次停靠查看跨日收费分段 | `GET /api/cases/dirty/pricing` | `app/billing/rates.py` | `tests/test_pricing.py` |
| M5 应实收和泊位对账 | 查看逐单差价、游离回执与泊位容量 | `GET /api/cases/dirty/settlement` | `app/billing/settle.py` `app/billing/payments.py` | `tests/test_settlement.py` |
| M6 批次留档复跑 | 原子落档、查历史及重放 | `POST /api/dockets` | `app/archive/docket.py` `app/archive/rerun.py` | `tests/test_docket.py` |
| M7 理赔证据签发 | 签发并核验脱敏证据、摘要与勾稽 | `GET /api/export/{key}` | `app/archive/appeal.py` `app/archive/canonical.py` | `tests/test_appeal.py` |
| M8 双校时交班放行 | 两种校时时刻比对应收，先结同额单、挂起重叠/跳档单，确认后查重算应收，旧校时重跑不回退 | `POST /api/calibrations/review` `POST/GET /api/dockets/{key}/clocks` `POST .../clocks/confirm` `GET /api/dockets/{key}/recalculation` | `app/curb/calibrate.py` `app/archive/clocks.py` | `tests/test_zz_agent_clock_rules.py` |

## 陷阱登记

| TRAP | 现象 | 直接处理的误差 | 处理 | 位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 杆侧 CSV 带 BOM | 识别首列失败 | 首位剥离 | `app/source/csvscan.py` | `tests/test_intake.py::test_started_with_bom` |
| TRAP-04 | 重复表头 | 表头被记作一次停靠 | 分段计数 | `app/source/csvscan.py` | `tests/test_intake.py::test_repeated_header_and_physical_count` |
| TRAP-05 | 残缺列含明文车牌 | 内容错列或隔离区泄露车牌 | 结构隔离、保留摘要 | `app/source/csvscan.py` | `tests/test_intake.py::test_ragged_row_keeps_hash_not_plaintext` |
| TRAP-07 | 回执金额 NULL | 误记零元回执 | 无效金额隔离并保留原值 | `app/billing/payments.py` | `tests/test_settlement.py::test_null_amount_never_looks_like_paid_zero` |
| TRAP-08 | 编号 0001 或 0007 | 数字转换丢零 | 原字符串贯穿 | `app/curb/detections.py` | `tests/test_intake.py::test_front_zero_id_survives_intake` |
| TRAP-11 | 同一时刻不同时区 | 分槽或重复占用 | 标准 UTC 对齐 | `app/curb/timeaxis.py` | `tests/test_occupancy.py::test_mixed_offsets_share_minute` |
| TRAP-14 | 同一检测编号出现两个结束时刻 | 输入顺序改变结果 | 冲突整组隔离 | `app/source/dedup.py` | `tests/test_intake.py::test_conflicting_sensor_event_never_last_wins` |
| TRAP-19 | 同泊多车和同车跨泊并存 | 多扣泊位分钟或漏掉争议 | 在各泊位分钟以同泊多车优先 | `app/curb/grid.py` | `tests/test_occupancy.py::test_collision_priority_matches_both_berths` |
| TRAP-23 | 两杆结束和开始同一分钟 | 重算端点或将一次停靠切开 | 半开并连续合并 | `app/curb/merge.py` | `tests/test_occupancy.py::test_touching_scans_single_stay` |

## 对账规则

- 脏样检测源 27 = 格式可读 25 + 重复表头 1 + 列数隔离 1。可读 25 再扣车牌 1、范围与重复编号等 4，采信 20。
- 应管 240 = 空闲 133 + 争议 7 + 首免 10 + 基础价 68 + 升级价 22。实收 2310 分 − 应收 2240 分 = 逐单差价 70 分，游离回执 140 分另列。
- 归档键以规则版本和输入全摘要生成，留档提交为单事务。重复提交取原运行快照，原件变动产生不同记录。

## 校时交班规则

- 校时复核在两套时刻上各跑一次「碰撞 → 预约采信 → 计价」全链路，按稳定 `stay` 编号逐单比较应收，不做只看一分钟差值的局部估算。
- 两时刻应收相同、且无下列例外的停靠进入 `SETTLE`（可先结）；否则 `HOLD`（待核）。
- 一分钟移动产生新的同车跨泊位相交分钟（`OVERLAP`）时，相交两单同时待核，即使该分钟本就免费、两时刻应收相同也不放行。
- 每车每天的付费档位是全局计数器：时差改变前单的付费分钟数后，未被直接移动的后一次停靠若 BASE/UPGRADE 构成变化（`TIER_JUMP`），那单待核。
- 到达越过预约撤销/版本边界时记 `BOOKING_FLIP`，并通常伴随 `DUE_CHANGED`；原时刻落在“撤销同刻生效即排除”而校时后严格早于撤销时，原单会多收。
- 校时事件按显式 `seq/version`（或签发时刻）单调生效：更新版本替换当前候选；同号同 ID 重放为 `DUPLICATE`；新版本生效后重跑旧版本返回 `STALE`，已确认的重算应收结论不变。
- `POST /api/dockets/{key}/clocks/confirm` 后，`GET /api/dockets/{key}/recalculation` 与对账页 `?key=...#clock-desk` 可查重算应收；原始四份单据另存于独立表，仅用于重算，不进入 evidence/manifest/export，明文车牌仍不外露。

## 已知边界

只使用完整杆侧区间，传感器失明但缺少额外证据的分钟按空闲计算；覆盖不完整的窗口应由外部系统重采。回执关联使用准确到达分钟，不模糊猜测。退款、法定票据签章及跨区共享付费档位由外部流程处理。
