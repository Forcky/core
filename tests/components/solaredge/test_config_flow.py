"""Tests for the SolarEdge config flow."""

from http import HTTPStatus
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant.typing import ClientSessionGenerator

from homeassistant.components.solaredge.application_credentials import (
    async_register_pending_oauth_flow,
)
from homeassistant.components.solaredge.const import (
    CONF_SITE_ID,
    DOMAIN,
    OAUTH2_AUTHORIZE,
    OAUTH2_CALLBACK_PATH,
    OAUTH2_TOKEN,
)
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_API_KEY, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .conftest import CLIENT_ID, SITE_ID, TOKEN


async def _complete_oauth(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    flow_id: str,
) -> dict:
    """Complete the external OAuth callback."""
    client = await hass_client_no_auth()
    response = await client.get(f"{OAUTH2_CALLBACK_PATH}?code=code&site_id={SITE_ID}")
    assert response.status == 200
    aioclient_mock.post(
        OAUTH2_TOKEN,
        json={
            **TOKEN,
            "token_type": "Bearer",
        },
    )
    return await hass.config_entries.flow.async_configure(flow_id)


async def _start_oauth(hass: HomeAssistant) -> str:
    """Start a user flow and return its ID at the external OAuth step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_ID: SITE_ID}
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    return result["flow_id"]


@pytest.mark.usefixtures(
    "recorder_mock",
    "current_request_with_host",
    "setup_credentials",
    "mock_setup_entry",
    "solaredge_api",
)
async def test_full_flow(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test OAuth setup."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_ID: SITE_ID}
    )
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["url"].startswith(
        f"{OAUTH2_AUTHORIZE}?response_type=code&client_id={CLIENT_ID}"
    )
    assert "scope=SITE_DATA+DEVICE_DATA" in result["url"]
    assert "access_duration=24" in result["url"]
    assert "code_challenge=" in result["url"]
    assert "code_challenge_method=S256" in result["url"]
    assert (
        "redirect_uri=https://example.com/api/solaredge/oauth/callback" in result["url"]
    )

    result = await _complete_oauth(
        hass, hass_client_no_auth, aioclient_mock, result["flow_id"]
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SITE_ID
    assert result["result"].data[CONF_SITE_ID] == SITE_ID
    assert result["result"].data[CONF_TOKEN]["refresh_token"] == "refresh-token"
    assert aioclient_mock.mock_calls[-1][2]["code_verifier"]


@pytest.mark.usefixtures(
    "recorder_mock",
    "current_request_with_host",
    "setup_credentials",
    "mock_setup_entry",
)
async def test_site_not_active(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    solaredge_api: AsyncMock,
) -> None:
    """Test an inactive site is rejected."""
    solaredge_api.get_details.return_value = {
        "details": {"status": "Disabled", "name": "Home"}
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SITE_ID: SITE_ID}
    )
    result = await _complete_oauth(
        hass, hass_client_no_auth, aioclient_mock, result["flow_id"]
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "site_not_active"


@pytest.mark.usefixtures(
    "recorder_mock",
    "current_request_with_host",
    "setup_credentials",
    "mock_setup_entry",
    "solaredge_api",
)
async def test_reauth_migrates_api_key_entry(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Test OAuth reauth removes the retired API key and keeps the site ID."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SolarEdge",
        unique_id=SITE_ID,
        data={CONF_SITE_ID: SITE_ID, CONF_API_KEY: "retired-api-key"},
        version=2,
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    result = await _complete_oauth(
        hass, hass_client_no_auth, aioclient_mock, result["flow_id"]
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert CONF_API_KEY not in entry.data
    assert entry.data[CONF_SITE_ID] == SITE_ID
    assert entry.data[CONF_TOKEN]["refresh_token"] == "refresh-token"


@pytest.mark.usefixtures("recorder_mock", "mock_setup_entry")
async def test_reconfigure_keeps_oauth_token(hass: HomeAssistant) -> None:
    """Test reconfigure does not replace OAuth data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SolarEdge",
        unique_id=SITE_ID,
        data={
            CONF_SITE_ID: SITE_ID,
            "auth_implementation": DOMAIN,
            CONF_TOKEN: TOKEN,
        },
        version=2,
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_TOKEN] == TOKEN


@pytest.mark.usefixtures("recorder_mock")
async def test_migration_starts_reauth(hass: HomeAssistant) -> None:
    """Test a V1 API-key entry is migrated and requests OAuth reauth."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="SolarEdge",
        unique_id=None,
        data={CONF_SITE_ID: SITE_ID, CONF_API_KEY: "retired-api-key"},
        version=1,
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
    assert entry.unique_id == SITE_ID
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH


@pytest.mark.usefixtures(
    "recorder_mock",
    "current_request_with_host",
    "setup_credentials",
    "mock_setup_entry",
    "solaredge_api",
)
@pytest.mark.parametrize(
    "query",
    [
        pytest.param("code=code", id="missing_site_id"),
        pytest.param(f"site_id={SITE_ID}", id="missing_code"),
        pytest.param("code=code&site_id=other-site", id="unknown_site"),
    ],
)
async def test_invalid_callback_keeps_flow(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    query: str,
) -> None:
    """Test a malformed or foreign callback leaves the pending flow usable."""
    flow_id = await _start_oauth(hass)
    client = await hass_client_no_auth()

    response = await client.get(f"{OAUTH2_CALLBACK_PATH}?{query}")
    assert response.status == HTTPStatus.BAD_REQUEST

    result = await _complete_oauth(hass, hass_client_no_auth, aioclient_mock, flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == SITE_ID


@pytest.mark.usefixtures(
    "recorder_mock",
    "current_request_with_host",
    "setup_credentials",
    "mock_setup_entry",
)
async def test_early_callback_keeps_pending_flow(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Test a callback before the authorize URL exists does not consume the flow."""
    await _start_oauth(hass)
    async_register_pending_oauth_flow(hass, "other-site", "other-flow")
    client = await hass_client_no_auth()

    response = await client.get(f"{OAUTH2_CALLBACK_PATH}?code=code&site_id=other-site")
    assert response.status == HTTPStatus.BAD_REQUEST
    assert await response.text() == "SolarEdge authorization flow is not ready"

    # Still pending: a consumed flow would now report "No matching ...".
    response = await client.get(f"{OAUTH2_CALLBACK_PATH}?code=code&site_id=other-site")
    assert await response.text() == "SolarEdge authorization flow is not ready"


@pytest.mark.usefixtures(
    "recorder_mock",
    "current_request_with_host",
    "setup_credentials",
    "mock_setup_entry",
)
async def test_callback_error_aborts_flow(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Test an authorization error from SolarEdge aborts the flow."""
    flow_id = await _start_oauth(hass)
    client = await hass_client_no_auth()

    response = await client.get(
        f"{OAUTH2_CALLBACK_PATH}?error=access_denied&site_id={SITE_ID}"
    )
    assert response.status == HTTPStatus.OK

    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "user_rejected_authorize"


@pytest.mark.usefixtures(
    "recorder_mock",
    "current_request_with_host",
    "setup_credentials",
    "mock_setup_entry",
)
async def test_callback_for_removed_flow(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """Test a callback for a flow that no longer exists is rejected."""
    flow_id = await _start_oauth(hass)
    hass.config_entries.flow.async_abort(flow_id)
    client = await hass_client_no_auth()

    response = await client.get(f"{OAUTH2_CALLBACK_PATH}?code=code&site_id={SITE_ID}")
    assert response.status == HTTPStatus.BAD_REQUEST
    assert await response.text() == "SolarEdge authorization flow expired"
