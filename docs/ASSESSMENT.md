# AI Engineer Assessment: Capital Markets Investment Research Agent

> Source: `AI_Engineer_Assessment_New.pdf` (transcribed). Where this project deliberately deviates
> from the brief, the reason is in [`DECISIONS.md`](DECISIONS.md).

| | |
|---|---|
| **Duration** | 5–7 days to complete |
| **Presentation** | 15 min high-level solution explanation + 20-minute demo + 15-minute Q&A during interview |
| **Submission** | GitHub repository + README + Architecture diagram |

## Scenario: Investment Research Assistant

You are building an AI-powered Investment Research Agent for a financial services firm. Research
managers need to quickly analyze companies by combining real-time market data, financial news,
internal assets/reports, trade analyst reports, and SEC (or equivalent local) filings. Currently, this
manual research takes days per company. Your solution should reduce this to under 5 minutes.

**Example user query:**

> "Analyze NVIDIA's Q3 earnings report. Compare revenue growth with AMD and Intel. Provide any
> information on top companies that are using NVIDIA GPUs (and revenue from them), competitive
> threats to NVIDIA. Summarize recent news sentiment and provide a risk assessment based on
> current market conditions."

## What to build

Build an agentic AI solution using AWS services that can answer investment research questions by
orchestrating multiple data sources.

### Required components
1. **AI Agent (Core):** Build using Amazon Bedrock (Claude or similar) with tool orchestration
   capability. The agent should analyze queries and decide which tools to invoke.
2. **Market Data Tool:** Integrate with a free financial API (Yahoo Finance, Alpha Vantage, or
   similar) to fetch real-time stock prices, volume, and key metrics.
3. **News Search Tool:** Implement financial news retrieval from RSS feeds or news APIs. Extract
   and summarize relevant news for queried companies.
4. **Document Knowledge Base:** Create a simple vector store (using Bedrock Knowledge Base,
   OpenSearch, or FAISS) containing sample SEC filings or earnings reports for 3–5 companies.
5. **API Interface:** Expose the agent via API Gateway + Lambda. Include a simple frontend or
   Postman collection for demonstration.

### Technical requirements
- Use Python for backend implementation
- Deploy on AWS (Lambda, Bedrock, S3 minimum; additional services as needed)
- Infrastructure as Code using Terraform, CDK, or SAM
- Include error handling, logging, and basic observability
- Document your architecture decisions and trade-offs

## Deliverables
1. **GitHub Repository** with complete source code, IaC templates, and clear README with setup instructions
2. **Architecture Diagram** showing all AWS components, data flows, and external integrations
3. **Working Demo** that can process at least 3 different investment research queries during the interview
4. **Brief Document (1–2 pages)** explaining your design choices, challenges faced, and potential production improvements
