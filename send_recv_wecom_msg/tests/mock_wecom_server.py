#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
模拟企业微信长连接服务端（本地验证用，不连接真实企业微信）

能力：
- WebSocket 服务：处理 aibot_subscribe / ping / aibot_send_msg
- HTTP 控制接口：注入用户回复、查看客户端发出的消息、模拟服务端异常

用法：
    python tests/mock_wecom_server.py --ws-port 9000 --http-port 9001

控制接口：
    POST /mock/reply   注入一条用户回复
        {"content": "0001\\n两行回复"}              # 两行：序号 + 内容
        {"content": "单行回复"}                     # 单行
        {"content": "...", "msgid": "fixed-id"}     # 指定 msgid（用于验证重复推送）
    GET  /mock/sent     查看客户端通过 aibot_send_msg 发出的消息体
    GET  /mock/frames   查看全部收发帧（调试）
    GET  /mock/status   当前连接数与错误码配置
    POST /mock/config   模拟服务端错误
        {"subscribe_errcode": 853000, "send_errcode": 846607}
    POST /mock/close    断开所有客户端连接（模拟断线）
    POST /mock/reset    清空记录并恢复默认配置
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import uuid

import websockets
from aiohttp import web

logger = logging.getLogger('mock-wecom')


class MockWecomServer:
    def __init__(self) -> None:
        self.clients: set = set()
        self.sent_messages: list[dict] = []
        self.frames: list[dict] = []
        self.subscribe_errcode = 0
        self.subscribe_errmsg = 'ok'
        self.send_errcode = 0
        self.send_errmsg = 'ok'

    # ---------- WebSocket ----------
    async def handle_ws(self, ws) -> None:
        self.clients.add(ws)
        logger.info('客户端已连接（当前 %s 个）', len(self.clients))
        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning('收到非 JSON 数据: %s', str(raw)[:200])
                    continue

                self.frames.append({
                    'direction': 'in',
                    'frame': frame,
                    'at': time.strftime('%H:%M:%S'),
                })

                cmd = frame.get('cmd')
                req_id = (frame.get('headers') or {}).get('req_id')

                if cmd == 'aibot_subscribe':
                    errcode, errmsg = self.subscribe_errcode, self.subscribe_errmsg
                elif cmd == 'aibot_send_msg':
                    self.sent_messages.append(frame.get('body') or {})
                    errcode, errmsg = self.send_errcode, self.send_errmsg
                else:  # ping 及其他命令
                    errcode, errmsg = 0, 'ok'

                resp = {'headers': {'req_id': req_id},
                        'errcode': errcode, 'errmsg': errmsg}
                await ws.send(json.dumps(resp, ensure_ascii=False))
                self.frames.append({
                    'direction': 'out',
                    'frame': resp,
                    'at': time.strftime('%H:%M:%S'),
                })
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            logger.info('客户端已断开（当前 %s 个）', len(self.clients))

    # ---------- 推送用户回复 ----------
    async def push_reply(self, content: str, msgid: str | None = None,
                         msgtype: str = 'text') -> int:
        msgid = msgid or uuid.uuid4().hex
        frame = {
            'cmd': 'aibot_msg_callback',
            'headers': {'req_id': uuid.uuid4().hex},
            'body': {
                'msgid': msgid,
                'aibotid': 'mock-bot',
                'chattype': 'single',
                'from': {'userid': 'mock-user'},
                'msgtype': msgtype,
                'text': {'content': content},
            },
        }
        payload = json.dumps(frame, ensure_ascii=False)

        delivered = 0
        for ws in list(self.clients):
            try:
                await ws.send(payload)
                delivered += 1
            except websockets.ConnectionClosed:
                self.clients.discard(ws)

        self.frames.append({
            'direction': 'out',
            'frame': frame,
            'at': time.strftime('%H:%M:%S'),
        })
        logger.info('推送用户回复 msgid=%s content=%r -> %s 个连接',
                    msgid, content, delivered)
        return delivered


# --------------------------------------------------------------------------- #
# HTTP 控制接口
# --------------------------------------------------------------------------- #
def _server(request: web.Request) -> MockWecomServer:
    return request.app['mock_server']


async def handle_reply(request: web.Request) -> web.Response:
    data = await request.json()
    content = data.get('content')
    if not isinstance(content, str):
        return web.json_response({'ok': False, 'error': 'content 必填'}, status=400)
    delivered = await _server(request).push_reply(
        content, data.get('msgid'), data.get('msgtype', 'text'))
    return web.json_response({'ok': True, 'delivered': delivered})


async def handle_sent(request: web.Request) -> web.Response:
    return web.json_response(_server(request).sent_messages)


async def handle_frames(request: web.Request) -> web.Response:
    return web.json_response(_server(request).frames)


async def handle_status(request: web.Request) -> web.Response:
    server = _server(request)
    return web.json_response({
        'clients': len(server.clients),
        'sent_count': len(server.sent_messages),
        'subscribe_errcode': server.subscribe_errcode,
        'send_errcode': server.send_errcode,
    })


async def handle_config(request: web.Request) -> web.Response:
    server = _server(request)
    data = await request.json()

    if 'subscribe_errcode' in data:
        server.subscribe_errcode = int(data['subscribe_errcode'])
        server.subscribe_errmsg = (
            'invalid bot_id or secret' if server.subscribe_errcode else 'ok')
    if 'send_errcode' in data:
        server.send_errcode = int(data['send_errcode'])
        server.send_errmsg = (
            'mock send error' if server.send_errcode else 'ok')

    return web.json_response({
        'ok': True,
        'subscribe_errcode': server.subscribe_errcode,
        'send_errcode': server.send_errcode,
    })


async def handle_close(request: web.Request) -> web.Response:
    server = _server(request)
    count = len(server.clients)
    for ws in list(server.clients):
        await ws.close()
    server.clients.clear()
    return web.json_response({'ok': True, 'closed': count})


async def handle_reset(request: web.Request) -> web.Response:
    server = _server(request)
    server.sent_messages.clear()
    server.frames.clear()
    server.subscribe_errcode = 0
    server.send_errcode = 0
    return web.json_response({'ok': True})


def create_http_app(server: MockWecomServer) -> web.Application:
    app = web.Application()
    app['mock_server'] = server
    app.router.add_post('/mock/reply', handle_reply)
    app.router.add_get('/mock/sent', handle_sent)
    app.router.add_get('/mock/frames', handle_frames)
    app.router.add_get('/mock/status', handle_status)
    app.router.add_post('/mock/config', handle_config)
    app.router.add_post('/mock/close', handle_close)
    app.router.add_post('/mock/reset', handle_reset)
    return app


async def run(args: argparse.Namespace) -> None:
    server = MockWecomServer()

    app = create_http_app(server)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.http_port)
    await site.start()
    logger.info('控制接口已启动: http://%s:%s', args.host, args.http_port)

    async with websockets.serve(server.handle_ws, args.host, args.ws_port):
        logger.info('模拟 WSS 服务已启动: ws://%s:%s', args.host, args.ws_port)
        await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser(description='模拟企业微信长连接服务端')
    parser.add_argument('--host', default='127.0.0.1', help='监听地址')
    parser.add_argument('--ws-port', type=int, default=9000, help='WebSocket 端口')
    parser.add_argument('--http-port', type=int, default=9001, help='控制接口端口')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    )
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info('已停止')


if __name__ == '__main__':
    main()