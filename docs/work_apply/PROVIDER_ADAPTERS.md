# Provider Adapters

The application flow includes a provider adapter layer to isolate provider-specific behavior.

Supported adapters may be generic HTTP or browser automation adapters. If the provider requires unsupported access controls, the system marks the application as `Needs Review` instead of pretending success.

## AI providers and fallback

`AIClient` uses OpenAI-compatible chat completions. It tries the configured `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` first, then tries any provider-specific keys that are set, in this order:

1. Google: `AI_GOOGLE_API_KEY` (`gemini-3.5-flash-lite`)
2. Groq: `AI_GROQ_API_KEY` (`llama-3.3-70b-versatile`)
3. OpenRouter: `AI_OPENROUTER_API_KEY` (`deepseek/deepseek-r1:free`)
4. Mistral: `AI_MISTRAL_API_KEY` (`mistral-small-latest`)
5. Together: `AI_TOGETHER_API_KEY` (`meta-llama/Llama-3.3-70B-Instruct-Turbo`)
6. Hugging Face: `AI_HUGGINGFACE_API_KEY` (`meta-llama/Llama-3.2-3B-Instruct`)

Provider-specific base URLs and model defaults are configured in the AI client. Set only the keys for providers you want enabled. If every configured provider fails, the client raises `AIProviderError` rather than returning a success-shaped response. With no configured keys, it retains the local mock response for development.
