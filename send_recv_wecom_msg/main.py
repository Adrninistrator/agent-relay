#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
企业微信消息收发 HTTP 服务

程序由两部分组成：
1. 后台 WSS 长连接：连接 wss://openws.work.weixin.qq.com，订阅鉴权、心跳保活、
   断线重连，持续接收用户回复的消息；
2. HTTP 服务：对外暴露“发送消息”和“获取回复”两个接口。

配置文件（默认同目录 config.toml）：
    bot_id  = "企业微信机器人 ID"
    secret  = "长连接专用密钥"
    chat_id = "接收消息的用户 userid"

接口：
    POST /send_message   {"sync_key": "...", "content": "..."}
    GET  /get_reply?sync_key=...
    GET  /health

运行：
    python main.py --config config.toml --host 127.0.0.1 --port 8080
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
import tomllib
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import websockets
from aiohttp import web

logger = logging.getLogger('wecom')

WSS_URL = 'wss://openws.work.weixin.qq.com'
HEARTBEAT_INTERVAL = 30       # 心跳间隔（秒）
REQUEST_TIMEOUT = 10          # 等待响应的超时（秒）
INITIAL_RECONNECT_DELAY = 1   # 网络异常初始重连延迟（秒）
MAX_RECONNECT_DELAY = 30      # 网络异常重连退避上限（秒）
SUBSCRIPTION_ERROR_DELAY = 30       # 订阅失败初始退避（秒），避免触发订阅频率保护
MAX_SUBSCRIPTION_ERROR_DELAY = 300  # 订阅失败退避上限（秒）
MSGID_CACHE_SIZE = 1000             # 消息 msgid 去重缓存条数上限
MAX_CONTENT_BYTES = 20480            # 企微 markdown 内容长度上限（UTF-8 字节）
SEQ_PATTERN = re.compile(r'^[0-9]{4}$')  # 消息序号：4 位 ASCII 数字


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    bot_id: str
    secret: str
    chat_id: str
    chat_type: int = 1      # 1: 单聊（userid） 2: 群聊（chatid）
    reply_ttl: int = 300     # 内存中消息数据的保存时长（秒），默认 5 分钟
    ws_url: str = WSS_URL    # 长连接地址，可通过配置覆盖（如本地 mock 服务端）


def load_config(path: str | Path) -> Config:
    """读取 TOML 配置（键：bot_id / secret / chat_id，兼容大写键）"""
    config_path = Path(path)
    if not config_path.is_file():
        raise FileNotFoundError(f'配置文件不存在: {config_path}')

    raw = config_path.read_bytes()
    if raw.startswith(b'\xef\xbb\xbf'):  # 兼容 Windows 记事本保存的 UTF-8 BOM
        raw = raw[3:]
    try:
        data = tomllib.loads(raw.decode('utf-8'))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f'配置文件 {config_path} 不是合法的 TOML: {exc}') from exc

    def get(key: str) -> str:
        value = data.get(key, data.get(key.upper(), ''))
        return str(value).strip()

    bot_id, secret, chat_id = get('bot_id'), get('secret'), get('chat_id')
    if not (bot_id and secret and chat_id):
        raise ValueError(f'配置文件 {config_path} 不完整，需要 bot_id / secret / chat_id')

    try:
        chat_type = int(data.get('chat_type', data.get('CHAT_TYPE', 1)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f'配置文件 {config_path} 的 chat_type 必须是整数') from exc
    if chat_type not in (1, 2):
        raise ValueError(f'配置文件 {config_path} 的 chat_type 只能是 1（单聊）或 2（群聊）')

    try:
        reply_ttl = int(data.get('reply_ttl', data.get('REPLY_TTL', 300)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f'配置文件 {config_path} 的 reply_ttl 必须是整数秒') from exc
    if reply_ttl <= 0:
        raise ValueError(f'配置文件 {config_path} 的 reply_ttl 必须大于 0')

    ws_url = get('ws_url') or WSS_URL
    if not ws_url.startswith(('ws://', 'wss://')):
        raise ValueError(f'配置文件 {config_path} 的 ws_url 必须以 ws:// 或 wss:// 开头')

    return Config(bot_id=bot_id, secret=secret, chat_id=chat_id,
                  chat_type=chat_type, reply_ttl=reply_ttl, ws_url=ws_url)


# --------------------------------------------------------------------------- #
# 内存存储：消息序号 -> 回复内容（带 TTL 自动过期）
# --------------------------------------------------------------------------- #
class SeqConflictError(RuntimeError):
    """消息序号在保存时长内已被占用"""

    def __init__(self, seq: str) -> None:
        super().__init__(f'消息序号 {seq} 已被占用')
        self.seq = seq


class MessageStore:
    """内存状态：消息序号 → 回复内容，条目按 TTL 自动过期"""

    def __init__(self, ttl: int = 300) -> None:
        self.ttl = ttl
        self._current_seq: Optional[str] = None
        self._entries: dict[str, dict[str, Any]] = {}

    @property
    def current_seq(self) -> Optional[str]:
        return self._current_seq

    def is_active(self, seq: str) -> bool:
        """序号是否仍在保存时长内"""
        entry = self._entries.get(seq)
        if entry is None:
            return False
        if time.time() >= entry['expire_at']:
            self._remove(seq)
            return False
        return True

    def register(self, seq: str) -> None:
        """登记消息序号，TTL 从此刻开始计算"""
        if self.is_active(seq):
            raise SeqConflictError(seq)
        self._entries[seq] = {'expire_at': time.time() + self.ttl, 'reply': None}

    def set_current(self, seq: Optional[str]) -> None:
        self._current_seq = seq

    def save_reply(self, content: str, raw: dict,
                   seq: Optional[str] = None) -> Optional[str]:
        """保存回复：指定序号则存该序号，否则存当前序号；序号无效返回 None"""
        target = seq if seq is not None else self._current_seq
        if target is None or not self.is_active(target):
            return None
        self._entries[target]['reply'] = {
            'content': content,
            'raw': raw,
            'received_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        return target

    def get_reply(self, seq: str) -> Optional[dict[str, Any]]:
        if not self.is_active(seq):
            return None
        return self._entries[seq]['reply']

    def remove(self, seq: str) -> None:
        """回滚登记（发送失败时调用）"""
        self._remove(seq)

    def _remove(self, seq: str) -> None:
        self._entries.pop(seq, None)
        if self._current_seq == seq:
            self._current_seq = None

    def purge_expired(self, now: Optional[float] = None) -> int:
        """清理过期序号及其回复，返回清理数量"""
        now = time.time() if now is None else now
        expired = [seq for seq, entry in self._entries.items()
                   if now >= entry['expire_at']]
        for seq in expired:
            self._entries.pop(seq, None)
            if self._current_seq == seq:
                self._current_seq = None
        return len(expired)

    async def cleanup_loop(self, interval: float) -> None:
        """后台定时清理过期数据"""
        while True:
            await asyncio.sleep(interval)
            removed = self.purge_expired()
            if removed:
                logger.info('已清理过期消息数据: %s 条', removed)

    def clear(self) -> None:
        self._current_seq = None
        self._entries.clear()


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class NotConnectedError(RuntimeError):
    """WSS 尚未连接 / 已断开"""


class WecomError(RuntimeError):
    """企业微信返回了非 0 错误码"""


class SubscriptionError(WecomError):
    """订阅（aibot_subscribe）失败：凭证错误、旧连接占用或触发订阅频率保护"""

    def __init__(self, errcode: int, errmsg: str) -> None:
        super().__init__(f'订阅失败: errcode={errcode}, errmsg={errmsg}')
        self.errcode = errcode
        self.errmsg = errmsg


# --------------------------------------------------------------------------- #
# 消息内容提取
# --------------------------------------------------------------------------- #
def extract_text(frame: dict) -> Optional[str]:
    """从事件帧中提取用户消息文本，兼容 text / markdown 等常见结构"""
    if not isinstance(frame, dict):
        return None
    body = frame.get('body')
    if not isinstance(body, dict):
        return None

    msgtype = body.get('msgtype')
    if msgtype == 'text':
        return (body.get('text') or {}).get('content')
    if msgtype == 'markdown':
        return (body.get('markdown') or {}).get('content')

    # 兜底：部分事件的消息体没有 msgtype
    text = (body.get('text') or {}).get('content')
    if isinstance(text, str) and text:
        return text
    content = body.get('content')
    if isinstance(content, str) and content:
        return content
    return None


def build_outgoing_message(content: str, seq: str) -> str:
    """拼接发送内容：调用方内容 + 带粗体强调的回复提示语"""
    return (
        f'{content}\n\n'
        f'---\n'
        f'当前消息序号：**{seq}**\n\n'
        f'**直接回复即可**，只需发送你的内容。\n\n'
        f'也可以分两行回复：\n'
        f'第一行只填序号 **{seq}**\n'
        f'第二行写你的内容\n\n'
        f'请不要引用本条消息。'
    )


def parse_reply(text: str) -> tuple[Optional[str], str]:
    """trim 后解析回复文本。

    - 两行及以上且首行为 4 位 ASCII 数字：返回 (序号, 第二行起的内容)
    - 其余情况：返回 (None, 整体文本)，由调用方按单行处理
    """
    text = text.strip()
    lines = text.splitlines()
    if len(lines) >= 2:
        first = lines[0].strip()
        if SEQ_PATTERN.match(first):
            return first, '\n'.join(lines[1:]).strip()
    return None, text


# --------------------------------------------------------------------------- #
# WSS 长连接客户端
# --------------------------------------------------------------------------- #
class WecomWssClient:
    """负责连接、订阅、心跳、断线重连，以及请求/响应配对和事件分发"""

    def __init__(self, config: Config, store: MessageStore) -> None:
        self.config = config
        self.store = store
        self._ws: Any = None
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._seen_msgids: OrderedDict[str, None] = OrderedDict()

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_forever(), name='wecom-wss')

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run_forever(self) -> None:
        network_delay = INITIAL_RECONNECT_DELAY
        subscribe_delay = SUBSCRIPTION_ERROR_DELAY
        while not self._stop.is_set():
            try:
                await self._session()
                logger.info('WSS 连接已结束，准备重连')
                network_delay = INITIAL_RECONNECT_DELAY
                subscribe_delay = SUBSCRIPTION_ERROR_DELAY
            except asyncio.CancelledError:
                raise
            except SubscriptionError as exc:
                # 凭证错误 / 旧连接占用 / 订阅频率保护：
                # 使用独立的长退避，避免反复订阅触发更严格的限制
                logger.warning('订阅失败: errcode=%s, errmsg=%s', exc.errcode, exc.errmsg)
                if self._stop.is_set():
                    break
                logger.info('%s 秒后重新订阅', subscribe_delay)
                await asyncio.sleep(subscribe_delay)
                subscribe_delay = min(subscribe_delay * 2, MAX_SUBSCRIPTION_ERROR_DELAY)
                continue
            except Exception as exc:  # 网络异常等
                logger.warning('WSS 异常: %s: %s', type(exc).__name__, exc)

            if self._stop.is_set():
                break
            logger.info('%s 秒后重连 WSS', network_delay)
            await asyncio.sleep(network_delay)
            network_delay = min(network_delay * 2, MAX_RECONNECT_DELAY)

    async def _session(self) -> None:
        # ping_interval=None：企微服务端不兼容 WebSocket 协议层 Ping 控制帧，
        # 收到后会以 1002 protocol error 断开，保活统一使用应用层 cmd=ping
        async with websockets.connect(self.config.ws_url, ping_interval=None) as ws:
            self._ws = ws
            logger.info('WSS 已连接: %s', self.config.ws_url)

            # 接收循环必须先启动，否则订阅响应无人读取
            tasks = [asyncio.create_task(self._recv_loop(ws), name='recv')]
            try:
                resp = await self._request(ws, 'aibot_subscribe', {
                    'bot_id': self.config.bot_id,
                    'secret': self.config.secret,
                })
                if resp.get('errcode') != 0:
                    raise SubscriptionError(resp.get('errcode'), resp.get('errmsg'))
                logger.info('订阅成功')
                self._connected.set()

                tasks.append(asyncio.create_task(self._heartbeat_loop(ws), name='heartbeat'))
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:  # 先结束的任务若抛异常，交由外层触发重连
                    task.result()
            finally:
                self._connected.clear()
                self._ws = None
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    # ---------- 心跳 ----------
    async def _heartbeat_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            resp = await self._request(ws, 'ping')
            if resp.get('errcode') != 0:
                raise WecomError(
                    f"心跳失败: errcode={resp.get('errcode')}, errmsg={resp.get('errmsg')}"
                )
            logger.debug('心跳正常')

    # ---------- 接收 ----------
    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning('收到非 JSON 数据: %s', str(raw)[:200])
                continue

            headers = frame.get('headers') if isinstance(frame, dict) else None
            req_id = headers.get('req_id') if isinstance(headers, dict) else None
            future = self._pending.get(req_id)

            # 响应帧只有 headers/errcode/errmsg，不带 cmd；带 cmd 的是服务端推送事件
            is_response = isinstance(frame, dict) and 'cmd' not in frame
            if is_response and future is not None and not future.done():
                future.set_result(frame)  # 请求响应：唤醒等待方
            else:
                self._on_event(frame)     # 服务端主动推送的事件

    def _is_duplicate_msgid(self, msgid: str) -> bool:
        """按 msgid 去重：返回 True 表示已处理过，应忽略（有界 FIFO 缓存）"""
        if msgid in self._seen_msgids:
            return True
        self._seen_msgids[msgid] = None
        if len(self._seen_msgids) > MSGID_CACHE_SIZE:
            self._seen_msgids.popitem(last=False)
        return False

    def _on_event(self, frame: dict) -> None:
        body = frame.get('body') if isinstance(frame, dict) else None
        msgid = body.get('msgid') if isinstance(body, dict) else None
        if msgid and self._is_duplicate_msgid(msgid):
            logger.info('收到重复消息 msgid=%s，已忽略', msgid)
            return

        raw_text = extract_text(frame)
        if raw_text is None:
            logger.info('收到非消息事件: %s',
                        json.dumps(frame, ensure_ascii=False)[:300])
            return

        seq, content = parse_reply(raw_text)
        target = seq if (seq is not None and self.store.is_active(seq)) else None
        if seq is not None and target is None:
            # 序号不存在或已过期：按单行方式处理（用第二行内容存当前序号）
            logger.warning('回复中的消息序号 %s 不存在或已过期，按单行消息处理', seq)

        saved_seq = self.store.save_reply(content, frame, target)
        if saved_seq is None:
            logger.warning('收到消息但当前没有有效的消息序号，已忽略: %s', content)
        else:
            logger.info('收到回复 [seq=%s]: %s', saved_seq, content)

    # ---------- 请求 / 响应 ----------
    async def _request(self, ws, cmd: str, body: Optional[dict] = None,
                       timeout: float = REQUEST_TIMEOUT) -> dict:
        req_id = uuid.uuid4().hex
        frame: dict[str, Any] = {'cmd': cmd, 'headers': {'req_id': req_id}}
        if body is not None:
            frame['body'] = body

        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            async with self._send_lock:
                await ws.send(json.dumps(frame, ensure_ascii=False))
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(req_id, None)

    # ---------- 对外：发送消息 ----------
    async def send_message(self, content: str) -> dict:
        """向配置的 CHAT_ID 发送一条 markdown 消息"""
        ws = self._ws
        if ws is None or not self._connected.is_set():
            raise NotConnectedError('企业微信长连接尚未就绪')

        try:
            resp = await self._request(ws, 'aibot_send_msg', {
                'chatid': self.config.chat_id,
                'chat_type': self.config.chat_type,
                'msgtype': 'markdown',
                'markdown': {'content': content},
            })
        except websockets.ConnectionClosed as exc:
            raise NotConnectedError(f'企业微信长连接已断开: {exc}') from exc

        if resp.get('errcode') != 0:
            raise WecomError(
                f"发送失败: errcode={resp.get('errcode')}, errmsg={resp.get('errmsg')}"
            )
        return resp


# --------------------------------------------------------------------------- #
# HTTP 接口
# --------------------------------------------------------------------------- #
WSS_KEY = web.AppKey('wss', WecomWssClient)
STORE_KEY = web.AppKey('store', MessageStore)


def is_truthy(value: Optional[str]) -> bool:
    """解析 query 参数中的布尔值，默认视为 False"""
    if value is None:
        return False
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


async def handle_send_message(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({'errcode': 400, 'errmsg': '请求体必须是 JSON'}, status=400)
    if not isinstance(data, dict):
        return web.json_response({'errcode': 400, 'errmsg': '请求体必须是 JSON 对象'}, status=400)

    seq = data.get('seq')
    content = data.get('content')
    if not isinstance(seq, str) or not SEQ_PATTERN.match(seq):
        return web.json_response(
            {'errcode': 400, 'errmsg': 'seq 必填，且必须是 4 位数字字符串'}, status=400)
    if not isinstance(content, str) or not content.strip():
        return web.json_response({'errcode': 400, 'errmsg': 'content 必填且不能为空'}, status=400)
    if len(content.encode('utf-8')) > MAX_CONTENT_BYTES:
        return web.json_response(
            {'errcode': 400, 'errmsg': f'content 超过 {MAX_CONTENT_BYTES} 字节'}, status=400)

    wss: WecomWssClient = request.app[WSS_KEY]
    store: MessageStore = request.app[STORE_KEY]

    # 请求到达即登记并开始计算 TTL，保证发送过程中用户秒回也能正确归属
    try:
        store.register(seq)
    except SeqConflictError:
        return web.json_response(
            {'errcode': 409, 'errmsg': f'消息序号 {seq} 已被占用，请重新生成'}, status=409)

    previous_seq = store.current_seq
    store.set_current(seq)

    message = build_outgoing_message(content, seq)
    try:
        await wss.send_message(message)
    except NotConnectedError as exc:
        store.remove(seq)
        store.set_current(previous_seq)
        return web.json_response({'errcode': 503, 'errmsg': str(exc)}, status=503)
    except WecomError as exc:
        store.remove(seq)
        store.set_current(previous_seq)
        return web.json_response({'errcode': 502, 'errmsg': str(exc)}, status=502)
    except Exception as exc:
        store.remove(seq)
        store.set_current(previous_seq)
        logger.exception('发送消息失败')
        return web.json_response(
            {'errcode': 500, 'errmsg': f'{type(exc).__name__}: {exc}'}, status=500
        )

    logger.info('消息已发送 [seq=%s]', seq)
    return web.json_response({'errcode': 0, 'errmsg': 'ok', 'seq': seq})


async def handle_get_reply(request: web.Request) -> web.Response:
    seq = (request.query.get('seq') or '').strip()
    detail = is_truthy(request.query.get('detail'))

    if seq and not SEQ_PATTERN.match(seq):
        return web.json_response({
            'success': False,
            'message': 'seq 必须是 4 位数字字符串',
            'reply': None,
        }, status=400)

    store: MessageStore = request.app[STORE_KEY]
    target = seq or store.current_seq  # 不指定时按最后一条成功发送的消息

    if not target:
        body: dict[str, Any] = {
            'success': False,
            'message': '暂无消息',
            'reply': None,
        }
        if detail:
            body['seq'] = None
        return web.json_response(body)

    item = store.get_reply(target)
    if item is None:
        body = {'success': False, 'message': '暂无回复', 'reply': None}
        if detail:
            body['seq'] = target
        return web.json_response(body)

    body = {
        'success': True,
        'message': 'ok',
        'reply': item['content'],
    }
    if detail:  # 详细信息模式：追加序号、接收时间与企微原始事件帧
        body['seq'] = target
        body['received_at'] = item['received_at']
        body['raw'] = item['raw']
    return web.json_response(body)


async def handle_health(request: web.Request) -> web.Response:
    wss: WecomWssClient = request.app[WSS_KEY]
    return web.json_response({
        'status': 'ok',
        'connected': wss._connected.is_set(),  # noqa: SLF001 - 仅用于状态查询
    })


def create_app(wss: WecomWssClient, store: MessageStore) -> web.Application:
    """创建 aiohttp 应用，便于被测试直接复用"""
    app = web.Application()
    app[WSS_KEY] = wss
    app[STORE_KEY] = store
    app.router.add_post('/send_message', handle_send_message)
    app.router.add_get('/get_reply', handle_get_reply)
    app.router.add_get('/health', handle_health)
    return app


# --------------------------------------------------------------------------- #
# 启动入口
# --------------------------------------------------------------------------- #
async def serve(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    store = MessageStore(config.reply_ttl)
    wss = WecomWssClient(config, store)
    await wss.start()

    # 清理间隔不超过保存时长的 1/10，最短 10 秒、最长 60 秒
    cleanup_interval = max(10, min(config.reply_ttl // 10, 60))
    cleanup_task = asyncio.create_task(
        store.cleanup_loop(cleanup_interval), name='cleanup')

    app = create_app(wss, store)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    await site.start()
    logger.info('HTTP 服务已启动: http://%s:%s（reply_ttl=%s 秒，清理间隔 %s 秒）',
                args.host, args.port, config.reply_ttl, cleanup_interval)

    try:
        await asyncio.Event().wait()
    finally:
        cleanup_task.cancel()
        await asyncio.gather(cleanup_task, return_exceptions=True)
        await wss.stop()
        await runner.cleanup()
        logger.info('服务已停止')


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='企业微信消息收发 HTTP 服务')
    parser.add_argument('--config', default=str(Path(__file__).with_name('config.toml')),
                        help='配置文件路径，默认同目录 config.toml')
    parser.add_argument('--host', default='127.0.0.1', help='监听地址，默认 127.0.0.1')
    parser.add_argument('--port', type=int, default=8080, help='监听端口，默认 8080')
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    )
    args = parse_args(argv)
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        logger.info('收到中断信号，进程退出')


if __name__ == '__main__':
    main()