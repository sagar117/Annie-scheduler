import asyncio
import json

import pytest

import aiohttp

from callback_checker import (
    parse_readings_from_response,
    fetch_call_readings,
    list_calls_for_org_on_date,
    API_BASE,
)


class FakeResp:
    def __init__(self, status: int, text_data: str):
        self.status = status
        self._text = text_data

    async def text(self):
        await asyncio.sleep(0)
        return self._text

    async def json(self):
        await asyncio.sleep(0)
        return json.loads(self._text)

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, None, status=self.status)


class FakeSession:
    def __init__(self, resp: FakeResp):
        self._resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *args, **kwargs):
        return self._resp


@pytest.mark.asyncio
async def test_fetch_call_readings_handles_json(monkeypatch):
    sample = json.dumps({"value": [{"BP": {"systolic": 123, "diastolic": 80, "units": "mmHg"}}]})
    fake = FakeResp(200, sample)

    async def fake_client_session():
        return FakeSession(fake)

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: FakeSession(fake))

    res = await fetch_call_readings("call-123")
    assert isinstance(res, dict) or isinstance(res, list)
    # Ensure the nested BP object is present
    normalized = parse_readings_from_response(res)
    assert normalized and isinstance(normalized[0], dict)
    assert "BP" in normalized[0] or "bp" in (k.lower() for k in normalized[0].keys())


@pytest.mark.asyncio
async def test_fetch_call_readings_handles_empty_list(monkeypatch):
    sample = json.dumps({"value": []})
    fake = FakeResp(200, sample)
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: FakeSession(fake))

    res = await fetch_call_readings("call-456")
    # Expect either None, empty list, or dict with value=[] depending on backend; parsing should yield []
    normalized = parse_readings_from_response(res)
    assert normalized == []


@pytest.mark.asyncio
async def test_list_calls_for_org_on_date_returns_list(monkeypatch):
    calls = [{"id": 1, "status": "completed", "agent": "annie_rpm", "patient_id": 2}]
    fake = FakeResp(200, json.dumps(calls))
    monkeypatch.setattr(aiohttp, "ClientSession", lambda: FakeSession(fake))

    res = await list_calls_for_org_on_date(6, "2025-11-01")
    assert isinstance(res, list)
    assert res and res[0].get("id") == 1
