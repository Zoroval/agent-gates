# Finding protected surfaces

A protected surface is any file where a change can alter what a model receives, which model runs, how it reasons, or how its output becomes product behavior.

## Search for these

Run searches like these from the repository root. Adapt them to the language.

```bash
# Model clients and calls
grep -rIlE "openai|anthropic|google.genai|vertexai|bedrock|mistral|cohere|litellm|ollama" --exclude-dir={node_modules,.git,dist,build,.venv} .
grep -rIlE "chat\.completions|responses\.create|messages\.create|generate_content|invoke_model" --exclude-dir={node_modules,.git,.venv} .

# Model names and settings
grep -rInE "(gpt-|claude-|gemini-|o[0-9]-|llama|mistral)[a-z0-9.-]*" --exclude-dir={node_modules,.git,.venv} . | grep -v test
grep -rInE "temperature|top_p|max_tokens|max_output_tokens|reasoning_effort|reasoning|thinking" --exclude-dir={node_modules,.git,.venv} .

# Prompts
find . -path ./node_modules -prune -o \( -iname "*prompt*" -o -iname "*.jinja*" -o -iname "*.j2" -o -iname "*.prompt" -o -iname "*system*message*" \) -print
grep -rIlE "system_prompt|SYSTEM_PROMPT|role\": *\"system|PromptTemplate" --exclude-dir={node_modules,.git,.venv} .

# Structured output and tools
grep -rIlE "response_format|json_schema|tool_choice|tools=|function_call|BaseModel" --exclude-dir={node_modules,.git,.venv} .

# Retrieval
grep -rIlE "embedding|vector|rerank|top_k|chunk|retriev" --exclude-dir={node_modules,.git,.venv} .
```

## Classify each hit

| Kind | Protect? | Example |
|---|---|---|
| Prompt text or templates | Yes | `prompts/support_reply.md` |
| Code that builds the prompt or context | Yes. This is the most often missed. | `context_builder.py`, `format_history()` |
| Model name, temperature, reasoning settings | Yes, with `line_patterns` if the file is shared | `settings.py: SUPPORT_MODEL = ...` |
| Output schema and the parser of model output | Yes | `schemas/verdict.py`, `parse_reply()` |
| Retrieval: chunking, ranking, filters, top-k | Yes | `retrieval/rank.py` |
| Code that decides what to do with model output | Yes, if it changes user-visible behavior | `apply_verdict()`, guardrail thresholds |
| Model-written reports or messages to users | Yes | `reports/summary.py` |
| Provider client wrappers with retries only | Usually no | `http_client.py` |
| Logging, metrics, tests, docs | No | |

## Group by stage

A stage is one place where the product makes a model-backed decision. Name stages in product words that the team uses. Examples: `intent_routing`, `retrieval`, `support_reply`, `judge`, `weekly_summary`.

A file used by two stages belongs to a rule with both stages. Then a change to it needs evidence for both.

## Shared files

For a shared file such as `settings.py` or `config.ts`, add `line_patterns` that match only the model-related lines, for example:

```json
"line_patterns": ["SUPPORT_MODEL", "SUPPORT_TEMPERATURE", "SUPPORT_REASONING"]
```

The gate then fires only when a changed line (added or removed) matches one of the patterns.

## Sanity check

After you draft the policy, look at the last 30 merged PRs. Count how many would have triggered the gate. If it is more than about half, the policy is too broad. If it is zero and the team changes prompts often, it is too narrow.
