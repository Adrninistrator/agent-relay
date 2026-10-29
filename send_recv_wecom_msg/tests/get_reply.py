#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
直接执行：查询运行中的 main.py 服务是否收到回复

前置条件：先启动服务
    python main.py --port 8080

用法：
    # 零参数：按最后一条消息查询，轮询等待最长 60 秒
    python tests/get_reply.py

    # 按指定序号查询
    python tests/get_reply.py --seq 0421

    # 只查一次，不等待
    python tests/get_reply.py --no-watch

    # 返回详细信息（seq / received_at / raw）
    python tests/get_reply.py --detail --no-watch
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def get_reply(base_url: str, seq: str | None = None, timeout: float = 10.0,
              detail: bool = False) -> tuple[int, dict]:
    params: dict[str, str] = {}
    if seq:
        params['seq'] = seq
    if detail:
        params['detail'] = 'true'

    query = urllib.parse.urlencode(params)
    url = f'{base_url.rstrip("/")}/get_reply' + (f'?{query}' if query else '')
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode('utf-8'))


def main() -> int:
    parser = argparse.ArgumentParser(description='调用 /get_reply 获取企业微信回复')
    parser.add_argument('--url', default='http://127.0.0.1:8080',
                        help='服务地址，默认 http://127.0.0.1:8080')
    parser.add_argument('--seq', default=None,
                        help='4 位数字序号，不传则按最后一条消息查询')
    parser.add_argument('--watch', action=argparse.BooleanOptionalAction, default=True,
                        help='轮询等待回复（默认开启），用 --no-watch 只查一次')
    parser.add_argument('--interval', type=float, default=2.0, help='轮询间隔（秒），默认 2')
    parser.add_argument('--timeout', type=float, default=60.0,
                        help='--watch 的最长等待时间（秒），默认 60')
    parser.add_argument('--detail', action='store_true',
                        help='返回详细信息（seq/received_at/raw）')
    args = parser.parse_args()

    deadline = time.time() + args.timeout
    while True:
        try:
            status, body = get_reply(args.url, args.seq, detail=args.detail)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode('utf-8', errors='replace')
            print(f'请求失败: HTTP {exc.code}\n{detail}')
            return 1
        except urllib.error.URLError as exc:
            print(f'无法连接服务 {args.url}，请先启动 main.py: {exc}')
            return 1

        if body.get('success'):
            print(f'HTTP {status}')
            print(json.dumps(body, ensure_ascii=False, indent=2))
            return 0

        if not args.watch:
            print(f'HTTP {status}')
            print(json.dumps(body, ensure_ascii=False, indent=2))
            print('\n尚未收到回复。可在企微回复后重新执行，或加 --watch 轮询等待。')
            return 0

        if time.time() + args.interval > deadline:
            print(json.dumps(body, ensure_ascii=False, indent=2))
            print(f'\n等待超时（{args.timeout:g}s），仍未收到回复。')
            return 1

        print(f'尚未收到回复，{args.interval:g}s 后重试...')
        time.sleep(args.interval)


if __name__ == '__main__':
    sys.exit(main())