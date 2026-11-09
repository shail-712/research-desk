# DESIGN — Research Desk

> **Draft.** Started in I2 to hold notes as they come up. The full 1–2 page version (design choices D1–D17,
> challenges, production improvements, everything cut in D13) is written in I5.

## Challenges and known limitations

### Deep mode: the asked-for period may not be filed yet (I2)
- **What happened.** The brief's query asks for "NVIDIA's Q3 earnings report". NVIDIA's fiscal year runs
  about a month behind the calendar quarter, so on 2026-11-09 its fiscal Q3 FY2027 wasn't reported yet.
  The newest NVIDIA filing in the index was the 10-Q for the period ending 2026-07-26 (Q2 FY2027).
- **First deployed run (AWS, 17.6 s, 10 Gemini calls).** The filings specialist retrieved that 10-Q's MD&A
  and its "Concentration of Revenue and Customers" note. It dropped both because they weren't "Q3". The
  writer only sees evidence that the specialists' findings cite, so the answer said NVIDIA's results and
  customers were "not found", even though the data was in the index.
- **Fix, round 1 (`086e417`).** A general rule in the filings prompt:
  - if the asked period isn't in the evidence, report the latest period found, named with its period and
    filing date;
  - add a gap saying the asked period isn't filed yet;
  - keep evidence that answers part of the question even when it comes from another period.

  Result (local): the "not filed yet" gap and the customer-concentration finding reached the answer. The
  latest-quarter revenue didn't, because one mixed query ("revenue … customers concentration") missed the
  passage that has it.
- **Fix, round 2 (`a0f0b37`, the last round).**
  - One company and one topic per search query.
  - Independent searches go out together in one step, so they fit in the specialist's 3 tool steps.
  - If the period is missing, search again for the company's latest results without naming a period.
- **Outcome: fixed locally (2026-11-10).** The filings specialist sent its searches in parallel, one topic
  each, and found the latest results. The answer now opens: "NVIDIA has not yet filed its Q3 earnings report;
  the latest available data is for the fiscal second quarter ended July 26, 2026", and compares "Q2 FY27
  revenue was $96.2B, up 106%" with AMD's and Intel's Q3. It keeps the 16% customer-concentration figure.
  - The cost went up: 15 Gemini calls and 57 s, against 10 calls and 17.6 s before. One Gemini 503 accounts
    for 14.9 s of that.
- **On AWS (2026-11-09 19:10 UTC, same image): partly.** The answer says Q3 isn't filed, names Q2 FY2027 as
  the latest period, and keeps the 16% concentration. It took 13 calls and 15.8 s. **But it left out the
  $96.2B / +106% revenue.** The specialist ignored "one topic per query" for NVIDIA, re-ran a mixed query, then
  called `latest_filing`, which gave the period but not the revenue passage.
- **Known limitation (accepted after the last prompt round).** When the asked period isn't filed, the answer
  reliably says so, but the latest period's figures are reported only some of the time, because the model
  follows the search rule unevenly. The production improvement below (rewriting the search in code from the
  filing metadata) would make it deterministic.
- **Production improvement.** Use the filing metadata (form, period, filing date) in the code: when the asked
  period is newer than the latest filed one, the code itself rewrites the search to "latest results". That
  replaces relying on the prompt.

Also from I2: the market specialist used to call the batch quote tool (price and day change only) before the
single-quote tool (market cap, P/E). That cost ~6 s and one Gemini call on the slowest specialist. The tool
description and the market prompt now send valuation questions straight to `get_quote`.

### Cold starts and Gemini overload
- **Cold starts.** The image is 1.27 GB (about 301 MB compressed in ECR).
  - On the first start of a newly pushed image (I1, 2026-11-07), the worker's init hit Lambda's 10 s limit.
    Lambda then re-ran init inside the request, which still answered, about 10 s later.
  - Later cold starts of the same image took 1.4–1.6 s (API) and 2.6–2.9 s (worker), with no timeout (11-09
    logs).
  - After the I2 redeploy (new image, 2026-11-09): API init 1.98 s, worker init 3.76 s, still no timeout.
    So the I1 timeout was a one-off, and no fix was made.
  - Production improvements: lazier imports (pandas and yfinance only when a quote is fetched), a smaller
    image, or one warm-up call after each deploy.
- **Gemini overload.** On the free tier, a model can return 503 or 504 under load. The fallback chain moves to
  the next model, but the wait counts. One Quick job took 39.1 s: a 503, then a 504 after 29.4 s, then the third
  model answered. Locally, one 503 added 14.9 s to a Deep answer.
  - Production improvements: a paid tier, or shorter per-model timeouts before falling back.

### Keyword routing misses open-ended company questions
- **What.** Quick mode chooses its tools with whole-word keyword rules (`core/agent.py`, `classify_query`):
  - market words → quote;
  - news words → headlines;
  - filing words, or nothing matched → document search.

  "Tell me about Intel" matches no market or news word, so it gets **filings search only**: no live quote
  and no headlines. In Deep mode the same question (one tool, one company, no "why"-type word) takes the
  fast path with only the filings specialist.
- **Why it's like this.** Routing with keywords costs no Gemini call and takes ~0 ms (D5), and the eval's
  routing score is measured on it.
- **Production improvement.** When a company is named but no intent word matches, use all three tools.
  Or route with a small classifier call, at the cost of one extra request per question.
