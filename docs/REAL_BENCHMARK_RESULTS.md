# Real-World Context Benchmark Results (Empirical)
**Hardware**: Dual GPU (RTX 5060 Ti 16GB + RTX 3060 12GB) | Total VRAM: ~28GB
**Method**: `llama-server` startup test. KV Cache: f16/q8/q4. Stop if f16 maxes out range. Timeout: 90s.

| Model Name | Source Repo | Model Quant | KV Cache | Max Stable Context | Load Time (s) | KV Allocation Delta (s) | Original Filename | Notes |
|------------|-------------|-------------|----------|-------------------|---------------|-------------------------|-------------------|-------|
| DeepSeek-R1-Distill-Qwen-14B-Q8_0 | UNKNOWN | UNKNOWN | f16 | **32768** | 5.04s | +0.00s | DeepSeek-R1-Distill-Qwen-14B-Q8_0.gguf | Verified |
| DeepSeek-R1-Distill-Qwen-14B-Q8_0 | UNKNOWN | UNKNOWN | q8_0 | **98304** | 5.02s | +0.00s | DeepSeek-R1-Distill-Qwen-14B-Q8_0.gguf | Verified |
| DeepSeek-R1-Distill-Qwen-14B-Q8_0 | UNKNOWN | UNKNOWN | q4_0 | **131072** | 5.04s | +0.02s | DeepSeek-R1-Distill-Qwen-14B-Q8_0.gguf | Verified |
| Ministral-3-14B-Reasoning-2512-Q8_0 | UNKNOWN | UNKNOWN | f16 | **262144** | 43.05s | +21.48s | Ministral-3-14B-Reasoning-2512-Q8_0.gguf | Verified |
| Ministral-3-14B-Reasoning-2512-Q8_0 | UNKNOWN | UNKNOWN | q8_0 | **524288** | 46.34s | +23.23s | Ministral-3-14B-Reasoning-2512-Q8_0.gguf | Verified |
| Ministral-3-14B-Reasoning-2512-Q8_0 | UNKNOWN | UNKNOWN | q4_0 | **1048576** | 49.07s | +32.23s | Ministral-3-14B-Reasoning-2512-Q8_0.gguf | Verified |
| Phi-4-reasoning-plus-Q8_0 | UNKNOWN | UNKNOWN | f16 | **32768** | 4.46s | +0.00s | Phi-4-reasoning-plus-Q8_0.gguf | Verified |
| Phi-4-reasoning-plus-Q8_0 | UNKNOWN | UNKNOWN | q8_0 | **98304** | 4.09s | +0.19s | Phi-4-reasoning-plus-Q8_0.gguf | Verified |
| Phi-4-reasoning-plus-Q8_0 | UNKNOWN | UNKNOWN | q4_0 | **131072** | 3.94s | +0.00s | Phi-4-reasoning-plus-Q8_0.gguf | Verified |
| Qwen3-VL-30B-A3B-Thinking-Q4_K_M | UNKNOWN | UNKNOWN | f16 | **524288** | 51.00s | +24.76s | Qwen3-VL-30B-A3B-Thinking-Q4_K_M.gguf | Verified |
| Qwen3-VL-30B-A3B-Thinking-Q4_K_M | UNKNOWN | UNKNOWN | q8_0 | **524288** | 32.14s | +4.04s | Qwen3-VL-30B-A3B-Thinking-Q4_K_M.gguf | Verified |
| Qwen3-VL-30B-A3B-Thinking-Q4_K_M | UNKNOWN | UNKNOWN | q4_0 | **262144** | 5.23s | +0.00s | Qwen3-VL-30B-A3B-Thinking-Q4_K_M.gguf | Verified |
| Step3-VL-10B-F16 | UNKNOWN | UNKNOWN | f16 | **65536** | 5.03s | +0.00s | Step3-VL-10B-F16.gguf | Verified |
| Step3-VL-10B-F16 | UNKNOWN | UNKNOWN | q8_0 | **131072** | 5.12s | +0.10s | Step3-VL-10B-F16.gguf | Verified |
| Step3-VL-10B-F16 | UNKNOWN | UNKNOWN | q4_0 | **262144** | 5.32s | +0.30s | Step3-VL-10B-F16.gguf | Verified |
| glm-4-9b-chat-Q8_0 | UNKNOWN | UNKNOWN | f16 | **262144** | 3.65s | +0.00s | glm-4-9b-chat-Q8_0.gguf | Verified |
| glm-4-9b-chat-Q8_0 | UNKNOWN | UNKNOWN | q8_0 | **524288** | 3.70s | +0.18s | glm-4-9b-chat-Q8_0.gguf | Verified |
| glm-4-9b-chat-Q8_0 | UNKNOWN | UNKNOWN | q4_0 | **1048576** | 3.85s | +0.33s | glm-4-9b-chat-Q8_0.gguf | Verified |
| gpt-oss-20b-uncensored.Q4_K_M | UNKNOWN | UNKNOWN | f16 | **131072** | 5.84s | +0.00s | gpt-oss-20b-uncensored.Q4_K_M.gguf | Verified |
| gpt-oss-20b-uncensored.Q4_K_M | UNKNOWN | UNKNOWN | q8_0 | **262144** | 6.53s | +0.82s | gpt-oss-20b-uncensored.Q4_K_M.gguf | Verified |
| gpt-oss-20b-uncensored.Q4_K_M | UNKNOWN | UNKNOWN | q4_0 | **524288** | 6.53s | +0.00s | gpt-oss-20b-uncensored.Q4_K_M.gguf | Verified |
| gpt-oss-20b-uncensored.Q8_0 | UNKNOWN | UNKNOWN | f16 | **65536** | 6.86s | +0.00s | gpt-oss-20b-uncensored.Q8_0.gguf | Verified |
| gpt-oss-20b-uncensored.Q8_0 | UNKNOWN | UNKNOWN | q8_0 | **131072** | 6.87s | +0.00s | gpt-oss-20b-uncensored.Q8_0.gguf | Verified |
| gpt-oss-20b-uncensored.Q8_0 | UNKNOWN | UNKNOWN | q4_0 | **262144** | 6.90s | +0.21s | gpt-oss-20b-uncensored.Q8_0.gguf | Verified |
| phi-4-Q8_0 | UNKNOWN | UNKNOWN | f16 | **32768** | 4.16s | +0.00s | phi-4-Q8_0.gguf | Verified |
| phi-4-Q8_0 | UNKNOWN | UNKNOWN | q8_0 | **98304** | 4.17s | +0.21s | phi-4-Q8_0.gguf | Verified |
| phi-4-Q8_0 | UNKNOWN | UNKNOWN | q4_0 | **131072** | 4.02s | +0.00s | phi-4-Q8_0.gguf | Verified |
| qwen2.5-coder-14b-instruct-q8_0 | UNKNOWN | UNKNOWN | f16 | **32768** | 4.43s | +0.00s | qwen2.5-coder-14b-instruct-q8_0.gguf | Verified |
| qwen2.5-coder-14b-instruct-q8_0 | UNKNOWN | UNKNOWN | q8_0 | **98304** | 4.51s | +0.21s | qwen2.5-coder-14b-instruct-q8_0.gguf | Verified |
| qwen2.5-coder-14b-instruct-q8_0 | UNKNOWN | UNKNOWN | q4_0 | **131072** | 5.02s | +0.64s | qwen2.5-coder-14b-instruct-q8_0.gguf | Verified |
