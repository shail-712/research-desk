# I2 runbook — the remaining steps, in order

> **Status 2026-11-10:**
> - Done: 0, A (passed), B1, B4.
> - B2's change set was deleted, not executed. It had a third row, `HttpApi` Body (Dynamic, caused by
>   `ApiFunction.Arn`), and the safety rule allowed exactly 2.
> - Not run: B3, C, D and E. See PROGRESS I2.
> - The image `sha256:f92da023…` is already in ECR, so re-running B1–B2 shouldn't push a new one.

Written by the Builder on 2026-11-09 for you to run. Every step is a hand-off: you run it, paste the
result back, and the Builder records it in `docs/PROGRESS.md` (I2 section, which has placeholders).

| Step | What | Gemini calls | Writes to AWS |
|---|---|---|---|
| 0 | Session setup (variables + two helper functions) | 0 | no |
| A | Local Deep check of the brief's query (last prompt round) | ~12 | no |
| B | Redeploy: build, change set (you review), execute | 0 | **yes** |
| C | Cold start, then one warm Quick ask; CloudWatch reads | 2 | no (asks only) |
| D | The brief's Deep query on AWS | ~12 | no (asks only) |
| E | Eval subset, both modes (10 answers) | ~30–45 | no (asks only) |

Total: about 56–71 Gemini generation calls, plus a few query embeddings (a separate daily quota).

## Gemini quota plan

- **Order.** A comes first because it decides whether the prompt fix is accepted. D is I2's main check.
  E is last because it is the largest and it can be resumed.
- **Stop signals.** Any of these means the free tier is exhausted for now:
  - `429` or `RESOURCE_EXHAUSTED` in the output;
  - a job with `status: error` and "The AI model is busy or unavailable right now";
  - a Deep result whose `trace.fallback` says "unavailable";
  - eval lines that say `NO ANSWER`.
- **When you see one:** stop every Gemini step. Still do the steps that don't use Gemini: the redeploy (B),
  the CloudWatch reads (C2) and the image size (B4). The Builder updates the docs. The rest is done the
  next day.
- **The eval is resumable.** Re-running the same E command skips answers already saved in
  `evals/results/v2-api.json`. A partial eval is recorded as partial (`--note "PARTIAL: …"`), never
  reported as complete.

---

## 0. Session setup (run once per PowerShell window)

**What it does:**
- Sets the Region.
- Reads the API URL and both function names from the stack.
- Reads the access code from `.env` without printing it.
- Defines two helpers: `Ask-Desk` sends a question and waits for the answer; `Show-Trace` prints a
  one-screen summary of a Deep result.

```powershell
cd C:\Shail\Projects\research-desk
git checkout iter-2
$env:PYTHONIOENCODING = "utf-8"
$region = "ap-southeast-2"
$url    = aws cloudformation describe-stacks --stack-name research-desk --region $region --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text
$api    = aws cloudformation describe-stack-resource --stack-name research-desk --logical-resource-id ApiFunction --region $region --query StackResourceDetail.PhysicalResourceId --output text
$worker = aws cloudformation describe-stack-resource --stack-name research-desk --logical-resource-id AskWorkerFunction --region $region --query StackResourceDetail.PhysicalResourceId --output text
$code   = (Select-String -Path .env -Pattern '^APP_ACCESS_CODE=(.+)$').Matches[0].Groups[1].Value
$h      = @{ "x-access-code" = $code }
$brief  = 'Analyze NVIDIA''s Q3 earnings report. Compare revenue growth with AMD and Intel. Provide any information on top companies that are using NVIDIA GPUs (and revenue from them), competitive threats to NVIDIA. Summarize recent news sentiment and provide a risk assessment based on current market conditions.'

# POST /ask, poll every 3 s (up to 3 min), save the full result as UTF-8 JSON, return it.
function Ask-Desk($question, $mode, $outFile) {
  $body = @{ question = $question; mode = $mode } | ConvertTo-Json
  $job  = Invoke-RestMethod -Method Post -Uri "$url/ask" -Headers $h -ContentType "application/json" -Body $body
  $t0   = Get-Date
  do { Start-Sleep 3; $r = Invoke-RestMethod -Uri "$url/ask/$($job.job_id)" -Headers $h }
  while ($r.status -eq "pending" -and ((Get-Date) - $t0).TotalSeconds -lt 180)
  $r | ConvertTo-Json -Depth 20 | Out-File -Encoding utf8 $outFile
  Write-Host ("{0}   ~{1:N1} s from 202 to done   job {2}" -f $r.status, ((Get-Date) - $t0).TotalSeconds, $job.job_id)
  return $r
}

# One-screen summary of a Deep result (local or API).
function Show-Trace($r) {
  "total: $($r.timings.total) s   Gemini calls: $($r.timings.llm_calls)   fallback: $($r.trace.fallback)   partial: $($r.trace.partial)"
  $i = 0
  foreach ($round in $r.trace.rounds) { $i++
    foreach ($rep in $round) {
      $tools = ($rep.steps | Where-Object action -eq 'tool' | ForEach-Object { $_.tool }) -join ', '
      "round $i  {0,-8} {1,6} s  Gemini {2}  findings {3}  gaps {4}  tools: {5}" -f $rep.agent, $rep.seconds, $rep.llm_calls, @($rep.findings).Count, @($rep.gaps).Count, $tools
    } }
  "checker: $($r.trace.checker.supported)/$($r.trace.checker.numbers_checked) matched, unsupported $(@($r.trace.checker.unsupported).Count)"
}
"url: $url   api: $api   worker: $worker   code loaded: $([bool]$code)"
```

**Expected:** one line with the URL, two function names starting `research-desk-`, and `code loaded: True`.
**Paste back if it fails:** the error line.

---

## A. Local Deep check of the brief's query (~12 Gemini calls, no AWS)

**What it does:** runs the brief's query in Deep mode on this laptop, using the round-2 filings prompt
(`a0f0b37`). It saves the JSON and prints the summary. `Out-File -Encoding utf8` is there because a
plain `>` in PowerShell 5.1 writes UTF-16.

```powershell
.\.venv\Scripts\python.exe cli.py agent --deep $brief | Out-File -Encoding utf8 "$env:TEMP\deep-brief-local.json"
$local = Get-Content "$env:TEMP\deep-brief-local.json" -Raw | ConvertFrom-Json
Show-Trace $local
```

**Expected:** 15–60 s, `fallback:` empty, one round-1 line each for filings, news and market. Market shows
only `get_quote` calls. The filings searches are separate by topic, for example NVDA results and NVDA
customers.

**Pass criteria. The Builder checks them by reading the JSON file:**
1. A filings finding gives NVIDIA's latest-quarter revenue with its period (the index has "$96.2 billion,
   up 106%", Q2 FY2027).
2. A gap says the requested quarter isn't filed yet.
3. The customer-concentration finding is still there.
4. The final answer uses 1–3.

**Decision (yours, already made):** this is the last prompt round. If (1) is still missing, it goes into
DESIGN.md as a known limitation and we continue with B.

**Paste back:** the `Show-Trace` lines, or just "A done". If the output contains `429`, apply the quota plan.

---

## B. Redeploy (no Gemini; writes to AWS)

**Why:** the deployed functions still run the old prompts. The new image has `086e417` and `a0f0b37`.

### B1. Build the image

**What it does:** rebuilds the one container image (all functions share it). Docker Desktop must be running.
The Docker cache keeps it to a few minutes.

```powershell
& "C:\Program Files\Amazon\AWSSAMCLI\bin\sam.cmd" build
```

**Expected:** `Build Succeeded`. **Paste back if it fails:** the last 20 lines.

### B2. Create the change set (preview: nothing changes yet)

**What it does:**
- Pushes the new image to ECR and creates a change set for the stack.
- `--image-repository` points at the ECR stack's repository (D15). It does not use `--resolve-image-repos`.
- The parameters are passed again **with the stack's current values**. If they were left out,
  CloudFormation would fall back to the defaults: `AlertEmail` would go blank, which would delete the
  budget and the alarm-email subscription. The email is read from the stack, so it isn't typed or
  committed.

```powershell
$repo  = aws cloudformation describe-stacks --stack-name research-desk-ecr --region $region --query "Stacks[0].Outputs[?OutputKey=='ImageRepositoryUri'].OutputValue" --output text
$email = aws cloudformation describe-stacks --stack-name research-desk --region $region --query "Stacks[0].Parameters[?ParameterKey=='AlertEmail'].ParameterValue" --output text
& "C:\Program Files\Amazon\AWSSAMCLI\bin\sam.cmd" deploy --no-execute-changeset --image-repository $repo --parameter-overrides EnableWatchdog=false "AlertEmail=$email"

$cs = aws cloudformation list-change-sets --stack-name research-desk --region $region --query "Summaries[?ExecutionStatus=='AVAILABLE'].ChangeSetId | [-1]" --output text
aws cloudformation describe-change-set --change-set-name $cs --region $region --query "Changes[].ResourceChange.[Action,LogicalResourceId,ResourceType,Replacement]" --output table
aws cloudformation describe-change-set --change-set-name $cs --region $region --query "Parameters[].[ParameterKey,ParameterValue]" --output table
```

**Expected:** exactly two rows, both `Replacement: False`:

| Action | LogicalResourceId | ResourceType |
|---|---|---|
| Modify | ApiFunction | AWS::Lambda::Function |
| Modify | AskWorkerFunction | AWS::Lambda::Function |

The parameters table shows `EnableWatchdog = false` and `AlertEmail` = your address (not empty).

**Stop and paste back** if there is any `Add`, any `Remove`, any `Replacement: True`, any other
resource, or an empty `AlertEmail`. Otherwise paste the table and go on.

### B3. Execute (after your review)

**What it does:** applies the change set, then waits until the update finishes (about 1–2 minutes).
Both functions then run the new image. Lambda throws away the old containers, so the next call of each
function is a cold start, which step C measures.

```powershell
aws cloudformation execute-change-set --change-set-name $cs --region $region
aws cloudformation wait stack-update-complete --stack-name research-desk --region $region
aws cloudformation describe-stacks --stack-name research-desk --region $region --query "Stacks[0].StackStatus" --output text
```

**Expected:** `UPDATE_COMPLETE`. **Paste back if it fails:** `aws cloudformation describe-stack-events
--stack-name research-desk --region $region --max-items 8 --query "StackEvents[].[LogicalResourceId,ResourceStatus,ResourceStatusReason]" --output table`.

### B4. Pushed image size (read-only, for the cold-start notes)

```powershell
aws ecr describe-images --repository-name research-desk --region $region --query "sort_by(imageDetails,&imagePushedAt)[-1].[imagePushedAt,imageSizeInBytes]" --output text
```

**Expected:** today's timestamp and a size in bytes. ECR stores images compressed, so this is smaller
than the 1.27 GB local image. **Paste back:** the line.

---

## C. Cold start, then warm (2 Gemini calls)

**Do C1 right after B3, with nothing in between**, so the first ask hits fresh containers. The question
is the same one as the I1 cold-start baseline (demo query 2), so the numbers are comparable.

### C1. Two Quick asks: cold, then warm

**What it does:** asks once (both functions start cold), waits 20 s, then asks again (both are warm).
It prints each answer's own timings: the tools, Gemini, and the total inside the worker.

```powershell
$q2 = "What is AMD's market cap and P/E ratio, and the latest headlines?"
$cold = Ask-Desk $q2 quick "$env:TEMP\quick-cold.json";  $cold.timings | Format-List
Start-Sleep 20
$warm = Ask-Desk $q2 quick "$env:TEMP\quick-warm.json";  $warm.timings | Format-List
```

**Expected:** `done` twice. The cold one is slower: in I1 it took ~31.5 s from 202 to done. The warm one
should be close to the ~6 s you measured before.

### C2. Read the start-up times from CloudWatch (read-only, no Gemini)

**What it does:** prints the Lambda platform lines from the last 20 minutes for both functions.
- `REPORT` lines carry `Duration`, `Max Memory Used` and, on a cold start only, `Init Duration`.
- `INIT_REPORT` lines appear when start-up hits Lambda's 10 s init limit (what happened in I1).

```powershell
$since = [DateTimeOffset]::UtcNow.AddMinutes(-20).ToUnixTimeMilliseconds()
foreach ($fn in @($api, $worker)) {
  "== $fn"
  aws logs filter-log-events --log-group-name "/aws/lambda/$fn" --region $region --start-time $since --filter-pattern '?REPORT ?INIT_REPORT' --query "events[].message" --output text
}
```

**Expected:**
- ApiFunction: one `REPORT` with `Init Duration` (cold), then ones without it (warm).
- AskWorkerFunction: the same. One of two things will show up for the first ask:
  - **Normal:** `Init Duration: x ms` under 10,000.
  - **Repeat of I1:** an `INIT_REPORT … Status: timeout` line, then a long first `Duration`.

**About the 10 s timeout we saw in I1:**
- Before a function's first request, Lambda runs its "init" phase: it starts the container and imports
  our code (google-genai, pandas, numpy, yfinance, boto3), and our code reads the secrets from SSM.
- On-demand functions get at most 10 s for that phase. If init runs longer, Lambda stops it and runs init
  again inside the first request, where it counts against the 120 s timeout. The request still succeeds,
  only slower.
- The first start of a **newly pushed** image is the slowest, because Lambda has to fetch and cache the
  image's layers. Later cold starts of the same image are faster.
- So I1's timeout may be a one-off for a new image.
- **Fix only if it repeats:** if C2 shows `INIT_REPORT … timeout` again, the Builder proposes lazier
  imports (load pandas and yfinance only when a quote is fetched), so less work happens in init.

**Paste back:** the full C2 output, plus the two `timings` lists from C1.

---

## D. The brief's Deep query on AWS (~12 Gemini calls)

**What it does:** I2's main check. It sends the brief's query to the deployed API in Deep mode, saves the
result and prints the summary.

```powershell
$deep = Ask-Desk $brief deep "$env:TEMP\deep-brief-aws2.json"
Show-Trace $deep
"E# citations: $(([regex]::Matches($deep.answer, '\bE\d+\b')).Count)   sources: $(@($deep.sources).Count)"
```

**Expected:** `done`, `fallback:` empty, all three specialists in round 1, market with `get_quote` only,
and citations well above 0. The Builder reads `%TEMP%\deep-brief-aws2.json` to check the same four
points as in A and compares it with the first AWS run (17.57 s, 10 calls).

**Paste back:** the summary lines, or "D done".

---

## E. Eval subset, both modes (~30–45 Gemini calls, resumable)

**What it does:** runs 5 eval questions × 2 modes against the API and writes the "Deployed" section of
`docs/eval/v2-results.md`. The frozen scorer (D8) is not changed.

| ID | Question | Why |
|---|---|---|
| s1 | What is NVIDIA's stock price right now? | simplest market path |
| c1 | Compare NVIDIA and AMD revenue | two-company filings comparison |
| c4 | Compare NVIDIA's and AMD's data center segment revenue | demo query 3 |
| w1 | Why did AMD stock fall in the last session? | market + news, with planning |
| f3 | What are NVIDIA's main business segments and how did they perform? | pure filings; uses the latest-period fix |

```powershell
.\.venv\Scripts\python.exe -m evals.run --target api --url $url --only s1 c1 c4 f3 w1 --note "Subset: s1 c1 c4 f3 w1, both modes (10 answers), after the I2 filings fix."
```

**Expected:** 10 progress lines like `-> deep, 14.2 s, 6 calls`, none with `NO ANSWER`, then
`Report: docs\eval\v2-results.md`. About 3–6 minutes. The local run of the same 10 used 33 calls.

**If it stops partway (quota):**
1. Stop.
2. Record what's there as partial:
   ```powershell
   .\.venv\Scripts\python.exe -m evals.run --target api --url $url --only s1 c1 c4 f3 w1 --report-only --note "PARTIAL: <n> of 10 answers; the rest stopped by the Gemini free-tier quota on <date>."
   ```
3. The next day, re-run the main E command. It skips the saved answers and finishes the rest, and its
   `--note` replaces the partial one.

**Paste back:** the 10 progress lines, or "E done". The Builder reads the report and fills in PROGRESS.
