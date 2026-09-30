---
layout: default
title: Keenable clients
---

# Keenable clients

**I see two paths:**

- **Agent distribution:** offer general-purpose search and monetize intent analytics.
- **B2B specialization:** sell search built for a specific workflow.

**My recommendation:**

- **Short term:** test demand for agent-search analytics.
- **Long term:** specialize. Large assistants may capture most user activity, limiting an independent provider's data business.

## 1. Agent distribution → intent analytics

**What works today:**

- **Distribution:** [Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search/) rotates unconfigured, keyless traffic across Exa, Parallel, Firecrawl and Keenable. [Ketch](https://github.com/1broseidon/ketch) includes Keenable as a fallback.
- **Adoption:** free access and easy integration help. I found no convincing evidence of these users switching for measured quality gains.
- **Quality:** Keenable's own [NEEDLE](https://keenableai.github.io/needle/) showed a 3.3-point overall lead over Exa Auto in my September 29 snapshot, with larger leads on some paper queries.

**What I would do:**

1. Help more agent developers integrate Keenable.
2. Test aggregated intent-data sales or a GEO analytics product.
3. Approach Attio, Runpod, Stripe, Vercel and Strapi, plus GEO tools and agencies. Contacts indicate category interest, not confirmed Keenable demand.

**What must hold:**

- **Advantage:** an owned data source with potentially unique insights.
- **Limits:** search queries do not reveal full conversations or purchases.
- **Economics:** check volume, permitted uses, buyer pricing and costs.

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
