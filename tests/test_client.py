import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import requests
from custom_components.dmss.client import AUTH, DMSSClient, DMSSError, APIError, VerificationRequired, challenge_password, decrypt_play_info, encrypt_device_field, legacy_password, pcs_headers, redact, save_private, signed_headers
PLAY_INFO = 'A2xZOBMO0GgnTNCDYw8aFieJybx+WbdVbKeMn4bFJsirCOMyGnkPS5VMO8hlV49LY/M91dty7rqosadi5Xkz7TgrxMC2Cykt7kjr0eEdyok='
DEVICE = {'deviceId': 'TEST01234567', 'name': 'Dummy VTO', 'devType': 12, 'playInfo': PLAY_INFO}

def response(value, status=200):
    return Mock(status_code=status, json=Mock(return_value=value))

class CryptoTests(unittest.TestCase):

    def test_java_reference_vectors(self):
        self.assertEqual(legacy_password('dummy-password'), 'BtBAyh+ieQKqzjJGwu6+4QAG1fHNaOgd77MixBKYTwSMB6dED1FGnUmIdaQ21n/a')
        self.assertEqual(challenge_password('dummy-password', 'dummy-salt', 'dummy-random'), '9fe976710d12f530e01719d8e6db264beeb33216b5f40f7b91bbaa37bb460da6ecc4da9311c5a76b93f49bc600a141c752a0e661e935901be1599882b247fea5')
        self.assertEqual(encrypt_device_field('admin', 'TEST01234567'), '3ar41zMgGj4+KLrIQhnRCw==')

    def test_play_info_fixture(self):
        self.assertEqual(decrypt_play_info(PLAY_INFO, DEVICE['deviceId']), {'devLoginName': 'admin', 'devLoginPassword': 'dummy-device-password'})
        with self.assertRaises(ValueError):
            decrypt_play_info(PLAY_INFO, 'wrong-serial')

    def test_smb_canonical_signature(self):
        body = b'{}'
        headers = signed_headers(AUTH + 'getUserInfo', body, account='dummy@example.com', country='BE', language='en', terminal_id='terminal', token='session', timestamp=123, nonce='nonce', trace='trace')
        lines = ['url:/dcloud-user/userManage/v1/getUserInfo', 'method:POST', 'content-type:application/json', 'content-sha256:' + hashlib.sha256(body).hexdigest(), 'x-dd-time:123', 'x-dd-nonce:nonce', 'x-dd-clientversion:2.6.22', 'x-dd-clienttype:phone', 'x-dd-client:Android', 'x-dd-traceid:trace', 'x-dd-transcode:dmss', 'x-dd-account:dummy@example.com', 'x-dd-country:BE', 'x-dd-projectid:Base', 'x-dd-language:en', 'x-dd-terminalid:terminal']
        expected = hmac.new(b'session', '\n'.join(sorted(lines)).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-DD-Signature'], expected)

    def test_pcs_signature_and_identity(self):
        body = b'{"data":{"deviceId":"TEST01234567"}}'
        h = pcs_headers('/pcs/v1/dclouduser.device.GetAccessControlNum', body, username='dummy-user', token='session', revision='220452', terminal_id='terminal')
        canonical = 'POST\n/pcs/v1/dclouduser.device.GetAccessControlNum\n' + h['Content-MD5'] + '\napplication/json\nx-pcs-apiver:220452\nx-pcs-client-ua:' + h['x-pcs-client-ua'] + '\nx-pcs-date:' + h['x-pcs-date'] + '\nx-pcs-nonce:' + h['x-pcs-nonce'] + '\nx-pcs-username:uuid\\dummy-user\n'
        expected = base64.b64encode(hmac.new(hashlib.md5(b'session').hexdigest().encode(), canonical.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(h['x-pcs-signature'], expected)
        self.assertEqual(h['Authorization'], 'Bearer session')
        self.assertEqual(json.loads(base64.b64decode(h['x-pcs-client-ua']))['appid'], 'dmssbaseapp')

class ClientTests(unittest.TestCase):

    def setUp(self):
        self.client = DMSSClient({'token': 'session', 'account': 'dummy@example.com', 'user_info': {'username': 'dummy-user'}})
        self.client.http = Mock()

    def test_inventory_paginates(self):
        self.client.http.post.side_effect = [response({'success': True, 'data': {'pageData': [DEVICE], 'totalPage': 2}}), response({'success': True, 'data': {'pageData': [{'deviceId': 'second'}], 'totalPage': 2}})]
        self.assertEqual(len(self.client.devices()), 2)
        payload = json.loads(self.client.http.post.call_args.kwargs['data'])
        self.assertEqual(payload['pageNum'], 2)

    def test_inventory_omits_feature_flags_row(self):
        self.client.http.post.return_value = response({'success': True, 'data': {'pageData': [{'supportVTHPaaS': True}, DEVICE], 'totalPage': 1}})
        self.assertEqual(self.client.devices(), [DEVICE])

    def test_live_pcs_success_code_is_accepted(self):
        self.client.http.post.return_value = response({'code': 10000, 'data': {'params': {'Channel': 2, 'AccessControlChannel': [0, 1]}}})
        d = {**DEVICE, 'deviceCatalog': 'VTH'}
        self.assertEqual([r['door_index'] for r in self.client.door_inventory(d, 2)['doors']], [0, 1])

    def test_web_session_renewal_preserves_auth_route(self):
        self.client.state['auth_method'] = 'web'
        self.client.password_provider = Mock(return_value='dummy-password')
        self.client.http.post.return_value = response({'success': True, 'data': {'username': 'dummy-user', 'openUserToken': 'separate-open-token'}})
        with patch('custom_components.dmss.web_auth.web_login', return_value={'token': 'web-session', 'username': 'dummy-user'}) as login:
            self.client.renew_session()
        login.assert_called_once()
        self.assertEqual(self.client.token, 'web-session')
        self.assertEqual(self.client.state['user_info']['openUserToken'], 'separate-open-token')
        self.assertEqual(self.client.state['auth_method'], 'web')

    def test_unlisted_primary_requires_explicit_override_and_known_vto_channel(self):
        device = {**DEVICE, 'deviceCatalog': 'VTH', 'deviceChannels': [{'channelId': '1', 'channelCatalog': 'VTO'}]}
        listed = response({'success': True, 'data': {'pageData': [device], 'totalPage': 1}})
        missing = response({'code': 10000, 'data': {'params': {'AccessControlChannel': None}}})
        self.client.http.post.side_effect = [listed, missing]
        with self.assertRaisesRegex(DMSSError, 'absent'):
            self.client.unlock(DEVICE['deviceId'], 0, channel=1)
        self.assertEqual(self.client.http.post.call_count, 2)
        self.client.http.post.side_effect = [listed, missing, response({'code': 10000, 'data': {}})]
        self.assertTrue(self.client.unlock(DEVICE['deviceId'], 0, channel=1, allow_unlisted_primary=True)['request_accepted'])
        self.client.http.post.side_effect = [listed, missing]
        with self.assertRaisesRegex(DMSSError, 'absent'):
            self.client.unlock(DEVICE['deviceId'], 1, channel=1, allow_unlisted_primary=True)

    def test_all_channels_includes_three_user_selected_lock_targets(self):
        device = {**DEVICE, 'deviceCatalog': 'VTH', 'deviceChannels': [{'channelId': '1', 'channelCatalog': 'VTO', 'channelName': 'front'}, {'channelId': '2', 'channelCatalog': 'VTO', 'channelName': 'back'}]}
        self.client.http.post.side_effect = [response({'success': True, 'data': {'pageData': [device], 'totalPage': 1}}), response({'code': 10000, 'data': {'params': {'AccessControlChannel': None}}}), response({'code': 10000, 'data': {'params': {'AccessControlChannel': [0, 1]}}})]
        locks = self.client.doors(DEVICE['deviceId'], all_channels=True, include_unlisted_primary=True)['locks']
        self.assertEqual([(r['door_name'], r['door_index']) for r in locks], [('front', 0), ('back', 0), ('back', 1)])
        self.assertTrue(locks[0]['requires_unlisted_primary_override'])

    def test_token_rotation_does_not_delay_password_renewal(self):
        self.client.state.update({'auth_method': 'web', 'issued_at': time.time(), 'password_login_at': time.time() - 21601})
        self.client.password_provider = Mock(return_value='dummy-password')
        self.client.http.post.return_value = response({'success': True, 'data': {'username': 'dummy-user'}})
        with patch('custom_components.dmss.web_auth.web_login', return_value={'token': 'fresh-web-session'}) as login:
            self.client.ensure_session()
        login.assert_called_once()
        self.assertEqual(self.client.token, 'fresh-web-session')

    def test_login_challenge_and_host(self):
        self.client.http.post.side_effect = [response({'success': True, 'data': {'salt': 'dummy-salt', 'random': 'dummy-random'}}), response({'success': True, 'data': {'salt': 'new-session', 'expiresIn': 'server-value'}}), response({'success': True, 'data': {'username': 'dummy-user', 'domainAddr': 'child.dolynkcloud.com'}})]
        self.client.login('dummy-password')
        self.assertEqual(self.client.host, 'https://child.dolynkcloud.com')
        self.assertEqual(self.client.token, 'new-session')
        self.assertNotIn('dummy-password', json.dumps(self.client.state))
        first = self.client.http.post.call_args_list[0].kwargs['headers']
        self.assertEqual(first['X-DD-Signature'], '')

    def test_unlock_exactly_once(self):
        self.client.http.post.side_effect = [response({'success': True, 'data': {'pageData': [DEVICE], 'totalPage': 1}}), response({'code': 0, 'data': {'accessControlNum': '2'}}), response({'code': 0, 'data': {'result': 'true'}})]
        result = self.client.unlock(DEVICE['deviceId'], 1)
        self.assertTrue(result['request_accepted'])
        calls = self.client.http.post.call_args_list
        self.assertEqual(sum(('OpenAccessControl' in call.args[0] for call in calls)), 1)
        payload = json.loads(calls[-1].kwargs['data'])
        self.assertEqual(payload['data']['doorIndex'], '1')
        self.assertEqual(payload['data']['devName'], '3ar41zMgGj4+KLrIQhnRCw==')
        self.assertNotIn('devLoginPassword', payload['data'])

    def test_invalid_relay_never_unlocks(self):
        self.client.http.post.side_effect = [response({'success': True, 'data': {'pageData': [DEVICE], 'totalPage': 1}}), response({'code': 0, 'data': {'accessControlNum': '2'}})]
        with self.assertRaises(DMSSError):
            self.client.unlock(DEVICE['deviceId'], 2)
        self.assertEqual(self.client.http.post.call_count, 2)

    def test_vth_preserves_reported_relay_ids(self):
        vth = {**DEVICE, 'devType': 19}
        self.client.http.post.side_effect = [response({'success': True, 'data': {'pageData': [vth], 'totalPage': 1}}), response({'code': 0, 'data': {'params': {'AccessControlChannel': [3, 7]}}}), response({'code': 0, 'data': {}})]
        self.client.unlock(DEVICE['deviceId'], 7, channel=2)
        call = self.client.http.post.call_args
        self.assertTrue(call.args[0].endswith('deviceuseroperate.vth.OpenVthDoor'))
        data = json.loads(call.kwargs['data'])['data']
        self.assertEqual(data['doorIndex'], 7)
        self.assertEqual(data['channel'], 2)

    def test_missing_ack_is_not_success(self):
        self.client.http.post.side_effect = [response({'success': True, 'data': {'pageData': [DEVICE], 'totalPage': 1}}), response({'code': 0, 'data': {'accessControlNum': '2'}}), response({'code': 0})]
        with self.assertRaises(DMSSError):
            self.client.unlock(DEVICE['deviceId'], 1)
        self.assertEqual(self.client.http.post.call_count, 3)

    def test_api_failure_does_not_retry(self):
        self.client.http.post.return_value = response({'code': 1234, 'data': {}})
        with self.assertRaises(DMSSError):
            self.client.pcs('dclouduser.device.OpenAccessControl', '223806', {})
        self.assertEqual(self.client.http.post.call_count, 1)

    def test_check_does_not_refresh(self):
        self.client.auto_renew = False
        self.client.http.post.return_value = response({'success': True, 'data': {'username': 'dummy-user'}})
        self.client.user_info()
        self.assertTrue(self.client.http.post.call_args.args[0].endswith('getUserInfo'))
        self.assertEqual(self.client.http.post.call_count, 1)
        self.assertFalse(self.client.token_status()['automatic_renewal'])

    def test_session_permissions_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            save_private(path, self.client.state)
            self.assertEqual(os.stat(path).st_mode & 511, 384)
        self.assertEqual(redact({'playInfo': 'cipher', 'has_separate_refresh_token': False}), {'playInfo': '<redacted>', 'has_separate_refresh_token': False})

class RecoveryTests(unittest.TestCase):

    def setUp(self):
        self.provider = Mock(return_value='dummy-password')
        self.persist = Mock()
        self.client = DMSSClient({'token': 'old-session', 'account': 'dummy@example.com', 'user_info': {'username': 'dummy-user'}, 'issued_at': time.time(), 'user_info_at': time.time()}, password_provider=self.provider, persist=self.persist)
        self.client.http = Mock()
        self.listed = response({'success': True, 'data': {'pageData': [DEVICE], 'totalPage': 1}})
        self.count = response({'code': 0, 'data': {'accessControlNum': '2'}})
        self.renewal = [response({'success': True, 'data': {'salt': 'challenge', 'random': 'random'}}), response({'success': True, 'data': {'salt': 'new-session'}}), response({'success': True, 'data': {'username': 'new-user', 'openUserToken': 'new-open-token'}})]

    def test_read_recovers_expired_session_once(self):
        self.client.http.post.side_effect = [response({'code': 'DC50100012', 'success': False}), *self.renewal, self.listed]
        self.assertEqual(self.client.devices()[0]['deviceId'], DEVICE['deviceId'])
        self.assertEqual(self.client.token, 'new-session')
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(self.client.http.post.call_count, 5)
        self.assertEqual(self.client.state['user_info']['openUserToken'], 'new-open-token')

    def test_unlock_rebuilds_after_explicit_session_rejection(self):
        self.client.http.post.side_effect = [self.listed, self.count, response({'code': '12002'}, 401), *self.renewal, self.listed, self.count, response({'code': 0, 'data': {'result': 'true'}})]
        self.assertTrue(self.client.unlock(DEVICE['deviceId'], 1)['request_accepted'])
        commands = [c for c in self.client.http.post.call_args_list if c.args[0].endswith('OpenAccessControl')]
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[-1].kwargs['headers']['Authorization'], 'Bearer new-session')
        self.assertEqual(commands[-1].kwargs['headers']['x-pcs-username'], 'uuid\\new-user')

    def test_ambiguous_unlock_timeout_never_retries(self):
        self.client.http.post.side_effect = [self.listed, self.count, requests.Timeout()]
        with self.assertRaisesRegex(DMSSError, 'outcome unknown'):
            self.client.unlock(DEVICE['deviceId'], 0)
        self.assertEqual(self.client.http.post.call_count, 3)
        self.provider.assert_not_called()

    def test_device_rejection_reloads_inventory_without_account_login(self):
        self.client.http.post.side_effect = [self.listed, self.count, response({'code': 13005}), response({'success': True, 'data': {'username': 'dummy-user'}}), self.listed, self.count, response({'code': 0, 'data': {}})]
        self.client.unlock(DEVICE['deviceId'], 0)
        self.assertEqual(self.client.http.post.call_count, 7)
        self.provider.assert_not_called()

    def test_verification_requires_code_not_password_guessing(self):
        terminal = self.client.terminal_id
        self.client.http.post.side_effect = [self.renewal[0], response({'success': False, 'code': 'DC00050525'})]
        with self.assertRaises(VerificationRequired):
            self.client.login('dummy-password')
        self.assertEqual(self.client.terminal_id, terminal)
        self.assertTrue(self.client.state['verification_required'])
        self.assertEqual(self.client.http.post.call_count, 2)
        self.persist.assert_called()

    def test_keepalive_and_user_token_schedules_are_separate(self):
        self.client.state['token_login_at'] = time.time() - 1801
        self.client.state['user_info_at'] = time.time() - 901
        self.client.http.post.side_effect = [response({'success': True, 'data': {'openUserToken': 'open-token'}}), response({'success': True, 'data': {'username': 'dummy-user', 'openUserToken': 'fresh-open-token'}})]
        self.client.ensure_session()
        paths = [call.args[0] for call in self.client.http.post.call_args_list]
        self.assertTrue(paths[0].endswith('tokenLogin'))
        self.assertTrue(paths[1].endswith('getUserInfo'))
        self.assertEqual(self.client.token, 'old-session')
        self.assertEqual(self.client.state['user_info']['openUserToken'], 'fresh-open-token')

    def test_proactive_full_login_and_no_renew_switch(self):
        self.client.state['issued_at'] = time.time() - 21601
        self.client.auto_renew = False
        self.client.ensure_session()
        self.client.http.post.assert_not_called()
        self.client.auto_renew = True
        self.client.http.post.side_effect = self.renewal
        self.client.ensure_session()
        self.assertEqual(self.client.token, 'new-session')
        self.assertEqual(self.provider.call_count, 1)

    def test_repeated_expiry_does_not_loop(self):
        self.client.http.post.side_effect = [response({'code': 12002}, 401), *self.renewal, response({'code': 12002}, 401)]
        with self.assertRaises(APIError):
            self.client.devices()
        self.assertEqual(self.provider.call_count, 1)

    def test_forbidden_is_not_treated_as_expiry(self):
        self.client.http.post.return_value = response({'code': 12100}, 403)
        with self.assertRaises(APIError):
            self.client.devices()
        self.provider.assert_not_called()
        self.assertEqual(self.client.http.post.call_count, 1)

    def test_transient_read_retry_is_bounded(self):
        self.client.http.post.side_effect = [response({'code': 'busy'}, 503), self.listed]
        with patch('custom_components.dmss.client.time.sleep') as sleep:
            self.client.devices()
        sleep.assert_called_once_with(1)
        self.assertEqual(self.client.http.post.call_count, 2)

    def test_clock_error_resigns_once(self):
        bad = response({'success': False, 'code': 'DC00050515'})
        bad.headers = {'Date': 'Tue, 13 Oct 2026 12:00:00 GMT'}
        self.client.http.post.side_effect = [bad, self.listed]
        self.client.devices()
        self.assertIn('clock_offset', self.client.state)
        headers = [c.kwargs['headers'] for c in self.client.http.post.call_args_list]
        self.assertNotEqual(headers[0]['X-DD-Time'], headers[1]['X-DD-Time'])
if __name__ == '__main__':
    unittest.main()
