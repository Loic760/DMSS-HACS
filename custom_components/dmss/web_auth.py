"""Official desktop portal login, verified with mobile and door APIs.

Recovered from the vendor's index-214443df.js. No CAPTCHA bypass or account
management mutations are performed here. Tokens returned here are not assumed
to be interchangeable with the mobile signing salt.
"""
import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

LOGIN_PATH = "/pcs/v1/dclouduser.account.GetTokenAndLogin"


def web_login(host, account, password, *, http=None):
    from .client import (legacy_password, json_bytes, validated_host, DMSSError,
                          APIError, VerificationRequired)
    host = validated_host(host)
    offset = datetime.now(ZoneInfo("Europe/Paris")).utcoffset()
    body = json_bytes({"data": {"timezoneOffset": -int(offset.total_seconds() * 1000),
                               "password": legacy_password(password), "account": account}})
    ua = {"project": "Base", "language": "zh_CN", "clientVersion": "V1.99.710",
          "clientType": "web", "clientProtocolVersion": "V6.0.0", "timezoneOffset": "28800",
          "appid": "dmssbaseapp", "phoneArea": "1"}
    headers = {"Content-Type": "application/json", "Content-MD5": base64.b64encode(hashlib.md5(body).digest()).decode(),
               "x-pcs-source": "external", "x-pcs-date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "x-pcs-apiver": "225932", "x-pcs-nonce": secrets.token_hex(16),
               "x-pcs-username": "account\\" + account,
               "x-pcs-client-ua": base64.b64encode(json_bytes(ua)).decode(),
               "X-B3-TraceId": secrets.token_hex(16), "X-B3-SpanId": secrets.token_hex(8)}
    canonical = "POST\n" + LOGIN_PATH + "\n" + headers["Content-MD5"] + "\napplication/json\n"
    canonical += "".join(name + ":" + headers[name] + "\n" for name in (
        "x-pcs-apiver", "x-pcs-client-ua", "x-pcs-date", "x-pcs-nonce", "x-pcs-username"))
    key = hashlib.md5(hashlib.md5(password.encode()).hexdigest().encode()).hexdigest().encode()
    headers["x-pcs-signature"] = base64.b64encode(hmac.new(key, canonical.encode(), hashlib.sha256).digest()).decode()
    response = (http or requests).post(host + LOGIN_PATH, data=body, headers=headers,
                                      timeout=30, allow_redirects=False)
    try:
        result = response.json()
    except ValueError:
        raise DMSSError(f"Desktop login returned HTTP {response.status_code} without JSON") from None
    if not isinstance(result, dict):
        raise DMSSError("Unexpected desktop login response")
    data = result.get("data")
    code = result.get("code")
    # Preserve only scalar protocol codes, never arbitrary server text in errors.
    code = str(code) if isinstance(code, (str, int)) and not isinstance(code, bool) else "unknown"
    if not re.fullmatch(r"(?:DC)?[0-9]{1,12}", code):
        code = "unknown"
    if code == "DC00050525":
        raise VerificationRequired(response.status_code, LOGIN_PATH)
    if response.status_code != 200 or code != "10000":
        error = APIError(code, response.status_code, LOGIN_PATH)
        if code in {"12318", "10009"}:
            reason = ("Desktop account does not exist" if code == "12318" else
                      "Desktop account locked; wait at least five minutes before retrying")
            error.args = (f"{reason}: HTTP {response.status_code}, API code {code}",)
        raise error
    if isinstance(data, dict) and "failNum" in data:
        error = APIError(code, response.status_code, LOGIN_PATH)
        error.args = ("Desktop account password rejected",)
        raise error
    if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"].strip():
        raise DMSSError("Desktop login did not return a usable session token")
    return data
