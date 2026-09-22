# 全球南方稿件交换

面向多机构协作的稿件授权流转服务：在原稿、译稿、节选与再编辑之间建立权利与
来源图，支持按地区、渠道、期限生效的授权，撤回只阻止未来使用并保留已发布
凭证，并发修订必须显式合并，投递幂等且重启续发，敏感附件按伙伴隔离，任何
接收方都能核验当前版本、署名义务及其与原文的关系。

## 运行

```bash
python3 -m unittest            # 运行测试
SERVICE_DATA_DIR=./.data python3 -m service.main   # 启动服务（127.0.0.1:8000）
```

所有业务数据写入 `SERVICE_DATA_DIR`（默认 `./.data`）：`event.log` 为只追加
哈希链事件日志，`attachments/` 存放附件二进制。业务数据与敏感配置应放在受控
运行环境。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/audit/verify` | 复核事件哈希链 |
| POST | `/v1/originals` `/v1/translations` `/v1/excerpts` `/v1/revisions` `/v1/merges` | 登记稿件与派生版本 |
| GET | `/v1/artifacts/{id}` | 当前版本视图（内容哈希、署名、语境） |
| GET | `/v1/artifacts/{id}/provenance` | 完整来源链与原文关系 |
| GET | `/v1/artifacts/{id}/rights?partner_id&region&channel&at` | 授权状态、可修改范围、署名义务 |
| POST | `/v1/artifacts/{id}/withdraw` | 撤回（只阻止未来使用） |
| GET | `/v1/works/{id}` | 作品版本图与当前首节点 |
| POST | `/v1/grants` | 签发授权（伙伴/地区/渠道/动作/期限） |
| POST | `/v1/grants/{id}/revoke` | 撤销授权 |
| POST | `/v1/artifacts/{id}/attachments` | 登记附件（含伙伴作用域） |
| PUT/GET | `/v1/artifacts/{id}/attachments/{att}/content` | 上传/按隔离策略读取附件 |
| POST | `/v1/deliveries` | 投递（`delivery_id` 幂等，失败排队重启续发） |
| POST | `/v1/deliveries/{id}/ack` | 接收方回执（幂等） |
| GET | `/v1/deliveries/{id}/receipt` | 核验收据、当前版本与当前权利 |
| POST | `/v1/queue/drain` | 手动触发待发队列续发 |

领域规则见 [docs/domain.md](docs/domain.md)。
