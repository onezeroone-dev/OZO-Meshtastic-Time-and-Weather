import json
import unittest
from unittest.mock import Mock, call, mock_open, patch

import ozo_meshtastic_tw as app


class SendMessagesTests(unittest.TestCase):
    def test_broadcast_uses_default_send_options(self):
        iface = Mock()
        with patch.object(app, "open_interface", return_value=iface), \
                patch.object(app.time, "sleep") as sleep:
            app.send_messages({"channel_index": 2}, ["first"], 1, 0)

        iface.sendText.assert_called_once_with("first", channelIndex=2)
        sleep.assert_called_once_with(5)
        iface.close.assert_called_once_with()

    def test_waits_after_last_message_before_closing(self):
        events = Mock()
        iface = Mock()
        events.attach_mock(iface, "iface")
        with patch.object(app, "open_interface", return_value=iface), \
                patch.object(app.time, "sleep") as sleep:
            events.attach_mock(sleep, "sleep")
            app.send_messages({"post_send_delay_seconds": 2.5}, ["first", "second"], 1, 0)

        self.assertEqual(events.mock_calls, [
            call.iface.sendText("first", channelIndex=0),
            call.sleep(3),
            call.iface.sendText("second", channelIndex=0),
            call.sleep(2.5),
            call.iface.close(),
        ])

    def test_zero_disables_delay(self):
        iface = Mock()
        with patch.object(app, "open_interface", return_value=iface), \
                patch.object(app.time, "sleep") as sleep:
            app.send_messages({"post_send_delay_seconds": 0}, ["first"], 1, 0)
        sleep.assert_called_once_with(0)
        iface.close.assert_called_once_with()

    def test_partial_send_waits_before_closing_and_retrying(self):
        events = Mock()
        first, second = Mock(), Mock()
        events.attach_mock(first, "first")
        events.attach_mock(second, "second")
        first.sendText.side_effect = [None, RuntimeError("send failed")]
        with patch.object(app, "open_interface", side_effect=[first, second]), \
                patch.object(app.time, "sleep") as sleep:
            events.attach_mock(sleep, "sleep")
            app.send_messages({"post_send_delay_seconds": 2}, ["first", "second"], 2, 1)

        self.assertEqual(events.mock_calls, [
            call.first.sendText("first", channelIndex=0),
            call.sleep(3),
            call.first.sendText("second", channelIndex=0),
            call.sleep(2),
            call.first.close(),
            call.sleep(1),
            call.sleep(3),
            call.second.sendText("second", channelIndex=0),
            call.sleep(2),
            call.second.close(),
        ])

    def test_no_wait_if_nothing_was_submitted(self):
        iface = Mock()
        iface.sendText.side_effect = RuntimeError("send failed")
        with patch.object(app, "open_interface", return_value=iface), \
                patch.object(app.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                app.send_messages({}, ["first"], 1, 0)
        sleep.assert_not_called()
        iface.close.assert_called_once_with()

    def test_retry_does_not_resend_submitted_messages(self):
        first, second = Mock(), Mock()
        first.sendText.side_effect = [None, RuntimeError("send failed")]
        with patch.object(app, "open_interface", side_effect=[first, second]), \
                patch.object(app.time, "sleep"):
            app.send_messages({}, ["first", "second"], 2, 0)

        self.assertEqual(first.sendText.call_args_list, [
            call("first", channelIndex=0),
            call("second", channelIndex=0),
        ])
        second.sendText.assert_called_once_with("second", channelIndex=0)
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()

    def test_delay_validation(self):
        for value in (0, 0.5, 5):
            with self.subTest(valid=value):
                cfg = {"schedule": {"interval_minutes": 60},
                       "meshtastic": {"post_send_delay_seconds": value}}
                with patch("builtins.open", mock_open(read_data=json.dumps(cfg))):
                    self.assertEqual(app.load_config("config.json"), cfg)
        for value in (-1, None, "5", True, float("nan"), float("inf")):
            with self.subTest(invalid=value):
                cfg = {"schedule": {"interval_minutes": 60},
                       "meshtastic": {"post_send_delay_seconds": value}}
                with patch("builtins.open", mock_open(read_data=json.dumps(cfg))):
                    with self.assertRaisesRegex(ValueError, "meshtastic.post_send_delay_seconds"):
                        app.load_config("config.json")


if __name__ == "__main__":
    unittest.main()
