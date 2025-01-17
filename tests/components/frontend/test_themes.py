import pytest
from homeassistant.core import HomeAssistant

@pytest.mark.asyncio
async def test_ui_theme_change(hass: HomeAssistant, hass_ws_client):
    """Test changing the UI theme."""
    client = await hass_ws_client(hass)
    await hass.services.async_call(
        "frontend", "set_theme", {"name": "Custom Theme"}, blocking=True
    )

    await client.send_json({"id": 1, "type": "frontend/get_themes"})
    result = await client.receive_json()

    assert result["result"]["default_theme"] =="Custom Theme"