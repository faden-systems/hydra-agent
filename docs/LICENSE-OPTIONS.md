# License options for hydra-agents

Goal stated by the founder: free for startups and small-to-medium companies; large companies (the Google / Meta tier)
should pay if they use it. This is not legal advice; a lawyer should read the chosen text before it is published.

## Why not plain MIT
MIT is free for everyone with no strings. It maximizes adoption and contribution and gives Google or Meta the right to
use, modify and ship hydra-agents without paying or talking to you. If revenue from large users is a goal, MIT is the
wrong tool.

## The pattern that matches the goal: source-available with a size-based grant, commercial license above it
Three well-used texts do this; the difference is where the line sits and what happens later.

1. **Business Source License 1.1 (BSL)** with an Additional Use Grant. The licensor writes the grant: for example,
   "production use is permitted for any entity whose annual gross revenue is under $50M and which is not a subsidiary
   of an entity above that threshold"; everyone else needs a commercial license. BSL also sets a Change Date (up to
   four years) after which the code becomes fully open source under a license you name (Apache-2.0 is common). Used by
   MariaDB, HashiCorp (2023), CockroachDB, Sentry before FSL. Strength: the threshold is yours to write. Weakness: not
   OSI open source, so some companies' policies block it; contributions need a CLA so you keep the right to relicense.
2. **PolyForm Small Business 1.0.0**: free for businesses with fewer than 100 employees and under $1M revenue, no
   commercial use above that. Cleanly written, but the threshold is fixed and much lower than "small to medium", so it
   would charge companies you want to keep free.
3. **Elastic License 2.0**: free for everyone except providing the software as a hosted or managed service and
   circumventing license keys. Targets cloud providers rather than company size; a large company using it internally
   pays nothing. Does not match the goal.
4. **Dual licensing, AGPL-3.0 + commercial**: fully open source (OSI), but any company that modifies and runs it as a
   network service must publish their changes, which large companies usually refuse, so they buy the commercial
   license. Startups and SMBs can use AGPL freely if they accept its terms; many are wary of it. Matches the goal by
   pressure rather than by rule, and needs a CLA.

## Recommendation
BSL 1.1 with an Additional Use Grant written for the goal, a Change Date of four years, and Apache-2.0 as the Change
License. A draft grant:

> Additional Use Grant: You may make production use of the Licensed Work, provided that (a) the annual gross revenue
> of you and your affiliates for the most recently completed fiscal year did not exceed US$100,000,000, and (b) the
> Licensed Work is not offered to third parties as a hosted or managed service that provides the substantial
> functionality of the Licensed Work. Any other production use requires a commercial license from the Licensor.

The revenue line is the founder's call; $100M keeps every startup and mid-size company free and catches the tier that
was asked for. Pair it with:
- a `CONTRIBUTING.md` with a contributor license agreement (or the simpler Developer Certificate of Origin plus an
  explicit relicensing grant), so contributions can be relicensed at the Change Date and sold under the commercial
  license;
- a one-page `COMMERCIAL.md` saying how to buy the commercial license;
- the standard BSL header in every source file.

## What each choice costs
- BSL: some large companies will not evaluate it at all; GitHub will not show a license badge; a few open-source
  purists will decline to contribute. In exchange, the revenue goal is enforceable by rule.
- AGPL + commercial: maximum openness and legitimacy, but SMB adoption suffers from AGPL's reputation, and enforcement
  is about network use, not company size.
- MIT: maximum adoption, zero leverage.

## Decision needed
1. The revenue threshold (draft: US$100M).
2. Change Date (draft: four years from each release) and Change License (draft: Apache-2.0).
3. Whether hosting the software for others is excluded from the free grant (draft: yes).
