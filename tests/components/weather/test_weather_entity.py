import pytest
from homeassistant.core import HomeAssistant
from homeassistant.components.weather import OpenWeatherEntity


@pytest.mark.asyncio
async def test_openweather_entity(hass: HomeAssistant):
    """Test OpenWeatherMap entity."""
    entity = OpenWeatherEntity(
        hass, api_key="9356f9452ec40246dfa93a442b0d4a36", city="London"
    )

    await hass.async_add_job(entity.async_update)
    await hass.async_block_till_done()

    assert entity.state is not None
    assert "temperature" in entity.extra_state_attributes
    assert entity.extra_state_attributes["temperature"] is not None