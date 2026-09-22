# 全球南方稿件交换

面向多国媒体联盟的稿件交换服务。新的稿件交换项目独立承担授权流转：为原稿、译稿、节选与再编辑维护权利与来源图，按地区、渠道与期限管理授权，支持并发修订的显式合并、幂等分发回执、按伙伴隔离的敏感附件、重启可续的待发队列，以及面向接收方的版本与署名核验。

## 运行

- `python3 -m unittest`：运行全部测试
- `python3 -m service.main`：启动服务（默认 `127.0.0.1:8000`，数据库文件 `exchange.db`，可用环境变量 `EXCHANGE_DB` 覆盖）

服务启动后，后台线程持续发出待发队列中的分发；队列持久化于数据库，重启后自动续发。

## API 概览

所有写操作要求调用方提供 `event_time`（事件时间），服务端另行记录接收时间。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/works` | 登记原稿/译稿/节选/再编辑，建立来源图节点 |
| GET | `/works/{id}` | 稿件与当前头版本 |
| POST | `/works/{id}/versions` | 提交修订；基于旧版本的修订成为分支，不落头版 |
| POST | `/works/{id}/merges` | 把分支版本显式合并进头版本 |
| GET | `/works/{id}/verification?version=&hash=` | 核验当前版本、署名义务、与原文的关系及授权状态 |
| POST | `/works/{id}/dispatch` | 分发给伙伴（须持有有效授权），写入待发队列 |
| POST | `/licenses` | 授权：按地区、渠道、期限生效，写明可修改范围 |
| GET | `/licenses/{id}` | 授权详情与状态 |
| POST | `/licenses/{id}/check` | 核验某时刻/地区/渠道是否可用 |
| POST | `/licenses/{id}/revoke` | 撤回授权：只阻止未来使用，已发布凭证保留 |
| POST | `/licenses/{id}/vouchers` | 登记发布凭证（发布时点须在授权覆盖内） |
| GET | `/licenses/{id}/vouchers` | 已发布凭证列表 |
| GET | `/deliveries/{id}` | 分发状态与回执计数 |
| POST | `/deliveries/{id}/receipts` | 登记回执；同一回执键重复提交不计数 |
| POST | `/attachments` | 上传附件；敏感附件按伙伴名单隔离 |
| GET | `/attachments/{id}?partner_id=` | 读取附件（敏感附件仅名单内伙伴可见） |
| GET | `/audit?entity=&entity_id=` | 只增审计日志 |

身份说明：本服务按调用方声明的稳定标识（`partner_id`、`editor_id` 等）执行业务规则，认证与身份签发由运行环境负责。
