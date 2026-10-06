#Tether

A model agnostic runtime harness for orchestrating secure tool execution, state management, and human in the loop approvals.

---

High level vision

Most people build chatbots.
Tether is the runtime that those chatbots run on.

Instead of hard wiring prompts, tools, and business logic into a single app, Tether provides a stable, model agnostic runtime that:

- Plans and executes multi step tasks through an explicit execution loop
- Orchestrates tool calls through a centralized tool registry
- Enforces fine grained permissions on every action
- Routes high impact steps through human in the loop checkpoints

The core idea:

Models are swappable. The runtime is the product.

Tether treats LLMs as interchangeable reasoning engines plugged into a consistent harness. Tools, permissions, and workflows live in the runtime, not inside a single prompt or notebook.

This is infrastructure, not an app.

---

Key architectural pillars

1. Tool Registry

The Tool Registry is the contract between the model and the outside world.
It provides a single place to define, validate, and manage all capabilities the runtime can call.

Responsibilities:
- Registration
  - Each tool declares a name, description, input schema, output schema, and permission requirements
  - Tools are registered once and discoverable by the runtime and by models via structured metadata
- Validation
  - Inputs and outputs are validated with Pydantic models
  - Invalid payloads are rejected before they ever hit external systems
- Introspection
  - The registry can expose a machine readable manifest for the model (e.g. function calling, tool use APIs)
  - This keeps the prompt surface small and consistent while the underlying tools can evolve
- Extensibility
  - New tools can be added without touching the core loop
  - Existing tools can be versioned and deprecated without breaking the runtime

Examples of tools:
- RAG search over a document store (your existing project becomes a first class tool)
- External APIs (CRMs, ticketing systems, internal services)
- Side effectful operations (creating issues, sending emails, writing to a database)

The registry is how Tether turns a single agent into an extensible system.

2. Execution Loop

The Execution Loop is the core runtime that coordinates the model, tools, state, and humans.

High level flow:
1. Receive a task (HTTP request, message, job)
2. Initialize or hydrate a Session State object
3. Call the model with the current state, tool manifest, and instructions
4. Interpret the model output as one of:
   - Final answer
   - Tool call proposal
   - Clarification question
   - Plan update (next steps)
5. If a tool call is proposed:
   - Check permissions
   - Optionally require human approval
   - Execute the tool through the registry
   - Append results to state
6. Loop until:
   - The model emits a final answer
   - A max step / timeout is reached
   - A human aborts or overrides

Core properties:
- Explicit state: All context (history, tools used, intermediate results, approvals) lives in a structured state object, not just in raw chat history
- Deterministic control: The runtime owns the loop and termination conditions; the model only proposes actions
- Inspectable: Every step is logged, typed, and traceable for debugging and evaluation

LangGraph is used to model this loop as a graph of nodes (model, tools, approval, routing) with explicit edges and conditions, instead of ad hoc while loops.

3. Permission Engine

The Permission Engine is the gatekeeper for every tool call.

Responsibilities:
- Policy evaluation
  - Check whether a given user, role, or environment is allowed to invoke a tool
  - Enforce constraints like rate limits, data access scopes, cost budgets, and allowed side effects
- Context aware checks
  - Policies can depend on the current state
  - Example: A tool that modifies production data only allowed if the task is tagged as ops_approved and the user has admin role
- Least privilege
  - Tools declare the minimum permissions they require
  - Default stance is deny unless explicitly allowed
- Auditability
  - Every allowed / denied decision is logged for post mortem analysis

This is what separates a demo agent from something you can put in front of real users and real systems.

4. Human in the loop

Tether is built with human control as a first class concept, not an afterthought.

Types of checkpoints:
- Pre execution approvals
  - Before calling sensitive tools (e.g. write operations, financial actions), the runtime pauses and requests explicit human approval
- Inline review
  - Humans can inspect intermediate state, override decisions, or inject instructions mid loop
- Post execution confirmation
  - For some actions, a human may be asked to confirm outcomes or adjust the final answer

Operationally, this looks like:
- A separate approval node in the LangGraph flow
- A FastAPI endpoint or UI that surfaces pending approvals
- A structured record of who approved what, and when

Human in the loop is how Tether keeps large models powerful but safe.

---

Technical Stack

Tether is intentionally built on boring, production friendly tools.

- Python
  - Primary implementation language
  - Rich ecosystem for AI, networking, and ops
- Pydantic
  - Defines strict schemas for:
    - Tool inputs and outputs
    - Session state
    - Runtime events and logs
  - Guarantees that every tool call and model response is validated before execution
- FastAPI
  - Exposes the runtime as an HTTP API
  - Endpoints for:
    - Submitting tasks
    - Managing tools and permissions
    - Surfacing human in the loop approvals
    - Observability and health checks
- LangGraph
  - Models the runtime as an explicit graph of nodes and edges
  - Handles:
    - State passing between model, tools, and approval steps
    - Branching logic (e.g. different flows for read vs write tools)
    - Persistence of in flight sessions

This stack is designed to be:
- Easy to reason about in interviews
- Practical for real world deployment
- Flexible enough to plug in any LLM provider or custom tool

---

Why this matters for production reliability

Most demos stop at "the model gave a good answer once".
Production systems need something very different.

Tether focuses on reliability through:

1. Separation of concerns
   - Models focus on reasoning and proposing actions
   - Runtime focuses on control, safety, and orchestration
   - Tools encapsulate side effects and integration logic
   - This separation makes the system easier to test, monitor, and evolve

2. Typed, observable behavior
   - Every step of the loop is typed with Pydantic and logged
   - You can inspect:
     - Which tools were used
     - What inputs and outputs flowed through the system
     - Which permissions were checked
     - Where humans stepped in
   - This makes failures diagnosable and reproducible

3. Guardrails before side effects
   - Permission checks and optional human approvals run before tools execute
   - This prevents costly mistakes (wrong user, wrong environment, wrong data)
   - Policies can be updated centrally without rewriting prompts or model logic

4. Model agnostic design
   - The runtime does not assume a specific provider or model family
   - You can:
     - Swap models as quality and price change
     - A/B test different reasoning engines
     - Fall back to cheaper models for low risk tasks
   - This avoids vendor lock in and lets you keep reliability while the model landscape changes

5. Eval friendly architecture
   - Because Tether has explicit state and steps, you can run:
     - End to end evaluations of full workflows
     - Step level checks (e.g. "Is this tool call appropriate given the instructions and state?")
   - This turns agent behavior from a black box into something you can measure, tune, and justify.

In short, Tether is about building the runtime layer that production agentic systems actually need:
A consistent harness for tools, permissions, state, and humans that stays stable even as models, prompts, and interfaces evolve.