---
layout: default
title: Two directions for Keenable
---

# Two directions for Keenable

**I see two paths: distribute search through general-purpose agents, or sell specialized search to B2B products.** Short term, I would test demand for agent-search analytics. Long term, I prefer specialization: large assistants may capture most user activity, leaving independent search providers with a smaller data business.

## 1. Agent distribution → intent analytics

**Free access and easy integration already bring distribution.** [Hermes](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search/) rotates unconfigured, keyless traffic across Exa, Parallel, Firecrawl and Keenable; [Ketch](https://github.com/1broseidon/ketch) includes Keenable as a fallback. This demonstrates access to traffic, not a preference based on superior search quality.

I have not found convincing evidence that these users switched after measuring better results. Keenable's own [NEEDLE](https://keenableai.github.io/needle/) shows an advantage, but it varies by task: my September 29 snapshot had a 3.3-point overall lead over Exa Auto and much larger leads on some scientific-paper queries.

1. **Expand distribution:** help agent developers integrate Keenable as a search provider.
2. **Test monetization:** sell aggregated intent data to analytics providers, or build a GEO analytics product showing what users seek and which tools surface.
3. **Talk to potential buyers:** Attio, Runpod, Stripe, Vercel and Strapi. Through industry contacts, I have indications of interest in this category, not confirmed demand for Keenable's dataset.

**The advantage is an owned data source.** GEO tools and agencies are another potential customer group, provided the data reveals something their existing analytics cannot.

Search queries alone do not reveal full conversations or purchases. Before scaling, validate usable volume, permitted uses and buyer pricing against serving and processing costs.

## 2. Specialized search → paid B2B API

**I would choose one domain and solve an existing customer's problem.** Each workflow needs its own coverage, freshness and benchmarks; a general search score does not establish business value.

- **Coding:** version-specific documentation for code reviews.
- **People and companies:** current roles, projects and identity matching for enrichment and outreach.
- **E-commerce:** product matching, prices, availability and reviews across merchants. I have worked on this need firsthand.
- **Legal and scientific research:** relevant rulings, regulations and papers with traceable sources.

I tested CodeRabbit because [it already uses Exa](https://exa.ai/customers/coderabbit) and has public evaluation references. My [published pilots](https://github.com/Alpus/keenable-search-eval#results) do not establish a clear Keenable advantage.

**Next, I would investigate Clay and e-commerce.** [Clay already combines external data providers and research agents](https://www.clay.com/claygent), so an additional data source or search API is a plausible fit.

1. Find a design partner that needs this infrastructure inside its product.
2. Collect failed tasks and agree on a business-linked metric, such as verified enrichment coverage or correct product matches at a fixed cost.
3. Demonstrate an improvement over its current solution before committing to the specialized product.
