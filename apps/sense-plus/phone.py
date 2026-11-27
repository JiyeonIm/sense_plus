"""Prepare a private LAN test certificate and launch the two local listeners.
Does not install trust, open firewall ports, use public relays or change OS settings.
"""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import ipaddress
import socket
import sys
import subprocess
import os

ROOT = Path(__file__).resolve().parent


def get_wifi_ip():
    command = (
        "Get-NetIPAddress "
        "-InterfaceAlias 'Wi-Fi' "
        "-AddressFamily IPv4 | "
        "Where-Object {$_.AddressState -eq 'Preferred'} | "
        "Select-Object -First 1 -ExpandProperty IPAddress"
    )

    result = subprocess.check_output(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            command
        ],
        text=True
    ).strip()

    return private_ip(result)

def private_ip(value):
    ip = ipaddress.IPv4Address(value.strip())
    if not any(ip in ipaddress.ip_network(x) for x in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')):
        raise ValueError('사설 Wi-Fi IPv4 주소(192.168.x.x, 10.x.x.x, 172.16~31.x.x)를 입력하세요.')
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((str(ip), 0))
    return ip


def prepare_cert(ip, root=ROOT):
    from cryptography import x509
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    folder = root / 'phone-certs' / str(ip)
    folder.mkdir(parents=True, exist_ok=True)
    ca_file, cert_file, key_file = (folder / 'SENSE-test-root.cer', folder / 'server.pem', folder / 'server-key.pem')
    now = datetime.now(timezone.utc)
    if all(p.is_file() for p in (ca_file, cert_file, key_file)):
        try:
            c = x509.load_pem_x509_certificate(cert_file.read_bytes())
            if c.not_valid_after_utc > now + timedelta(hours=2):
                return ca_file, cert_file, key_file
        except Exception:
            pass
    ca_key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f'SENSE+ TEST ONLY {ip}')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=7))
          .add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True)
          .add_extension(x509.KeyUsage(digital_signature=False,content_commitment=False,key_encipherment=False,data_encipherment=False,key_agreement=False,key_cert_sign=True,crl_sign=True,encipher_only=False,decipher_only=False),critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),critical=False).sign(ca_key,hashes.SHA256()))
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,str(ip))])).issuer_name(ca_name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=7))
            .add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]),critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True,content_commitment=False,key_encipherment=True,data_encipherment=False,key_agreement=False,key_cert_sign=False,crl_sign=False,encipher_only=False,decipher_only=False),critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),critical=False).sign(ca_key,hashes.SHA256()))
    # CA private key is never written. Transfer ONLY the public DER certificate.
    ca_file.write_bytes(ca.public_bytes(serialization.Encoding.DER))
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    if os.name != 'nt': key_file.chmod(0o600)
    return ca_file,cert_file,key_file


def main():
    print('PC와 휴대폰을 같은 Wi-Fi에 연결하세요. 공용/게스트 Wi-Fi는 기기 연결을 차단할 수 있습니다.')
    if os.name == 'nt': subprocess.run(['ipconfig'], check=False)
    else: print('시스템 설정 > Wi-Fi > 세부사항에서 IPv4 주소를 확인하세요.')
    try:
        if os.name == 'nt':
            try:
                ip = get_wifi_ip()
                print(f'[자동 감지] Wi-Fi IPv4: {ip}')

            except Exception:
                print('Wi-Fi IP 자동 감지 실패')
                subprocess.run(['ipconfig'], check=False)

                ip = private_ip(
                    input('PC Wi-Fi IPv4 주소 입력: ')
                )

        else:
            print('시스템 설정에서 Wi-Fi IPv4 주소를 확인하세요.')

            ip = private_ip(
                input('PC Wi-Fi IPv4 주소 입력: ')
            )

        ca, cert, key = prepare_cert(ip)
    except Exception as e:
        print('휴대폰 연결 설정 실패:',type(e).__name__,str(e)); return 1
    print('\n휴대폰에는 이 공개 인증서 파일만 전달하세요:\n',ca)
    print('서버 개인키는 절대 전달하지 마세요. 개인 테스트 기기에서만 CA 신뢰를 설정하고 시험 후 제거하세요.')
    print('휴대폰의 카메라 앱으로 PC 관제 화면에 표시되는 접속 QR을 스캔하세요.')
    print('PC 관제 주소: http://127.0.0.1:8766')
    print('방화벽 허용은 신뢰하는 개인 네트워크에만 적용하세요. 포트 포워딩/외부 공개 금지.')
    return subprocess.call([sys.executable,str(ROOT/'run.py'),'--host',str(ip),'--cert',str(cert),'--key',str(key),'--open-browser'])

if __name__ == '__main__': raise SystemExit(main())
