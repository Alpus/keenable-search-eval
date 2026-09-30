---
layout: default
title: Keenable clients
---

# Keenable clients

**Two paths:**

- **General search for agents.** Offer general-purpose search. Distribute it as a free option in agents like Hermes. Monetize data analytics.
- **Domain-specific search.** Build search for a specific workflow.

**My recommendation:**

- **Short term:** turn agent search queries into analytics. Sell these insights to companies improving their visibility in AI answers (GEO).
- **Long term:** choose a niche with a clear unmet need, such as e-commerce or sales. Adapt the product to its workflows.

**Why:**

- **General search:** I expect ChatGPT-scale distribution to depend more on relationships than product quality. Without those deals, Keenable risks staying an open-source niche.
- **Domain-specific search:** I see a more defensible and potentially larger business.

## 1. Agent distribution → intent analytics

**What works today:**

- **Already integrated:** [Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search/), [Lightpanda](https://lightpanda.io/docs/usage/agent) and [Ketch](https://github.com/1broseidon/ketch) include Keenable in their keyless search rotation or fallback paths.
- **Why they choose it:** I found no evidence of switching for better quality. Free queries and integration help seem more likely reasons to add another provider.
- **Quality:** [NEEDLE](https://keenableai.github.io/needle/) shows a modest overall lead, but it is Keenable's own benchmark with specific task selection.

**What I would do:**

1. **Test demand:** approach Attio, Runpod, Stripe, Vercel and Strapi, plus GEO tools and agencies. “I have agent-search data. Would it be useful to you?” Contacts indicate interest in the category, not confirmed demand for this dataset.
2. **In parallel, expand distribution:** approach agent teams and platforms, from larger communities to smaller ones: [OpenClaw](https://github.com/openclaw/openclaw), [AutoGPT](https://github.com/Significant-Gravitas/AutoGPT), [Dify](https://github.com/langgenius/dify), [Langflow](https://github.com/langflow-ai/langflow), [OpenHands](https://github.com/OpenHands/OpenHands), [Cline](https://github.com/cline/cline), [Goose](https://github.com/aaif-goose/goose), [Vane](https://github.com/ItzCrazyKns/Vane), [GPT Researcher](https://github.com/assafelovic/gpt-researcher) and [Letta](https://github.com/letta-ai/letta). Pitch: “Keenable is already in Hermes. Can I help you integrate it with free search queries?” These are outreach candidates; check existing integrations first.

**Pros and cons:**

- **Pro:** proprietary usage data could become a moat for analytics. Existing integrations provide a starting point.
- **Con:** without distribution through ChatGPT or Claude, this may stay a small data business serving open-source users. I am not convinced that is attractive long term.

## 2. Specialized search → paid B2B API

**I would choose one domain.** Each workflow needs its own coverage, freshness and benchmarks.

- **Coding:** version-specific documentation for code reviews.
- **People and companies:** roles and identity matching for enrichment and outreach.
- **E-commerce:** product matching and competitor prices, a need I have worked on firsthand.
- **Legal and science:** rulings, regulations and papers with traceable sources.

**Where I would start:**

- **CodeRabbit:** [already uses Exa](https://exa.ai/customers/coderabbit) and publishes benchmark results. My [published pilots](https://github.com/Alpus/keenable-search-eval#results) do not establish a clear Keenable advantage.
- **Clay and e-commerce:** my next hypotheses. [Clay combines external providers and research agents](https://www.clay.com/claygent), so another data source could fit.

**How I would validate it:**

1. Find a design partner that needs search inside its product.
2. Collect failed tasks. Agree on a metric: enrichment coverage or correct product matches at a fixed cost.
3. Beat its current solution before committing to the specialized product.
