"""Verify pragmas survive the payload round-trip from server → durable queue → tasks."""

from __future__ import annotations

from unittest.mock import patch

from robomp.tasks import _attach_thread, _directive_from_payload
from robomp.worker import DirectiveInfo, ThreadMessage


def test_directive_from_payload_parses_pragmas() -> None:
    directive = _directive_from_payload(
        {
            "_robomp_directive": {
                "body": "do the thing",
                "author": "can1357",
                "pragmas": [["model", "gpt"], ["thinking", "low"]],
            }
        }
    )
    assert directive is not None
    assert directive.body == "do the thing"
    assert directive.author == "can1357"
    assert directive.pragmas == (("model", "gpt"), ("thinking", "low"))
    assert directive.authorizes_impl is False


def test_directive_from_payload_missing_pragmas_is_empty_tuple() -> None:
    directive = _directive_from_payload({"_robomp_directive": {"body": "x", "author": "can1357"}})
    assert directive is not None
    assert directive.pragmas == ()
    assert directive.authorizes_impl is False


def test_directive_from_payload_drops_malformed_pragma_entries() -> None:
    directive = _directive_from_payload(
        {
            "_robomp_directive": {
                "body": "x",
                "author": "can1357",
                "pragmas": [
                    ["model", "gpt"],
                    ["bad"],  # wrong arity
                    [1, "v"],  # non-string key
                    "string-instead-of-pair",
                ],
            }
        }
    )
    assert directive is not None
    assert directive.pragmas == (("model", "gpt"),)


def test_directive_from_payload_parses_implementation_authorization() -> None:
    directive = _directive_from_payload(
        {
            "_robomp_directive": {
                "body": "do the thing",
                "author": "can1357",
                "authorizes_impl": True,
            }
        }
    )
    assert directive is not None
    assert directive.authorizes_impl is True


def test_directive_from_payload_returns_none_for_missing_directive() -> None:
    assert _directive_from_payload({}) is None
    assert _directive_from_payload({"_robomp_directive": "not-a-mapping"}) is None


def test_attach_thread_preserves_authorizes_impl() -> None:
    """Test that _attach_thread preserves DirectiveInfo.authorizes_impl=True."""
    directive = DirectiveInfo(
        body="go", 
        author="maintainer", 
        authorizes_impl=True
    )
    mock_thread = ThreadMessage(
        kind="comment",
        author="test",
        body="test message",
        created_at="2026-06-09T00:00:00Z"
    )
    
    # Mock _fetch_thread to return our test thread
    with patch("robomp.tasks._fetch_thread", return_value=[mock_thread]):
        import asyncio
        from robomp.github_backend import GitHubBackend
        
        # Create a mock GitHubBackend since we're not actually using it
        github = None
        
        # Call _attach_thread directly
        result = asyncio.run(_attach_thread(github, directive, "repo", 123, is_pr=False))
    
    # Verify authorizes_impl is preserved and thread is attached
    assert result is not None
    assert result.authorizes_impl is True
    assert result.thread == [mock_thread]
    assert result.body == "go"
    assert result.author == "maintainer"
