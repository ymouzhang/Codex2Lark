from __future__ import annotations

from types import SimpleNamespace

from codex2lark.capabilities.im.context_provider import IMContextRequest
from codex2lark.capabilities.im.live_reader import OfficialLiveIMReader, WireMessagePage


def wire_message(
    message_id: str,
    *,
    text: str = "hello",
    thread_id: str | None = None,
    root_id: str | None = None,
    parent_id: str | None = None,
) -> object:
    return SimpleNamespace(
        message_id=message_id,
        chat_id="oc_group",
        msg_type="text",
        create_time=100,
        update_time=101,
        deleted=False,
        sender=SimpleNamespace(id="ou_user", sender_type="user", tenant_key="tenant-1"),
        body=SimpleNamespace(content=f'{{"text":"{text}"}}'),
        mentions=(SimpleNamespace(id="ou_bot", name="Agent", key="@_user_1"),),
        thread_id=thread_id,
        root_id=root_id,
        parent_id=parent_id,
    )


class FakeMessageAPI:
    def __init__(self, pages: list[WireMessagePage] | None = None) -> None:
        self.pages = pages or []
        self.get_calls: list[str] = []
        self.list_calls: list[dict[str, object]] = []

    async def get(self, message_id: str) -> object:
        self.get_calls.append(message_id)
        return wire_message(message_id, text="@_user_1 please help")

    async def list(self, **parameters: object) -> WireMessagePage:
        self.list_calls.append(parameters)
        return self.pages.pop(0)


async def test_live_reader_refetches_and_normalizes_the_bound_trigger() -> None:
    reader = OfficialLiveIMReader(FakeMessageAPI(), bot_open_id="ou_bot")

    result = await reader.get_message(
        IMContextRequest("tenant-1", "app-1", "oc_group", "om_trigger")
    )

    assert result.message_id == "om_trigger"
    assert result.body_text == "please help"
    assert result.mentions[0].open_id == "ou_bot"
    assert result.updated_at_ms == 101_000


async def test_live_reader_paginates_bounded_recent_context_and_reports_truncation() -> None:
    api = FakeMessageAPI(
        [
            WireMessagePage((wire_message("om_1"),), True, "next"),
            WireMessagePage((wire_message("om_2"),), True, "more"),
        ]
    )
    reader = OfficialLiveIMReader(api, bot_open_id="ou_bot")
    trigger = await reader.get_message(
        IMContextRequest("tenant-1", "app-1", "oc_group", "om_trigger")
    )

    page = await reader.recent_messages(trigger, since_ms=10_000, limit=2)

    assert [item.message_id for item in page.messages] == ["om_1", "om_2"]
    assert page.complete is False
    assert api.list_calls[0]["page_token"] is None
    assert api.list_calls[1]["page_token"] == "next"
    assert api.list_calls[0]["container_type"] == "chat"


async def test_live_reader_uses_thread_container_for_related_context() -> None:
    api = FakeMessageAPI([WireMessagePage((wire_message("om_1"),), False)])
    reader = OfficialLiveIMReader(api, bot_open_id="ou_bot")
    trigger = wire_message("om_trigger", thread_id="omt_1")
    normalized = reader._normalize(
        trigger,
        IMContextRequest("tenant-1", "app-1", "oc_group", "om_trigger"),
    )

    page = await reader.related_messages(normalized, limit=20)

    assert page.complete is True
    assert api.list_calls[0]["container_type"] == "thread"
    assert api.list_calls[0]["container_id"] == "omt_1"


async def test_live_reader_fetches_known_reply_parent_without_listing_chat() -> None:
    api = FakeMessageAPI()
    reader = OfficialLiveIMReader(api, bot_open_id="ou_bot")
    normalized = reader._normalize(
        wire_message("om_trigger", root_id="om_root", parent_id="om_file"),
        IMContextRequest("tenant-1", "app-1", "oc_group", "om_trigger"),
    )

    page = await reader.related_messages(normalized, limit=20)

    assert [item.message_id for item in page.messages] == ["om_file", "om_root"]
    assert api.get_calls == ["om_file", "om_root"]
    assert api.list_calls == []
    assert page.complete is True
