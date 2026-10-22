# 1. Record architecture decisions

Date: 2026-10-22. Status: Accepted.

## Context
This is a portfolio project meant to show production judgment. Reviewers need to see
why each tool exists, not just that it does.

## Decision
Every key tool choice gets a short ADR in `docs/adr/`, numbered sequentially, with
Context, Decision and Consequences. Superseded ADRs stay in place and are marked as such.

## Consequences
Small writing overhead per decision. Phase 12 assembles the full set (n8n, MCP,
Databricks, local-first cost, TTS strategy, PHI and the Twilio/R2 trust boundary).
