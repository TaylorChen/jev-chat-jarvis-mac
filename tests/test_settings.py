"""Settings persistence and wire-level checks, no credentials or external services."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import userconfig
import settings_config as config


class SettingsFiles(unittest.TestCase):
    def test_missing_provider_keys_never_fall_back_to_a_packaged_credential(self):
        from generate import load_credentials
        empty = {'key': '', 'base': '', 'model': '', 'source': 'none'}
        with patch.object(userconfig, 'provider', return_value=empty):
            base, key, _model, source, api = load_credentials()
        self.assertEqual(base, 'https://api.openai.com/v1')
        self.assertEqual(key, '')
        self.assertEqual(source, 'none')
        self.assertEqual(api, 'openai')

    def test_production_source_has_no_packaged_credential_module(self):
        self.assertFalse((Path(__file__).resolve().parents[1] / 'src/builtin.py').exists())

    def test_preserves_comments_unknown_and_shell_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "env"
            original = '# comment\nexport OPENAI_API_KEY="old" # key comment\nJEV_TONES="名字=说明"\nOPENAI_API_KEY=duplicate\nCUSTOM=keep\n'
            path.write_text(original)
            value = 'a\'b"$HOME$(echo SHOULD_NOT_RUN)`echo BAD` # c\\d'
            text = config.write_settings(path, original, {"OPENAI_API_KEY": value, "OPENAI_MODEL": "模型"})
            self.assertIn('# key comment', text)
            self.assertIn('JEV_TONES="名字=说明"\n', text)
            self.assertIn('CUSTOM=keep\n', text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(userconfig.parse_env_file(path)["OPENAI_API_KEY"], value)
            result = subprocess.check_output(['zsh', '-c', 'source "$1"; print -rn -- "$OPENAI_API_KEY"', 'test', str(path)], text=True)
            self.assertEqual(result, value)

    def test_conflicting_edit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "env"
            path.write_text('new content')
            with self.assertRaises(ValueError):
                config.write_settings(path, 'old content', {'OPENAI_MODEL': 'test'})
            self.assertEqual(path.read_text(), 'new content')

    def test_no_partial_save_on_invalid_value(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "env"
            with self.assertRaises(ValueError):
                config.write_settings(path, '', {'OPENAI_API_KEY': 'a\nb'})
            self.assertFalse(path.exists())

    def test_parse_empty_quotes_comments_and_literals(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'env'
            path.write_text('A="" # empty\nB="abc # def" # outside\nC=abc#def\nD=\'$(security find-generic-password)\'\n')
            self.assertEqual(userconfig.parse_env_file(path), {'A': '', 'B': 'abc # def', 'C': 'abc#def', 'D': '$(security find-generic-password)'})

    def test_custom_provider_format_and_priority(self):
        from generate import load_credentials
        oai = {'key': '', 'base': '', 'model': '', 'source': 'none'}
        anth = {'key': 'custom', 'base': 'https://example.invalid/gateway', 'model': 'model', 'source': 'test'}
        with patch.object(userconfig, 'provider', side_effect=lambda p: oai if p == 'OPENAI' else anth):
            self.assertEqual(load_credentials()[-1], 'anthropic')
            oai.update(key='own', base='https://example.invalid/anthropic-name', model='other')
            self.assertEqual(load_credentials()[-1], 'openai')
            self.assertEqual(load_credentials()[2], 'other')

    def test_provider_key_never_inherits_endpoint_from_a_lower_priority_source(self):
        sources = [
            ('环境变量', {'OPENAI_API_KEY': 'environment-key'}),
            ('项目配置', {'OPENAI_BASE_URL': 'https://attacker.invalid/v1',
                      'OPENAI_MODEL': 'attacker-model'}),
        ]
        with patch.object(userconfig, '_startup_sources', sources):
            provider = userconfig.provider('OPENAI')
        self.assertEqual(provider['key'], 'environment-key')
        self.assertEqual(provider['base'], '')
        self.assertEqual(provider['model'], '')

    def test_running_config_stays_until_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'env'
            path.write_text('OPENAI_API_KEY=old\nOPENAI_MODEL=before\n')
            with patch.dict(os.environ, {}, clear=True), patch.object(userconfig, '_startup_sources', None), patch.object(userconfig, 'env_files', return_value=[path]), patch.object(userconfig, 'PROJECT_ENV', Path(d) / '.env'):
                userconfig.load()
                self.assertNotEqual(userconfig.provider('OPENAI')['source'], '环境变量')
                config.write_settings(path, path.read_text(), {'OPENAI_API_KEY': 'new', 'OPENAI_MODEL': 'after'})
                self.assertEqual(userconfig.provider('OPENAI')['model'], 'before')
                with patch.object(userconfig, '_startup_sources', None), patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(userconfig.provider('OPENAI')['model'], 'after')


class Server(BaseHTTPRequestHandler):
    requests = []
    response = {}
    code = 200
    redirect_to = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.respond(None)

    def do_POST(self):
        if self.redirect_to and self.path == '/redirect':
            self.requests.append((self.path, dict(self.headers), None))
            self.send_response(302)
            self.send_header('Location', self.redirect_to)
            self.end_headers()
            return
        self.respond(json.loads(self.rfile.read(int(self.headers['Content-Length']))))

    def respond(self, body):
        self.requests.append((self.path, dict(self.headers), body))
        self.send_response(self.code)
        self.end_headers()
        self.wfile.write(json.dumps(self.response).encode())


class SettingsNetwork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def setUp(self):
        Server.requests = []
        Server.code = 200
        Server.redirect_to = None

    def test_dynamic_models_and_versioned_custom_base(self):
        Server.response = {'data': [{'id': 'actual-model'}, {'id': 'actual-model'}, {'id': 'new-model'}]}
        self.assertEqual(config.list_models('OPENAI', self.base + '/gateway/v4', 'draft-key'), ['actual-model', 'new-model'])
        self.assertEqual(Server.requests[0][0], '/gateway/v4/models')
        self.assertEqual(Server.requests[0][1]['authorization'], 'Bearer draft-key')

    def test_jev_models_follow_typesafe_schema(self):
        Server.response = {'models': [
            {'name': 'jev-latest', 'description': 'Stable', 'release_date': '2026-09-15'},
            {'name': 'jev-preview', 'description': 'Preview', 'release_date': '2026-09-15'},
        ]}
        self.assertEqual(config.list_models('TYPESAFE', self.base, 'draft-key'),
                         ['jev-latest', 'jev-preview'])
        self.assertEqual(Server.requests[0][0], '/v1/models')
        self.assertEqual(Server.requests[0][1]['authorization'], 'Bearer draft-key')

    def test_anthropic_models_headers(self):
        Server.response = {'data': [{'id': 'model'}]}
        config.list_models('ANTHROPIC', self.base, 'draft-key')
        self.assertEqual(Server.requests[0][0], '/v1/models')
        self.assertEqual(Server.requests[0][1]['x-api-key'], 'draft-key')

    def test_unsaved_generation_values_and_body(self):
        Server.response = {'choices': [{'message': {'content': '连接成功'}}]}
        with patch('generate.load_credentials', side_effect=AssertionError('must not use saved credentials')):
            config.test_connection('OPENAI', self.base + '/v1', 'draft-key', 'draft-model', {'enable_thinking': False})
        path, headers, body = Server.requests[0]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(body['model'], 'draft-model')
        self.assertFalse(body['enable_thinking'])
        self.assertEqual(headers['authorization'], 'Bearer draft-key')

    def test_anthropic_and_jev_real_response_shape(self):
        Server.response = {'content': [{'type': 'text', 'text': '连接成功'}]}
        config.test_connection('ANTHROPIC', self.base, 'key', 'model')
        self.assertEqual(Server.requests[-1][0], '/v1/messages')
        Server.response = {'answers': {'test': {'choice': '问候'}}}
        config.test_connection('TYPESAFE', self.base, 'key', 'model')
        self.assertEqual(Server.requests[-1][0], '/v1/systemone')

    def test_empty_and_thinking_are_not_success(self):
        for response in ({}, {'choices': [{'message': {'content': '', 'reasoning_content': 'thinking'}}]}):
            Server.response = response
            with self.assertRaises(Exception):
                config.test_connection('OPENAI', self.base, 'key', 'model')

    def test_no_redirect_or_fallback_and_no_secret_in_error(self):
        for status in (302, 401, 403, 500):
            Server.code = status
            Server.response = {'error': 'SECRET'}
            with self.assertRaises(Exception) as caught:
                config.list_models('OPENAI', self.base, 'SECRET')
            msg = config.error_message(caught.exception)
            self.assertIn(str(status), msg)
            self.assertNotIn('SECRET', msg)
        self.assertEqual(len(Server.requests), 4)

    def test_remote_http_is_rejected_but_loopback_http_is_allowed(self):
        for allowed in (
                self.base,
                'http://localhost:11434/v1',
                'http://127.99.0.1:11434/v1',
                'http://[::1]:11434/v1',
                'https://api.example.com/v1'):
            self.assertEqual(config.validate_endpoint(allowed), allowed.rstrip('/'))
        for rejected in (
                'http://example.com/v1',
                'http://192.168.1.20/v1',
                'http://0.0.0.0:11434/v1',
                'http://localhost.evil.example/v1'):
            with self.subTest(rejected=rejected), self.assertRaises(ValueError):
                config.validate_endpoint(rejected)

    def test_private_http_requires_explicit_jev_opt_in(self):
        import generate
        private_urls = (
            'http://10.0.0.8:8000',
            'http://172.16.0.8:8000',
            'http://192.168.1.20:8000',
        )
        for url in private_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    generate.validate_transport_url(url)
                self.assertEqual(
                    generate.validate_transport_url(url, allow_private_http=True), url)
        for url in ('http://8.8.8.8:8000', 'http://0.0.0.0:8000',
                    'http://service.example.com:8000'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                generate.validate_transport_url(url, allow_private_http=True)

    def test_runtime_transport_rejects_env_configured_remote_http_before_connecting(self):
        import generate
        pool = generate._KeepAlivePool()
        with patch.object(pool, '_checkout', side_effect=AssertionError('socket opened')):
            with self.assertRaises(ValueError):
                pool.post_json('http://example.com/v1/chat/completions', {}, {}, 1)

    def test_direct_generator_call_without_a_key_fails_before_network(self):
        from generate import Generator
        with patch('generate.load_credentials', return_value=(
                'https://api.openai.com/v1', '', 'model', 'none', 'openai')):
            generator = Generator()
            with patch.object(generator, '_post', side_effect=AssertionError('network called')):
                with self.assertRaisesRegex(ValueError, 'Key'):
                    generator._call('hello')

    def test_streaming_request_does_not_follow_a_credential_bearing_redirect(self):
        from generate import Generator
        Server.redirect_to = self.base + '/target'
        generator = Generator(timeout=2)
        with self.assertRaises(urllib.error.HTTPError):
            generator._stream_openai(
                self.base + '/redirect',
                {'content-type': 'application/json', 'authorization': 'Bearer SECRET'},
                {'model': 'm', 'messages': []}, 'm', 'alt', lambda _frag: None)
        self.assertEqual([row[0] for row in Server.requests], ['/redirect'])

    def test_streaming_transport_does_not_use_environment_proxies(self):
        import generate
        proxy_handlers = [h for h in generate._NO_REDIRECT_OPENER.handlers
                          if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(proxy_handlers, [])

    def test_jev_key_never_inherits_endpoint_from_a_lower_priority_source(self):
        from judge_jev import JevJudge, DEFAULT_BASE
        sources = [
            ('环境变量', {'TYPESAFE_API_KEY': 'environment-key'}),
            ('项目配置', {'TYPESAFE_BASE_URL': 'https://attacker.invalid'}),
        ]
        with patch.object(userconfig, '_startup_sources', sources):
            judge = JevJudge()
        self.assertEqual(judge.key, 'environment-key')
        self.assertEqual(judge.base, DEFAULT_BASE)

    def test_direct_jev_call_without_a_key_fails_before_network(self):
        from judge_jev import JevJudge
        with patch.object(userconfig, 'provider', return_value={
                'key': '', 'base': '', 'model': '', 'source': 'none'}):
            judge = JevJudge()
        with patch('judge_jev.http_post_json', side_effect=AssertionError('network called')):
            with self.assertRaisesRegex(ValueError, 'key'):
                judge.judge('hello')

    def test_jev_private_http_opt_in_is_forwarded_only_when_enabled(self):
        from judge_jev import JevJudge
        configured = {'key': 'key', 'base': 'http://192.168.1.20:8000',
                      'model': 'jev-1', 'source': 'test'}
        response = {'answers': {'intent': {'choice': '闲聊'}, 'risk': {'score': 0}}}
        with patch.object(userconfig, 'provider', return_value=configured), \
                patch.object(userconfig, 'get', return_value='1'), \
                patch('judge_jev.http_post_json', return_value=response) as post:
            JevJudge().judge('hello')
        self.assertTrue(post.call_args.kwargs['allow_private_http'])

    def test_jev_fallback_reports_the_primary_error_before_loading_local(self):
        from judge import FallbackJudge
        judge = FallbackJudge.__new__(FallbackJudge)
        judge.primary = Mock()
        judge.primary.judge.side_effect = ValueError('transport blocked')
        judge.local = None
        judge.fell_back = False
        judge.reason = ''
        reported = []
        judge.on_fallback = reported.append
        with patch.object(judge, '_fallback', side_effect=RuntimeError('local load')):
            with self.assertRaisesRegex(RuntimeError, 'local load'):
                judge.judge('hello')
        self.assertEqual(reported, ['ValueError: transport blocked'])


class PerceptionSource(unittest.TestCase):
    """数据源可配置：默认 db（数据库直读），没有密钥时回退 OCR，命令行能临时覆盖。"""

    def _with_env(self, values):
        return patch.dict(os.environ, values, clear=True), \
            patch.object(userconfig, '_startup_sources', None), \
            patch.object(userconfig, 'env_files', return_value=[]), \
            patch.object(userconfig, 'PROJECT_ENV', Path('/nonexistent/.env'))

    def test_default_is_db(self):
        a, b, c, d = self._with_env({})
        with a, b, c, d:
            self.assertEqual(userconfig.perception_source(), 'db')
            self.assertEqual(userconfig.DEFAULT_PERCEPTION_SOURCE, 'db')

    def test_env_can_select_ocr(self):
        a, b, c, d = self._with_env({'JEV_SOURCE': 'ocr'})
        with a, b, c, d:
            self.assertEqual(userconfig.perception_source(), 'ocr')

    def test_legacy_switch_still_works(self):
        # 旧写法 JEV_DB_MODE=1 必须继续认，否则老用户的配置会静默失效
        a, b, c, d = self._with_env({'JEV_DB_MODE': '1'})
        with a, b, c, d:
            self.assertEqual(userconfig.perception_source(), 'db')

    def test_source_wins_over_legacy_switch(self):
        a, b, c, d = self._with_env({'JEV_SOURCE': 'ocr', 'JEV_DB_MODE': '1'})
        with a, b, c, d:
            self.assertEqual(userconfig.perception_source(), 'ocr')

    def test_cli_overrides_everything(self):
        a, b, c, d = self._with_env({'JEV_SOURCE': 'db'})
        with a, b, c, d:
            self.assertEqual(userconfig.perception_source('ocr'), 'ocr')
            self.assertEqual(userconfig.perception_source('DB'), 'db')

    def test_unknown_value_is_passed_through_for_the_caller_to_reject(self):
        # 解析层不静默纠正：hud 会打出「不认识，按 ocr 启动」，让用户看见自己写错了
        a, b, c, d = self._with_env({'JEV_SOURCE': 'screen'})
        with a, b, c, d:
            self.assertEqual(userconfig.perception_source(), 'screen')
            self.assertNotIn('screen', userconfig.PERCEPTION_SOURCES)

    def test_source_arg_forms(self):
        self.assertEqual(userconfig.source_arg(['--source', 'db']), 'db')
        self.assertEqual(userconfig.source_arg(['--source=ocr']), 'ocr')
        self.assertIsNone(userconfig.source_arg(['--boxes']))
        self.assertEqual(userconfig.source_arg(['--source']), '')

    def test_settings_write_accepts_source_and_keys_path(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'env'
            original = '# keep\nOPENAI_MODEL=old\n'
            path.write_text(original)
            config.write_settings(path, original, {'JEV_SOURCE': 'db',
                                                   'JEV_KEYS_FILE': '~/keys.json'})
            values = userconfig.parse_env_file(path)
            self.assertEqual(values['JEV_SOURCE'], 'db')
            self.assertEqual(values['JEV_KEYS_FILE'], '~/keys.json')
            self.assertIn('# keep\n', path.read_text())
            self.assertEqual(values['OPENAI_MODEL'], 'old')

    def test_settings_write_normalises_and_rejects_bad_source(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'env'
            text = config.write_settings(path, '', {'JEV_SOURCE': ' DB '})
            self.assertEqual(userconfig.parse_env_file(path)['JEV_SOURCE'], 'db')
            with self.assertRaises(ValueError):
                config.write_settings(path, text, {'JEV_SOURCE': 'screen'})
            with self.assertRaises(ValueError):
                config.write_settings(path, text, {'UNRELATED_KEY': 'x'})


class WechatKeys(unittest.TestCase):
    """密钥文件定位：显式配置 > 应用数据目录 > 旧手工布局，且不泄漏密钥内容。"""

    def test_default_is_the_app_data_dir(self):
        import wechat_keys
        with tempfile.TemporaryDirectory() as d, \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(userconfig, '_startup_sources', None), \
                patch.object(userconfig, 'env_files', return_value=[]), \
                patch.object(userconfig, 'PROJECT_ENV', Path('/nonexistent/.env')), \
                patch.object(wechat_keys, 'KEYS_FILE', Path(d) / 'wechat_keys.json'), \
                patch.object(wechat_keys, 'LEGACY_KEYS_FILE', Path(d) / 'legacy.json'):
            self.assertEqual(wechat_keys.keys_file(), Path(d) / 'wechat_keys.json')

    def test_legacy_layout_is_still_read(self):
        # 早期手工 clone 到 ~/python 的用户不该因为升级就「密钥消失」
        import wechat_keys
        with tempfile.TemporaryDirectory() as d, \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(userconfig, '_startup_sources', None), \
                patch.object(userconfig, 'env_files', return_value=[]), \
                patch.object(userconfig, 'PROJECT_ENV', Path('/nonexistent/.env')), \
                patch.object(wechat_keys, 'KEYS_FILE', Path(d) / 'wechat_keys.json'), \
                patch.object(wechat_keys, 'LEGACY_KEYS_FILE', Path(d) / 'legacy.json'):
            (Path(d) / 'legacy.json').write_text('{}')
            self.assertEqual(wechat_keys.keys_file(), Path(d) / 'legacy.json')

    def test_explicit_configuration_wins(self):
        with patch.dict(os.environ, {'JEV_KEYS_FILE': '/tmp/jev-test-keys.json'}, clear=True), \
                patch.object(userconfig, '_startup_sources', None), \
                patch.object(userconfig, 'env_files', return_value=[]), \
                patch.object(userconfig, 'PROJECT_ENV', Path('/nonexistent/.env')):
            import wechat_keys
            self.assertEqual(str(wechat_keys.keys_file()), '/tmp/jev-test-keys.json')
            self.assertNotIn('--decrypt', wechat_keys.extraction_command())

    def test_project_extractor_only_creates_private_key_material(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / 'tools/extract_wechat_keys.command').read_text()
        self.assertIn('umask 077', script)
        self.assertIn('chmod 700 "$APP_SUPPORT"', script)
        self.assertIn('chmod 600 "$KEYS_FILE"', script)
        self.assertNotIn('extract --output "$KEYS_FILE" --decrypt', script)
        self.assertNotIn('APP_SUPPORT/decrypted', script)
        self.assertIn('/decrypted/', (root / '.gitignore').read_text())

    def test_packaging_secret_scan_has_no_source_file_exception(self):
        build = (Path(__file__).resolve().parents[1] / 'packaging/build_app.sh').read_text()
        self.assertNotIn('--exclude=builtin.py', build)
        self.assertNotIn('内置凭据是刻意保留', build)
        self.assertIn('"$APP/Contents/Resources/app"', build)
        self.assertIn('PRIVATE KEY', build)

    def test_key_count_tolerates_missing_and_broken_files(self):
        import wechat_keys
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'keys.json'
            self.assertEqual(wechat_keys.key_count(path), 0)
            path.write_text('not json')
            self.assertEqual(wechat_keys.key_count(path), 0)
            path.write_text(json.dumps({'message/message_0.db': {'enc_key': 'aa'},
                                        'message/message_1.db': {'salt': 'bb'},
                                        'junk': 3}))
            self.assertEqual(wechat_keys.key_count(path), 1)

    def test_status_line_never_contains_key_material(self):
        import wechat_keys
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'keys.json'
            path.write_text(json.dumps({'message/message_0.db': {'enc_key': 'deadbeef'}}))
            with patch.object(wechat_keys, 'keys_file', return_value=path):
                line = wechat_keys.status_line()
            self.assertIn('1 个库', line)
            self.assertNotIn('deadbeef', line)

    def test_resolve_source_falls_back_with_a_reason(self):
        import wechat_keys
        with tempfile.TemporaryDirectory() as d, \
                patch.dict(os.environ, {}, clear=True), \
                patch.object(userconfig, '_startup_sources', None), \
                patch.object(userconfig, 'env_files', return_value=[]), \
                patch.object(userconfig, 'PROJECT_ENV', Path('/nonexistent/.env')), \
                patch.object(wechat_keys, 'keys_file', return_value=Path(d) / '无密钥.json'):
            # 默认就是 db：没密钥时必须回退并说明原因，而不是让应用起不来
            source, note = wechat_keys.resolve_source()
            self.assertEqual(source, 'ocr')
            self.assertIn('密钥', note)
            self.assertEqual(wechat_keys.resolve_source('db')[0], 'ocr')
            self.assertIn('密钥', wechat_keys.resolve_source('db')[1])
            self.assertEqual(wechat_keys.resolve_source('ocr'), ('ocr', ''))
            self.assertEqual(wechat_keys.resolve_source('screen'),
                             ('ocr', "数据源 'screen' 不认识（只支持 ocr / db），已按 ocr 启动"))

    def test_missing_sqlcipher_falls_back_to_ocr_with_the_install_hint(self):
        # 提取密钥不需要 sqlcipher，用户很容易漏装；缺它时 db 每跳都会子进程报错，
        # 不如直接退回读屏并把安装命令写清楚
        import wechat_keys
        with tempfile.TemporaryDirectory() as d:
            keys = Path(d) / 'wechat_keys.json'
            keys.write_text(json.dumps({'message/message_0.db': {'enc_key': 'aa'}}))
            with patch.object(wechat_keys, 'KEYS_FILE', keys), \
                    patch.object(wechat_keys, 'LEGACY_KEYS_FILE', Path(d) / 'none.json'), \
                    patch.object(wechat_keys, 'sqlcipher_path', return_value=None), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch.object(userconfig, '_startup_sources', None), \
                    patch.object(userconfig, 'env_files', return_value=[]), \
                    patch.object(userconfig, 'PROJECT_ENV', Path('/nonexistent/.env')):
                source, note = wechat_keys.resolve_source('db')
                self.assertEqual(source, 'ocr')
                self.assertIn('sqlcipher', note)
                self.assertIn('brew install sqlcipher', wechat_keys.status_line())

    def test_status_line_is_quiet_when_sqlcipher_is_present(self):
        import wechat_keys
        with tempfile.TemporaryDirectory() as d:
            keys = Path(d) / 'wechat_keys.json'
            keys.write_text(json.dumps({'message/message_0.db': {'enc_key': 'aa'}}))
            with patch.object(wechat_keys, 'KEYS_FILE', keys), \
                    patch.object(wechat_keys, 'sqlcipher_path', return_value='/usr/local/bin/sqlcipher'):
                self.assertNotIn('sqlcipher', wechat_keys.status_line())

    def test_lldb_is_optional_not_fatal(self):
        # 上游是四级路径：只有「首次抓 passphrase」（微信 4.1.10+）才需要 lldb
        import wechat_keys
        with tempfile.TemporaryDirectory() as d:
            keys = Path(d) / 'wechat_keys.json'
            keys.write_text(json.dumps({'message/message_0.db': {'enc_key': 'aa'}}))
            with patch.object(wechat_keys, 'KEYS_FILE', keys), \
                    patch.object(wechat_keys, 'sqlcipher_path', return_value='/usr/local/bin/sqlcipher'), \
                    patch.object(wechat_keys, 'lldb_path', return_value=None), \
                    patch.object(wechat_keys, 'passphrase_cached', return_value=True):
                # 有密钥 → 状态行照旧，不因为缺 lldb 报警
                self.assertNotIn('lldb', wechat_keys.status_line())
            with patch.object(wechat_keys, 'KEYS_FILE', Path(d) / 'none.json'), \
                    patch.object(wechat_keys, 'LEGACY_KEYS_FILE', Path(d) / 'none2.json'), \
                    patch.object(wechat_keys, 'lldb_path', return_value=None), \
                    patch.object(wechat_keys, 'passphrase_cached', return_value=False):
                # 没密钥 + 没 passphrase 缓存 + 没 lldb → 提前说清首次提取需要什么
                self.assertIn('xcode-select --install', wechat_keys.status_line())
            with patch.object(wechat_keys, 'KEYS_FILE', Path(d) / 'none.json'), \
                    patch.object(wechat_keys, 'LEGACY_KEYS_FILE', Path(d) / 'none2.json'), \
                    patch.object(wechat_keys, 'lldb_path', return_value=None), \
                    patch.object(wechat_keys, 'passphrase_cached', return_value=True):
                # passphrase 已缓存：重新提取不必再走 lldb，状态行不该提它
                self.assertNotIn('lldb', wechat_keys.status_line())

    def test_resolve_source_accepts_db_once_keys_exist(self):
        import wechat_keys
        with tempfile.TemporaryDirectory() as d:
            keys = Path(d) / 'wechat_keys.json'
            keys.write_text(json.dumps({'message/message_0.db': {'enc_key': 'aa'}}))
            with patch.object(wechat_keys, 'KEYS_FILE', keys), \
                    patch.object(wechat_keys, 'LEGACY_KEYS_FILE', Path(d) / 'none.json'), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch.object(userconfig, '_startup_sources', None), \
                    patch.object(userconfig, 'env_files', return_value=[]), \
                    patch.object(userconfig, 'PROJECT_ENV', Path('/nonexistent/.env')):
                self.assertEqual(wechat_keys.resolve_source('db'), ('db', ''))
                self.assertEqual(wechat_keys.resolve_source(), ('db', ''))   # 默认也是 db


if __name__ == '__main__':
    unittest.main()
