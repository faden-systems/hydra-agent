"""requirement 7 (Slack block normalization) and requirement 14 (attribution footer stripping), loops/b7.md."""
import os

import pytest

from conftest import B, S


# ----------------------------------------------------------------------------------------------- normalize_blocks

def test_rich_text_section_renders_mentions_links_and_text():
    blocks = [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
        {"type": "user", "user_id": "U1"}, {"type": "text", "text": " hello "},
        {"type": "link", "url": "https://x", "text": "there"}, {"type": "emoji", "name": "tada"}]}]}]
    text = B.normalize_blocks(blocks)
    assert "<@U1>" in text and "hello" in text and "there" in text and ":tada:" in text


def test_link_without_label_falls_back_to_url():
    blocks = [{"type": "rich_text", "elements": [{"type": "rich_text_section",
               "elements": [{"type": "link", "url": "https://example.test/x"}]}]}]
    assert "https://example.test/x" in B.normalize_blocks(blocks)


def test_section_and_context_blocks():
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "sect-text"}},
              {"type": "context", "elements": [{"type": "plain_text", "text": "ctx-text"}]}]
    text = B.normalize_blocks(blocks)
    assert "sect-text" in text and "ctx-text" in text


def test_malformed_and_unknown_elements_are_ignored_not_raised():
    blocks = [None, {"type": "unknown-block"}, {"type": "rich_text", "elements": [None, {"type": "unknown"}]},
              {"type": "section", "text": None}, {"type": "section", "text": {"type": "unknown", "text": "x"}}]
    assert B.normalize_blocks(blocks) == ""


def test_empty_blocks_and_none():
    assert B.normalize_blocks(None) == ""
    assert B.normalize_blocks([]) == ""


# ----------------------------------------------------------------------------------------------- strip_attribution_footer

def test_footer_stripped_with_newline_separator():
    text = "<@MANAGER> engine acc=codex\n*Sent using* <@APP>"
    assert B.strip_attribution_footer(text) == "<@MANAGER> engine acc=codex"


def test_footer_stripped_with_space_separator_and_label():
    text = "<@MANAGER> engine acc=codex *Sent using* <@APP|Claude>"
    assert B.strip_attribution_footer(text) == "<@MANAGER> engine acc=codex"


def test_footer_not_stripped_when_not_trailing():
    text = "<@MANAGER> engine acc=codex *Sent using* <@APP> trailing words"
    assert B.strip_attribution_footer(text) == text


def test_footer_not_stripped_inside_quote():
    text = "<@MANAGER> engine acc=codex\n> *Sent using* <@APP>"
    assert B.strip_attribution_footer(text) == text


def test_footer_not_stripped_inside_unclosed_fence():
    text = "<@MANAGER> engine acc=codex\n```\n*Sent using* <@APP>"
    assert B.strip_attribution_footer(text) == text


def test_footer_not_stripped_inside_closed_fence_is_still_interior_text():
    # a closed fence before the footer does not make the footer itself fenced
    text = "<@MANAGER> engine acc=codex\n```\ncode\n```\n*Sent using* <@APP>"
    assert B.strip_attribution_footer(text) == "<@MANAGER> engine acc=codex\n```\ncode\n```"


def test_footer_preserves_multiline_body():
    text = "<@MANAGER> engine acc=codex\nmodel=gpt6\n*Sent using* <@APP>"
    assert B.strip_attribution_footer(text) == "<@MANAGER> engine acc=codex\nmodel=gpt6"


def test_empty_and_none_text():
    assert B.strip_attribution_footer("") == ""
    assert B.strip_attribution_footer(None) is None


# ----------------------------------------------------------------------------------------------- integration through Bridge.handle_message

def test_blocks_fall_back_only_when_text_is_blank(home, poster):
    br = B.Bridge(home=home, allowlist={"U": {"instructs": True}}, poster=poster, token_env={}, bot_user_id="M")
    ev = {"channel": "C", "ts": "1", "user": "U", "text": "",
          "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "<@M> status"}}]}
    queued = br.handle_message(ev)
    assert queued and queued.get("command") == "status"
    ev2 = {"channel": "C", "ts": "2", "user": "U", "text": "plain text with no mention",
           "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "<@M> status"}}]}
    assert br.handle_message(ev2) is None, "nonblank top-level text is authoritative"


def test_footer_cleaned_copy_used_for_routing_but_mirror_may_keep_raw(home, poster):
    br = B.Bridge(home=home, allowlist={"U": {"instructs": True}}, poster=poster, token_env={}, bot_user_id="M")
    text = "<@M> engine acc=codex model=gpt6\n*Sent using* <@APP>"
    result = br.handle_message({"channel": "C", "ts": "1", "user": "U", "text": text})
    assert result and result.get("ok") and S.read_engine(home)["model"] == "gpt-6-astra"
    mirror = S.read_jsonl(os.path.join(home, "mirror", "C.jsonl"))[-1]
    assert "*Sent using*" in mirror["text"], "the raw mirror may retain the footer"


def test_footer_cannot_be_run_by_an_informational_sender(home, poster):
    br = B.Bridge(home=home, allowlist={"OP": {"instructs": False}}, poster=poster, token_env={}, bot_user_id="M")
    before = None
    text = "<@M> engine acc=codex model=gpt6\n*Sent using* <@APP>"
    result = br.handle_message({"channel": "C", "ts": "1", "user": "OP", "text": text})
    assert result and result.get("ok") is False
    assert S.read_engine(home)["acc"] != "codex"


def test_footer_does_not_confer_authority_over_sender_metadata(home, poster):
    br = B.Bridge(home=home, allowlist={"U": {"instructs": True}}, poster=poster, token_env={}, bot_user_id="M")
    # a stranger (not allowlisted) cannot gain authority merely because a footer names an allowlisted user
    result = br.handle_message({"channel": "C", "ts": "1", "user": "STRANGER",
                                "text": "<@M> engine acc=codex\n*Sent using* <@U>"})
    assert result is None
