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

- **General search:** I expect most agent-search traffic to go through ChatGPT, Claude, maybe Muse. Getting integrated depends more on relationships than product quality; without those deals, Keenable risks staying an open-source niche.
- **Domain-specific search:** I see a more defensible and potentially larger business.

## 1. Agent distribution → intent analytics

**What works today:**

- **Already integrated:** [Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search/), [Lightpanda](https://lightpanda.io/docs/usage/agent) and [Ketch](https://github.com/1broseidon/ketch) include Keenable in their keyless search rotation or fallback paths.
- **Why they choose it:** I found no evidence of switching for better quality. Free queries and integration help seem more likely reasons to add another provider.
- **Quality:** [NEEDLE](https://keenableai.github.io/needle/) shows a modest overall lead, but it is Keenable's own benchmark with specific task selection.

**What I would do:**

1. **Test demand:** approach Attio, Runpod, Stripe, Vercel and Strapi, where personal contacts indirectly confirm a need for this type of data. Also approach GEO tools and agencies. Pitch: “I have agent-search data. Would it be useful to you?”
2. **In parallel, expand distribution:** target existing search defaults: [OpenClaw](https://docs.openclaw.ai/tools/web) (built-in provider selection), [AutoGPT](https://github.com/Significant-Gravitas/AutoGPT/blob/master/autogpt_platform/backend/backend/copilot/tools/web_search.py) (Sonar in AutoPilot), [Dify](https://marketplace.dify.ai/templates) (Tavily/Exa in research templates) and [Langflow](https://docs.langflow.org/web-search) (DuckDuckGo in its Web Search component). Pitch: “Keenable is already in Hermes. Can I help make it a free search option in your default setup or templates?” OpenClaw, Dify and Langflow already have optional Keenable integrations; the goal is default placement.

**Pro:**

- Proprietary data: a moat for analytics.
- Hermes is already a good starting point.

**Con:**

- Without distribution through ChatGPT or Claude, this may stay a small data business serving open-source users.

## 2. Specialized search → paid B2B API

**I would choose one domain.** Each workflow needs its own coverage, freshness and benchmarks.

- **Coding:** version-specific documentation for code reviews.
- **People and companies:** roles and identity matching for enrichment and outreach.
- **E-commerce:** product matching and competitor prices, a need I have worked on firsthand.
- **Legal and science:** rulings, regulations and papers with traceable sources.

**Where I started:**

- **CodeRabbit:** I started here because it has clear benchmarks and the hypothesis is relatively easy to test in a day. Search is secondary here, mostly a documentation lookup. It [already uses Exa](https://exa.ai/customers/coderabbit). My [published evals](https://github.com/Alpus/keenable-search-eval#results) do not establish a clear Keenable advantage.
- **Clay and e-commerce:** my next hypotheses. [Clay combines external providers and research agents](https://www.clay.com/claygent), so another data source could fit.

**How I would validate it:**

1. Find a design partner that needs search inside its product.
2. Collect failed tasks. Agree on a metric: enrichment coverage or correct product matches at a fixed cost.
3. Beat its current solution before committing to the specialized product.
