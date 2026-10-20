"""Bounded HTTPS calls pinned to validated public IPs with original TLS hostname."""
import ipaddress,json,socket,http.client,ssl
from urllib.parse import urlsplit

class UnknownResult(ValueError):pass

def resolve(url):
    parts=urlsplit(url)
    if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.port not in (None,443):raise ValueError('仅允许公网HTTPS标准端口地址')
    try:addresses=socket.getaddrinfo(parts.hostname,443,type=socket.SOCK_STREAM)
    except OSError:raise ValueError('无法解析来源地址') from None
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):raise ValueError('禁止访问本地或私有网络来源')
    return parts,sorted({x[4][0] for x in addresses},key=lambda ip:':' in ip)

def public_url(url):
    resolve(url);return url

def request(url,data=None,headers=None,limit=2000000,timeout=30,method=None):
    parts,addresses=resolve(url)
    # A second DNS lookup must not turn a user-supplied public source into a private target.
    class PinnedHTTPS(http.client.HTTPSConnection):
        def connect(self):
            sock=socket.create_connection((addresses[0],443),self.timeout)
            self.sock=self._context.wrap_socket(sock,server_hostname=parts.hostname)
    connection=PinnedHTTPS(parts.hostname,443,timeout=timeout,context=ssl.create_default_context())
    try:
        target=(parts.path or '/')+('?'+parts.query if parts.query else '')
        connection.request(method or ('POST' if data is not None else 'GET'),target,body=data,headers={'User-Agent':'VentureWorkspace/2',**(headers or {})})
        response=connection.getresponse()
        if not 200<=response.status<300:raise UnknownResult('外部服务HTTP '+str(response.status)+'；先核对结果，不自动重发')
        raw=response.read(limit+1)
        if len(raw)>limit:raise UnknownResult('外部结果超出读取上限，请核查服务端操作')
        return raw,dict(response.getheaders())
    except (OSError,TimeoutError,http.client.HTTPException):raise UnknownResult('外部连接超时或中断；结果未知，不自动重发') from None
    finally:connection.close()

def json_request(url,data=None,headers=None,method=None,limit=2000000,timeout=30):
    raw,_=request(url,json.dumps(data).encode() if data is not None else None,{'Accept':'application/json',**({'Content-Type':'application/json'} if data is not None else {}),**(headers or {})},method=method,limit=limit,timeout=timeout)
    try:return json.loads(raw)
    except (ValueError,UnicodeError):raise UnknownResult('外部服务未返回可核验JSON') from None
