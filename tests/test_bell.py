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
        self.assertIsNone(normalizer.normalize({'Code':'VTPCallState','Data':{'callState':'Talking'}}))
        self.assertIsNone(normalizer.normalize({'Code':'VTPCallState','Data':{'emCallState':2}}))

    def test_missed_record_does_not_become_a_ring(self):
        event=BellNormalizer({}).normalize({'Code':'VideoTalkLog','Data':{'EndState':'Missed'}})
        self.assertEqual((event['kind'],event['phase']),('missed_call','end'))

    def test_door_attribution_and_caller_redaction(self):
        normalizer=BellNormalizer({'deviceChannels':[{'channelIp':'192.168.1.2','channelName':'back'}]})
        event=normalizer.normalize({'Code':'Invite','Data':{'PeerIP':'192.168.1.2','UserID':'private caller'}})
        self.assertEqual(event['door'],'back')
        self.assertNotIn('192.168',json.dumps(event));self.assertNotIn('private caller',json.dumps(event))
        self.assertEqual(normalizer.normalize({'Code':'Invite','Data':{'Channel':0}})['door'],'unknown')

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
            frame({'id':4,'result':9}),frame({'id':5,'result':True})])
        with patch('socket.create_connection',return_value=sock):client.connect()
        sent=[json.loads(call.args[0][32:]) for call in sock.sendall.call_args_list]
        self.assertEqual([x['method'] for x in sent],['global.login','global.login','eventManager.attach',
                'VideoTalkPhone.factory.instance','VideoTalkPhone.attachCallState'])
        md5=lambda value:hashlib.md5(value.encode()).hexdigest().upper()
        self.assertEqual(sent[1]['params']['password'],md5('u:n:'+md5('u:r:p')))
        self.assertTrue(client.call_state_supported)
        self.assertEqual(sent[-1]['object'],9)

if __name__=='__main__':unittest.main()
