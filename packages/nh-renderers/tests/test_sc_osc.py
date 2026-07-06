import pytest
from unittest.mock import MagicMock, patch

from nh_core import HarmonicField, Partial
from nh_renderers import SuperColliderOSCAdapter


def test_sc_adapter_start_stop():
    with patch("nh_renderers.sc_osc.SimpleUDPClient") as mock_client:
        adapter = SuperColliderOSCAdapter()
        adapter.start()
        assert adapter.is_running is True
        mock_client.assert_called_once_with("127.0.0.1", 57120)
        adapter.stop()
        assert adapter.is_running is False


def test_sc_adapter_render():
    with patch("nh_renderers.sc_osc.SimpleUDPClient") as mock_client:
        instance = mock_client.return_value
        adapter = SuperColliderOSCAdapter(max_partials=3)
        adapter.start()
        field = HarmonicField(f1=65.0)
        field.partials[1] = Partial(n=1, gain=1.0, spatial={"az": 30.0, "dist": 2.0, "on": True})
        field.partials[2] = Partial(n=2, gain=0.5)
        adapter.render(field)
        assert instance.send_message.call_count >= 4
        adapter.stop()
