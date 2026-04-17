"""
Unit tests for `render_card` — pure-function, no network, no DI.

These tests pin the exact Adaptive Card JSON shape produced from each DSL
construct so regressions are loud and obvious.
"""

from src.schemas.enums import BannerStyle, TextAlign, TextColor, TextSize, TextWeight
from src.schemas.teams import Banner, Button, MessageRow, TeamsMessage, TextSpan
from src.services.teams import render_card


def _card(payload: dict) -> dict:
    """Unwrap the Teams envelope -> the inner AdaptiveCard content."""
    return payload["attachments"][0]["content"]


def test_minimal_single_row_title_only() -> None:
    msg = TeamsMessage(title=TextSpan(text="Hello"))
    out = render_card(msg)

    assert out["type"] == "message"
    assert out["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"

    card = _card(out)
    assert card["type"]    == "AdaptiveCard"
    assert card["version"] == "1.4"
    assert len(card["body"]) == 1
    assert card["body"][0]["type"]                == "TextBlock"
    assert card["body"][0]["text"]                == "Hello"
    assert card["body"][0]["horizontalAlignment"] == "Left"


def test_two_column_row_produces_columnset() -> None:
    """Left + right on one row -> ColumnSet with stretch + auto."""
    msg = TeamsMessage(
        rows = [
            MessageRow(
                left  = TextSpan(text="Ticket"),
                right = TextSpan(text="#123"),
            )
        ]
    )
    card = _card(render_card(msg))
    elem = card["body"][0]

    assert elem["type"] == "ColumnSet"
    assert [c["width"] for c in elem["columns"]] == ["stretch", "auto"]
    assert elem["columns"][0]["items"][0]["text"]                == "Ticket"
    assert elem["columns"][0]["items"][0]["horizontalAlignment"] == "Left"
    assert elem["columns"][1]["items"][0]["text"]                == "#123"
    assert elem["columns"][1]["items"][0]["horizontalAlignment"] == "Right"


def test_right_only_row_is_right_aligned_textblock() -> None:
    """right without left -> single TextBlock horizontally right-aligned."""
    msg = TeamsMessage(rows=[MessageRow(right=TextSpan(text="R"))])
    card = _card(render_card(msg))
    block = card["body"][0]
    assert block["type"]                == "TextBlock"
    assert block["horizontalAlignment"] == "Right"


def test_bold_color_size_are_preserved() -> None:
    """Styling choices from TextSpan carry over to the TextBlock verbatim."""
    msg = TeamsMessage(
        title = TextSpan(
            text   = "Bold large accent",
            weight = TextWeight.BOLDER,
            size   = TextSize.LARGE,
            color  = TextColor.ACCENT,
            align  = TextAlign.CENTER,
        )
    )
    block = _card(render_card(msg))["body"][0]
    assert block["weight"]              == "Bolder"
    assert block["size"]                == "large"
    assert block["color"]               == "Accent"
    assert block["horizontalAlignment"] == "Center"


def test_banner_prepended_as_styled_container() -> None:
    msg = TeamsMessage(
        banner = Banner(text="SYSTEM DOWN", style=BannerStyle.ATTENTION, bold=True),
        title  = TextSpan(text="Details"),
    )
    body = _card(render_card(msg))["body"]

    assert body[0]["type"]             == "Container"
    assert body[0]["style"]            == "attention"
    assert body[0]["items"][0]["text"] == "SYSTEM DOWN"
    assert body[0]["items"][0]["weight"] == "Bolder"


def test_buttons_become_action_openurl() -> None:
    msg = TeamsMessage(
        title   = TextSpan(text="Click below"),
        buttons = [Button(title="Open Portal", url="https://example.com/p")],
    )
    card = _card(render_card(msg))
    assert card["actions"][0]["type"]  == "Action.OpenUrl"
    assert card["actions"][0]["title"] == "Open Portal"
    assert card["actions"][0]["url"]   == "https://example.com/p"


def test_separator_flag_propagates() -> None:
    msg = TeamsMessage(rows=[MessageRow(left=TextSpan(text="line"), separator=True)])
    assert _card(render_card(msg))["body"][0]["separator"] is True


def test_empty_card_rejected_by_schema() -> None:
    """TeamsMessage itself validates that at least one visible element exists."""
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TeamsMessage()


def test_webhook_xor_enforced_by_schema() -> None:
    """webhook_url and webhook_target cannot both be set."""
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TeamsMessage(
            title          = TextSpan(text="hi"),
            webhook_url    = "https://example.com/hook",
            webhook_target = "superstat",
        )


def test_row_must_have_a_side() -> None:
    """A row with neither left nor right is rejected."""
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        MessageRow()
