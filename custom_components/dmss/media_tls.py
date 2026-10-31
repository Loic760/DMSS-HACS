"""Native media TLS compatibility, scoped to authenticated vendor leases.

APK CertVerifyCallback at 0x1dff448 tolerates only X509 errors 9/10
(certificate not-yet-valid/expired). Other chain failures remain fatal.
Dynamic IP relays must present the vendor's lechange.com certificate identity
under an APK-shipped trusted root. DNS endpoints require their own name.
"""
import ipaddress
from pathlib import Path
import select
import socket
import threading
import time


def date_compatible_verify(connection, certificate, error, depth, preverified):
    return bool(preverified or error in (9, 10))


def _dns_matches(pattern, hostname):
    pattern,hostname=pattern.lower().rstrip('.'),hostname.lower().rstrip('.')
    if pattern.startswith('*.'):
        return hostname.endswith(pattern[1:]) and hostname.count('.')==pattern.count('.')
    return pattern==hostname


class VendorTLSSocket:
    """Socket facade with bounded nonblocking OpenSSL reads and writes."""
    def __init__(self, raw, host, timeout=10):
        from OpenSSL import SSL
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        self.raw,self.timeout=raw,timeout
        self.date_errors=set()
        self.closed=False
        self.io_lock=threading.Lock()
        context=SSL.Context(SSL.TLS_CLIENT_METHOD)
        try:context.load_verify_locations(str(Path(__file__).with_name('dmss_media_ca.pem')))
        except SSL.Error:raise OSError('Media certificate store is unavailable') from None
        def verify(connection,certificate,error,depth,preverified):
            if error in (9,10):self.date_errors.add(error)
            return date_compatible_verify(connection,certificate,error,depth,preverified)
        context.set_verify(SSL.VERIFY_PEER,verify)
        self.connection=SSL.Connection(context,raw)
        self.connection.set_connect_state()
        raw.setblocking(False)
        try:
            try:ipaddress.ip_address(host)
            except ValueError:self.connection.set_tlsext_host_name(host.encode('idna'))
            self._run(self.connection.do_handshake)
            certificate=self.connection.get_peer_certificate().to_cryptography()
            try:names=certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
            except x509.ExtensionNotFound:names=[x.value for x in certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)]
            try:ipaddress.ip_address(host)
            except ValueError:
                if not any(_dns_matches(name,host) for name in names):
                    raise OSError('Vendor media certificate hostname mismatch')
            else:
                if not any(name.lower() in ('*.lechange.com','lechange.com') for name in names):
                    raise OSError('IP relay certificate is not the expected vendor identity')
            self.peer_subject=certificate.subject.rfc4514_string()
            self.peer_issuer=certificate.issuer.rfc4514_string()
        except Exception:
            self.close();raise

    def _run(self, operation):
        from OpenSSL import SSL
        deadline=time.monotonic()+self.timeout
        while not self.closed:
            try:
                with self.io_lock:return operation()
            except (SSL.WantReadError,SSL.WantWriteError) as error:
                left=deadline-time.monotonic()
                if left<=0:raise socket.timeout()
                read,write=([self.raw],[]) if isinstance(error,SSL.WantReadError) else ([],[self.raw])
                ready=select.select(read,write,[],left)
                if not ready[0] and not ready[1]:raise socket.timeout()
            except SSL.ZeroReturnError:return b''
            except (SSL.Error,SSL.SysCallError):
                raise OSError('Vendor media TLS negotiation or connection failed') from None
        raise OSError('Media TLS connection closed')

    def settimeout(self, value):self.timeout=value
    def recv(self, count):return self._run(lambda:self.connection.recv(count))
    def sendall(self, data):
        offset=0
        while offset<len(data):
            count=self._run(lambda:self.connection.send(data[offset:]))
            if not count:raise OSError('Media TLS connection closed')
            offset+=count
    def shutdown(self, how):self.raw.shutdown(how)
    def close(self):
        if not self.closed:self.closed=True;self.raw.close()
