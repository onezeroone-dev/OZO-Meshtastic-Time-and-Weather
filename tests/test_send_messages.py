import unittest
from unittest.mock import Mock, call, patch

import ozo_meshtastic_tw as app


class SendMessagesTests(unittest.TestCase):
    def test_broadcast_uses_cli_ack_options(self):
        iface = Mock()
        with patch.object(app, "open_interface", return_value=iface):
            app.send_messages({"channel_index": 2}, ["first"], 1, 0)

        iface.sendText.assert_called_once_with(
            "first", wantAck=True, channelIndex=2, onResponse=iface.localNode.onAckNak
        )
        iface.close.assert_called_once_with()

    def test_retry_does_not_resend_submitted_messages(self):
        first, second = Mock(), Mock()
        first.sendText.side_effect = [None, RuntimeError("send failed")]
        with patch.object(app, "open_interface", side_effect=[first, second]), \
                patch.object(app.time, "sleep"):
            app.send_messages({}, ["first", "second"], 2, 0)

        self.assertEqual(first.sendText.call_args_list, [
            call("first", wantAck=True, channelIndex=0, onResponse=first.localNode.onAckNak),
            call("second", wantAck=True, channelIndex=0, onResponse=first.localNode.onAckNak),
        ])
        second.sendText.assert_called_once_with(
            "second", wantAck=True, channelIndex=0, onResponse=second.localNode.onAckNak
        )
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
