"""Thin clients for external services shared across engines/tools, kept dependency-free
of any one engine so both a `LeadProvider` and a plain agent tool can use the same
client without an odd import direction between them."""
