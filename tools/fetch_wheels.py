"""离线获取 PyPI wheel 并本地安装（适用于 TLS 客户端无法完成握手的受限环境）。

原理：本脚本用"代理 CONNECT 隧道 + 不校验证书的 SSL"手工下载 wheel。
仅用于沙箱/受限环境离线安装；正常环境请直接用 pip。
"""

import json
import os
import socket
import ssl
import sys

PROXY = os.environ.get("PIP_PROXY", "http://127.0.0.1:7897")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", ".wheels") if len(sys.argv) < 2 else sys.argv[1]

# 需要下载的包（含传递依赖）；值为 None 表示取最新版
PACKAGES = [
    "taospy",
    "pytz",
    ("iso8601", "1.0.2"),          # taospy 钉死 iso8601==1.0.2
    "requests",
    ("typing-extensions", "4.14.0"),  # taospy 要求 <4.15.0
    "certifi",
    "charset-normalizer",
    "idna",
    "urllib3",
    "pytest",
    "iniconfig",
    "packaging",
    "pluggy",
    "exceptiongroup",
    "tomli",
    "colorama",
    "pygments",
]


def parse_proxy(p):
    scheme, _, rest = p.partition("://")
    if scheme not in ("http", "https"):
        raise SystemExit("仅支持 http(s) 代理: %s" % p)
    host, _, port = rest.partition(":")
    return host, int(port or 7897)


def http_over_tunnel(proxy_host, proxy_port, target_host, path, headers=None, unverified=True, follow=True, max_hops=5):
    hop = 0
    while hop < max_hops:
        sock = socket.create_connection((proxy_host, proxy_port), timeout=15)
        sock.sendall(
            ("CONNECT %s:443 HTTP/1.1\r\nHost: %s:443\r\n\r\n" % (target_host, target_host)).encode()
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("代理 CONNECT 无响应")
            resp += chunk
        if b" 200 " not in resp.split(b"\r\n")[0]:
            raise ConnectionError("代理 CONNECT 失败: %r" % resp.split(b"\r\n")[0])
        ctx = ssl._create_unverified_context() if unverified else ssl.create_default_context()
        tls = ctx.wrap_socket(sock, server_hostname=target_host)
        head = "".join("%s: %s\r\n" % (k, v) for k, v in (headers or {}).items())
        tls.sendall(
            ("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n%s\r\n" % (path, target_host, head)).encode()
        )
        data = b""
        while True:
            chunk = tls.recv(65536)
            if not chunk:
                break
            data += chunk
        tls.close()
        raw_head, _, body = data.partition(b"\r\n\r\n")
        lines = raw_head.split(b"\r\n")
        status = lines[0].decode(errors="replace")
        code = int(status.split(" ")[1])
        if follow and code in (301, 302, 303, 307, 308):
            location = None
            for line in lines[1:]:
                if line.lower().startswith(b"location:"):
                    location = line.split(b":", 1)[1].strip().decode()
                    break
            if not location:
                raise ConnectionError("重定向无 Location: %r" % status)
            # 解析新的 host/path（可能跨主机 -> 重新建隧道）
            from urllib.parse import urlsplit

            parts = urlsplit(location)
            target_host = parts.hostname
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            hop += 1
            continue
        return status, body
    raise ConnectionError("重定向次数超过 %d" % max_hops)


def split_url(url):
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return parts.hostname, path


def pick_wheel(filename: str) -> bool:
    if not filename.endswith(".whl"):
        return False
    low = filename.lower()
    return "py3-none-any" in low or "cp310" in low or "abi3" in low


def fetch(pkg, version=None):
    url_path = "/pypi/%s/json" % pkg if version is None else "/pypi/%s/%s/json" % (pkg, version)
    status, body = http_over_tunnel(proxy_host, proxy_port, "pypi.org", url_path)
    if not status.startswith("HTTP/1.1 200"):
        print("[skip] %s: %s" % (pkg, status))
        return None
    info = json.loads(body)
    version = info["info"]["version"]
    reqs = info["info"].get("requires_dist") or []
    candidates = [u for u in info["urls"] if pick_wheel(u["filename"])]
    if not candidates:
        print("[skip] %s: 无匹配平台 wheel" % pkg)
        return version, reqs, None
    # 优先纯 Python wheel
    candidates.sort(key=lambda u: 0 if "py3-none-any" in u["filename"] else 1)
    url = candidates[0]
    try:
        fhost, fpath = split_url(url["url"])
        status, body = http_over_tunnel(proxy_host, proxy_port, fhost, fpath)
        if not status.startswith("HTTP/1.1 200"):
            raise SystemExit("[%s] 下载失败: %s" % (pkg, status))
    except SystemExit:
        raise
    except Exception as exc:
        print("[warn] %s 下载异常: %s" % (pkg, exc))
        return version, reqs, None
    dest = os.path.join(OUT, url["filename"])
    with open(dest, "wb") as f:
        f.write(body)
    print("[ok] %s %s -> %s (%d KB)" % (pkg, version, url["filename"], len(body) // 1024))
    return version, reqs, dest


if __name__ == "__main__":
    proxy_host, proxy_port = parse_proxy(PROXY)
    os.makedirs(OUT, exist_ok=True)
    for entry in PACKAGES:
        pkg, version = entry if isinstance(entry, tuple) else (entry, None)
        fetch(pkg, version)

    names = [e[0] if isinstance(e, tuple) else e for e in PACKAGES]
    print("\n下载完成，执行本地安装：")
    print('  python -m pip install --no-index --find-links "%s" %s' % (OUT, " ".join(names)))