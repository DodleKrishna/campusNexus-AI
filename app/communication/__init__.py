"""AgentOS V2 Phase 5: durable communication (jobs, controlled delivery connectors, voice).

``service`` turns COMMUNICATION_REQUESTED into exactly one ``CommunicationJob`` per Guardian follow-up and owns
every job transition; ``worker`` delivers due jobs through ``connectors`` under ``app.rules.communication_policy``;
``voice`` holds the Exotel provider, the stream token and the turn-based conversation pipeline. Contact details are
resolved only inside a connector (``contacts.ContactResolver``) and never leave it.
"""
