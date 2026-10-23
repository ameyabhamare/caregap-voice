# 8. Customer configs: one validated YAML file per health plan

Date: 2026-10-23. Status: Accepted.

## Context
The system serves more than one health plan. Plans differ in who their members are,
which care gaps they call about, required wording, languages, voices, calling hours,
escalation rules and how much member data may reach a model. The brief's test: a second
customer runs end to end with zero code changes.

## Decision
- **One YAML file per customer** in `customers/`, validated by a pydantic model
  (`caregap.customers.CustomerConfig`, unknown keys rejected). It holds:
  - the member rule (age on December 31 of the measurement year);
  - measures in priority order, word limit and gaps per script;
  - allowed member fields;
  - per-language wording (disclaimer, opt-out, spoken confirmations, how each measure is
    described), voice, telephony language and prompt names;
  - the call window and retry policy, first-N approval;
  - DTMF keys and whether failed deliveries alert a person.
- **Validation catches config mistakes before a call is placed.** Per file: measures the
  pipeline computes, member fields that exist, a Kokoro voice that speaks the language,
  descriptions for exactly the configured measures, a disclaimer that names the plan, an
  opt-out sentence that names the opt-out key, distinct keys. Across files: unique IDs
  matching file names, no member in two customers (age ranges must not overlap), every
  referenced prompt exists and uses only the variables the customer allows, the plan name
  has a pronunciation entry. The core service validates at startup;
  `make validate-config` runs the same checks, and a unit test runs them in CI.
- **Code sees a per-language view** (`OutreachProfile`), so generation and compliance did
  not change shape. Campaigns and scripts record their customer; scripts record their
  language.
- **Capabilities in code, choices in config.** Spanish (prompts, compliance term lists,
  Spanish TTS) and the no-name greeting are platform capabilities with their own tests.
  A config switches them on. That is what keeps adding a customer config-only.
- **Prompt files are part of the platform**, not of a customer. A customer picks prompts
  by name; validation checks each prompt only uses variables the customer allows, so a
  customer that allows no member fields cannot be given a prompt that needs a name.
- Members whose language a customer does not support get the customer's default
  language. Membership is by age: Sunrise (Medicare Advantage) 65+, Horizon (ACA
  Marketplace) under 65.

## Consequences
- A new customer is a YAML file plus, if it needs one, a new capability (a language, a
  measure) added to the platform first with tests.
- The age rule stands in for an eligibility feed; a real deployment reads plan enrollment.
- Spanish wording and term lists were written at dev time and have not been reviewed by a
  native speaker. English drug and diagnosis terms are also checked in Spanish scripts.
- Kokoro speaks English plan names in Spanish scripts with Spanish phonemes; the
  pronunciation overrides are English-only.
