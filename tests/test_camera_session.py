from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import camera_session
import crypto_utils
import wire_protocol as wire


class HandshakeTests(unittest.TestCase):
    def setUp(self):
        self.log = self.enterContext(patch.object(camera_session, "log"))
        self.session = camera_session.CameraSession("test", bind_ip="127.0.0.1",
                                                    did="TEST-000001-ABCDE")
        self.session.sock.close()
        self.session.sock = Mock()
        self.session.sock.getsockname.return_value = ("127.0.0.1", 40000)

    def test_discovery_does_not_repeat_probes_for_foreign_packets(self):
        foreign = (wire.build_f1(wire.MSG_ALIVE_REQ,
                                wire.encode_did("TEST-000002-ABCDE")), ("127.0.0.2", 20000))
        selected = (wire.build_f1(wire.MSG_ALIVE_REQ,
                                 wire.encode_did(self.session.did)), ("127.0.0.3", 20001))
        self.session.sock.recvfrom.side_effect = [foreign] * 50 + [selected]
        with patch.object(camera_session.time, "monotonic", return_value=100):
            self.assertTrue(self.session.discover(target_ip="127.0.0.3"))
        self.session.sock.sendto.assert_called_once_with(
            wire.build_f1(wire.MSG_SEARCH), ("127.0.0.3", wire.DISCOVERY_PORT))
        self.assertEqual(self.session.camera_ip, "127.0.0.3")

    def test_alive_ignores_foreign_ack_and_preserves_early_challenge(self):
        self.session.camera_ip, self.session.camera_port = "127.0.0.3", 20001
        ack = wire.build_f1(wire.MSG_ALIVE_ACK)
        challenge = wire.build_d0(0, wire.CHANNEL_AUTH,
                                  wire.build_auth_head(wire.AUTH_TYPE_CHALLENGE, bytes(16)))
        endpoint = (self.session.camera_ip, self.session.camera_port)
        self.session.sock.recvfrom.side_effect = [
            (ack, ("127.0.0.2", 20000)), (challenge, endpoint), (ack, endpoint)]
        with patch.object(camera_session.time, "monotonic", return_value=100):
            self.session.alive_handshake()
        self.assertEqual(self.session.sock.recvfrom.call_count, 3)
        self.assertEqual(self.session.sock.sendto.call_count, 1)
        self.assertEqual(self.session._pending_datagrams, [(challenge, endpoint)])

    def test_auth_stops_immediately_when_selected_camera_closes(self):
        self.session.camera_ip, self.session.camera_port = "127.0.0.3", 20001
        self.session.sock.recvfrom.return_value = (
            wire.build_f1(wire.MSG_STREAM_END), ("127.0.0.3", 20001))
        self.assertFalse(self.session.auth())
        self.session.sock.recvfrom.assert_called_once()
        self.session.sock.sendto.assert_not_called()

    def test_auth_accepts_selected_camera_and_acks_real_sequences(self):
        self.session.camera_ip, self.session.camera_port = "127.0.0.3", 20001
        endpoint = (self.session.camera_ip, self.session.camera_port)
        challenge = bytes(range(16))
        session_key = bytes(reversed(range(16)))
        self.session.sock.recvfrom.side_effect = [
            (wire.build_f1(wire.MSG_STREAM_END), ("127.0.0.2", 20000)),
            (wire.build_d0(7, wire.CHANNEL_AUTH,
                           wire.build_auth_head(wire.AUTH_TYPE_CHALLENGE, challenge)), endpoint),
            (wire.build_d0(8, wire.CHANNEL_AUTH,
                           wire.build_auth_head(wire.AUTH_TYPE_OK,
                                                crypto_utils.encrypt_block("test", session_key))), endpoint)]
        self.assertTrue(self.session.auth())
        self.assertEqual(self.session.session_key, session_key)
        self.assertNotIn(session_key.hex(), str(self.log.call_args_list))
        self.assertEqual([call.args for call in self.session.sock.sendto.call_args_list], [
            (wire.build_ack(0, 7), endpoint),
            (wire.build_d0(0, wire.CHANNEL_AUTH,
                           wire.build_auth_head(wire.AUTH_TYPE_RESPONSE,
                                                crypto_utils.encrypt_block("test", challenge))), endpoint),
            (wire.build_ack(0, 8), endpoint)])

    def test_discovery_ignores_wrong_did_at_target_ip(self):
        endpoint = ("127.0.0.3", 20001)
        self.session.sock.recvfrom.side_effect = [
            (wire.build_f1(wire.MSG_ALIVE_REQ, wire.encode_did("TEST-000002-ABCDE")), endpoint),
            (wire.build_f1(wire.MSG_ALIVE_REQ, wire.encode_did(self.session.did)), endpoint)]
        with patch.object(camera_session.time, "monotonic", return_value=100):
            self.assertTrue(self.session.discover(target_ip=endpoint[0]))
        self.assertEqual(self.session.sock.recvfrom.call_count, 2)
        self.assertEqual(self.session.did, "TEST-000001-ABCDE")

    def test_discovery_retries_after_interval_not_per_packet(self):
        now = [100.0]
        received = 0

        def receive(_size):
            nonlocal received
            received += 1
            now[0] += 0.25
            if received < 6:
                return b"unrelated", ("127.0.0.2", 20000)
            return (wire.build_f1(wire.MSG_ALIVE_REQ, wire.encode_did(self.session.did)),
                    ("127.0.0.3", 20001))

        self.session.sock.recvfrom.side_effect = receive
        with patch.object(camera_session.time, "monotonic", side_effect=lambda: now[0]):
            self.assertTrue(self.session.discover(target_ip="127.0.0.3", timeout=5))
        self.assertEqual(self.session.sock.sendto.call_count, 2)

    def test_alive_ignores_ack_from_previous_port_on_same_camera(self):
        self.session.camera_ip, self.session.camera_port = "127.0.0.3", 20001
        ack = wire.build_f1(wire.MSG_ALIVE_ACK)
        self.session.sock.recvfrom.side_effect = [
            (ack, ("127.0.0.3", 20000)), (ack, ("127.0.0.3", 20001))]
        with patch.object(camera_session.time, "monotonic", return_value=100):
            self.session.alive_handshake()
        self.assertEqual(self.session.sock.recvfrom.call_count, 2)

    def test_auth_consumes_preserved_challenge_before_reading_socket(self):
        self.session.camera_ip, self.session.camera_port = "127.0.0.3", 20001
        endpoint = ("127.0.0.3", 20001)
        challenge = wire.build_d0(0, wire.CHANNEL_AUTH,
                                  wire.build_auth_head(wire.AUTH_TYPE_CHALLENGE, bytes(16)))
        self.session._pending_datagrams.append((challenge, endpoint))

        def receive(_size):
            self.assertEqual(self.session.sock.sendto.call_count, 2)
            return (wire.build_d0(1, wire.CHANNEL_AUTH,
                                 wire.build_auth_head(wire.AUTH_TYPE_OK,
                                                      crypto_utils.encrypt_block("test", bytes(16)))), endpoint)

        self.session.sock.recvfrom.side_effect = receive
        self.assertTrue(self.session.auth())
        self.assertEqual(self.session._pending_datagrams, [])
        self.assertEqual(self.session.session_key, bytes(16))

    def test_auth_password_rejection_does_not_establish_session(self):
        self.session.camera_ip, self.session.camera_port = "127.0.0.3", 20001
        self.session.sock.recvfrom.return_value = (
            wire.build_d0(9, wire.CHANNEL_AUTH, wire.build_auth_head(wire.AUTH_TYPE_FAILED)),
            ("127.0.0.3", 20001))
        self.assertFalse(self.session.auth())
        self.assertIsNone(self.session.session_key)
        self.session.sock.recvfrom.assert_called_once()
        self.session.sock.sendto.assert_called_once_with(wire.build_ack(0, 9), ("127.0.0.3", 20001))

    def test_auth_requests_challenge_at_expected_intervals(self):
        for did, expected_times in (("TEST-000001-ABCDE", [5, 8]),
                                    ("DOAB-000001-ABCDE", [0, 3, 6, 9])):
            with self.subTest(did=did):
                self.session.did = did
                self.session.camera_ip, self.session.camera_port = "127.0.0.3", 20001
                self.session.local_seq = 0
                now = [0]
                requests = []

                def receive(_size):
                    now[0] += 1
                    raise socket.timeout()

                self.session.sock.recvfrom.side_effect = receive
                with patch.object(camera_session.time, "time", side_effect=lambda: now[0]), \
                        patch.object(self.session, "_send_auth",
                                     side_effect=lambda *args, **kwargs: requests.append((now[0], args, kwargs))):
                    self.assertFalse(self.session.auth(timeout=10))
                self.assertEqual([request[0] for request in requests], expected_times)
                self.assertTrue(all(request[1] == (0, b"") and
                                    request[2] == {"stream_type": wire.CHANNEL_AUTH_REQUEST}
                                    for request in requests))

    def test_auth_answers_ping_without_treating_it_as_a_challenge(self):
        self.session.camera_ip, self.session.camera_port = "127.0.0.3", 20001
        endpoint = ("127.0.0.3", 20001)
        self.session.sock.recvfrom.side_effect = [
            (wire.build_f1(wire.MSG_PING), endpoint),
            (wire.build_f1(wire.MSG_STREAM_END), endpoint)]
        self.assertFalse(self.session.auth())
        self.session.sock.sendto.assert_called_once_with(wire.build_f1(wire.MSG_PONG), endpoint)

    def test_reconnect_retries_false_and_network_errors_with_capped_backoff(self):
        self.session.reconnect = Mock(side_effect=[False, OSError("network unavailable"), False, True])
        with patch.object(camera_session.time, "sleep") as sleep:
            self.session.reconnect_until_success(resolution=2, max_backoff=3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(1.0,), (2.0,), (3,)])
        self.assertEqual(self.session.reconnect.call_count, 4)
        self.assertTrue(all(call.kwargs == {"resolution": 2}
                            for call in self.session.reconnect.call_args_list))
        self.assertIsNone(self.session.reconnecting_since)


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.session = camera_session.CameraSession("test", bind_ip="127.0.0.1",
                                                    did="TEST-000001-ABCDE")
        self.session.sock.close()
        self.session.sock = Mock()
        self.session.session_key = bytes(16)
        for method in ("preflight", "start_video", "start_audio", "stop_video"):
            setattr(self.session, method, Mock())

    def run_stream(self, scenario, keep_stream_open=False):
        class EndStream(Exception):
            pass

        with patch.object(camera_session.gst_rtsp_server, "GstAbusRtspServer") as server, \
                patch.object(camera_session.threading, "Thread"), \
                patch.object(camera_session.threading, "Timer") as timer, \
                patch.object(camera_session.time, "time", return_value=100), \
                patch.object(camera_session.time, "sleep"), \
                patch.object(camera_session, "log") as log:
            def receive(_size):
                callbacks = server.call_args.kwargs
                scenario(callbacks["on_first_client_connect"],
                         callbacks["on_last_client_disconnect"], timer, log)
                raise EndStream()

            self.session.sock.recvfrom.side_effect = receive
            with self.assertRaises(EndStream):
                self.session.read_stream("rtsp://127.0.0.1:8554/test", enable_talkback=False,
                                         keep_stream_open=keep_stream_open)

    def test_keep_open_reuses_stream_without_restart_or_stop_timer(self):
        def scenario(connect, disconnect, timer, _log):
            connect()
            disconnect()
            connect()
            self.session.start_video.assert_called_once()
            self.session.start_audio.assert_called_once()
            self.session.preflight.assert_called_once()
            self.session.stop_video.assert_not_called()
            timer.assert_not_called()
            self.assertTrue(self.session.is_streaming)

        self.run_stream(scenario, keep_stream_open=True)

    def test_callbacks_do_not_reuse_stream_during_reconnect(self):
        def scenario(connect, disconnect, timer, log):
            connect()
            log.reset_mock()
            for session_key in (None, bytes(16)):
                self.session.session_key = session_key
                self.session.reconnecting_since = 100
                disconnect()
                connect()
            self.session.start_video.assert_called_once()
            timer.assert_not_called()
            self.assertEqual(log.call_count, 4)
            self.assertTrue(all("reconnecting" in call.args[0] for call in log.call_args_list))

        self.run_stream(scenario, keep_stream_open=True)

    def test_default_disconnect_stops_after_grace_and_next_client_restarts(self):
        def scenario(connect, disconnect, timer, _log):
            connect()
            disconnect()
            self.session.stop_video.assert_not_called()
            self.assertEqual(timer.call_args.args[0], 30.0)
            timer.return_value.start.assert_called_once()
            timer.call_args.args[1](*timer.call_args.kwargs["args"])
            self.session.stop_video.assert_called_once()
            self.assertFalse(self.session.is_streaming)
            connect()
            self.assertEqual(self.session.start_video.call_count, 2)
            self.assertTrue(self.session.is_streaming)

        self.run_stream(scenario)

    def test_stale_grace_callback_cannot_stop_reconnected_stream(self):
        def scenario(connect, disconnect, timer, _log):
            connect()
            disconnect()
            callback = timer.call_args.args[1]
            arguments = timer.call_args.kwargs["args"]
            connect()
            timer.return_value.cancel.assert_called_once()
            callback(*arguments)
            self.session.stop_video.assert_not_called()
            self.session.start_video.assert_called_once()
            self.assertTrue(self.session.is_streaming)

        self.run_stream(scenario)


if __name__ == "__main__":
    unittest.main()