"""Real local WebRTC round trip using synthetic, explicitly labelled media.

No camera, microphone, AI endpoint, network relay, or API key is needed.
The peer uses loopback ICE and checks both uplink counters and a decoded tone.
"""

import asyncio
import contextlib
from fractions import Fraction
import unittest
from unittest.mock import patch

import httpx
import numpy as np

try:
    from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
    from av import AudioFrame, VideoFrame
    from senseplus.rtc import RTCService
    RTC_IMPORT_ERROR = None
except ImportError as exc:
    RTC_IMPORT_ERROR = str(exc)

from senseplus.config import Config
from senseplus.admin import create_admin
from senseplus.adapter import AnalysisResult
from senseplus.server import create_app


if RTC_IMPORT_ERROR is None:
    class SyntheticVideo(VideoStreamTrack):
        async def recv(self):
            pts, time_base = await self.next_timestamp()
            rgb = np.zeros((120, 160, 3), dtype=np.uint8)
            rgb[:, :, 0] = 230
            # A sharp, static target on a mostly red image also exercises still capture.
            y, x = np.indices((48, 56))
            rgb[8:56, 8:64] = np.where(((x // 7 + y // 6) % 2)[:, :, None], 245, 15)
            frame = VideoFrame.from_ndarray(rgb, format="rgb24")
            frame.pts, frame.time_base = pts, time_base
            return frame

    class SyntheticAudio(AudioStreamTrack):
        def __init__(self):
            super().__init__()
            self.count = 0

        async def recv(self):
            await asyncio.sleep(.02)
            t = (np.arange(960) + self.count) / 48000
            pcm = (np.sin(2 * np.pi * 220 * t) * 3000).astype(np.int16).reshape(1, -1)
            frame = AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
            frame.sample_rate = 48000
            frame.pts, frame.time_base = self.count, Fraction(1, 48000)
            self.count += 960
            return frame


@unittest.skipIf(RTC_IMPORT_ERROR is not None, f"FastRTC is not installed: {RTC_IMPORT_ERROR}")
class RealRtcTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_video_audio_uplink_and_generated_tone_downlink(self):
        # The hosted workspace cannot enumerate system interfaces. Keep this
        # test intentionally local: actual ICE/DTLS/SRTP over loopback UDP only.
        addresses = patch("aioice.ice.get_host_addresses", return_value=["127.0.0.1"])
        addresses.start()
        self.addCleanup(addresses.stop)
        app = create_app(config=Config())
        self.assertIsNotNone(app.state.rtc)
        monitor = create_admin(
            app.state.manager, user_url="http://127.0.0.1:8765", port=8766, rtc_available=True,
            inbound_video_stats=app.state.rtc.inbound_video_stats,
        )
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))
        pc.addTrack(SyntheticAudio())
        video = pc.addTransceiver(SyntheticVideo(), direction="sendonly")
        pc.createDataChannel("text")
        tone_received = asyncio.Event()
        readers = []

        @pc.on("track")
        def on_track(track):
            async def consume():
                try:
                    while True:
                        frame = await track.recv()
                        if track.kind == "audio" and np.max(np.abs(frame.to_ndarray().astype(np.int32))) > 300:
                            tone_received.set()
                except Exception:
                    pass
            readers.append(asyncio.create_task(consume()))

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                self.assertTrue((await client.get("/api/health")).json()["video_uplink_only"])
                created = (await client.post("/api/sessions")).json()
                sid = created["session_id"]
                headers = {"Authorization": f"Bearer {created['token']}"}
                try:
                    await pc.setLocalDescription(await pc.createOffer())
                    response = await client.post("/webrtc/offer", headers=headers, json={"sdp":pc.localDescription.sdp, "type":"offer", "webrtc_id":sid})
                    self.assertEqual(response.status_code, 200, response.text)
                    answer = response.json()
                    self.assertEqual(answer.get("type"), "answer", answer)
                    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))

                    async def media_arrives():
                        while True:
                            state = (await client.get(f"/api/sessions/{sid}/state", headers=headers)).json()
                            if state["video_frames"] >= 46 and state["audio_samples"] >= 1600:
                                return state
                            await asyncio.sleep(.05)

                    state = await asyncio.wait_for(media_arrives(), timeout=12)
                    self.assertEqual(state["transport"], "connected")
                    self.assertEqual(video.currentDirection, "sendonly")
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=monitor), base_url="http://127.0.0.1:8766") as admin:
                        response = await admin.get("/api/monitor")
                        response.raise_for_status()
                        received = response.json()["sessions"][0]
                        self.assertGreaterEqual(received["video_packets_received"], state["video_frames"])
                        self.assertGreater(received["video_bytes_received"], 0)
                        self.assertGreaterEqual(received["video_fps"], 25)
                        self.assertLessEqual(received["video_fps"], 35)
                    self.assertFalse(tone_received.is_set(), "No unsolicited audio may be generated")
                    # Verify FastRTC's BGR source conversion preserves red JPEG input.
                    rgb = app.state.manager.sessions[sid].latest_video
                    self.assertGreater(float(rgb[:, :, 0].mean()), 180)
                    self.assertLess(float(rgb[:, :, 2].mean()), 40)

                    response = await client.post(f"/api/sessions/{sid}/control", headers=headers, json={"action":"audio.test", "mode":"guide"})
                    self.assertEqual(response.status_code, 200, response.text)
                    await asyncio.wait_for(tone_received.wait(), timeout=5)
                    self.assertEqual(response.json()["latest_result"]["source"], "transport-test")

                    class TestAdapter:
                        async def analyze(self, payload):
                            self.payload = payload
                            return AnalysisResult("테스트 장면")

                    adapter = TestAdapter()
                    app.state.manager.adapter = adapter
                    app.state.manager.ai_configured = True
                    response = await client.post(f"/api/sessions/{sid}/control", headers=headers,
                                                 json={"action": "guide.start", "mode": "scene"})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertTrue(response.json()["guide_active"])
                    session = app.state.manager.sessions[sid]

                    async def first_ai_result():
                        while (session.latest_result or {}).get("source") != "ai":
                            await asyncio.sleep(.05)

                    await asyncio.wait_for(first_ai_result(), 5)
                    self.assertEqual(session.latest_result["source"], "ai")
                    self.assertGreater(session.last_ai_frame_number, received["video_frames"])
                    self.assertEqual(session.last_ai_frame_received_at, adapter.payload["image_captured_at"])
                    self.assertIsNone(adapter.payload["audio_wav_base64"])
                    response = await client.post(f"/api/sessions/{sid}/control", headers=headers,
                                                 json={"action": "guide.stop", "mode": "scene"})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertFalse(response.json()["guide_active"])
                    await client.delete(f"/api/sessions/{sid}", headers=headers)
                    self.assertNotIn(sid, app.state.manager.sessions)
                finally:
                    await pc.close()
                    for task in readers:
                        task.cancel()
                    for task in readers:
                        with contextlib.suppress(asyncio.CancelledError):
                            await task


if __name__ == "__main__":
    unittest.main()
