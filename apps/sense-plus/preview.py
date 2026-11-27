"""Dependency-free UI preview; never starts an AI or WebRTC service."""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import threading
import urllib.parse
import urllib.request
import webbrowser


PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_ROOT = PROJECT_ROOT / "frontend"


def validate_binding(host: str, port: int, cert: Path | None, key: Path | None) -> tuple[Path | None, Path | None]:
    if not 1 <= port <= 65535:
        raise ValueError("포트는 1~65535 사이 숫자여야 합니다.")
    if bool(cert) != bool(key):
        raise ValueError("HTTPS는 --cert와 --key를 함께 지정해야 합니다.")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if not loopback and not cert:
        raise ValueError("스마트폰 접속용 주소에는 HTTPS가 필요합니다. --cert와 --key를 지정하세요.")
    if cert:
        cert, key = cert.expanduser().resolve(), key.expanduser().resolve()
        if not cert.is_file() or not key.is_file():
            raise ValueError("지정한 인증서 또는 개인키 파일을 찾을 수 없습니다.")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
    return cert, key


def ensure_port_available(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            if os.name == "nt":
                # Windows permits sharing some active bindings with REUSEADDR.
                # Check for a listener first so the health opener cannot target it.
                target = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
                with socket.socket(family, socket.SOCK_STREAM) as active:
                    active.settimeout(0.25)
                    if active.connect_ex((target, port)) == 0:
                        raise OSError("같은 포트에서 서버가 실행 중입니다.")
            # Match the servers' reuse policy; an old TIME_WAIT connection is
            # not an active server and must not prevent a normal restart.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
        except OSError as exc:
            raise OSError(f"{host}:{port}를 열 수 없습니다. 해당 포트의 서버를 종료하거나 실행 옵션에서 포트를 변경하세요. ({exc})") from exc


def local_url(host: str, port: int, tls: bool) -> str:
    target = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if ":" in target:
        target = f"[{target}]"
    return f"{'https' if tls else 'http'}://{target}:{port}"


def browser_opener(host: str, port: int, tls: bool) -> threading.Event:
    """Open only after our local health endpoint responds; never changes browser trust."""
    stopped = threading.Event()
    url = local_url(host, port, tls)

    def wait_for_health() -> None:
        handlers: list = [urllib.request.ProxyHandler({})]
        if tls:
            # This probe contacts this locally launched server only and sends no secrets.
            # A developer CA may not be in Python's store. Browser/phone trust is unchanged.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
        opener = urllib.request.build_opener(*handlers)
        for _ in range(80):
            if stopped.is_set():
                return
            try:
                with opener.open(url + "/api/health", timeout=0.5) as response:
                    health = json.load(response)
                if health.get("ok") is True and "rtc_available" in health:
                    if not stopped.is_set():
                        webbrowser.open(url, new=2)
                    return
            except (OSError, ValueError):
                pass
            if stopped.wait(0.25):
                return
        print(f"브라우저가 열리지 않으면 직접 {url} 에 접속하세요.")

    threading.Thread(target=wait_for_health, daemon=True, name="sense-browser-opener").start()
    return stopped


class PreviewHandler(SimpleHTTPRequestHandler):
    def send_json(self, value: dict, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            self.send_json({
                "ok": True,
                "rtc_available": False,
                "ai_configured": False,
                "mode": "preview",
                "detail": "UI 미리보기입니다. 실제 AI 및 실시간 서버에는 연결되지 않았습니다.",
                "rtc_configuration": {"iceServers": []},
            })
            return
        if path.startswith(("/api/", "/webrtc/")):
            self.send_json({"ok": False, "detail": "UI 미리보기에서는 실시간 서버 기능을 사용할 수 없습니다."}, 404)
            return
        resolved = Path(self.translate_path(path)).resolve()
        if not resolved.is_relative_to(FRONTEND_ROOT.resolve()):
            self.send_error(404)
            return
        super().do_GET()

    def do_POST(self) -> None:
        self.send_json({"ok": False, "detail": "UI 미리보기 모드입니다. 실제 연결 시험은 run.py로 실행하세요."}, 503)

    def list_directory(self, path: str):
        self.send_error(404, "Directory listing is disabled")
        return None


def serve_preview(host: str, port: int, cert: Path | None = None, key: Path | None = None, open_browser: bool = False) -> int:
    if not (FRONTEND_ROOT / "index.html").is_file():
        print("frontend/index.html을 찾을 수 없습니다. 압축을 모두 푼 폴더에서 실행하세요.")
        return 1
    server_class = ThreadingHTTPServer
    if ":" in host:
        class IPv6PreviewServer(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_class = IPv6PreviewServer
    server = server_class((host, port), partial(PreviewHandler, directory=str(FRONTEND_ROOT)))
    if cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    stop_opener = browser_opener(host, port, cert is not None) if open_browser else None
    print(f"SENSE+ UI 미리보기: {local_url(host, port, cert is not None)}", flush=True)
    print("화면과 디자인만 확인합니다. AI 추론이나 WebRTC 스트리밍을 제공하지 않습니다.", flush=True)
    print("종료: Ctrl+C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if stop_opener is not None:
            stop_opener.set()
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SENSE+ 화면 미리보기 (추가 패키지 불필요)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        cert, key = validate_binding(args.host, args.port, args.cert, args.key)
        ensure_port_available(args.host, args.port)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    return serve_preview(args.host, args.port, cert, key, args.open_browser)


if __name__ == "__main__":
    raise SystemExit(main())
