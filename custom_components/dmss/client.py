#!/usr/bin/env python3
"""DMSS 2.6.22 static-protocol reconstruction. No network calls on import.

Cloud login and inventory use the actual SmbCommonInterceptor protocol.
Credentials are prompted or supplied through environment variables.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
import uuid

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DEFAULT_HOST = "https://dmss.dolynkcloud.com"
AUTH = "/gateway/dcloud-user/userManage/v1/"


class DMSSError(RuntimeError):
    pass


class APIError(DMSSError):
    def __init__(self, code, status, path):
        self.code, self.status, self.path = str(code), status, path
        reasons = {"DC00050526": "Account password rejected; use login --prompt-password to update it",
                   "DC00050531": "Account locked; stop login attempts and recover it through DMSS",
                   "DC00050533": "Verification code invalid",
                   "DC00050527": "Verification attempts limited; wait before retrying",
                   "DC00050515": "Request clock differs from server time"}
        reason = reasons.get(self.code, "Request failed")
        self.clock_adjusted = False
        super().__init__(f"{reason}: HTTP {status}, API code {self.code}, endpoint {path}")

    @property
    def session_expired(self):
        return self.status == 401 or self.code in {"DC50100012", "12001", "12002"}

    @property
    def device_credentials_expired(self):
        return self.code == "13005"


class VerificationRequired(APIError):
    def __init__(self, status, path):
        super().__init__("DC00050525", status, path)
        self.args = ("DMSS requires a verification code for this terminal (DC00050525). Complete email verification; automatic renewal cannot bypass this challenge.",)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def legacy_password(password: str) -> str:
    """StringUtils.getAccountPasswd (AES key is ASCII hex, not binary MD5)."""
    key = hashlib.md5(b"DAHUAKEY").hexdigest().encode()
    value = hashlib.md5(password.encode()).hexdigest().encode()
    pad = padding.PKCS7(128).padder()
    value = pad.update(value) + pad.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(b"0a52uuEvqlOLc5TO")).encryptor()
    return base64.b64encode(enc.update(value) + enc.finalize()).decode()


def challenge_password(password, salt, random):
    first = hmac.new(salt.encode(), legacy_password(password).encode(), hashlib.sha512).hexdigest()
    return hmac.new(random.encode(), first.encode(), hashlib.sha512).hexdigest()


def encrypt_device_field(value, serial):
    key = hashlib.md5(serial.upper().encode()).hexdigest().upper().encode()
    pad = padding.PKCS7(128).padder()
    value = pad.update(value.encode()) + pad.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(b"HLMUQE2342MABCER")).encryptor()
    return base64.b64encode(enc.update(value) + enc.finalize()).decode()


def decrypt_play_info(play_info, serial):
    """Native decryptIOTPlayInfo, with both SDK arguments set to deviceId."""
    iv = hashlib.md5(serial.encode()).hexdigest()[8:24].encode()
    derived = hashlib.pbkdf2_hmac("sha256", serial.encode(), iv, 1200, 32)
    key = hashlib.md5(base64.b64encode(derived).upper()).hexdigest()[8:24].encode()
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plaintext = dec.update(base64.b64decode(play_info, validate=True)) + dec.finalize()
    unpad = padding.PKCS7(128).unpadder()
    return json.loads((unpad.update(plaintext) + unpad.finalize()).decode())


def pcs_headers(path, body, *, username, token, revision, terminal_id, language="en", country="BE", session_id=None, clock_offset=0):
    """Literal SaasApiRequest.sign for the Java cloud door-control route.

    PCS uses MD5(session salt) as key. The secondary X-DD signature is empty
    because HsviewClientEnvironment.gCos_signKey has no reachable Java setter.
    This route differs from modern SmbCommonInterceptor authentication.
    """
    ua = {"clientType": "phone", "clientVersion": "V2.6.22", "clientOV": "Android 14",
          "clientOS": "Android", "terminalModel": "Python", "terminalId": "",
          "appid": "dmssbaseapp", "project": "Base", "language": language,
          "clientProtocolVersion": "V6.0.0", "timezoneOffset": "0", "terminalBrand": "", "phoneArea": "1"}
    encoded_ua = base64.b64encode(json_bytes(ua)).decode()
    nonce = uuid.uuid4().hex
    date = datetime.fromtimestamp(time.time() + clock_offset, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
    pcs_user = "uuid\\" + username.removeprefix("token/").removeprefix("uuid\\")
    canonical = f"POST\n{path}\n{md5}\napplication/json\nx-pcs-apiver:{revision}\nx-pcs-client-ua:{encoded_ua}\nx-pcs-date:{date}\nx-pcs-nonce:{nonce}\n"
    if session_id:
        canonical += f"x-pcs-session-id:{session_id}\n"
    canonical += "x-pcs-username:" + pcs_user + "\n"
    key = hashlib.md5(token.encode()).hexdigest().encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Content-MD5": md5, "x-pcs-username": pcs_user, "x-pcs-apiver": revision,
               "x-pcs-nonce": nonce, "x-pcs-date": date, "x-pcs-client-ua": encoded_ua,
               "x-pcs-signature": base64.b64encode(hmac.new(key, canonical.encode(), hashlib.sha256).digest()).decode(),
               "Authorization": "Bearer " + token, "cos-request-version": "2.6.22",
               "X-DD-Time": str(int((time.time() + clock_offset) * 1000)), "X-DD-Nonce": uuid.uuid4().hex,
               "X-DD-Version": "", "X-DD-Device-Id": "", "X-DD-Signature": "", "X-DD-Client-Id": "app",
               "openUserId": "", "appSource": ""}
    if session_id:
        headers["x-pcs-session-id"] = session_id
    return headers


def signed_headers(path, body, *, account, country, language, terminal_id,
                   token="", version="2.6.22", timestamp=None, nonce=None, trace=None):
    """Exact canonical ordering from SmbCommonInterceptor.intercept."""
    headers = {
        "X-DD-Time": str(timestamp if timestamp is not None else int(time.time() * 1000)),
        "X-DD-Nonce": nonce or uuid.uuid4().hex,
        "X-DD-ClientVersion": version,
        "X-DD-ClientType": "phone", "X-DD-Client": "Android",
        "X-DD-TraceId": trace or uuid.uuid4().hex, "X-DD-TransCode": "dmss",
        "X-DD-Account": account, "X-DD-Country": country,
        "X-DD-ProjectId": "Base", "X-DD-Language": language,
        "X-DD-TerminalId": terminal_id,
    }
    canonical_path = path[8:] if path.startswith("/gateway") else path
    lines = ["url:" + canonical_path, "method:POST", "content-type:application/json",
             "content-sha256:" + hashlib.sha256(body).hexdigest()]
    lines += [key.lower() + ":" + value for key, value in headers.items()]
    canonical = "\n".join(sorted(lines))
    headers["X-DD-Signature"] = hmac.new(token.encode(), canonical.encode(), hashlib.sha256).hexdigest() if token else ""
    headers.update({"Authorization": "Bearer " + token if token else "",
                    "Content-Type": "application/json", "Accept": "application/json",
                    "X-B3-TraceId": uuid.uuid4().hex, "X-B3-Spanid": uuid.uuid4().hex[:16]})
    return headers


def validated_host(value):
    value = value if "://" in value else "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise DMSSError("Cloud host must be an HTTPS origin without a path or credentials")
    return value.rstrip("/")


def save_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".dmss-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def redact(value):
    """Account inventory includes bearer-like playInfo and playToken fields."""
    if isinstance(value, dict):
        return {k: "<redacted>" if isinstance(v, str) and any(s in k.lower() for s in
                ("password", "token", "salt", "secret", "playinfo", "safeCode".lower()))
                else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(x) for x in value]
    return value


class DMSSClient:
    def __init__(self, state=None, *, host=DEFAULT_HOST, account="", country="BE", area_code="32", language="en", timeout=20,
                 password_provider=None, persist=None, auto_renew=True, session_interval=1800, user_info_interval=900,
                 reauth_interval=21600):
        self.state = dict(state or {})
        self.host = validated_host(self.state.get("host", host))
        self.account = self.state.get("account", account)
        self.country = self.state.get("country", country)
        self.area_code = self.state.get("area_code", area_code)
        self.language = self.state.get("language", language)
        self.terminal_id = self.state.get("terminal_id") or uuid.uuid4().hex
        self.token = self.state.get("token", "")
        self.http = requests.Session()
        self.timeout = timeout
        self.password_provider = password_provider
        self.persist = persist
        self.auto_renew = auto_renew
        self.session_interval = session_interval
        self.user_info_interval = user_info_interval
        self.reauth_interval = reauth_interval
        self._renewing = False
        self._renew_lock = threading.RLock()
        self.clock_offset = self.state.get("clock_offset", 0)
        self.state.update({"host": self.host, "account": self.account, "country": self.country,
                           "area_code": self.area_code, "language": self.language, "terminal_id": self.terminal_id})

    def checkpoint(self):
        if self.persist:
            self.persist(self.state)

    def ensure_session(self):
        if not self.auto_renew or self._renewing or not self.password_provider:
            self.require_token()
            return
        with self._renew_lock:
            if not self.token or time.time() - self.state.get("password_login_at", self.state.get("issued_at", time.time())) >= self.reauth_interval:
                self.renew_session()
            elif time.time() - self.state.get("token_login_at", self.state.get("issued_at", time.time())) >= self.session_interval:
                try:
                    self._renewing = True
                    self.token_login()
                except APIError as exc:
                    if not exc.session_expired:
                        raise
                    self.renew_session()
                finally:
                    self._renewing = False
            if time.time() - self.state.get("user_info_at", 0) >= self.user_info_interval:
                self.user_info()

    def renew_session(self, verify_code=None):
        with self._renew_lock:
            password = self.password_provider(self.account) if self.password_provider else None
            if not password:
                raise DMSSError("Session needs renewal but no saved password is available; run login")
            self._renewing = True
            try:
                self.login(password, verify_code)
                self.state["last_renewed_at"] = time.time()
                self.checkpoint()
                return self.token_status()
            finally:
                self._renewing = False

    def post(self, path, payload, *, pcs_revision=None, mutation=False):
        auth_request = path.startswith(AUTH) and not path.endswith("/getUserInfo")
        mutation = mutation or path.endswith(".OpenAccessControl") or path.endswith(".OpenVthDoor")
        for attempt in range(3):
            try:
                return self._post_once(path, payload, pcs_revision=pcs_revision)
            except APIError as exc:
                if isinstance(exc, VerificationRequired):
                    raise
                if exc.clock_adjusted and not mutation and attempt == 0:
                    continue
                if exc.session_expired and self.auto_renew and not self._renewing and not auth_request and not mutation and attempt == 0:
                    self.renew_session()
                    continue
                if not mutation and not auth_request and exc.status in {429, 500, 502, 503, 504} and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise
            except requests.RequestException as exc:
                if not mutation and not auth_request and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                detail = "; command outcome unknown, not retried" if mutation else ""
                raise DMSSError(f"Cloud connection failed ({type(exc).__name__}){detail}") from None

    def _post_once(self, path, payload, *, pcs_revision=None):
        body = json_bytes(payload)
        if pcs_revision:
            username = self.state.get("user_info", {}).get("username")
            if not username:
                raise DMSSError("Session has no PCS username; run check-token first")
            headers = pcs_headers(path, body, username=username, token=self.token, revision=pcs_revision,
                                  terminal_id=self.terminal_id, language=self.language, country=self.country,
                                  clock_offset=self.clock_offset)
        else:
            headers = signed_headers(path, body, account=self.account, country=self.country,
                                     language=self.language, terminal_id=self.terminal_id, token=self.token,
                                     timestamp=int((time.time() + self.clock_offset) * 1000))
        response = self.http.post(self.host + path, data=body, headers=headers,
                                  timeout=self.timeout, allow_redirects=False)
        self.state["last_http_status"] = response.status_code
        self.state["last_checked_at"] = time.time()
        try:
            result = response.json()
        except ValueError:
            if response.status_code >= 400:
                raise APIError("unknown", response.status_code, path) from None
            raise DMSSError(f"Cloud returned HTTP {response.status_code} without JSON") from None
        if not isinstance(result, dict):
            raise DMSSError("Unexpected cloud response format")
        code = str(result.get("code", "unknown"))
        self.state["last_api_code"] = code
        # Retain diagnostics in the OS vault, never dump the response secrets.
        if code not in {"0", "200", "unknown"}:
            self.state["last_error_message"] = result.get("msg") or result.get("desc")
            self.checkpoint()
        if code == "DC00050515" or response.status_code == 412:
            error = APIError(code, response.status_code, path)
            date_header = response.headers.get("Date")
            if isinstance(date_header, str):
                try:
                    self.clock_offset = parsedate_to_datetime(date_header).timestamp() - time.time()
                    self.state["clock_offset"] = self.clock_offset
                    error.clock_adjusted = True
                    self.checkpoint()
                except (ValueError, TypeError, OverflowError):
                    pass
            raise error
        if code == "DC00050525":
            self.state["verification_required"] = True
            self.checkpoint()
            raise VerificationRequired(response.status_code, path)
        if response.status_code >= 300 or result.get("success") is False:
            error = APIError(code, response.status_code, path)
            self.state["last_valid"] = False if error.session_expired else None
            raise error
        accepted_codes = {"0", "200", "10000"} if pcs_revision else {"0", "200"}
        if result.get("success") is not True and "code" in result and str(result["code"]) not in accepted_codes:
            raise APIError(code, response.status_code, path)
        if pcs_revision and str(result.get("code", "0")) not in accepted_codes:
            raise APIError(code, response.status_code, path)
        # Legacy PCS response envelopes use result.code in addition to HTTP status.
        status = result.get("result")
        if isinstance(status, dict) and str(status.get("code", "0")) not in ("0", "200"):
            raise APIError(status.get("code"), response.status_code, path)
        self.state["last_valid"] = True
        return result

    def login(self, password, verify_code=None):
        with self._renew_lock:
            previous = self._renewing
            self._renewing = True
            try:
                return self._login_once(password, verify_code)
            finally:
                self._renewing = previous

    def _login_once(self, password, verify_code=None):
        if self.state.get("auth_method") == "web":
            from .web_auth import web_login
            try:
                data = web_login(DEFAULT_HOST, self.account, password, http=self.http)
            except requests.RequestException as exc:
                raise DMSSError(f"Desktop login connection failed ({type(exc).__name__})") from None
            self.token = data["token"]
            self.state.update({"token": self.token, "issued_at": time.time(),
                               "login_metadata": data, "account": self.account,
                               "password_login_at": time.time(),
                               "token_login_at": time.time()})
            self.state.pop("verification_required", None)
            self.checkpoint()
            self.user_info()
            return self.token_status()
        self.token = ""
        self.state.pop("token", None)
        for field in ("user_info", "user_info_at", "token_login_at", "token_login_metadata", "login_metadata"):
            self.state.pop(field, None)
        self.state.update({"account": self.account, "terminal_id": self.terminal_id,
                           "country": self.country, "area_code": self.area_code})
        self.checkpoint()
        salt_response = self.post(AUTH + "getSalt", {"account": self.account, "areaCode": self.area_code})
        challenge = salt_response.get("data") or {}
        if not challenge.get("salt") or not challenge.get("random"):
            raise DMSSError("getSalt did not return a salt and random challenge")
        payload = {"account": self.account,
                   "password": challenge_password(password, challenge["salt"], challenge["random"]),
                   "oldPassword": legacy_password(password), "areaCode": self.area_code,
                   "terminalName": "Python DMSS PoC", "multiTerminalValidationFlag": True}
        if verify_code:
            payload["checkCode"] = verify_code
        result = self.post(AUTH + "login", payload)
        data = result.get("data") or {}
        self.token = data.get("salt") or ""
        if not self.token:
            raise DMSSError("Login returned no session salt (verification or account migration may be required)")
        self.state.update({"host": self.host, "account": self.account, "country": self.country,
                           "area_code": self.area_code, "language": self.language,
                           "terminal_id": self.terminal_id, "token": self.token,
                           "issued_at": time.time(), "password_login_at": time.time(), "login_metadata": data})
        self.state.pop("verification_required", None)
        self.checkpoint()
        info = self.user_info()
        new_host = (info.get("data") or {}).get("domainAddr") or data.get("domainAddr")
        if new_host:
            self.host = validated_host(new_host)
            self.state["host"] = self.host
        self.checkpoint()
        return self.token_status()

    def user_info(self):
        self.require_token()
        result = self.post(AUTH + "getUserInfo", {})
        if not isinstance(result.get("data"), dict):
            raise DMSSError("getUserInfo returned no user data")
        self.state["user_info"] = result["data"]
        self.state["user_info_at"] = time.time()
        new_host = result["data"].get("domainAddr")
        if new_host:
            self.host = validated_host(new_host)
            self.state["host"] = self.host
        self.checkpoint()
        return result

    def token_login(self):
        """APK persisted-session login; also used by scheduled keepalive."""
        self.require_token()
        result = self.post(AUTH + "tokenLogin", {"clientPushId": ""})
        self.state["token_login_at"] = time.time()
        data = result.get("data") or {}
        # Never confuse openUserToken/device tokens with the signing salt.
        if data.get("salt"):
            self.token = data["salt"]
            self.state["token"] = self.token
            self.state["issued_at"] = time.time()
        if data.get("domainAddr"):
            self.host = validated_host(data["domainAddr"])
            self.state["host"] = self.host
        self.state["token_login_metadata"] = data
        self.checkpoint()
        return {"success": result.get("success"), "note": "Whether tokenLogin extends session lifetime is unverified"}

    def require_token(self):
        if not self.token:
            raise DMSSError("No saved session; run login first")

    def token_status(self):
        metadata = self.state.get("login_metadata", {})
        issued = self.state.get("issued_at")
        # Field exists in APK. Units/enforcement aren't established statically.
        return {"has_session": bool(self.token), "auth_method": self.state.get("auth_method", "mobile"),
                "session_age_seconds": round(time.time() - issued, 1) if issued else None,
                "server_expiresIn_raw": metadata.get("expiresIn"),
                "expiresIn_units": "unverified; no lifetime invented",
                "has_separate_refresh_token": bool(metadata.get("refreshToken")),
                "last_checked_at": self.state.get("last_checked_at"),
                "last_valid": self.state.get("last_valid"),
                "automatic_renewal": self.auto_renew,
                "verification_required": self.state.get("verification_required", False),
                "session_keepalive_interval_seconds": self.session_interval,
                "user_token_reload_interval_seconds": self.user_info_interval,
                "full_reauth_interval_seconds": self.reauth_interval,
                "last_renewed_at": self.state.get("last_renewed_at")}

    def devices(self):
        self.ensure_session()
        devices = []
        for page in range(1, 1001):
            response = self.post("/gateway/dcloud-device/device/getDeviceList", {
                "withSubDevice": 1, "pageNum": page, "pageSize": 10000, "groupIds": [], "devType": 0})
            data = response.get("data") or {}
            rows = data.get("pageData")
            if not isinstance(rows, list):
                raise DMSSError("Device list response has no data.pageData array")
            # The live API includes a feature-flags row without a deviceId.
            devices.extend(row for row in rows if isinstance(row, dict) and row.get("deviceId"))
            pages = data.get("totalPage")
            if not rows or (pages is not None and page >= int(pages)) or (pages is None and len(rows) < 10000):
                return devices
        raise DMSSError("Device pagination exceeded 1000 pages")

    def device_credentials(self, device):
        serial = device["deviceId"]
        encrypted = device.get("playInfo")
        if not encrypted:
            raise DMSSError("Device returned no playInfo; device credentials cannot be recovered from inventory")
        try:
            info = decrypt_play_info(encrypted, serial)
            username = info["devLoginName"]
            password = info["devLoginPassword"]
            return {"deviceId": serial, "devName": encrypt_device_field(username, serial),
                    "devPassword": encrypt_device_field(password, serial)}
        except (ValueError, KeyError, TypeError) as exc:
            raise DMSSError(f"Device playInfo decryption failed ({type(exc).__name__}); no command sent") from None

    def pcs(self, method, revision, data):
        self.require_token()
        return self.post("/pcs/v1/" + method, {"data": data}, pcs_revision=revision)

    def select_device(self, serial):
        found = [d for d in self.devices() if d.get("deviceId") == serial]
        if len(found) != 1:
            raise DMSSError("Device ID must match exactly one device in this account")
        return found[0]

    def door_inventory(self, device, channel=0):
        serial = device["deviceId"]
        if device.get("devType") == 19 or str(device.get("deviceCatalog", "")).upper() == "VTH":
            response = self.pcs("deviceuseroperate.vth.GetChannelInfo", "222463",
                                {**self.device_credentials(device), "channel": channel})
            indices = (response.get("data") or {}).get("params", {}).get("AccessControlChannel")
            if indices is None:
                return {"kind": "vth", "channel": channel, "doors": [], "relay_inventory_available": False,
                        "note": "This channel returned no relay list; a known primary relay requires --allow-unlisted-primary"}
            if not isinstance(indices, list) or any(type(x) is not int or x < 0 for x in indices):
                raise DMSSError("VTH returned no valid AccessControlChannel relay list")
            return {"kind": "vth", "channel": channel, "doors": [
                {"door_index": index, "label": f"Lock {n + 1}"} for n, index in enumerate(indices)]}
        if device.get("devType") == 17 or "PeripheralLock" in str(device.get("ability", "")):
            response = self.post("/gateway/deviceoperate/accessController/v1/getLockInfo", {"deviceId": serial})
            rows = (response.get("data") or {}).get("lockInfoList")
            if not isinstance(rows, list):
                raise DMSSError("getLockInfo returned no lockInfoList")
            return {"kind": "vto", "doors": [
                {"door_index": n, "label": row.get("name") or f"Door {n + 1}",
                 "status": row.get("status"), "doorAlwaysStatus": row.get("doorAlwaysStatus"),
                 "unlockHoldInterval": row.get("unlockHoldInterval")} for n, row in enumerate(rows)]}
        response = self.pcs("dclouduser.device.GetAccessControlNum", "220452", self.device_credentials(device))
        data = response.get("data") or {}
        try:
            count = int(data["accessControlNum"])
        except (ValueError, TypeError, KeyError):
            raise DMSSError("Door-count response has no valid accessControlNum") from None
        if not 0 <= count <= 128:
            raise DMSSError("Unexpected door count")
        return {"kind": "vto", "doors": [{"door_index": index, "label": f"Door {index + 1}"} for index in range(count)]}

    def doors(self, serial, channel=0, all_channels=False, include_unlisted_primary=False):
        device = self.select_device(serial)
        if all_channels:
            targets = []
            for row in device.get("deviceChannels", []):
                if row.get("channelCatalog") != "VTO":
                    continue
                video_channel = int(row["channelId"])
                inventory = self.door_inventory(device, video_channel)
                locks = inventory["doors"]
                unlisted = inventory.get("relay_inventory_available") is False
                if unlisted and include_unlisted_primary:
                    locks = [{"door_index": 0, "label": "Lock 1"}]
                for lock in locks:
                    targets.append({"door_name": row.get("channelName"), "channel": video_channel,
                                    **lock, "relay_inventory_available": not unlisted,
                                    "requires_unlisted_primary_override": unlisted,
                                    "physical_operation": "unverified"})
            return {"deviceId": serial, "deviceName": device.get("name"), "locks": targets}
        return {"deviceId": serial, "deviceName": device.get("name"),
                **self.door_inventory(device, channel)}

    def unlock(self, serial, door_index, room_no="", channel=0, allow_unlisted_primary=False):
        for attempt in range(2):
            try:
                return self._unlock_once(serial, door_index, room_no, channel, allow_unlisted_primary)
            except APIError as exc:
                if not self.auto_renew or attempt or self._renewing:
                    raise
                if exc.session_expired:
                    self.renew_session()
                elif exc.device_credentials_expired:
                    self.user_info()
                else:
                    raise
                # Only an explicit rejection is retried; rebuild credentials,
                # relay inventory and headers before issuing the command.

    def _unlock_once(self, serial, door_index, room_no="", channel=0, allow_unlisted_primary=False):
        if door_index < 0:
            raise DMSSError("Door index must be zero or greater")
        device = self.select_device(serial)
        inventory = self.door_inventory(device, channel)
        credentials = self.device_credentials(device)
        if door_index not in [row["door_index"] for row in inventory["doors"]]:
            known_vto_channel = any(str(row.get("channelId")) == str(channel) and
                                    row.get("channelCatalog") == "VTO"
                                    for row in device.get("deviceChannels", []))
            if not (allow_unlisted_primary and door_index == 0 and known_vto_channel and
                    inventory["kind"] == "vth" and inventory.get("relay_inventory_available") is False):
                raise DMSSError(f"Door index {door_index} is absent from device relay inventory")
        if inventory["kind"] == "vth":
            response = self.pcs("deviceuseroperate.vth.OpenVthDoor", "222387",
                                {**credentials, "channel": channel, "doorIndex": door_index})
        else:
            response = self.pcs("dclouduser.device.OpenAccessControl", "223806",
                                {**credentials, "doorIndex": str(door_index), "roomNo": room_no})
        if not isinstance(response.get("data"), dict):
            raise DMSSError("Unlock returned no acknowledgement data; physical result is unknown")
        if str(response["data"].get("result", "")).lower() == "false":
            raise DMSSError("Cloud response reported an unsuccessful unlock")
        return {"request_accepted": True, "deviceId": serial, "door_index": door_index,
                "response": response.get("data"), "physical_door_state": "unverified"}

