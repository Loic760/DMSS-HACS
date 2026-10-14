"""Local VTH bell-event protocol, independently implemented.

Protocol references: github.com/rroller/dahua (vto.py, rpc2.py),
github.com/myhomeiot/DahuaVTO (event meanings), DMSS APK SDK enums.
No cloud credentials, raw caller identity or configuration writes.
"""
import hashlib
import time
from datetime import datetime, timezone


class LocalEventError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class LocalDHIP:
    """Authenticated persistent event subscription, not call answering/control."""
    def __init__(self, host, username, password, port=5000, timeout=3):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.timeout = timeout
        self.sock = None
        self.session = None
        self.sequence = 0
        self.buffer = bytearray()
        self.pending = []
        self.keepalive_interval = 20
        self.call_state_handle = None
        self.call_state_poll_supported = False
        self.call_state_poll_interval = 1.0

    def _queue_notification(self, reply):
        method=str(reply.get('method','')).lower()
        params=reply.get('params') or {}
        if method=='client.notifyeventstream':
            self.pending.extend(x for x in (params.get('eventList') or []) if isinstance(x,dict))
            return True
        if method.startswith('client.') and ('callstate' in method or ('videotalkphone' in method and 'notify' in method)):
            self.pending.append({'Code':'VTPCallState','Action':'State','Data':params})
            return True
        return False

    def send(self, method, params, object_id=None):
        import json, struct
        self.sequence += 1
        data = {'id':self.sequence,'method':method,'params':params}
        if self.session: data['session'] = self.session
        if object_id is not None: data['object']=object_id
        body = json.dumps(data,separators=(',',':')).encode()+b'\n'
        self.sock.sendall(struct.pack('<I4s6I',32,b'DHIP',0,0,len(body),0,len(body),0)+body)
        return self.sequence

    def receive(self):
        import json, struct
        while len(self.buffer) < 32:
            chunk=self.sock.recv(65536)
            if not chunk: raise LocalEventError('Local event connection closed')
            self.buffer.extend(chunk)
        if self.buffer[4:8] != b'DHIP': raise LocalEventError('Unsupported event framing')
        length=struct.unpack_from('<I',self.buffer,16)[0]
        if not 0 < length <= 1000000: raise LocalEventError('Local event frame exceeds its limit')
        while len(self.buffer) < 32+length:
            chunk=self.sock.recv(65536)
            if not chunk: raise LocalEventError('Local event connection closed')
            self.buffer.extend(chunk)
        raw=bytes(self.buffer[32:32+length]);del self.buffer[:32+length]
        try: reply=json.loads(raw)
        except ValueError: raise LocalEventError('Invalid local event message') from None
        if not isinstance(reply,dict): raise LocalEventError('Invalid local event message')
        return reply

    def request(self, method, params, verify=True, object_id=None):
        sequence=self.send(method,params,object_id)
        deadline=time.monotonic()+self.timeout
        while time.monotonic()<deadline:
            reply=self.receive()
            if self._queue_notification(reply): continue
            if reply.get('id') != sequence: continue
            if verify and reply.get('result') is not True:
                code=(reply.get('error') or {}).get('code')
                raise LocalEventError(f'Local event method rejected (code {code if isinstance(code,int) else "unknown"})',code)
            return reply
        raise LocalEventError('Local event request timed out')

    def connect(self):
        import socket
        self.sock=socket.create_connection((self.host,self.port),self.timeout)
        self.sock.settimeout(self.timeout)
        try:
            login={'clientType':'','ipAddr':'(null)','loginType':'Direct','userName':self.username,'password':''}
            first=self.request('global.login',login,verify=False)
            p=first.get('params') or {}
            if not all(isinstance(p.get(k),str) for k in ('random','realm')):
                raise LocalEventError('Unsupported local event challenge')
            self.session=first.get('session')
            md5=lambda s:hashlib.md5(s.encode()).hexdigest().upper()
            inner=md5(self.username+':'+p['realm']+':'+self.password)
            login.update(password=md5(self.username+':'+p['random']+':'+inner),authorityType='Default')
            result=self.request('global.login',login)
            self.session=result.get('session') or self.session
            self.keepalive_interval=max(5,min(30,int((result.get('params') or {}).get('keepAliveInterval') or 20)))
            result=self.request('eventManager.attach',{'codes':['All']})
            if not (result.get('params') or {}).get('SID'):
                raise LocalEventError('Local event subscription has no identifier')
            self.call_state_supported=False
            try:
                made=self.request('VideoTalkPhone.factory.instance',{},verify=False)
                handle=made.get('result')
                if type(handle) is int and handle>0:
                    self.call_state_handle=handle
                    self.request('VideoTalkPhone.attachCallState',{},object_id=handle)
                    self.call_state_supported=True
            except LocalEventError:
                pass
            if self.call_state_handle is not None:
                self.poll_call_state()
            self.sock.settimeout(1)
            return self
        except Exception:
            self.close();raise

    def listen(self, callback, stop):
        import socket
        last_keepalive=time.monotonic()
        last_poll=time.monotonic()
        while not stop.is_set():
            pending,self.pending=self.pending,[]
            for event in pending: callback(event)
            if self.call_state_handle is not None and time.monotonic()-last_poll>=self.call_state_poll_interval:
                self.poll_call_state()
                last_poll=time.monotonic()
                continue
            if time.monotonic()-last_keepalive>=self.keepalive_interval:
                self.send('global.keepAlive',{'timeout':60,'active':True})
                last_keepalive=time.monotonic()
            try: reply=self.receive()
            except socket.timeout: continue
            if self._queue_notification(reply): continue
            if reply.get('result') is False:
                raise LocalEventError('Local event session rejected; reconnect required')

    def poll_call_state(self):
        """Read current state, preserving push notifications received mid-reply."""
        if self.call_state_handle is None:return
        reply=self.request('VideoTalkPhone.getCallState',{},verify=False,object_id=self.call_state_handle)
        if reply.get('result') is True:
            self.call_state_poll_supported=True
            self.pending.append({'Code':'VTPCallState','Action':'State','Data':reply.get('params') or {},'Source':'local state poll'})
        else:
            self.call_state_poll_supported=False
            self.call_state_handle=None  # Unsupported method; retain push.

    def close(self):
        import socket
        if self.sock:
            try: self.sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            self.sock.close(); self.sock=None


class BellNormalizer:
    """Track live incoming, connected and ended calls independently of logs."""
    def __init__(self, device):
        self.addresses = {r.get('channelIp'):r.get('channelName') for r in device.get('deviceChannels',[]) if r.get('channelName') in ('front','back') and r.get('channelIp')}
        self.recent = {}
        self.active = {}
        self.call = None
        self.counter = 0

    @staticmethod
    def _fields(data, depth=0):
        if depth > 5 or not isinstance(data,dict): return {}
        result = {}
        for key,value in data.items():
            normalized=''.join(c for c in str(key).lower() if c.isalnum())
            if isinstance(value,dict): result.update(BellNormalizer._fields(value,depth+1))
            elif isinstance(value,list):
                for item in value[:20]: result.update(BellNormalizer._fields(item,depth+1))
            elif isinstance(value,(str,int,float,bool)):
                result[normalized]=value
        return result

    def _door(self, fields):
        for value in fields.values():
            if isinstance(value,str) and value in self.addresses:return self.addresses[value]
        for key in ('door','doorname','channelname'):
            if fields.get(key) in ('front','back'):return fields[key]
        # SDK Channel and RPC channelId identify the linked VTO. Index on a
        # VTH event is its indoor index, so it must never be treated as a door.
        channel=fields.get('channel',fields.get('channelid'))
        return {'1':'front','2':'back'}.get(str(channel),'unknown')

    @staticmethod
    def _state(fields):
        # Integers have a specified meaning only in the SDK emCallState field.
        sdk=fields.get('emcallstate')
        if type(sdk) is int:
            return {0:'unknown',1:'idle',2:'outgoing',3:'incoming',4:'ringing',
                    5:'connecting',6:'connecting',7:'ended'}.get(sdk,'unknown')
        value=next((fields[k] for k in ('callstate','state','status') if isinstance(fields.get(k),str)), '')
        value=''.join(c for c in value.lower() if c.isalnum())
        return {'ring':'ringing','ringing':'ringing','called':'incoming','incoming':'incoming',
                'receive':'incoming','beingcalled':'incoming','incomingcall':'incoming',
                'invite':'incoming','invited':'incoming','calling':'outgoing',
                'talking':'connected','talk':'connected','connected':'connected','answered':'connected',
                'connecting':'connecting','connecting2':'connecting','idle':'idle','disconnected':'ended',
                'disconnect':'ended','hangup':'ended','ended':'ended','end':'ended',
                'cancelled':'ended','canceled':'ended','missed':'missed'}.get(value,'unknown')

    def normalize(self, event):
        import json
        if not isinstance(event,dict) or not isinstance(event.get('Code'),str):return None
        code=event['Code'];action=str(event.get('Action','')).lower()
        data=event.get('Data') or {}
        fields=self._fields(data)
        door=self._door(fields)
        state=self._state(fields)
        phase='unknown';kind='notification';title='Device event: '+code[:80]
        if code=='VTPCallState':
            if state in ('incoming','ringing'):
                if self.call and self.call['state'] in ('incoming','ringing'):
                    if door!='unknown':self.call['door']=door
                    self.call['code']=code
                    return None
                self.call={'state':state,'door':door,'code':code}
                kind='ring';phase='start';title='Incoming doorbell call'
            elif state in ('connecting','connected'):
                previous=self.call
                if door=='unknown' and previous:door=previous['door']
                if previous and previous['state']==state:return None
                self.call={'state':state,'door':door}
                kind='call';phase=state;title='Door call '+state
            elif state in ('idle','ended','missed'):
                previous=self.call;self.call=None
                if not previous:return None
                if door=='unknown':door=previous['door']
                kind='missed_call' if state=='missed' else 'call_ended'
                phase='end';title='Missed door call' if state=='missed' else 'Door call ended'
            else:return None
        elif code=='BackKeyLight':
            key=(code,door);ringing=str(fields.get('state')) in ('1','2')
            previous=self.active.get(key,False);self.active[key]=ringing
            if not ringing and previous and self.call and self.call.get('code')=='BackKeyLight':
                self.call=None
            if not ringing or previous:return None
            if self.call and self.call['state'] in ('incoming','ringing'):return None
            self.call={'state':'ringing','door':door,'code':code}
            kind='ring';phase='start';title='Doorbell rang'
        elif code in ('Invite','PhoneCallDetect','AccessDoorBell','DoorbellCall','TalkingInvite'):
            if action in ('stop','inactive'):return None
            if self.call and self.call['state'] in ('incoming','ringing'):
                if door!='unknown':self.call['door']=door
                return None
            self.call={'state':'incoming','door':door,'code':code}
            kind='ring';phase='start';title='Doorbell rang'
        elif code=='VideoTalkLog':
            # A delayed log can describe an older call while a new call is
            # already ringing. Never reset or attribute the live call from it.
            kind='missed_call' if str(fields.get('endstate','')).lower() in ('missed','noanswer','noanswered') else 'call_ended'
            phase='end';state='missed' if kind=='missed_call' else 'ended'
            title='Missed door call' if kind=='missed_call' else 'Door call ended'
        elif code in ('VideoTalk','VideoTalkPhone','Talking','TalkingHangup','Hangup'):
            if action=='stop' or code in ('TalkingHangup','Hangup'):
                previous=self.call;self.call=None
                if door=='unknown' and previous:door=previous['door']
                kind='call_ended';phase='end';state='ended';title='Door call ended'
            else:
                if door=='unknown' and self.call:door=self.call['door']
                self.call={'state':'connected','door':door}
                kind='call';phase='connected';state='connected';title='Door call connected'
        fingerprint=hashlib.sha256(json.dumps(event,sort_keys=True,default=str).encode()).hexdigest()
        current=time.monotonic()
        if code in ('VTPCallState','BackKeyLight'):pass  # State edges already suppress duplicates.
        elif current-self.recent.get(fingerprint,-100)<=5:return None
        else:self.recent[fingerprint]=current
        self.recent={k:v for k,v in self.recent.items() if current-v<60}
        self.counter+=1
        return {'id':f'local:{int(time.time()*1000)}:{self.counter}','door':door,'kind':kind,
                'title':title,'time':datetime.now(timezone.utc).isoformat(),'received_at':datetime.now(timezone.utc).isoformat(),
                'verified':kind!='notification','phase':phase,'call_state':state,
                'source':'local state poll' if event.get('Source')=='local state poll' else 'local event push','event_code':code[:80],
                'data_fields':sorted(str(k)[:60] for k in fields)[:30]}
