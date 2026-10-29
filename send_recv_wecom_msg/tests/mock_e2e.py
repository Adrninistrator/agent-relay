#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一键端到端验证：启动本地 mock 服务端 + main.py，覆盖核心场景。

不连接真实企业微信，不需要真实 BOT_ID / SECRET。

用法：
    python tests/mock_e2e.py

覆盖场景：
1. 发送消息（mock 收到内容）
2. 两行回复按序号归属
3. 单行回复归属最近消息
4. 无效序号降级到最近消息
5. 重复 msgid 去重
6. 重复序号返回 409
7. 序号格式校验（3 位、全角）
8. 企微发送失败返回 502 且序号可复用
9. detail 模式返回原始帧
10. 断线后自动重连
"""
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def request(method: str, url: str, payload=None, timeout: float = 10.0):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json; charset=utf-8'

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {'raw': body}


def wait_ready(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, body = request('GET', url)
            if status == 200 and body.get('connected'):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, condition: bool, detail: str = '') -> None:
        if condition:
            self.passed += 1
            print(f'  [PASS] {name}')
        else:
            self.failed += 1
            print(f'  [FAIL] {name}  {detail}')


def main() -> int:
    ws_port, http_port, svc_port = free_port(), free_port(), free_port()
    tmp = tempfile.TemporaryDirectory()
    tmp_path = Path(tmp.name)

    cfg = tmp_path / 'config.toml'
    cfg.write_text(
        'bot_id = "mock-bot"\n'
        'secret = "mock-secret"\n'
        'chat_id = "mock-user"\n'
        'chat_type = 1\n'
        'reply_ttl = 60\n'
        f'ws_url = "ws://127.0.0.1:{ws_port}"\n',
        encoding='utf-8',
    )

    mock_log = (tmp_path / 'mock.log').open('w', encoding='utf-8')
    svc_log = (tmp_path / 'service.log').open('w', encoding='utf-8')

    mock_proc = subprocess.Popen(
        [PYTHON, str(ROOT / 'tests' / 'mock_wecom_server.py'),
         '--ws-port', str(ws_port), '--http-port', str(http_port)],
        cwd=str(ROOT), stdout=mock_log, stderr=subprocess.STDOUT,
    )
    svc_proc = subprocess.Popen(
        [PYTHON, str(ROOT / 'main.py'), '--config', str(cfg), '--port', str(svc_port)],
        cwd=str(ROOT), stdout=svc_log, stderr=subprocess.STDOUT,
    )

    base = f'http://127.0.0.1:{svc_port}'
    mock_base = f'http://127.0.0.1:{http_port}'
    checker = Checker()

    try:
        if not wait_ready(f'{base}/health'):
            print('服务未就绪，日志尾部：')
            print((tmp_path / 'service.log').read_text(encoding='utf-8')[-3000:])
            return 1

        # 1. 发送成功
        status, body = request('POST', f'{base}/send_message',
                               {'seq': '0001', 'content': '第一条'})
        checker.check('发送 seq=0001', status == 200 and body.get('errcode') == 0, str(body))

        status, sent = request('GET', f'{mock_base}/mock/sent')
        checker.check('mock 收到消息',
                      len(sent) == 1 and '第一条' in sent[0]['markdown']['content'],
                      str(sent))

        # 2. 两行回复按序号归属
        request('POST', f'{mock_base}/mock/reply', {'content': '0001\n第一条回复'})
        time.sleep(0.3)
        status, body = request('GET', f'{base}/get_reply?seq=0001')
        checker.check('两行回复按序号归属', body.get('reply') == '第一条回复', str(body))

        # 3. 单行回复归属最近消息
        request('POST', f'{base}/send_message', {'seq': '0002', 'content': '第二条'})
        request('POST', f'{mock_base}/mock/reply', {'content': '单行回复'})
        time.sleep(0.3)
        status, body = request('GET', f'{base}/get_reply')
        checker.check('单行回复归属最近消息', body.get('reply') == '单行回复', str(body))

        # 4. 无效序号降级
        request('POST', f'{mock_base}/mock/reply', {'content': '9999\n兜底回复'})
        time.sleep(0.3)
        status, body = request('GET', f'{base}/get_reply')
        checker.check('无效序号降级到最近消息', body.get('reply') == '兜底回复', str(body))

        # 5. 重复 msgid 去重
        request('POST', f'{mock_base}/mock/reply',
                {'content': '第一次', 'msgid': 'dup-1'})
        request('POST', f'{mock_base}/mock/reply',
                {'content': '第二次', 'msgid': 'dup-1'})
        time.sleep(0.3)
        status, body = request('GET', f'{base}/get_reply?seq=0002')
        checker.check('重复 msgid 被忽略', body.get('reply') == '第一次', str(body))

        # 6. 序号冲突
        status, body = request('POST', f'{base}/send_message',
                               {'seq': '0002', 'content': '重复序号'})
        checker.check('重复序号返回 409', status == 409, str(body))

        # 7. 格式校验
        status, _ = request('POST', f'{base}/send_message',
                            {'seq': '123', 'content': 'x'})
        checker.check('3 位序号被拒绝', status == 400)

        status, _ = request('POST', f'{base}/send_message',
                            {'seq': '１２３４', 'content': 'x'})
        checker.check('全角序号被拒绝', status == 400)

        # 8. 模拟发送失败 + 序号释放
        request('POST', f'{mock_base}/mock/config', {'send_errcode': 846607})
        status, body = request('POST', f'{base}/send_message',
                               {'seq': '0003', 'content': '失败'})
        checker.check('发送失败返回 502', status == 502, str(body))

        request('POST', f'{mock_base}/mock/config', {'send_errcode': 0})
        status, body = request('POST', f'{base}/send_message',
                               {'seq': '0003', 'content': '重试'})
        checker.check('失败后序号已释放', status == 200, str(body))

        # 9. detail 模式
        status, body = request('GET', f'{base}/get_reply?seq=0002&detail=true')
        msgid = (body.get('raw') or {}).get('body', {}).get('msgid')
        checker.check('detail 返回原始帧',
                      body.get('seq') == '0002' and msgid == 'dup-1',
                      str(body)[:300])

        # 10. 断线自动重连
        request('POST', f'{mock_base}/mock/close')
        connected = False
        for _ in range(30):
            time.sleep(0.5)
            status, body = request('GET', f'{base}/health')
            if body.get('connected'):
                connected = True
                break
        checker.check('断线后自动重连', connected, str(body))

    finally:
        for proc in (svc_proc, mock_proc):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        mock_log.close()
        svc_log.close()
        tmp.cleanup()

    print(f'\n结果：通过 {checker.passed}，失败 {checker.failed}')
    return 0 if checker.failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())