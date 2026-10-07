"""AgentOS V2 Phase 5: voice delivery through Exotel.

``exotel`` -- configuration, the call-placing client interface (real HTTP client + test doubles) and the voice
``DeliveryConnector``; ``callbacks`` -- the provider's call-status callbacks; ``conversation`` -- the bounded,
turn-based conversation behind the WebSocket stream; ``tokens`` -- signed, expiring callback/stream tokens.
Nothing here stores audio, a transcript or a phone number; a ``VoiceSession`` keeps counters and an outcome code.
"""
