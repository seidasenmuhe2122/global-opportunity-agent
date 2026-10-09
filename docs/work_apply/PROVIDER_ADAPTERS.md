# Provider Adapters

The application flow includes a provider adapter layer to isolate provider-specific behavior.

Supported adapters may be generic HTTP or browser automation adapters. If the provider requires unsupported access controls, the system marks the application as `Needs Review` instead of pretending success.

## AI providers and fallback

`AIClient` uses OpenAI-compatible chat completions. It tries the configured `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` first, then tries any provider-specific keys that are set, in this order:

1. Google: `AI_GOOGLE_API_KEY` (`gemini-3.8-flash`)
2. Groq: `AI_GROQ_API_KEY` (`openai/gpt-oss-120b`)
3. OpenRouter: `AI_OPENROUTER_API_KEY` (`deepseek/deepseek-r1:free`)
4. Mistral: `AI_MISTRAL_API_KEY` (`mistral-small-latest`)
5. Together: `AI_TOGETHER_API_KEY` (`meta-llama/Llama-3.3-70B-Instruct-Turbo`)
6. Hugging Face: `AI_HUGGINGFACE_API_KEY` (`meta-llama/Llama-3.2-3B-Instruct`)

Provider-specific base URLs and model defaults are configured in the AI client and can be overridden with `AI_GOOGLE_MODEL`, `AI_GROQ_MODEL`, and the corresponding provider model variables. Set only the keys for providers you want enabled. If every configured provider fails, or no provider is configured, the client raises `AIProviderError`; it does not return a mock-shaped success response. Tests should use explicit mocks/fixtures instead of an implicit no-provider response.

Website, RSS, and API sources scan a configurable batch of candidates per run. `SOURCE_CANDIDATE_BATCH_SIZE` defaults to 50 and accepts values from 1 to 200. A persisted cursor continues on the next scan so candidates beyond the current batch are not silently ignored.
