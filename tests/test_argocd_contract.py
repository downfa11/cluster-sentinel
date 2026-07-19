import json

from sentinel.integrations.argocd import ArgoCdClient


def test_argocd_log_stream_decodes_content_and_discards_terminal_marker() -> None:
    stream = "\n".join(
        [
            json.dumps({"result": {"content": "first line\n", "last": False}}),
            json.dumps({"result": {"content": "second line\n", "last": False}}),
            json.dumps({"result": {"content": "", "last": True}}),
        ]
    )

    assert ArgoCdClient._decode_log_stream(stream) == "first line\nsecond line\n"


def test_argocd_log_stream_preserves_unexpected_non_stream_response() -> None:
    raw = "plain log response"

    assert ArgoCdClient._decode_log_stream(raw) == raw
