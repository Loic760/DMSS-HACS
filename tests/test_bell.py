"""Native bell protocol tests: no network, door commands or audio transmission."""
import hashlib
import json
import struct
import unittest
from unittest.mock import Mock, patch
from custom_components.dmss.bell_protocol import BellNormalizer, LocalDHIP


def frame(reply):
    raw=json.dumps(reply).encode()+b'\n'
    return struct.pack('<I4s6I',32,b'DHIP',0,0,len(raw),0,len(raw),0)+raw


class BellTests(unittest.TestCase):
    def test_incoming_start_and_connected_state_are_distinct(self):
        normalizer=BellNormalizer({})
        self.assertIsNone(normalizer.normalize({'Code':'VTPCallState','Data':{'callState':'Idle'}}))
        event=normalizer.normalize({'Code':'VTPCallState','Data':{'callState':'Ringing'}})
        self.assertEqual((event['kind'],event['phase']),('ring','start'))
        self.assertIsNone(normalizer.normalize({'Code':'VTPCallState','Data':{'callState':'Ringing'}}))
        connected=normalizer.normalize({'Code':'VTPCallState','Data':{'callState':'Talking'}})
        self.assertEqual((connected['kind'],connected['phase']),('call','connected'))
        self.assertIsNone(normalizer.normalize({'Code':'VTPCallState','Data':{'emCallState':2}}))

    def test_missed_record_does_not_become_a_ring(self):
        event=BellNormalizer({}).normalize({'Code':'VideoTalkLog','Data':{'EndState':'Missed'}})
        self.assertEqual((event['kind'],event['phase']),('missed_call','end'))

    def test_door_attribution_and_caller_redaction(self):
        normalizer=BellNormalizer({'deviceChannels':[{'channelIp':'192.168.1.2','channelName':'back'}]})
        event=normalizer.normalize({'Code':'Invite','Data':{'PeerIP':'192.168.1.2','UserID':'private caller'}})
        self.assertEqual(event['door'],'back')
        self.assertNotIn('192.168',json.dumps(event));self.assertNotIn('private caller',json.dumps(event))
        self.assertEqual(BellNormalizer({}).normalize({'Code':'Invite','Data':{'Channel':0}})['door'],'unknown')

    def test_ring_edges_allow_a_second_call_after_end(self):
        n=BellNormalizer({})
        self.assertIsNotNone(n.normalize({'Code':'BackKeyLight','Data':{'State':1}}))
        self.assertIsNone(n.normalize({'Code':'BackKeyLight','Data':{'State':2}}))
        self.assertIsNone(n.normalize({'Code':'BackKeyLight','Data':{'State':0}}))
        self.assertIsNotNone(n.normalize({'Code':'BackKeyLight','Data':{'State':1}}))

    def test_fragmented_packet_and_buffered_second_packet(self):
        client=LocalDHIP('192.168.1.20','u','p');client.sock=Mock()
        raw=frame({'id':1,'result':True})+frame({'id':2,'result':True})
        client.sock.recv.side_effect=[raw[:7],raw[7:23],raw[23:]]
        self.assertEqual(client.receive()['id'],1);self.assertEqual(client.receive()['id'],2)
        self.assertEqual(client.sock.recv.call_count,3)

    def test_read_only_auth_and_early_state_subscription(self):
        client=LocalDHIP('192.168.1.20','u','p');sock=Mock()
        sock.recv.return_value=b''.join([frame({'id':1,'session':7,'params':{'realm':'r','random':'n'},'result':False}),
            frame({'id':2,'session':7,'result':True}),frame({'id':3,'result':True,'params':{'SID':513}}),
            frame({'id':4,'result':9}),frame({'id':5,'result':True}),
            frame({'id':6,'result':True,'params':{'callState':'Idle'}})])
        with patch('socket.create_connection',return_value=sock):client.connect()
        sent=[json.loads(call.args[0][32:]) for call in sock.sendall.call_args_list]
        self.assertEqual([x['method'] for x in sent],['global.login','global.login','eventManager.attach',
                'VideoTalkPhone.factory.instance','VideoTalkPhone.attachCallState','VideoTalkPhone.getCallState'])
        md5=lambda value:hashlib.md5(value.encode()).hexdigest().upper()
        self.assertEqual(sent[1]['params']['password'],md5('u:n:'+md5('u:r:p')))
        self.assertTrue(client.call_state_supported)
        self.assertEqual(sent[-1]['object'],9)
        self.assertTrue(client.call_state_poll_supported)

    def test_nested_start_connected_end_sequence_preserves_door(self):
        n=BellNormalizer({'deviceChannels':[{'channelIp':'192.0.2.2','channelName':'back'}]})
        start=n.normalize({'Code':'VTPCallState','Data':{'Info':{'CallState':'Called','PeerIP':'192.0.2.2'}}})
        self.assertEqual((start['kind'],start['door'],start['phase']),('ring','back','start'))
        self.assertIsNone(n.normalize({'Code':'VTPCallState','Data':{'callState':'Ringing'}}))
        talking=n.normalize({'Code':'VTPCallState','Data':{'Info':{'CallState':'Talking'}}})
        self.assertEqual((talking['kind'],talking['door'],talking['phase']),('call','back','connected'))
        end=n.normalize({'Code':'VTPCallState','Data':{'callState':'Idle'}})
        self.assertEqual((end['kind'],end['door'],end['phase']),('call_ended','back','end'))
        self.assertIsNone(n.normalize({'Code':'VTPCallState','Data':{'callState':'Idle'}}))

    def test_sdk_numeric_states_are_not_rpc_numeric_guesses(self):
        n=BellNormalizer({})
        for value in (0,1,2):self.assertIsNone(n.normalize({'Code':'VTPCallState','Data':{'emCallState':value}}))
        self.assertEqual(n.normalize({'Code':'VTPCallState','Data':{'emCallState':3}})['kind'],'ring')
        self.assertIsNone(n.normalize({'Code':'VTPCallState','Data':{'emCallState':4}}))
        self.assertEqual(n.normalize({'Code':'VTPCallState','Data':{'emCallState':5}})['kind'],'call')
        self.assertEqual(n.normalize({'Code':'VTPCallState','Data':{'emCallState':7}})['kind'],'call_ended')
        self.assertIsNone(n.normalize({'Code':'VTPCallState','Data':{'CallState':3}}))

    def test_notification_during_state_query_is_not_dropped(self):
        client=LocalDHIP('192.0.2.20','u','p');client.sock=Mock();client.call_state_handle=9
        client.sock.recv.return_value=frame({'method':'client.VideoTalkPhone.notifyCallState','params':{'Info':{'CallState':'Called','ChannelID':1}}})+frame({'id':1,'result':True,'params':{'callState':'Talking'}})
        client.poll_call_state()
        self.assertEqual(len(client.pending),2)
        n=BellNormalizer({})
        self.assertEqual([n.normalize(x)['kind'] for x in client.pending],['ring','call'])

    def test_push_start_and_poll_start_do_not_duplicate(self):
        n=BellNormalizer({})
        self.assertEqual(n.normalize({'Code':'Invite','Data':{'ChannelID':1}})['kind'],'ring')
        self.assertIsNone(n.normalize({'Code':'VTPCallState','Data':{'callState':'Called','ChannelID':1}}))

    def test_delayed_missed_log_does_not_restart_or_misattribute_live_ring(self):
        n=BellNormalizer({})
        n.normalize({'Code':'VTPCallState','Data':{'callState':'Called','ChannelID':1}})
        record=n.normalize({'Code':'VideoTalkLog','Data':{'EndState':'Missed'}})
        self.assertEqual(record['door'],'unknown')
        self.assertEqual(record['kind'],'missed_call')
        self.assertIsNone(n.normalize({'Code':'VTPCallState','Data':{'callState':'Called','ChannelID':1}}))

    def test_unsupported_poll_keeps_push_subscription(self):
        client=LocalDHIP('192.0.2.20','u','p');client.call_state_handle=9;client.call_state_supported=True
        client.request=Mock(return_value={'result':False,'error':{'code':-267976703}})
        client.poll_call_state()
        self.assertIsNone(client.call_state_handle)
        self.assertTrue(client.call_state_supported)

    def test_late_door_identity_is_reported_once_while_call_is_ringing(self):
        for door, channel in (('front', 1), ('back', 2)):
            with self.subTest(door=door):
                n=BellNormalizer({})
                first=n.normalize({'Code':'VTPCallState','Data':{'callState':'Called'}})
                self.assertEqual((first['kind'],first['door']),('ring','unknown'))
                identified=n.normalize({'Code':'Invite','Data':{'ChannelID':channel}})
                self.assertEqual((identified['kind'],identified['door'],identified['phase']),
                                 ('ring_identified',door,'start'))
                self.assertTrue(identified['verified'])
                self.assertIsNone(n.normalize({'Code':'VTPCallState','Data':{'callState':'Ringing','ChannelID':channel}}))
                self.assertEqual(n.normalize({'Code':'VTPCallState','Data':{'callState':'Talking'}})['door'],door)

    def test_late_identity_from_poll_or_bell_push_is_not_dropped(self):
        for raw in (
            {'Code':'VTPCallState','Data':{'callState':'Called','ChannelID':2}},
            {'Code':'BackKeyLight','Data':{'State':1,'ChannelID':2}},
        ):
            with self.subTest(raw=raw):
                n=BellNormalizer({})
                n.normalize({'Code':'VTPCallState','Data':{'callState':'Called'}})
                event=n.normalize(raw)
                self.assertEqual((event['kind'],event['door']),('ring_identified','back'))
                self.assertIsNone(n.normalize(raw))

    def test_identical_invite_after_call_ends_is_a_new_ring_even_within_five_seconds(self):
        n=BellNormalizer({})
        invite={'Code':'Invite','Data':{'ChannelID':1}}
        with patch('custom_components.dmss.bell_protocol.time.monotonic', return_value=42):
            self.assertEqual(n.normalize(invite)['kind'],'ring')
            self.assertIsNone(n.normalize(invite))
            self.assertEqual(n.normalize({'Code':'VTPCallState','Data':{'callState':'Idle'}})['kind'],'call_ended')
            self.assertEqual(n.normalize(invite)['kind'],'ring')

if __name__=='__main__':unittest.main()
