# Real-World Context Benchmark Results (Empirical)
**Hardware**: Dual GPU (RTX 5060 Ti 16GB + RTX 3060 12GB) | Total VRAM: ~28GB
**Method**: `llama-server` startup test. KV Cache: f16/q8/q4. Stop if f16 maxes out range. Timeout: 90s.

| Model Name | Source Repo | Model Quant | KV Cache | Max Stable Context | Load Time (s) | Original Filename | Notes |
|------------|-------------|-------------|----------|-------------------|---------------|-------------------|-------|
| DeepSeek-R1-Distill-Qwen-32B-Uncensored.Q4_K_M | deepseek-ai/DeepSeek-R1-Distill-Qwen-32B-GGUF | Q4_K_M | f16 | **16384** | 7.06s | deepseek-r1-distill-qwen-32b-q4_k_m.gguf | Verified |
| DeepSeek-R1-Distill-Qwen-32B-Uncensored.Q4_K_M | deepseek-ai/DeepSeek-R1-Distill-Qwen-32B-GGUF | Q4_K_M | q8_0 | **98304** | 21.68s | deepseek-r1-distill-qwen-32b-q4_k_m.gguf | Verified |
| DeepSeek-R1-Distill-Qwen-32B-Uncensored.Q4_K_M | deepseek-ai/DeepSeek-R1-Distill-Qwen-32B-GGUF | Q4_K_M | q4_0 | **0** | 0.00s | deepseek-r1-distill-qwen-32b-q4_k_m.gguf | Verified |
| GLM-4.7-Flash-Q4_K_M-latest | zai-org/GLM-4.7-Flash-GGUF | Q4_K_M | f16 | **98304** | 6.64s | glm-4.7-flash-q4_k_m.gguf | Verified |
