import unittest
from unittest.mock import Mock, call, patch

import ozo_meshtastic_tw as app


class SendMessagesTests(unittest.TestCase):
    def test_broadcast_uses_default_send_options(self):
        iface = Mock()
        with patch.object(app, "open_interface", return_value=iface):
            app.send_messages({"channel_index": 2}, ["first"], 1, 0)

        iface.sendText.assert_called_once_with("first", channelIndex=2)
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


if __name__ == "__main__":
    unittest.main()
