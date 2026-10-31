"""Dahua HTTP/RTP/DHAV media framing. No network operations on import."""
import hashlib
import os
import re
import socket
import struct
import time
from urllib.parse import urlsplit, urlunsplit
from .client import DMSSError

class MediaError(DMSSError):
    pass

class DeadlineSocket:
    def __init__(self,sock,deadline):self.sock,self.deadline=sock,deadline
    def _bound(self):
        left=self.deadline-time.monotonic()
        if left<=0:raise socket.timeout()
        self.sock.settimeout(left)
    def recv(self,count):self._bound();return self.sock.recv(count)
    def sendall(self,data):self._bound();return self.sock.sendall(data)
    def close(self):self.sock.close()

class RTSPBackchannel:
    # Shared Digest/socket primitives; this bridge sends HTTP GET only.
    def __init__(self,url,*,username,password,timeout,tls=False,vendor_tls=False,backchannel=False):
        p=urlsplit(url)
        self.host,self.port=p.hostname,p.port or 554
        self.username,self.password,self.timeout=username,password,timeout
        self.tls=tls;self.auth=None;self.nc=0
    def open_socket(self):
        sock=socket.create_connection((self.host,self.port),self.timeout)
        if not self.tls:return sock
        try:
            from .media_tls import VendorTLSSocket
            return VendorTLSSocket(sock,self.host,self.timeout)
        except Exception:
            sock.close();raise

    def _authorization(self, method, uri):
            if not self.auth:
                return None
            fields = self.auth
            if fields.get('algorithm', 'MD5').upper() != 'MD5':
                raise MediaError('Unsupported RTSP digest algorithm')
            md5 = lambda s: hashlib.md5(s.encode()).hexdigest()
            ha1 = md5(f'{self.username}:{fields["realm"]}:{self.password}')
            ha2 = md5(f'{method}:{uri}')
            qops = fields.get('qop', '').split(',')
            self.nc += 1
            nc, cnonce = f'{self.nc:08x}', os.urandom(8).hex()
            if 'auth' in qops:
                response = md5(f'{ha1}:{fields["nonce"]}:{nc}:{cnonce}:auth:{ha2}')
            elif fields.get('qop'):
                raise MediaError('Unsupported RTSP digest qop')
            else:
                response = md5(f'{ha1}:{fields["nonce"]}:{ha2}')
            parts = [f'username="{self.username}"', f'realm="{fields["realm"]}"', f'nonce="{fields["nonce"]}"', f'uri="{uri}"', f'response="{response}"']
            if 'opaque' in fields:
                parts.append(f'opaque="{fields["opaque"]}"')
            if 'auth' in qops:
                parts += ['qop=auth', 'nc=' + nc, f'cnonce="{cnonce}"']
            return 'Digest ' + ', '.join(parts)

def rtp_payload(packet, payload_type):
    if len(packet) < 12 or packet[0] >> 6 != 2 or packet[1] & 127 != payload_type:
        raise MediaError('Invalid audio RTP packet')
    start = 12 + (packet[0] & 15) * 4
    if packet[0] & 16:
        if len(packet) < start + 4: raise MediaError('Invalid RTP extension')
        start += 4 + struct.unpack('!H', packet[start + 2:start + 4])[0] * 4
    end = len(packet)
    if packet[0] & 32:
        padding = packet[-1]
        if not padding or padding > end - start: raise MediaError('Invalid RTP padding')
        end -= padding
    if start > end: raise MediaError('Truncated RTP payload')
    return packet[start:end]

class DHAVFrames:
    """Reassemble a DHAV byte stream split across HTTP/RTP packet boundaries."""
    def __init__(self):self.buffer=bytearray();self.extension_tags=set()
    def feed(self,data):
        self.buffer.extend(data)
        if len(self.buffer)>8_000_000:raise MediaError('HTTP camera frame buffer exceeds the limit')
        frames=[]
        while len(self.buffer)>=24:
            if self.buffer[:4]!=b'DHAV':raise MediaError('HTTP camera DHAV framing was lost')
            size=struct.unpack_from('<I',self.buffer,12)[0]
            extra=self.buffer[22]
            if not 32+extra<=size<=4_000_000:raise MediaError('Invalid DHAV frame length')
            if len(self.buffer)<size:break
            frame=bytes(self.buffer[:size]);del self.buffer[:size]
            if frame[-8:-4]!=b'dhav' or struct.unpack_from('<I',frame,size-4)[0]!=size:
                raise MediaError('Invalid DHAV frame trailer')
            index=24
            while index<24+extra:
                tag=frame[index];self.extension_tags.add(tag)
                length=8 if tag in (0x82,0x88,0x8c,0x91,0x92,0x93,0x95,0x9a,0x9b,0xb3) else 4
                index+=length
            frames.append(frame)
        return frames

class HTTPMediaFeed:
    def __init__(self,url,username,password,*,timeout=13,tls=False,on_progress=None):
        parsed=urlsplit(url)
        if parsed.scheme not in ('http','https','rtsp','rtsps') or not parsed.hostname or parsed.username or parsed.password or any(c in url for c in '\r\n'):
            raise MediaError('Invalid cloud HTTP media URL')
        self.url=url;self.host=parsed.hostname
        self.port=parsed.port or (554 if parsed.scheme.startswith('rtsp') else 443 if parsed.scheme=='https' else 80)
        self.secure=tls or parsed.scheme in ('https','rtsps');self.timeout=timeout
        self.path=urlunsplit(('','',parsed.path or '/',parsed.query,''))
        self.path+=('&' if parsed.query else '?')+'method=0'
        socket_url=urlunsplit(('rtsp',f'[{self.host}]:{self.port}' if ':' in self.host else f'{self.host}:{self.port}',parsed.path,parsed.query,''))
        self.auth=RTSPBackchannel(socket_url,username=username,password=password,timeout=timeout,
            tls=self.secure,vendor_tls=self.secure,backchannel=False)
        self.deadline=time.monotonic()+timeout;self.sock=None;self.buf=bytearray();self.status=None;self.headers={}
        self.sdp=b'';self.body_bytes=0
        self.on_progress=on_progress
        self.framing=None;self.first_header=None

    def exact(self,count):
        while len(self.buf)<count:
            chunk=self.sock.recv(max(4096,count-len(self.buf)))
            if not chunk:raise MediaError('HTTP media connection closed')
            self.buf.extend(chunk)
        result=bytes(self.buf[:count]);del self.buf[:count];return result

    def connect(self):
        try:
            self.sock=DeadlineSocket(self.auth.open_socket(),self.deadline)
            for sequence in (1,2):
                headers={'Host':urlsplit(self.url).netloc,'Connection':'keep-alive','Cseq':str(sequence),
                    'Accpet-Sdp':'Private','User-Agent':'Http Stream Client/1.0'}
                authorization=self.auth._authorization('GET',self.path)
                if authorization:headers['Authorization']=authorization
                self.sock.sendall(('GET '+self.path+' HTTP/1.1\r\n'+''.join(k+': '+v+'\r\n' for k,v in headers.items())+'\r\n').encode())
                raw=bytearray()
                while not raw.endswith(b'\r\n\r\n'):
                    raw.extend(self.exact(1))
                    if len(raw)>65536:raise MediaError('HTTP media headers exceed the limit')
                rows=raw.decode('latin1').split('\r\n')
                if not rows[0].startswith('HTTP/1.'):raise MediaError('Unexpected HTTP media response protocol')
                self.status=int(rows[0].split()[1])
                self.headers={k.lower():v.strip() for k,v in (r.split(':',1) for r in rows[1:] if ':' in r)}
                media_type=self.headers.get('content-type','').lower().split(';',1)[0].strip()
                if media_type.endswith(('rtplav','rtpxav','-rtp-lav','-rtp-xav')):self.framing=4
                elif media_type.endswith(('lav','xav')):self.framing=6
                if self.on_progress:
                    challenge=self.headers.get('www-authenticate','').lower()
                    self.on_progress({'stage':'http_headers','http_status':self.status,
                        'auth_scheme':'digest' if challenge.startswith('digest ') else 'wsse' if challenge.startswith('wsse ') else 'other' if challenge else None})
                if self.status==401 and sequence==1:
                    challenge=self.headers.get('www-authenticate','')
                    if not challenge.lower().startswith('digest '):raise MediaError('HTTP preview requires a different authentication method')
                    self.auth.auth={k:v or plain for k,v,plain in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))',challenge[7:])}
                    if not {'realm','nonce'}<=self.auth.auth.keys():raise MediaError('Invalid HTTP preview authentication challenge')
                    length=int(self.headers.get('content-length','0'))
                    if not 0<=length<=1_000_000:raise MediaError('HTTP challenge body exceeds the limit')
                    if length:self.exact(length)
                    if self.headers.get('connection','').lower()=='close':
                        self.sock.close();self.buf.clear();self.sock=DeadlineSocket(self.auth.open_socket(),self.deadline)
                    continue
                if self.status!=200:raise MediaError(f'HTTP preview rejected with status {self.status}')
                length=int(self.headers.get('private-length','0'))
                if not 0<=length<=1_000_000:raise MediaError('HTTP preview metadata exceeds the limit')
                if length:self.sdp=self.exact(length)
                if self.on_progress:self.on_progress({'stage':'http_metadata','private_sdp_bytes':len(self.sdp),
                    'sdp_codecs':re.findall(r'(?m)^a=rtpmap:(\d+) ([A-Za-z0-9_-]+)/(\d+)',self.sdp.decode('latin1')),
                    'sdp_attribute_names':sorted(set(re.findall(r'(?m)^a=([A-Za-z][A-Za-z0-9_-]*)(?=[:\r\n])',self.sdp.decode('latin1')))),
                    'content_type':media_type if re.fullmatch(r'[a-z0-9.+-]+/[a-z0-9.+-]+',media_type) else 'unknown',
                    'response_header_names':sorted(self.headers)})
                return self
            raise MediaError('HTTP preview authentication was rejected')
        except Exception:
            self.close();raise

    def packet(self):
        first=self.exact(1)
        if first==b'#':raise MediaError('HTTP preview control messages require RTSV encryption')
        if first!=b'$':raise MediaError('HTTP preview does not use the expected private framing')
        head=self.exact(3);channel=head[0];short_size=struct.unpack('!H',head[1:])[0]
        if self.framing==4:
            if self.first_header is None:self.first_header=(first+head).hex()
            payload=self.exact(short_size);self.body_bytes+=short_size
            return channel,payload
        tail=self.exact(2);size=struct.unpack('!I',head[1:]+tail)[0]
        if self.first_header is None:self.first_header=(first+head+tail).hex()
        if self.framing is None and size>4_000_000 and short_size>=2 and (tail[0]>>6==2 or tail==b'DH'):
            # CHttpRtpSeparator uses BE16 length / four-byte interleaving.
            self.framing=4
            payload=tail+self.exact(short_size-2);self.body_bytes+=short_size
            return channel,payload
        if size>4_000_000:raise MediaError('HTTP preview packet exceeds the limit')
        self.framing=6
        payload=self.exact(size);self.body_bytes+=size
        return channel,payload

    def close(self):
        if self.sock:
            try:self.sock.shutdown(socket.SHUT_RDWR)
            except (OSError,AttributeError):pass
            self.sock.close();self.sock=None
