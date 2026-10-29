#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
直接执行：调用运行中的 main.py 服务，发送一条企业微信消息

前置条件：先启动服务
    python main.py --port 8080

用法：
    # 零参数：自动生成 4 位随机序号
    python tests/send_message.py

    # 自定义内容
    python tests/send_message.py --content "请回复确认"

    # 指定序号与内容
    python tests/send_message.py --seq 0421 --content "请回复确认"
"""
import argparse
import json
import random
import sys
import urllib.error
import urllib.request

DEFAULT_CONTENT = '这是一条来自 send_message.py 的测试消息，请直接回复任意内容'


def gen_seq() -> str:
    """生成 4 位随机数字序号"""
    return f'{random.randint(0, 9999):04d}'


def post_json(url: str, payload: dict, timeout: float = 10.0) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(
        url,
        data=data,
        method='POST',
        headers={'Content-Type': 'application/json; charset=utf-8'},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode('utf-8'))


def main() -> int:
    parser = argparse.ArgumentParser(description='调用 /send_message 发送企业微信消息')
    parser.add_argument('--url', default='http://127.0.0.1:8080',
                        help='服务地址，默认 http://127.0.0.1:8080')
    parser.add_argument('--seq', default=None,
                        help='4 位数字序号，默认随机生成')
    parser.add_argument('--content', default=DEFAULT_CONTENT,
                        help='消息内容，默认使用内置测试文案')
    args = parser.parse_args()

    seq = args.seq or gen_seq()
    url = args.url.rstrip('/') + '/send_message'
    try:
        status, body = post_json(url, {'seq': seq, 'content': args.content})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        print(f'请求失败: HTTP {exc.code}\n{detail}')
        if exc.code == 409:
            print(f'序号 {seq} 已被占用，请换一个：--seq {gen_seq()}')
        return 1
    except urllib.error.URLError as exc:
        print(f'无法连接服务 {args.url}，请先启动 main.py: {exc}')
        return 1

    print(f'HTTP {status}')
    print(json.dumps(body, ensure_ascii=False, indent=2))

    if body.get('errcode') != 0:
        return 1
    print(f'\n消息已发送（seq={seq}），请在企微中回复，然后执行：\n'
          f'  python tests/get_reply.py\n'
          f'（如需指定序号：python tests/get_reply.py --seq {seq}）')
    return 0


if __name__ == '__main__':
    sys.exit(main())