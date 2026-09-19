# 造血干细胞捐献协同

支撑非血缘造血干细胞捐献的完整协同领域服务：患者检索、候选排序、志愿者联络、
体检、采集、跨地运输与顺位替补。患者与志愿者处于不同身份域，业务流程中只出现
按病例派生、不可反查的别名；真实身份与业务数据分离保存，紧急身份解封需双人
批准并自动到期。

## 领域规则

- **患者侧最小提交**：只提交检索所需分型与临床时限；患者真实身份不进入检索流程。
- **候选排序可回溯**：排序快照保留算法版本（`hla-match-1.2.0`）、10/10 分级
  （高分辨全合=1，前缀相容=0.5）、逐位点明细、排序依据与每位排除者的理由
  （体检暂缓、分型不全、同意范围缺失、承诺互斥、捐献后冷却、不可用窗口等）。
- **同意范围与冷静期**：每次联络校验最新同意范围（初次告知/确认请求/体检/采集/
  随访），撤回同意立即生效；同类联络有冷静期，跨病例共享全局联络间隔，
  初次告知后强制 48 小时考虑期。
- **通知幂等**：以幂等键去重，重试不会重复通知，重复尝试写审计可查。
- **一人多流程隔离**：同一志愿者跨病例得到不同别名；待答复决定互斥；
  采集窗口承诺互斥；实际采集全局唯一且有 90 天捐献后冷却，杜绝重复采集。
  所有互斥结论都不回传其他病例的任何信息。
- **优先级替代方案**：体检异常→针对性复检/顺位替补/扩大检索；航班延误→
  改签/医护手提/紧急地面转运；预计到达超过 24 小时产品时效窗时自动升级为
  时效风险方案。替补严格按排序快照顺位激活，替补依据入审计。
- **里程碑时效窗**：采集医院、移植医院、运输方按病例指派上报里程碑，时间一律
  规范为 UTC 绝对值比较并保留提交时区；超窗送达直接拒绝交付。
- **身份解封（break-glass）**：协调员发起必须填写依据并限定病例+对象；
  两名身份官分别批准（不可重复批准、请求人身份官也不能自批）；许可带 TTL
  （默认 60 分钟，上限 240 分钟），到期自动失效；每次读取真实身份都写审计，
  审计内容只含依据不含明文身份。
- **只增审计**：所有匹配、联络、状态迁移、替补、延误、解封事件以哈希链追加，
  导出时可校验完整性。

## 运行

```bash
python3 service.py --check       # 基础自检
python3 service.py --selftest    # 双病例端到端剧本（含跨时区延误/替补/解封）
python3 service.py --port 8000   # HTTP 服务
npm test                         # 全部测试（48 个用例）
```

## HTTP 接口（节选）

开发期鉴权通过请求头 `X-Actor-Id` / `X-Actor-Name` / `X-Actor-Roles` 表明身份；
联调可用 `POST /test/clock/advance` 推进虚拟时钟。所有写操作接受
`idempotency_key`。

| 方法 & 路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /donors`、`/patients` | identity_officer | 身份与业务画像分离登记 |
| `POST /donors/{id}/consent:grant\|withdraw` | identity_officer | 同意范围变更 |
| `POST /cases` | coordinator | 患者侧仅提交分型与临床时限 |
| `POST /cases/{id}/search`、`/rerank` | coordinator | 排序/扩大检索 |
| `POST /cases/{id}/notifications/*` | coordinator | 告知/确认/体检/采集通知 |
| `POST /cases/{id}/decisions`、`/withdrawals` | coordinator | 志愿者决定/撤回 |
| `POST /cases/{id}/exams/report`、`/exams/reexam` | collection_hospital | 体检与复检结论 |
| `POST /cases/{id}/collection`、`/pickup`、`/delays` | hospital/courier | 采集、取件、航班延误 |
| `POST /cases/{id}/delivery`、`/infusion` | courier/transplant_hospital | 送达、回输 |
| `POST /cases/{id}/backup/activate`、`/release-active` | coordinator | 顺位替补 |
| `POST /cases/{id}/unseals/request` | coordinator | 紧急解封申请（依据+对象+TTL） |
| `POST /cases/{id}/unseals/{grant}/approve` | identity_officer | 双人批准 |
| `POST /cases/{id}/reveal` | coordinator（持有效许可） | 读取真实身份 |
| `GET /audit?case_id=&action=` | auditor | 审计导出与哈希链校验 |

## 代码结构

```
coordination/
  clock.py      可注入虚拟时钟（UTC 规范化，跨时区联调）
  errors.py     稳定错误码
  audit.py      只增哈希链审计
  identity.py   角色、病例作用域别名 HMAC、身份保险库、双人解封与 TTL
  matching.py   分型比较、10/10 分级、算法版本与排除理由
  comms.py      同意范围、冷静期、考虑期、联络幂等账本
  workflow.py   状态机、承诺/采集账本、产品时效窗、延误与替代方案
  store.py      业务/身份分键存储
  app.py        用例编排与别名视图
  api.py        HTTP JSON 路由
scenario.py     联调世界（2 患者、7 志愿者、跨地医院/运输方）
selftest.py     端到端剧本（自检与集成测试共用）
test_domain.py / test_workflow.py / test_integration.py / service_contract.py
```
