# 企业微信消息收发 HTTP 服务

> 面向「脚本获取短信验证码 / Token」场景的使用指南见 [docs/企业微信传递验证码与Token.md](docs/企业微信传递验证码与Token.md)。

通过 HTTP 接口向外提供两项能力：

1. **发送企业微信消息**：调用企业微信长连接接口，向配置中指定的用户发送消息；
2. **获取企业微信回复消息**：按「消息序号」查询用户对该消息的回复。

程序内部维护一条到 `wss://openws.work.weixin.qq.com` 的长连接，负责订阅鉴权、心跳保活、
断线重连，并实时接收用户回复，按消息序号存入内存；数据按配置的保存时长自动清理。

## 目录结构

```
send_recv_wecom_msg/
├── main.py              # 服务主程序（HTTP 服务 + WSS 长连接）
├── config.toml          # 配置文件（含密钥，勿提交到仓库）
├── requirements.txt     # 依赖清单
├── README.md
└── tests/
    ├── test_main.py          # 单元测试（不访问真实网络）
    ├── send_message.py       # 可直接执行：发送消息验证
    ├── get_reply.py          # 可直接执行：获取回复验证
    ├── mock_wecom_server.py  # 本地模拟企业微信长连接服务端
    └── mock_e2e.py           # 一键 mock 端到端场景验证
```

## 安装依赖

```bash
pip install -r requirements.txt
```

- Python 3.11+（配置文件解析使用标准库 `tomllib`）
- `aiohttp`：HTTP 服务
- `websockets`：WSS 长连接

## 配置文件

`config.toml` 采用 TOML 格式，支持 `#` 注释：

```toml
# 企业微信机器人配置
bot_id = "aibx2OJB6UUeBhr78rOzSbcOhh_FKUNQ3xC"
secret = "BRvNk1Oty9GHNDGAaQ7pA8xXfhMvsaaiasp10TgNT4O"
chat_id = "easonzheng"
chat_type = 1
reply_ttl = 300
```

- `bot_id` / `secret`：企业微信后台获取的机器人 ID 与长连接密钥
- `chat_id`：会话 ID，单聊填用户 userid，群聊填群 chatid
- `chat_type`：会话类型，`1` 单聊（默认）、`2` 群聊
- `reply_ttl`：内存中消息数据的保存时长（秒），默认 `300`（5 分钟）
- `ws_url`：长连接地址，默认官方 `wss://openws.work.weixin.qq.com`，本地联调时可指向 mock 服务端

键名同时兼容大写写法（`BOT_ID` / `SECRET` / `CHAT_ID` / `CHAT_TYPE` / `REPLY_TTL` / `WS_URL`）。

## 启动

```bash
python main.py --config config.toml --host 127.0.0.1 --port 8080
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--config` | 同目录 `config.toml` | 配置文件路径 |
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8080` | 监听端口 |

启动后日志会显示 WSS 连接、订阅结果、保存时长与清理间隔；`GET /health` 可查询连接状态。

## 接口说明

### 1. 发送企业微信消息

```
POST /send_message
Content-Type: application/json
```

请求体：

```json
{
  "seq": "0421",
  "content": "您的订单已发货，请确认收货"
}
```

- `seq`：消息序号，**由调用方生成**，必须是 4 位数字字符串（`0000`–`9999`），在保存时长内不可重复
- `content`：消息内容，UTF-8 编码不超过 20480 字节

服务端会在内容后自动追加回复提示语（Markdown 格式，关键动作用粗体强调），实际发送内容为：

```
{content}

---
当前消息序号：**{seq}**

**直接回复即可**，只需发送你的内容。

也可以分两行回复：
第一行只填序号 **{seq}**
第二行写你的内容

请不要引用本条消息。
```

成功响应：

```json
{
  "errcode": 0,
  "errmsg": "ok",
  "seq": "0421"
}
```

错误响应：

| HTTP 状态 | 场景 |
| --- | --- |
| `400` | 请求体非 JSON、`seq` 不是 4 位数字字符串、`content` 为空或超过 20480 字节 |
| `409` | 该 `seq` 在保存时长内已被占用（调用方需重新生成） |
| `502` | 企业微信返回非 0 错误码（如频率超限 846607） |
| `503` | WSS 长连接尚未就绪或已断开 |
| `500` | 其他内部错误 |

> 保存时长自服务端**接收到请求时**开始计算；发送失败会回滚登记并释放 `seq`。

### 2. 获取企业微信回复消息

```
GET /get_reply?seq=0421[&detail=true]
GET /get_reply[?detail=true]          # 不传 seq：按最后一条消息
```

- `seq`：消息序号，可选；不传时按最后一条成功发送的消息查询
- `detail`：是否返回详细信息，默认否；支持 `true/1/yes/on`

**默认模式**（只返回三项）：

已收到回复：

```json
{
  "success": true,
  "message": "ok",
  "reply": "确认收货"
}
```

尚未收到回复：

```json
{
  "success": false,
  "message": "暂无回复",
  "reply": null
}
```

没有任何消息（或已过期）：

```json
{
  "success": false,
  "message": "暂无消息",
  "reply": null
}
```

**详细信息模式**（`detail=true`）在默认字段基础上追加：

| 字段 | 说明 |
| --- | --- |
| `seq` | 实际查询的消息序号 |
| `received_at` | 服务端收到回复的时间 |
| `raw` | 企业微信推送的原始事件帧（`msgid`、`from.userid`、`chattype`、`msgtype`、`response_url` 等） |

```json
{
  "success": true,
  "message": "ok",
  "reply": "确认收货",
  "seq": "0421",
  "received_at": "2026-10-28 10:30:12",
  "raw": { "cmd": "aibot_msg_callback", "body": { ... } }
}
```

外部按需轮询该接口即可获取回复（建议间隔 1~2 秒）。

### 回复的两种格式

用户收到消息后，可以按以下任一方式回复（服务端会先 `trim` 首尾空白，且仅处理文本消息）：

| 回复格式 | 归属规则 |
| --- | --- |
| 两行：第一行为 4 位数字序号，第二行（及之后）为内容 | 按该序号归属 |
| 一行：只有内容 | 归属到最近一条成功发送的消息 |

两行示例：

```
0421
这是给消息 0421 的回复
```

若两行中的序号不存在或已过期，则取第二行内容按单行方式处理，归属到最近一条消息。

### 3. 健康检查

```
GET /health
```

```json
{ "status": "ok", "connected": true }
```

`connected` 表示 WSS 长连接是否已连接并完成订阅认证。

## 工作流程

```
外部系统                 main.py                     企业微信服务端
   |  POST /send_message    |                              |
   |  {seq, content}        |                              |
   |----------------------->|  登记 seq（开始计时）          |
   |                        |  aibot_send_msg (WSS)        |
   |                        |----------------------------->|
   |                        |  记录 current_seq=seq         |
   |                        |                              |
   |                        |  <---- 用户回复的消息推送 ------|
   |                        |  两行→按 seq 归属             |
   |                        |  单行→current_seq            |
   |                        |                              |
   |  GET /get_reply?seq    |                              |
   |----------------------->|  返回该 seq 的回复            |
```

说明：企业微信长连接是**服务端推送**模型，并非 HTTP 轮询。程序在后台持续接收事件，
对外仍以“轮询 `/get_reply`”的方式提供查询能力，符合调用方的轮询语义。

## 可靠性机制

### 数据自动清理

内存中每条消息数据（序号、回复内容）从**接收到发送请求时**开始计时，超过 `reply_ttl`
后由后台任务清理：

- 清理间隔：`max(10, min(reply_ttl / 10, 60))` 秒（默认 300 秒 TTL → 30 秒清理一次）
- 清理后该序号可被重新使用，旧序号的回复查询返回「暂无回复」
- 若 `current_seq` 指向的消息已过期，则会被清空，「按最后一条消息」查询返回「暂无消息」

### 消息去重

企业微信以 `body.msgid` 标识每条回调，网络抖动或重试时可能重复推送同一条消息。
服务端按 `msgid` 做有界 FIFO 去重（默认缓存最近 1000 条），重复消息直接忽略并记录日志：

```
收到重复消息 msgid=xxx，已忽略
```

### 重连与退避

| 场景 | 退避策略 |
| --- | --- |
| 网络异常 / 连接断开 / 心跳失败 | 1s → 2s → 4s → … → 30s 上限 |
| 订阅失败（如 `853000 invalid bot_id or secret`） | 30s → 60s → 120s → 240s → 300s 上限 |

订阅失败单独使用长退避，原因：

- 官方对 `aibot_subscribe` 有**频率保护**，快速重试会加剧限制；
- `853000` 也可能由「同一机器人已有旧连接」引起（新连接订阅才会踢掉旧连接），需要给服务端留出释放时间。

### 单连接限制

官方规定：**每个机器人同一时间只能保持一个有效长连接**，新连接完成订阅后会踢掉旧连接，
旧连接会收到 `disconnected_event` 事件。因此：

- 不要同时启动多个实例连接同一个 `bot_id`；
- 高可用应采用**主备切换**，而不是多活。

## 测试与验证

### 1. 单元测试（无需启动服务）

```bash
python -m unittest discover -s tests -v
```

覆盖：

- 配置文件解析（TOML、大写键、BOM、`reply_ttl` 校验、异常场景）
- 内存存储（序号登记与冲突、TTL 过期、清理、回滚、按序号存/取）
- 发送内容拼接与回复解析（4 位数字校验、全角数字、单行/两行、CRLF、trim）
- WSS 事件分发（单行归属当前序号、两行按序号归属、序号过期降级）
- msgid 去重（相同/不同 msgid、无 msgid、缓存上限淘汰）
- 重连退避（网络异常 1s 起、订阅失败 30s 起）
- HTTP 接口（参数校验、409 冲突、内容超长、未连接 503、企微错误 502、查询三种方式、detail 模式、过期查询）

测试使用假的 WSS 客户端，不访问真实企业微信网络。

### 2. 端到端验证（必须先启动服务）

**终端 1：启动服务**

```bash
python main.py --port 8080
```

确认服务与长连接就绪：

```bash
curl http://127.0.0.1:8080/health
# {"status": "ok", "connected": true}
```

**终端 2：发送消息（零参数即可执行，会自动生成 4 位随机序号）**

```bash
python tests/send_message.py
```

**在企微中回复机器人后，获取回复：**

```bash
# 零参数：按最后一条消息查询，轮询等待最长 60 秒
python tests/get_reply.py

# 指定序号
python tests/get_reply.py --seq 0421

# 只查一次，不等待
python tests/get_reply.py --no-watch

# 返回详细信息（seq / received_at / raw）
python tests/get_reply.py --detail --no-watch
```

两个验证脚本均通过 HTTP 调用服务，支持 `--url` 指定地址（默认 `http://127.0.0.1:8080`）。
服务未启动时会提示“无法连接服务，请先启动 main.py”。

### 3. 本地 mock 端到端验证（不连接真实企业微信）

一键运行全部场景：

```bash
python tests/mock_e2e.py
```

脚本会自动：启动本地 mock 服务端 → 用临时配置启动 `main.py`（`ws_url` 指向 mock）→
依次验证各场景 → 结束后清理进程与临时文件。

覆盖场景：

| 场景 | 预期 |
| --- | --- |
| 发送消息 | 200，mock 收到完整内容（含序号提示语） |
| 两行回复 | 按序号归属 |
| 单行回复 | 归属最近一条消息 |
| 无效序号 | 降级按单行处理 |
| 重复 msgid | 第二次被忽略 |
| 重复序号 | 409 |
| 3 位 / 全角序号 | 400 |
| 企微发送失败 | 502，且序号已释放可复用 |
| `detail=true` | 返回 `seq` / `received_at` / `raw` |
| 主动断开连接 | 自动重连成功 |

也可以手动联调：

```bash
# 终端 1：mock 服务端（WSS 9000 / 控制接口 9001）
python tests/mock_wecom_server.py --ws-port 9000 --http-port 9001

# 终端 2：主服务（先在 config.toml 设置 ws_url = "ws://127.0.0.1:9000"）
python main.py --port 8080

# 注入一条两行格式的用户回复
curl -X POST http://127.0.0.1:9001/mock/reply \
     -H "Content-Type: application/json" \
     -d "{\"content\":\"0421\\n我的回复\"}"

# 查看客户端发出的消息
curl http://127.0.0.1:9001/mock/sent
```

mock 控制接口：

| 接口 | 说明 |
| --- | --- |
| `POST /mock/reply` | 注入用户回复（`content` 必填，可选 `msgid`、`msgtype`） |
| `GET /mock/sent` | 查看客户端通过 `aibot_send_msg` 发出的消息体 |
| `GET /mock/frames` | 查看全部收发帧（调试用） |
| `GET /mock/status` | 当前连接数与错误码配置 |
| `POST /mock/config` | 模拟错误码：`subscribe_errcode` / `send_errcode` |
| `POST /mock/close` | 断开所有连接（验证断线重连） |
| `POST /mock/reset` | 清空记录并恢复默认配置 |

## 注意事项

1. **回复归属规则**：两行回复按序号精确归属；单行回复归属到最近一条成功发送的消息。
   多人并发回复时，不带序号可能串到最新的消息，建议在消息内容中引导用户按两行格式回复。
2. **序号容量**：4 位数字只有 10000 个取值。单次发送冲突概率约为「当前活跃消息数 / 10000」，
   默认 5 分钟 TTL 下通常远低于 1%；若业务吞吐很高，应缩短 TTL 或扩展序号位数。
3. **TTL 偏短的风险**：默认 5 分钟，用户超时回复会导致数据已清理，回复按降级规则处理或丢弃。
   可根据业务调整 `reply_ttl`。
4. **频率限制**：同一会话的回复与主动推送合计限制为 30 条/分钟、1000 条/小时。
5. **推送前提**：企业微信要求用户先在该会话中给机器人发过消息，之后机器人才能主动推送。
6. **内存存储**：数据保存在进程内存中，进程重启后丢失；受单连接限制，只能单实例部署。
7. **密钥安全**：`config.toml` 含机器人密钥，请勿提交到 Git 仓库，生产环境建议改用环境变量
   或密钥管理服务。
8. **心跳**：应用层每 30 秒发送一次 `ping`；同时必须关闭 `websockets` 库自带的协议层 Ping
   （`ping_interval=None`），否则企微服务端会以 `1002 protocol error` 断开连接。