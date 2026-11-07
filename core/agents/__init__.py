"""
Multi-agent research (Deep mode, SPEC F6 and §6). Ported from v1 core/agents (73b6fda).

    supervisor.plan -> specialists in parallel (market / news / filings, each a
    Gemini function-calling loop over core/ tools) -> optional follow-up round
    -> supervisor.write (cites evidence IDs) -> checker.verify (fact-checks numbers)

Entry point: core.agents.deep.answer_deep(question). Every tool result lands on a
shared EvidenceBoard with an ID (E1, E2, ...) that agents and the answer cite.
"""
