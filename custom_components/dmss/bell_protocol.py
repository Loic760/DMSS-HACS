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
            if reply.get('method') == 'client.notifyEventStream':
                self.pending.extend((reply.get('params') or {}).get('eventList') or [])
                continue
            if 'callstate' in str(reply.get('method','')).lower():
                self.pending.append({'Code':'VTPCallState','Action':'State','Data':reply.get('params') or {}})
                continue
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
                    self.request('VideoTalkPhone.attachCallState',{},object_id=handle)
                    self.call_state_supported=True
            except LocalEventError:
                pass
            self.sock.settimeout(1)
            return self
        except Exception:
            self.close();raise

    def listen(self, callback, stop):
        import socket
        for event in self.pending: callback(event)
        self.pending.clear()
        last_keepalive=time.monotonic()
        while not stop.is_set():
            if time.monotonic()-last_keepalive>=self.keepalive_interval:
                self.send('global.keepAlive',{'timeout':60,'active':True})
                last_keepalive=time.monotonic()
            try: reply=self.receive()
            except socket.timeout: continue
            if reply.get('method') == 'client.notifyEventStream':
                events=(reply.get('params') or {}).get('eventList') or []
                for event in events:
                    if isinstance(event,dict): callback(event)
            elif 'callstate' in str(reply.get('method','')).lower():
                callback({'Code':'VTPCallState','Action':'State','Data':reply.get('params') or {}})
            elif reply.get('result') is False:
                raise LocalEventError('Local event session rejected; reconnect required')

    def close(self):
        import socket
        if self.sock:
            try: self.sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            self.sock.close(); self.sock=None


class BellNormalizer:
    """Recognize explicit bell/call signals; retain unknown codes without alerts."""
    def __init__(self, device):
        self.addresses = {r.get('channelIp'):r.get('channelName') for r in device.get('deviceChannels',[]) if r.get('channelName') in ('front','back') and r.get('channelIp')}
        self.recent = {}
        self.active = {}
        self.counter = 0

    def normalize(self, event):
        import json
        if not isinstance(event,dict) or not isinstance(event.get('Code'),str): return None
        code=event['Code']; action=event.get('Action')
        data=event.get('Data') or {}
        if not isinstance(data,dict): data={}
        door='unknown'
        for value in data.values():
            if isinstance(value,str) and value in self.addresses: door=self.addresses[value]
        channel=data.get('Channel',data.get('ChannelID'))
        if door=='unknown' and str(channel) in ('1','2'): door='front' if str(channel)=='1' else 'back'
        state=data.get('State')
        if code=='VTPCallState':
            value=data.get('emCallState',data.get('CallState',data.get('callState',data.get('State',data.get('state')))))
            if isinstance(value,dict):
                data={**data,**value}
                value=value.get('State',value.get('state'))
            # Named wire states; integer mapping only for the explicit SDK field.
            ringing=(isinstance(value,str) and value.lower() in ('ring','ringing','called','incoming','receive','beingcalled','incomingcall','invited')) or data.get('emCallState') in (3,4)
            key=(code,door)
            previous=self.active.get(key,False);self.active[key]=ringing
            if not ringing or previous:return None
            kind='ring';title='Incoming doorbell call'
        elif code=='BackKeyLight':
            key=(code,door)
            ringing=str(state) in ('1','2')
            previous=self.active.get(key,False);self.active[key]=ringing
            if not ringing or previous: return None
            kind='ring';title='Doorbell rang'
        elif code in ('Invite','PhoneCallDetect','AccessDoorBell','DoorbellCall','TalkingInvite'):
            if action in ('Stop','Inactive'): return None
            kind='ring';title='Doorbell rang'
        elif code=='VideoTalkLog':
            kind='missed_call' if data.get('EndState')=='Missed' else 'call'
            title='Missed door call' if kind=='missed_call' else 'Door call ended'
        elif code in ('VideoTalk','VideoTalkPhone','Talking'):
            if action=='Stop': return None
            kind='call';title='Door call activity'
        else:
            kind='notification';title='Device event: '+code[:80]
        fingerprint=hashlib.sha256(json.dumps(event,sort_keys=True,default=str).encode()).hexdigest()
        current=time.monotonic()
        if code in ('BackKeyLight','VTPCallState') or current-self.recent.get(fingerprint,-100)>5:
            self.recent[fingerprint]=current
        else: return None
        self.recent={k:v for k,v in self.recent.items() if current-v<60}
        self.counter+=1
        return {'id':f'local:{int(time.time()*1000)}:{self.counter}','door':door,'kind':kind,
                'title':title,'time':datetime.now(timezone.utc).isoformat(),'received_at':datetime.now(timezone.utc).isoformat(),
                'verified':kind!='notification','phase':'start' if kind=='ring' else 'end' if code=='VideoTalkLog' else 'unknown','source':'local event push','event_code':code[:80],
                'data_fields':sorted(str(k)[:60] for k in data)[:30]}
