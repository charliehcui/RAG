# Adaptive Enterprise Knowledge Assistant

A selective Agentic RAG system for answering enterprise knowledge and operational questions with traceable evidence.

The system keeps simple questions fast and inexpensive. It uses a bounded agent only when a question requires multiple sources, several reasoning steps, or recovery from failed retrieval.

## Problem

Knowledge inside a B2B SaaS company is spread across product documentation, API guides, support procedures, policies, release notes, incident reports, business reports, and structured operational data.

A fixed RAG pipeline is effective for direct document questions, but it is less reliable when a request requires both documents and database records. Running an agent for every request can handle more complex cases, but it adds unnecessary delay and model cost to simple questions.

This project uses a selective approach: start with the simplest suitable path, and involve the agent only when it can materially improve the answer.

## How It Works

```text
User question
      |
      v
Question router
      |
      +--> Document fast path --> Evidence check --+
      |                                             |
      +--> Read-only SQL fast path --> Result check +--> Answer with sources
      |                                             |
      +--> Bounded agent --------------------------+
                 |
                 +--> combine document and SQL evidence
                 +--> retry or change strategy after failure
                 +--> ask for clarification when needed
                 +--> stop when evidence is insufficient
```

## Core Capabilities

- Answers direct document questions through a fast retrieval path.
- Answers operational data questions through a read-only PostgreSQL path.
- Uses a LangGraph agent for multi-source questions, incomplete evidence, and controlled recovery.
- Searches documents with dense retrieval, BM25 keyword retrieval, Reciprocal Rank Fusion, and cross-encoder reranking.
- Stores and searches document chunks in Milvus Standalone.
- Returns source references, document versions, effective dates, and access-scope metadata with supported answers.
- Avoids unsupported enterprise claims and clearly states when the available evidence is not enough.
- Limits agent decisions, retries, tool calls, and SQL execution to keep behavior predictable.

## When the Agent Is Used

The agent is reserved for questions such as:

- requests that combine policy documents with current operational data;
- questions that need several facts gathered in sequence;
- retrieval results that are conflicting, outdated, or incomplete;
- failed searches that may succeed after a controlled query rewrite or source switch;
- ambiguous requests that require clarification before answering.

Direct questions remain on the document or SQL fast path whenever possible.

## Knowledge Sources

The document collection represents common enterprise knowledge found in a SaaS company:

- product guides and feature documentation;
- API references and integration guides;
- support playbooks and troubleshooting procedures;
- pricing, security, access, refund, and escalation policies;
- release notes, deprecation notices, and migration guides;
- incident reports and postmortems;
- business reports and customer-facing operational documents.

Structured data represents read-only operational records such as customers, plans, subscriptions, usage, support tickets, incidents, and service metrics.

## Evidence and Safety Rules

- Enterprise-specific claims must be supported by retrieved document text or approved SQL results.
- Document access scope is filtered before retrieved text reaches the language model.
- SQL access is read-only and restricted to an approved schema.
- Current versions and effective dates take priority over obsolete information.
- Conflicting evidence is surfaced instead of silently merged.
- The system may ask a clarifying question or refuse to guess when evidence is insufficient.

## Evaluation

The same question set is used to compare three approaches:

1. fixed one-pass RAG;
2. an agent used for every question;
3. the selective agent approach used by this project.

Evaluation covers:

- answer correctness;
- source and citation correctness;
- route and tool selection;
- recovery from failed retrieval;
- appropriate refusal when evidence is missing;
- model calls, tool calls, latency, token use, and estimated cost.

This comparison tests whether selective agent use improves difficult cases without imposing agent overhead on every request.

## Example Questions

- What does the current refund policy say, and which source supports the answer?
- Which API version should a customer use after the latest deprecation notice?
- Which enterprise customers were affected by a specific incident, and what response does the support playbook require?
- Why did ticket volume rise for one product area, based on both the incident record and the relevant documentation?

## Technology

- Python
- LangGraph and LangChain
- Milvus Standalone
- PostgreSQL
- dense embeddings and BM25 retrieval
- Reciprocal Rank Fusion and cross-encoder reranking
- command-line interface

## Scope Boundaries

The project focuses on the agent's decision-making, evidence use, recovery behavior, and measurable value. It intentionally excludes a full web interface, production authentication, microservice deployment, multi-agent coordination, database write operations, and model fine-tuning.

## Data Notice

The enterprise corpus and evaluation data are synthetic and kept outside the public repository. They do not contain information from a real company.

## License

This project is licensed under the terms in [LICENSE](LICENSE).
