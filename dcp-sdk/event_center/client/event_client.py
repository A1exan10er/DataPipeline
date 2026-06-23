"""Compatibility wrapper for the shared DCS event SDK."""

from dcs_sdk.events import EventClient, emit_event, get_event_client

__all__ = ["EventClient", "emit_event", "get_event_client"]
