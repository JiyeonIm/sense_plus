"""Start the SENSE+ local server from any working directory."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import http.client
import ipaddress
import json
import secrets
import signal
from pathlib import Path
import sys
import webbrowser

from preview import browser_opener, ensure_port_available, validate_binding, local_url
from senseplus import __version__


PROJECT_ROOT = Path(__file__).resolve().parent


def read_loopback_status(port: int, path: str) -> dict:
    with contextlib.closing(http.client.HTTPConnection("127.0.0.1", port, timeout=1)) as connection:
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return {}
        body = response.read(512_001)
        if len(body) > 512_000:
            return {}
        status = json.loads(body)
        return status if isinstance(status, dict) else {}


def open_running_local(user_port: int, admin_port: int) -> bool:
    user_url = local_url("127.0.0.1", user_port, False)
    admin_url = local_url("127.0.0.1", admin_port, False)
    try:
        health = read_loopback_status(user_port, "/api/health")
        if not (health.get("ok") is True and health.get("version") == __version__
                and health.get("mode") == "live" and health.get("pairing_required") is False):
            return False
        monitor = read_loopback_status(admin_port, "/api/monitor")
        if "pairing_required" in monitor:
            pairing_inactive = monitor["pairing_required"] is False
        else:
            pairing_inactive = "pairing_code" in monitor and monitor["pairing_code"] is None
        if monitor.get("user_url") != user_url or not pairing_inactive:
            return False
    except (OSError, ValueError, http.client.HTTPException):
        return False

    print("SENSE+가 이미 실행 중입니다. 기존 연결을 사용합니다.")
    print(f"사용자 웹앱: {user_url}")
    print(f"PC 관제 화면: {admin_url}")
    try:
        if webbrowser.open(admin_url, new=2):
            return True
    except webbrowser.Error:
        pass
    print("브라우저가 열리지 않으면 위 PC 관제 주소로 접속하세요.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SENSE+ 로컬 실행 서버")
    parser.add_argument("--host", default="127.0.0.1", help="기본: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="기본: 8765")
    parser.add_argument("--cert", type=Path, help="HTTPS 인증서 PEM 경로")
    parser.add_argument("--key", type=Path, help="HTTPS 개인키 PEM 경로")
    parser.add_argument("--admin-port", type=int, default=8766, help="PC 로컬 관제 포트")
    parser.add_argument("--open-browser", action="store_true", help="서버 준비 후 브라우저 열기")
    parser.add_argument("--ui-only", action="store_true", help="추가 패키지 없이 UI만 미리보기")
    args = parser.parse_args(argv)

    try:
        cert, key = validate_binding(args.host, args.port, args.cert, args.key)
        if not args.ui_only:
            if not 1 <= args.admin_port <= 65535:
                raise ValueError("관제 포트는 1~65535 사이 숫자여야 합니다.")
            if args.admin_port == args.port:
                raise ValueError("사용자 포트와 관리자 포트는 달라야 합니다.")
            if args.host not in ("127.0.0.1", "localhost", "::1"):
                ip = ipaddress.ip_address(args.host)
                if not isinstance(ip, ipaddress.IPv4Address) or not any(ip in ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")):
                    raise ValueError("휴대폰 모드는 사설 IPv4 주소에만 연결합니다. start_phone_windows.bat를 사용하세요.")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    try:
        ensure_port_available(args.host, args.port)
        if not args.ui_only:
            ensure_port_available("127.0.0.1", args.admin_port)
    except OSError as exc:
        if (args.open_browser and not args.ui_only and args.host == "127.0.0.1" and cert is None
                and open_running_local(args.port, args.admin_port)):
            return 0
        parser.error(str(exc))

    if args.ui_only:
        from preview import serve_preview

        return serve_preview(args.host, args.port, cert, key, args.open_browser)

    try:
        import uvicorn
    except ImportError:
        print("실행에 필요한 패키지가 아직 없습니다.", file=sys.stderr)
        print("Windows: setup_windows.bat / macOS: setup_mac.command를 먼저 실행하세요.", file=sys.stderr)
        print("화면만 보려면: python run.py --ui-only --open-browser", file=sys.stderr)
        return 1

    sys.path.insert(0, str(PROJECT_ROOT))
    from senseplus.server import app
    from senseplus.admin import create_admin
    manager = app.state.manager
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        manager.pairing_code = secrets.token_hex(6).upper()
    user_url = local_url(args.host, args.port, cert is not None)
    rtc = app.state.rtc
    admin = create_admin(
        manager, user_url=user_url, port=args.admin_port, rtc_available=rtc is not None,
        inbound_video_stats=rtc.inbound_video_stats if rtc is not None else None,
    )
    stop_opener = browser_opener("127.0.0.1", args.admin_port, False) if args.open_browser else None
    print(f"사용자 웹앱: {user_url}")
    print(f"PC 관제 화면: http://127.0.0.1:{args.admin_port} (PC 로컬 전용)")
    analysis_status = "AI 분석 설정됨" if manager.ai_configured else "AI 미연결 · 통신 시험만 가능"
    print(f"{analysis_status} · 종료: Ctrl+C")

    class ManagedServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield

        def install_signal_handlers(self):
            pass

    async def run_servers():
        # Both apps share a single event loop and one SessionManager. No admin
        # routes are registered on the phone-facing listener.
        operator = ManagedServer(uvicorn.Config(admin, host="127.0.0.1", port=args.admin_port,
            log_level="warning", access_log=False, proxy_headers=False))
        public = ManagedServer(uvicorn.Config(app, host=args.host, port=args.port,
            ssl_certfile=str(cert) if cert else None, ssl_keyfile=str(key) if key else None,
            log_level="info", access_log=False, proxy_headers=False))

        def stop_servers(_signum, _frame):
            public.should_exit = operator.should_exit = True

        watched = [signal.SIGINT, signal.SIGTERM]
        if sys.platform == "win32":
            watched.append(signal.SIGBREAK)
        handlers = {signum: signal.signal(signum, stop_servers) for signum in watched}
        task = asyncio.create_task(operator.serve())
        try:
            while not operator.started and not task.done():
                await asyncio.sleep(.05)
            if not public.should_exit:
                if task.done():
                    await task
                    raise RuntimeError("관제 서버 시작 실패")
                await public.serve()
        finally:
            operator.should_exit = True
            try:
                await task
            finally:
                for signum, handler in handlers.items():
                    signal.signal(signum, handler)
    try:
        asyncio.run(run_servers())
    except KeyboardInterrupt:
        pass
    finally:
        if stop_opener is not None:
            stop_opener.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
