"""Pluggable market data sources for the KAI agent.

Currently includes:
- ``coinbase``: Coinbase Advanced Trade REST + WebSocket client
  (adapted from vpn-stack/workspace/coinbase-candles)

Future additions will live here as sibling modules. The intent is
a simple abstraction so the agent can pick an exchange per query
without rewriting the crypto tools.
"""
