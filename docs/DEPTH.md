# 深度登记 水产拍卖结算

规模：中型。使用者：水产批发市场结算员。核心规则：先毛皮配对，再扣杂计价，按更正链选择唯一生效版，只支付差额。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 称重采信 | 查看并执行称重采信 | `POST /api/intake` | `app/modules/intake/rules.py` | `tests/test_intake.py` |
| M2 货品换算 | 查看并执行货品换算 | `POST /api/conversion` | `app/modules/conversion/rules.py` | `tests/test_conversion.py` |
| M3 毛皮配对 | 查看并执行毛皮配对 | `POST /api/pairing` | `app/modules/pairing/rules.py` | `tests/test_pairing.py` |
| M4 扣杂竞价 | 查看并执行扣杂竞价 | `POST /api/auction` | `app/modules/auction/rules.py` | `tests/test_auction.py` |
| M5 更正裁决 | 查看并执行更正裁决 | `POST /api/correction` | `app/modules/correction/rules.py` | `tests/test_correction.py` |
| M6 应付对账 | 查看并执行应付对账 | `POST /api/reconcile` | `app/modules/reconcile/rules.py` | `tests/test_reconcile.py` |
| M7 日结放款 | 核对额度并发布当日可释放票 | `POST /api/release` | `app/modules/release/rules.py` | `tests/test_release.py` |
| M8 结算归档 | 查看并执行结算归档 | `POST /api/archive` | `app/modules/archive/rules.py` | `tests/test_archive.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码中文票据 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_legacy_encoding` |
| TRAP-02 | BOM表头 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bom_real` |
| TRAP-04 | 分段表头 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_repeated_header_real` |
| TRAP-05 | 缺列记录 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_bad_columns_real` |
| TRAP-07 | 空值哨兵 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_sentinel_real` |
| TRAP-08 | 前导零票号 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_leading_zero_real` |
| TRAP-14 | 重复编号不同原值 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_duplicate_conflict_real` |
| TRAP-18 | 三类空态 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/intake/rules.py` | `tests/test_intake.py::test_empty_states` |
| TRAP-15 | 吨与千克混合 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_tonnes_counterexample` |
| TRAP-06 | 全角千分位 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/conversion/rules.py` | `tests/test_conversion.py::test_fullwidth_thousands` |
| TRAP-10 | 金额半分舍入 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/auction/rules.py` | `tests/test_auction.py::test_half_cent_rounding` |
| TRAP-26 | 陈旧更正分支 | 静默错算或丢失证据 | 显式归一与原因裁决 | `app/modules/correction/rules.py` | `tests/test_correction.py::test_stale_branch` |
| TRAP-27 | 更正未闭合但其余票待放款 | 全批等更正，买家错过当天提货 | 已核对票先释放，未闭合票挂预留敞口，列明采用的更正版本 | `app/modules/release/rules.py` | `tests/test_release.py::test_release_open_correction_held` |
| TRAP-28 | 迟到更正把累计欠款推过额度 | 只按已核对票算余额 | 已占额度=前日未结+更正预留+已释放欠款，逐票校验剩余额度 | `app/modules/release/rules.py` | `tests/test_release.py::test_release_occupied_covers_carryover_and_reserve` |
| TRAP-29 | 同票金额冲突或版本链冲突 | 任选一版放款 | 冲突票暂缓，保留候选版本与证据 | `app/modules/release/rules.py` | `tests/test_release.py::test_release_amount_conflict_held` |
| TRAP-30 | 同一批重跑重复占用信用 | 每次发布都新增一条放款 | 放款批号由计划内容摘要生成，同内容复用旧版次 | `app/store.py` | `tests/test_release.py::test_release_rerun_same_key_no_double_occupancy` |
| TRAP-31 | 发布中断丢失旧版释放结果 | 先删旧再写新 | 单事务写版次，旧版保留；导出同时含发布版次与历史 | `app/store.py` | `tests/test_release.py::test_release_mid_publish_failure_rolls_back` |
| TRAP-32 | 票号未映射买家 | 默认归到第一个买家 | 单独挂未映射买家并全部暂缓 | `app/modules/release/rules.py` | `tests/test_release.py::test_release_unmapped_ticket_held` |

## 日结放款规则

输入=当日生效票（更正裁决后唯一生效版）+前日未结票+更正通知+买家信用额度。每位买家展示信用额度、前日未结、更正预留、已释放欠款、已占额度、剩余额度、状态，以及逐票的可释放/暂缓明细（采用版本、候选版本、金额、预留敞口、原因码）。

- 敞口预留：暂缓票取候选版本最大金额减已付，至少为 0；若更正未给出金额，记未知敞口并暂缓该买家其余欠款票，避免累计欠款越额度。
- 暂缓原因码：`CORRECTION_OPEN`（更正未闭合）、`TICKET_AMOUNT_CONFLICT`（同票金额冲突）、`VERSION_CONFLICT`（更正版本链冲突）、`EXPOSURE_UNKNOWN`（敞口未定）、`OVER_LIMIT`（超剩余额度）、`BUYER_OVER_LIMIT`（买家已超额度）、`UNMAPPED_BUYER`（未映射买家）。
- 退款或零待付票先放行，只释放额度。
- 放款批号=计划内容+源快照摘要+规则版本的哈希；同内容重发复用旧版次（`duplicate=true`），不重复占用信用。发布用 `POST /api/runs/{run_id}/release`，传入当前 `revision`，与复核共用并发校验；版本过期返回 409。
- 发布在单事务内写版次并推进 revision，中断时旧版次与旧释放结果保留；导出包附 `release_published.json` 与 `release_versions.json`。

## 对账规则

输入=采纳原始行+隔离行+空行+同值重复行+重复表头。计价版本总额与生效票总额分别展示，避免把更正前后重复求和。最终应付=已付+待付，负待付为退款。

## 已知不覆盖

无称重设备实时接入，采用现场导出文件。无支付执行，只生成可复核结算证据。
