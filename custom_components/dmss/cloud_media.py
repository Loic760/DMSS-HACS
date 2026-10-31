"""Experimental native OpenSDK media URL acquisition; kept local after 0.1.0.

URL acquisition is live verified. Generic RTSP decoding and talk negotiation
are not yet verified against the actual relay. Never log returned URLs.
"""
from dataclasses import dataclass, field
import base64
import hashlib
import hmac
import json
import re
import subprocess
from urllib.parse import urlsplit

import requests
from .client import DMSSError, decrypt_play_info, json_bytes


class CloudMediaError(DMSSError):
    pass


@dataclass(frozen=True)
class MediaLease:
    kind: str
    url: str = field(repr=False)
    tls_url: str = field(default="", repr=False)
    protocol: str = 'rtsp'

    def probe(self, timeout=15):
        """Independently inspect codecs with FFmpeg; protect signed URLs."""
        try:
            result = subprocess.run([
                "ffprobe", "-v", "quiet", "-rtsp_transport", "tcp", "-rw_timeout", "8000000",
                "-show_streams", "-of", "json", self.url], stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise CloudMediaError("Cloud RTSP probe timed out") from None
        except FileNotFoundError:
            raise CloudMediaError("FFprobe is unavailable") from None
        if result.returncode:
            raise CloudMediaError("Cloud relay could not be decoded with generic RTSP")
        try:
            streams = json.loads(result.stdout).get("streams", [])
        except (ValueError, AttributeError):
            raise CloudMediaError("FFprobe returned invalid metadata") from None
        return [{k: stream.get(k) for k in ("codec_type", "codec_name", "width", "height",
                                           "sample_rate", "channels")} for stream in streams]


def request_media(client, serial, channel, *, kind="real", stream=0, route="auto", encrypt_mode=0, device_type=None, linked_station=False, protocol='rtsp'):
    """Get a fresh lease, independently reloading device/user credentials.

Native evidence: TransferStreamRequest at 0x1ce2ac; lcopenapi_request_signSaas
at 0x1cfcac in APK libLCBusiness.so. It uses the params envelope and a different
PCS signature than the door-control API. No result is persisted or printed.
    """
    if kind not in {"real", "talk"} or type(channel) is not int or channel < 0 or stream not in (0, 1) or route not in {"auto", "native", "legacy"} or type(encrypt_mode) is not int or encrypt_mode not in (0, 1, 2, 3) or protocol not in ('rtsp','http'):
        raise CloudMediaError("Invalid media operation/channel/stream")
    if protocol=='http' and kind!='real':raise CloudMediaError('HTTP talk is not implemented')
    if device_type not in (None,'device','channel'):
        raise CloudMediaError('Invalid native device target type')
    client.ensure_session()
    client.user_info()  # refresh the separate open-user token before inventory
    device = client.select_device(serial)  # fresh inventory and media credentials
    if not any(str(row.get("channelId")) == str(channel) for row in device.get("deviceChannels", [])):
        raise CloudMediaError("Channel is not in this account's device inventory")
    target_serial,target_channel=serial,channel
    if linked_station:
        rows=[r for r in device.get('deviceChannels',[]) if str(r.get('channelId'))==str(channel)]
        target_serial=rows[0].get('channelSn')
        if not isinstance(target_serial,str) or not target_serial or target_serial==serial:
            raise CloudMediaError('This entrance has no separate station serial in inventory')
        target_channel=0
    if not client.state.get("user_info", {}).get("username"):
        client.user_info()
    info = decrypt_play_info(device["playInfo"], serial)
    entry = info.get("streamInfo", {}).get("streamEntryAddr", "")
    origin = urlsplit(entry if "://" in entry else "https://" + entry)
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password or
            not origin.hostname.endswith(".imoulife.com") or origin.port not in (None, 443) or
            origin.query or origin.fragment or origin.path not in ("", "/")):
        raise CloudMediaError("Unsupported media entry origin; credentials were not sent")
    token = info.get("userToken")
    if not isinstance(token, str) or not token:
        raise CloudMediaError("Device inventory has no media user token")
    params = {"ahEncrypt": False, "talkType": "call" if kind == 'talk' else "", "token": token, "callId": "", "streamId": stream,
              "appendUrl": f"/real/{target_channel}/{stream}" if kind == "real" else (f"/talk/{target_channel}" if target_channel else "/talk"),
              "design": "", "deviceId": target_serial}
    if encrypt_mode:
        suffix = "/encrypt" + ("/TCM" if encrypt_mode == 3 else "")
        params["appendUrl"] = (f"/real/{target_channel}/{stream}" + suffix if kind == "real"
                               else "/talk" + suffix + (f"/{target_channel}" if target_channel else ""))
    if protocol=='http':
        # CLCBusiness::httpRt 0x186238..186338; RTSV2 uses RTSV1 URL routing.
        params['appendUrl']+='/RTSV1'
        params['design']='first'
        params['streamId']=0
    metadata = info.get("streamInfo", {})
    native = route == "native" or (route == "auto" and kind == "real" and bool(metadata.get("rtspToken")))
    endpoint = "getRtspUrl" if native else "transferStream"
    if native:
        if kind != "real":
            raise CloudMediaError("Native channel-talk routing has not been established")
        # CLCOpenInnerApi::getRtspUrl (0x1a5f44) passes the inventory's
        # independently issued relay token/owner/project, then overrides the
        # requested channel and stream. Legacy transferStream omits these.
        names = ("windowNum", "videoLimit", "deviceType", "skipAuth", "rtspToken", "project",
                 "timeLimit", "requestType", "encrypt", "type", "recordId", "owner",
                 "assistStream", "duration", "endTime", "startTime", "streamId", "channelId", "ownerType")
        params = {name: metadata[name] for name in names if name in metadata}
        params.update(token=token, deviceId=target_serial, channelId=str(target_channel), streamId=str(stream),
                      requestType="real", type="RTSV1" if protocol=='http' else "RTSP", encrypt=str(encrypt_mode), design="first" if protocol=='http' else "", assistStream="false")
        if device_type is not None:params['deviceType']=device_type
    body = json_bytes({"params": params})
    ua = base64.b64encode(json_bytes({"clientType": "", "project": "", "clientVersion": "2.6.22",
        "appid": "dmssbaseapp", "clientProtocolVersion": "V6.0.0", "clientOS": "Android", "clientOV": "14"})).decode()
    username = "uuid\\" + client.state["user_info"]["username"].removeprefix("token/").removeprefix("uuid\\")
    md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
    canonical = f"{md5}\nx-pcs-username:{username}\nx-pcs-client-ua:{ua}\n"
    headers = {"Content-Type": "application/json", "Content-MD5": md5, "x-pcs-username": username,
               "x-pcs-client-ua": ua, "Authorization": client.token,
               "x-pcs-signature": base64.b64encode(hmac.new(hashlib.md5(client.token.encode()).hexdigest().encode(),
                                                    canonical.encode(), hashlib.sha256).digest()).decode()}
    try:
        reply = client.http.post(f"https://{origin.hostname}/openapi/{endpoint}", data=body,
                                 headers=headers, timeout=20, allow_redirects=False)
        result = reply.json().get("result", {})
    except (requests.RequestException, ValueError, AttributeError):
        raise CloudMediaError("Cloud media request failed or returned invalid JSON") from None
    code = str(result.get("code", "unknown")) if isinstance(result, dict) else "unknown"
    if not re.fullmatch(r"(?:OP)?[0-9]{1,12}", code):
        code = "unknown"
    if reply.status_code != 200 or code != "0":
        raise CloudMediaError(f"Cloud media rejected: HTTP {reply.status_code}, code {code}")
    data = result.get("data") or {}
    if not isinstance(data, dict):
        raise CloudMediaError("Cloud media returned invalid lease data")
    url, tls_url = data.get("url"), data.get("tlsUrl", "")
    normalized=[]
    for secure,value in ((False,url),(True,tls_url)):
        if not value:
            normalized.append(value)
            continue
        if not isinstance(value, str) or len(value) > 65536:
            raise CloudMediaError("Cloud media returned an invalid URL")
        # HTTP preview leases use bare host:port/path addresses, as does the
        # SDK's HTTP P2P URL builder. Add only the transport scheme; preserve
        # the signed path and query byte-for-byte.
        if protocol=='http' and '://' not in value:
            value=('https://' if secure else 'http://')+value.removeprefix('//')
        parsed = urlsplit(value)
        # HttpRTCamera receives this opaque lease verbatim in the native SDK.
        # Some relays retain an RTSP URI label for an HTTP-requested resource;
        # the selected player, not that label, determines the wire protocol.
        schemes=('http','https','rtsp','rtsps') if protocol=='http' else ('rtsp','rtsps')
        if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
            scheme=parsed.scheme if re.fullmatch(r'[A-Za-z]{1,12}',parsed.scheme) else 'absent'
            raise CloudMediaError(f"Cloud media returned an unsupported stream URL (scheme {scheme}, expected {protocol})")
        try:parsed.port
        except ValueError:raise CloudMediaError('Cloud media returned an invalid relay port') from None
        normalized.append(value)
    url,tls_url=normalized
    if not url:
        raise CloudMediaError("Cloud media returned no stream URL")
    return MediaLease(kind, url, tls_url,protocol)
