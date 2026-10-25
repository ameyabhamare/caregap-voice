# 9. Callback Q&A: retrieval over each plan's benefits, with a deterministic grounding check

Date: 2026-10-23. Status: Accepted.

## Context
Members call back with questions about their plan. A wrong copay, another plan's
benefit or a medical opinion is worse than no answer, so the agent must answer only from
the caller's own plan's Summary of Benefits, hand everything else to a person, and its
hallucinations must be measured. It runs on a 3B local model by default.

## Decision
- **Documents.** One synthetic Summary of Benefits per customer, written as markdown and
  rendered to committed PDFs (deterministic bytes). The customer config names its
  document. Ingestion reads the PDF, as it would a real plan's document.
- **One benefit line per chunk** in pgvector (`nomic-embed-text` on Ollama, 768-d), each
  keeping its section title and list header, retrieved top-8 and always filtered to the
  caller's customer. Section-sized chunks let the model mix up amounts within one
  section (ER vs urgent care); line chunks made those detectable.
- **Pipeline:** route (model: benefit / medical / member / off_topic) -> retrieve
  (LangChain retriever) -> answer with chunk citations or `NOT_FOUND` (model) ->
  **grounding check without a model**: at least one citation, every citation was
  retrieved, every dollar amount and percentage is in a cited chunk, no other plan is
  named. Anything that fails, and every non-benefit question, gets the customer's
  hand-off wording and an escalation in the outbox (IDs only; the question stays in
  `callback_questions`). A failed check hands off rather than retrying the model.
- **LangChain is used for the parts it standardizes**: documents, the text splitter, the
  retriever and embeddings interfaces. Model calls stay on our metered `LLMClient` and
  `EmbeddingClient`, prompts stay in the MLflow registry. `langchain-postgres` is not
  used (it brings SQLAlchemy and its own tables). LangChain pulls in the `langsmith`
  client; tracing to it is off (never configured).
- **Eval** (`make eval-rag`): golden Q&A per customer (answerable, not in the document or
  about the other plan, medical/member/off-topic). Scores what the caller heard and the
  raw draft separately. Categories: fabricated benefit, wrong cost-share and wrong plan
  (found by which plan's document an amount comes from), missed refusal, over-refusal,
  incomplete. Report per customer in MLflow (`rag-evals`).
- **Prompt iteration** follows the Phase 8 rule early: one change per run, promote only
  on improvement. `callback-answer` v2 made both customers worse and was rolled back by
  moving the `production` alias to v1.

## Consequences
- The check prefers hand-offs: a correct amount cited to the wrong line is blocked. Most
  remaining failures are over-refusals, not wrong answers.
- Amount-level checks cannot see wrong statements without numbers ("you need to meet
  your deductible" when it does not apply). The Phase 8 judge has to cover those.
- Answers are text: inbound calls (speech in, speech out) are wired in Phase 11.
- Changing the embedding model to one of another size needs a schema change.
