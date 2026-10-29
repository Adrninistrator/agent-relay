# -*- coding: utf-8 -*-
"""
main.py 的单元测试（不访问真实企业微信网络，无需先启动服务）

运行（在 send_recv_wecom_msg 目录下）：
    python -m unittest discover -s tests -v
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# 让 tests/ 下的测试能导入上一级目录的 main.py
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aiohttp.test_utils import TestClient, TestServer

import main


class FakeConnected:
    def __init__(self, value: bool = True) -> None:
        self.value = value

    def is_set(self) -> bool:
        return self.value


class FakeWssClient:
    """替代 WecomWssClient，只保留 HTTP 层用到的能力"""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.error: Exception | None = None
        self._connected = FakeConnected(True)

    async def send_message(self, content: str) -> dict:
        if self.error is not None:
            raise self.error
        self.sent.append(content)
        return {'errcode': 0, 'errmsg': 'ok'}


class TestLoadConfig(unittest.TestCase):
    def _write(self, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'config.toml'
        path.write_text(text, encoding='utf-8')
        return path

    def test_toml_format(self):
        cfg = main.load_config(self._write(
            '# 注释\nbot_id = "bot-1"\nsecret = "secret-1"\nchat_id = "chat-1"\n'))
        self.assertEqual((cfg.bot_id, cfg.secret, cfg.chat_id),
                         ('bot-1', 'secret-1', 'chat-1'))

    def test_uppercase_keys(self):
        cfg = main.load_config(self._write(
            'BOT_ID = "bot-2"\nSECRET = "secret-2"\nCHAT_ID = "chat-2"\n'))
        self.assertEqual((cfg.bot_id, cfg.secret, cfg.chat_id),
                         ('bot-2', 'secret-2', 'chat-2'))

    def test_reply_ttl_default_and_custom(self):
        cfg = main.load_config(self._write(
            'bot_id = "b"\nsecret = "s"\nchat_id = "c"\n'))
        self.assertEqual(cfg.reply_ttl, 300)

        cfg2 = main.load_config(self._write(
            'bot_id = "b"\nsecret = "s"\nchat_id = "c"\nreply_ttl = 900\n'))
        self.assertEqual(cfg2.reply_ttl, 900)

    def test_invalid_reply_ttl(self):
        with self.assertRaises(ValueError):
            main.load_config(self._write(
                'bot_id = "b"\nsecret = "s"\nchat_id = "c"\nreply_ttl = 0\n'))

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            main.load_config('no-such-config.toml')

    def test_incomplete_config(self):
        with self.assertRaises(ValueError):
            main.load_config(self._write('bot_id = "only-bot"\n'))

    def test_invalid_toml(self):
        with self.assertRaises(ValueError):
            main.load_config(self._write('bot_id = \n'))

    def test_utf8_bom(self):
        cfg = main.load_config(
            self._write('\ufeffbot_id = "b"\nsecret = "s"\nchat_id = "c"\n'))
        self.assertEqual(cfg.bot_id, 'b')


class TestIsTruthy(unittest.TestCase):
    def test_truthy_values(self):
        for value in ('1', 'true', 'TRUE', 'yes', 'on', ' true '):
            self.assertTrue(main.is_truthy(value), value)

    def test_falsy_values(self):
        for value in (None, '', '0', 'false', 'no', 'other'):
            self.assertFalse(main.is_truthy(value), value)


class TestMessageStore(unittest.TestCase):
    def test_register_and_current(self):
        store = main.MessageStore(ttl=60)
        store.register('0001')
        store.set_current('0001')
        self.assertEqual(store.current_seq, '0001')
        self.assertTrue(store.is_active('0001'))

    def test_register_conflict(self):
        store = main.MessageStore(ttl=60)
        store.register('0001')
        with self.assertRaises(main.SeqConflictError):
            store.register('0001')

    def test_seq_reusable_after_expire(self):
        store = main.MessageStore(ttl=60)
        store.register('0001')
        store.purge_expired(now=time.time() + 61)
        store.register('0001')  # 过期后可复用
        self.assertTrue(store.is_active('0001'))

    def test_save_and_get_reply(self):
        store = main.MessageStore(ttl=60)
        store.register('0001')
        store.set_current('0001')
        self.assertEqual(store.save_reply('你好', {'raw': 1}), '0001')
        self.assertEqual(store.get_reply('0001')['content'], '你好')
        self.assertEqual(store.get_reply('0001')['raw'], {'raw': 1})

    def test_save_reply_to_explicit_seq(self):
        store = main.MessageStore(ttl=60)
        store.register('0001')
        store.register('0002')
        store.set_current('0002')
        self.assertEqual(store.save_reply('内容', {}, '0001'), '0001')
        self.assertIsNotNone(store.get_reply('0001'))
        self.assertIsNone(store.get_reply('0002'))

    def test_save_reply_without_valid_seq(self):
        store = main.MessageStore(ttl=60)
        self.assertIsNone(store.save_reply('你好', {}))

    def test_expired_entry_returns_none(self):
        store = main.MessageStore(ttl=60)
        store.register('0001')
        store.save_reply('内容', {})
        store.purge_expired(now=time.time() + 61)
        self.assertIsNone(store.get_reply('0001'))

    def test_purge_clears_current(self):
        store = main.MessageStore(ttl=60)
        store.register('0001')
        store.set_current('0001')
        self.assertEqual(store.purge_expired(now=time.time() + 61), 1)
        self.assertIsNone(store.current_seq)

    def test_purge_partial(self):
        store = main.MessageStore(ttl=60)
        store.register('0001')
        store.register('0002')
        store._entries['0001']['expire_at'] = time.time() - 1  # noqa: SLF001
        self.assertEqual(store.purge_expired(), 1)
        self.assertTrue(store.is_active('0002'))

    def test_remove_rolls_back_current(self):
        store = main.MessageStore(ttl=60)
        store.register('0001')
        store.set_current('0001')
        store.remove('0001')
        self.assertIsNone(store.current_seq)
        self.assertFalse(store.is_active('0001'))


class TestOutgoingAndParse(unittest.TestCase):
    def test_build_outgoing_message(self):
        msg = main.build_outgoing_message('请确认', '0421')
        self.assertTrue(msg.startswith('请确认\n\n'))
        self.assertIn('当前消息序号：**0421**', msg)
        self.assertIn('**直接回复即可**', msg)
        self.assertIn('第一行只填序号 **0421**', msg)
        self.assertIn('请不要引用本条消息', msg)

    def test_parse_single_line(self):
        self.assertEqual(main.parse_reply('  内容  '), (None, '内容'))

    def test_parse_two_lines_with_seq(self):
        self.assertEqual(main.parse_reply('0421\n内容'), ('0421', '内容'))

    def test_parse_two_lines_crlf(self):
        self.assertEqual(main.parse_reply(' 0421\r\n内容\r\n'), ('0421', '内容'))

    def test_parse_invalid_seq_fullwidth(self):
        self.assertEqual(main.parse_reply('０４２１\n内容'), (None, '０４２１\n内容'))

    def test_parse_invalid_seq_length(self):
        self.assertEqual(main.parse_reply('421\n内容'), (None, '421\n内容'))

    def test_parse_seq_only_one_line(self):
        self.assertEqual(main.parse_reply('0421\n'), (None, '0421'))


class TestWssEventHandling(unittest.TestCase):
    def setUp(self):
        self.store = main.MessageStore(ttl=60)
        self.wss = main.WecomWssClient(
            main.Config('bot', 'secret', 'chat'), self.store)

    def test_single_line_uses_current_seq(self):
        self.store.register('0001')
        self.store.set_current('0001')
        self.wss._on_event(
            {'body': {'msgtype': 'text', 'text': {'content': '单行回复'}}})
        self.assertEqual(self.store.get_reply('0001')['content'], '单行回复')

    def test_two_line_routes_by_seq(self):
        self.store.register('0001')
        self.store.register('0002')
        self.store.set_current('0002')
        self.wss._on_event(
            {'body': {'msgtype': 'text', 'text': {'content': '0001\n第一条的回复'}}})
        self.assertEqual(self.store.get_reply('0001')['content'], '第一条的回复')
        self.assertIsNone(self.store.get_reply('0002'))

    def test_unknown_seq_falls_back_to_current(self):
        self.store.register('0001')
        self.store.set_current('0001')
        self.wss._on_event(
            {'body': {'msgtype': 'text', 'text': {'content': '9999\n兜底回复'}}})
        self.assertEqual(self.store.get_reply('0001')['content'], '兜底回复')

    def test_message_ignored_without_current(self):
        self.wss._on_event(
            {'body': {'msgtype': 'text', 'text': {'content': 'hi'}}})
        self.assertIsNone(self.store.current_seq)

    def test_non_message_event_ignored(self):
        self.store.register('0001')
        self.store.set_current('0001')
        self.wss._on_event({'body': {'event': 'enter_chat'}})
        self.assertIsNone(self.store.get_reply('0001'))


class TestMsgidDedup(unittest.TestCase):
    def setUp(self):
        self.store = main.MessageStore(ttl=60)
        self.wss = main.WecomWssClient(
            main.Config('bot', 'secret', 'chat'), self.store)

    @staticmethod
    def _frame(msgid: str, content: str) -> dict:
        return {'cmd': 'aibot_msg_callback',
                'body': {'msgid': msgid, 'msgtype': 'text',
                         'text': {'content': content}}}

    def test_same_msgid_ignored(self):
        self.store.register('0001')
        self.store.set_current('0001')
        self.wss._on_event(self._frame('m1', '第一次'))
        self.wss._on_event(self._frame('m1', '第二次'))
        self.assertEqual(self.store.get_reply('0001')['content'], '第一次')

    def test_different_msgid_processed(self):
        self.store.register('0001')
        self.store.set_current('0001')
        self.wss._on_event(self._frame('m1', '第一次'))
        self.wss._on_event(self._frame('m2', '第二次'))
        self.assertEqual(self.store.get_reply('0001')['content'], '第二次')

    def test_frame_without_msgid_processed(self):
        self.store.register('0001')
        self.store.set_current('0001')
        self.wss._on_event(
            {'body': {'msgtype': 'text', 'text': {'content': '无 msgid'}}})
        self.assertEqual(self.store.get_reply('0001')['content'], '无 msgid')

    def test_cache_is_bounded(self):
        for i in range(main.MSGID_CACHE_SIZE + 20):
            self.wss._is_duplicate_msgid(f'm{i}')
        self.assertLessEqual(len(self.wss._seen_msgids), main.MSGID_CACHE_SIZE)
        self.assertFalse(self.wss._is_duplicate_msgid('m0'))


class TestSubscriptionError(unittest.TestCase):
    def test_fields(self):
        exc = main.SubscriptionError(853000, 'invalid bot_id or secret')
        self.assertEqual(exc.errcode, 853000)
        self.assertEqual(exc.errmsg, 'invalid bot_id or secret')
        self.assertIn('853000', str(exc))
        self.assertIsInstance(exc, main.WecomError)


class TestReconnectBackoff(unittest.IsolatedAsyncioTestCase):
    async def _run_with_session_error(self, error: Exception) -> list[float]:
        wss = main.WecomWssClient(main.Config('b', 's', 'c'), main.MessageStore())
        sleeps: list[float] = []

        async def fake_session():
            raise error

        async def fake_sleep(delay):
            sleeps.append(delay)
            wss._stop.set()  # 第一轮休眠后停止，避免死循环

        wss._session = fake_session  # type: ignore[assignment]
        with patch('asyncio.sleep', fake_sleep):
            await wss._run_forever()
        return sleeps

    async def test_network_error_uses_short_backoff(self):
        sleeps = await self._run_with_session_error(ConnectionError('boom'))
        self.assertEqual(sleeps, [main.INITIAL_RECONNECT_DELAY])

    async def test_subscription_error_uses_long_backoff(self):
        sleeps = await self._run_with_session_error(
            main.SubscriptionError(853000, 'invalid bot_id or secret'))
        self.assertEqual(sleeps, [main.SUBSCRIPTION_ERROR_DELAY])


class TestHttpApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = main.MessageStore(ttl=60)
        self.wss = FakeWssClient()
        app = main.create_app(self.wss, self.store)  # type: ignore[arg-type]
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_send_message_ok(self):
        resp = await self.client.post(
            '/send_message', json={'seq': '0421', 'content': '你好'})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data['errcode'], 0)
        self.assertEqual(data['seq'], '0421')
        self.assertEqual(len(self.wss.sent), 1)
        self.assertIn('你好', self.wss.sent[0])
        self.assertIn('当前消息序号：**0421**', self.wss.sent[0])
        self.assertEqual(self.store.current_seq, '0421')

    async def test_send_message_seq_required(self):
        resp = await self.client.post('/send_message', json={'content': '你好'})
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.wss.sent, [])

    async def test_send_message_invalid_seq(self):
        for bad in ('421', '12345', 'abcd', '０４２１', 421, ''):
            resp = await self.client.post(
                '/send_message', json={'seq': bad, 'content': '你好'})
            self.assertEqual(resp.status, 400, bad)

    async def test_send_message_empty_content(self):
        resp = await self.client.post(
            '/send_message', json={'seq': '0421', 'content': '   '})
        self.assertEqual(resp.status, 400)

    async def test_send_message_content_too_long(self):
        resp = await self.client.post(
            '/send_message',
            json={'seq': '0421', 'content': 'a' * (main.MAX_CONTENT_BYTES + 1)})
        self.assertEqual(resp.status, 400)

    async def test_send_message_invalid_json(self):
        resp = await self.client.post(
            '/send_message', data='not-json',
            headers={'Content-Type': 'application/json'})
        self.assertEqual(resp.status, 400)

    async def test_send_message_conflict(self):
        await self.client.post(
            '/send_message', json={'seq': '0421', 'content': '第一条'})
        resp = await self.client.post(
            '/send_message', json={'seq': '0421', 'content': '第二条'})
        self.assertEqual(resp.status, 409)
        self.assertEqual(len(self.wss.sent), 1)

    async def test_send_message_not_connected_rolls_back(self):
        await self.client.post(
            '/send_message', json={'seq': '0002', 'content': 'first'})
        self.wss.error = main.NotConnectedError('尚未连接')

        resp = await self.client.post(
            '/send_message', json={'seq': '0003', 'content': 'second'})
        self.assertEqual(resp.status, 503)
        self.assertFalse(self.store.is_active('0003'))  # 序号已释放
        self.assertEqual(self.store.current_seq, '0002')  # 恢复上一条

    async def test_send_message_wecom_error(self):
        self.wss.error = main.WecomError('errcode=846607')
        resp = await self.client.post(
            '/send_message', json={'seq': '0421', 'content': 'x'})
        self.assertEqual(resp.status, 502)
        self.assertFalse(self.store.is_active('0421'))
        self.assertIsNone(self.store.current_seq)

    async def test_get_reply_by_seq(self):
        self.store.register('0421')
        self.store.save_reply('用户回复内容', {'cmd': 'aibot_msg_callback'}, '0421')

        resp = await self.client.get('/get_reply', params={'seq': '0421'})
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data, {
            'success': True,
            'message': 'ok',
            'reply': '用户回复内容',
        })

    async def test_get_reply_default_latest(self):
        self.store.register('0001')
        self.store.register('0002')
        self.store.set_current('0002')
        self.store.save_reply('最新回复', {})

        resp = await self.client.get('/get_reply')
        data = await resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['reply'], '最新回复')

    async def test_get_reply_detail(self):
        self.store.register('0421')
        self.store.save_reply('回复内容', {'cmd': 'aibot_msg_callback'}, '0421')

        resp = await self.client.get(
            '/get_reply', params={'seq': '0421', 'detail': 'true'})
        data = await resp.json()
        self.assertTrue(data['success'])
        self.assertEqual(data['seq'], '0421')
        self.assertEqual(data['raw'], {'cmd': 'aibot_msg_callback'})
        self.assertIn('received_at', data)

    async def test_get_reply_detail_false(self):
        self.store.register('0421')
        self.store.save_reply('内容', {}, '0421')

        resp = await self.client.get(
            '/get_reply', params={'seq': '0421', 'detail': 'false'})
        data = await resp.json()
        self.assertNotIn('raw', data)
        self.assertNotIn('received_at', data)

    async def test_get_reply_not_found(self):
        self.store.register('0421')
        resp = await self.client.get('/get_reply', params={'seq': '0421'})
        data = await resp.json()
        self.assertEqual(data, {
            'success': False,
            'message': '暂无回复',
            'reply': None,
        })

    async def test_get_reply_no_message(self):
        resp = await self.client.get('/get_reply')
        data = await resp.json()
        self.assertEqual(data, {
            'success': False,
            'message': '暂无消息',
            'reply': None,
        })

    async def test_get_reply_invalid_seq(self):
        resp = await self.client.get('/get_reply', params={'seq': '42'})
        self.assertEqual(resp.status, 400)
        data = await resp.json()
        self.assertFalse(data['success'])

    async def test_get_reply_expired(self):
        self.store.register('0421')
        self.store.save_reply('内容', {}, '0421')
        self.store.purge_expired(now=time.time() + 61)

        resp = await self.client.get('/get_reply', params={'seq': '0421'})
        data = await resp.json()
        self.assertFalse(data['success'])
        self.assertEqual(data['message'], '暂无回复')

    async def test_health(self):
        resp = await self.client.get('/health')
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data['status'], 'ok')
        self.assertTrue(data['connected'])


if __name__ == '__main__':
    unittest.main(verbosity=2)