import unittest, time, tempfile, ssl, ipaddress
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
import httpx
import numpy as np
from PIL import Image
from senseplus.admin import create_admin
from senseplus.server import create_app
from senseplus.config import Config
from senseplus.core import AUDIO_WAVEFORM_BUCKETS, SessionManager, INPUT_RATE
from senseplus.qr import render_qr_png
from phone import prepare_cert

class AdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_received_frame_and_metadata_not_secrets(self):
        m = SessionManager(Config(ai_key='SECRET_KEY'))
        s = m.create(); s.recording = True
        m.media_video(s, np.full((720,1280,3), 150, dtype=np.uint8))
        m.media_audio(s, INPUT_RATE, np.full((1,320), 3000, dtype=np.int16))
        m._system_result(s,'PRIVATE ANSWER')
        m.pairing_code='TEST_CODE'
        app = create_admin(m, user_url='https://192.168.0.4:8765', port=8766, rtc_available=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8766') as c:
            d=await c.get('/api/monitor'); self.assertEqual(d.status_code,200)
            self.assertNotIn('SECRET_KEY',d.text); self.assertNotIn('PRIVATE ANSWER',d.text); self.assertNotIn(s.token,d.text)
            session=d.json()['sessions'][0]; self.assertEqual(session['video_frames'],1); self.assertEqual(session['audio_samples'],320); self.assertGreater(session['audio_rms'],0)
            self.assertEqual(session['video_size'], [1280, 720])
            self.assertEqual(session['preview_size'], [960, 540])
            f=await c.get('/api/frame/'+s.session_id); self.assertEqual(f.headers['content-type'],'image/jpeg'); self.assertTrue(f.content.startswith(b'\xff\xd8'))
            with Image.open(BytesIO(f.content)) as preview:
                self.assertEqual(list(preview.size), session['preview_size'])
            self.assertEqual((await c.get('/api/monitor',headers={'Host':'not-local.example'})).status_code,403)
            self.assertEqual((await c.get('/api/monitor',headers={'Sec-Fetch-Site':'cross-site'})).status_code,403)
            s.last_video_at=time.monotonic()-5
            self.assertEqual((await c.get('/api/frame/'+s.session_id)).status_code,404)
            await m.close(s.session_id); self.assertEqual((await c.get('/api/frame/'+s.session_id)).status_code,404)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('192.168.0.5',1234)),base_url='http://127.0.0.1:8766') as c:
            self.assertEqual((await c.get('/api/monitor')).status_code,403)
            self.assertEqual((await c.get('/api/pairing-qr.png')).status_code,403)

    async def test_monitor_shows_fps_and_has_no_manual_capture(self):
        manager = SessionManager(Config())
        session = manager.create()
        app = create_admin(manager, user_url='http://127.0.0.1:8765', port=8766, rtc_available=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1:8766') as client:
            with patch.object(session, 'video_fps', return_value=12.5):
                monitor = await client.get('/api/monitor')
            self.assertEqual(monitor.json()['sessions'][0]['video_fps'], 12.5)
            self.assertEqual((await client.post(f'/api/frame/{session.session_id}/stable')).status_code, 404)
            self.assertNotIn('선명한 컷 확인', (await client.get('/')).text)

    async def test_question_waveform_and_arrival_status(self):
        manager = SessionManager(Config())
        session = manager.create()
        app = create_admin(manager, user_url='http://127.0.0.1:8765', port=8766, rtc_available=False)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8766') as client:
            async def monitor():
                return (await client.get('/api/monitor')).json()['sessions'][0]

            self.assertEqual((await monitor())['av_arrival_state'], 'waiting')
            manager.media_video(session, np.zeros((120, 160, 3), dtype=np.uint8))
            manager.media_audio(session, INPUT_RATE, np.full(320, 20000, dtype=np.int16))
            info = await monitor()
            self.assertEqual(info['av_arrival_state'], 'audio_muted')
            self.assertEqual(info['audio_waveform'], [])
            self.assertIsNone(info['av_arrival_gap_ms'])

            await manager.control(session, 'question.start')
            self.assertEqual((await monitor())['av_arrival_state'], 'waiting')
            manager.media_audio(session, INPUT_RATE, np.full(INPUT_RATE + 320, -16384, dtype=np.int16))
            info = await monitor()
            self.assertTrue(info['audio_level_active'])
            self.assertEqual(info['audio_rms'], 0.5)
            self.assertEqual(info['audio_waveform'], [0.5] * AUDIO_WAVEFORM_BUCKETS)
            self.assertEqual(info['av_arrival_state'], 'observed')
            self.assertIsInstance(info['av_arrival_gap_ms'], int)

            session.last_audio_at -= 2
            info = await monitor()
            self.assertEqual(info['av_arrival_state'], 'stale')
            self.assertFalse(info['audio_level_active'])
            self.assertEqual(info['audio_waveform'], [])
            self.assertIsNone(info['av_arrival_gap_ms'])

            manager.media_audio(session, INPUT_RATE, np.zeros(320, dtype=np.int16))
            self.assertEqual((await monitor())['audio_waveform'], [0.0])
            await manager.control(session, 'question.end')
            info = await monitor()
            self.assertEqual(info['av_arrival_state'], 'audio_muted')
            self.assertEqual(info['audio_waveform'], [])
            self.assertFalse(session.audio_peaks)
            self.assertEqual(session.audio_peak_samples, 0)
            await manager.close(session.session_id)

    async def test_pairing_qr_only_for_phone_mode(self):
        manager = SessionManager(Config())
        app = create_admin(manager, user_url='https://192.168.0.4:8765', port=8766, rtc_available=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8766') as client:
            self.assertEqual((await client.get('/api/pairing-qr.png')).status_code, 404)
            unpaired = (await client.get('/api/monitor')).json()
            self.assertFalse(unpaired['pairing_required'])
            self.assertIsNone(unpaired['pairing_qr_version'])

            manager.pairing_code = 'SENSE+/ #'
            with patch('senseplus.admin.render_qr_png', wraps=render_qr_png) as render:
                qr = await client.get('/api/pairing-qr.png')
                render.assert_called_once_with('https://192.168.0.4:8765/#pair=SENSE%2B%2F%20%23')
            self.assertEqual(qr.status_code, 200)
            self.assertEqual(qr.headers['content-type'], 'image/png')
            self.assertEqual(qr.headers['cache-control'], 'no-store')
            self.assertTrue(qr.content.startswith(b'\x89PNG\r\n\x1a\n'))
            monitor = await client.get('/api/monitor')
            paired = monitor.json()
            self.assertTrue(paired['pairing_required'])
            self.assertNotIn('pairing_code', paired)
            self.assertNotIn(manager.pairing_code, monitor.text)
            self.assertTrue(paired['pairing_qr_version'])
            self.assertEqual((await client.get('/api/monitor')).json()['pairing_qr_version'], paired['pairing_qr_version'])

        loopback = create_admin(manager, user_url='https://127.0.0.1:8765', port=8766, rtc_available=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=loopback), base_url='http://127.0.0.1:8766') as client:
            self.assertEqual((await client.get('/api/pairing-qr.png')).status_code, 404)

    async def test_pairing_and_no_admin_routes_on_phone(self):
        m=SessionManager(Config()); m.pairing_code='ABC123'
        app=create_app(m.config,m,enable_rtc=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='https://test') as c:
            health = await c.get('/api/health')
            self.assertTrue(health.json()['pairing_required'])
            self.assertNotIn(m.pairing_code, health.text)
            self.assertEqual((await c.post('/api/sessions')).status_code,403)
            self.assertEqual((await c.post('/api/sessions',headers={'X-Sense-Pairing':'ABC123'})).status_code,201)
            self.assertEqual((await c.get('/api/monitor')).status_code,404)
            self.assertEqual((await c.get('/admin.js')).status_code,404)
            self.assertEqual((await c.get('/api/pairing-qr.png')).status_code,404)
            self.assertEqual((await c.post('/api/frame/unknown/stable')).status_code,405)

class CertificateTests(unittest.TestCase):
    def test_cert_ip_and_reuse(self):
        from cryptography import x509
        with tempfile.TemporaryDirectory() as directory:
            ca,cert,key=prepare_cert(ipaddress.IPv4Address('192.168.1.25'),Path(directory))
            chain=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); chain.load_cert_chain(cert,key)
            parsed=x509.load_pem_x509_certificate(cert.read_bytes())
            self.assertEqual(parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.IPAddress),[ipaddress.IPv4Address('192.168.1.25')])
            root=x509.load_der_x509_certificate(ca.read_bytes()); self.assertTrue(root.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
            self.assertEqual(parsed.issuer,root.subject)
            before=ca.read_bytes(); prepare_cert(ipaddress.IPv4Address('192.168.1.25'),Path(directory)); self.assertEqual(ca.read_bytes(),before)
