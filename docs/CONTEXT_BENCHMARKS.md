# Optimal Context Size Benchmarks for >19B Models

This document outlines the recommended context window settings for Large Language Models (LLMs) with more than 19 billion parameters, tailored for a dual-GPU setup (12GB + 16GB = ~28GB VRAM).

## Methodology

*   **Total VRAM:** ~28 GB (RTX 3060 12GB + RTX 4060 Ti 16GB assumed comparable split)
*   **Base Requirement:** Q4_K_M quantization usually takes 0.7-0.8 GB per billion parameters.
*   **KV Cache:** Context takes additional memory. Multi-Head Attention (MHA) consumes significantly more than Grouped Query Attention (GQA).
*   **Safety Margin:** 1-2 GB reserved for system overhead and OS display.

## Benchmark Results & Recommendations

| Model | Parameters | Quant | Est. VRAM Load (0 ctx) | KV Cache / 1k | Max Safe Context (100% VRAM) | Recommended Setting |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Command R** | 35B | Q4_K_M | ~23.6 GB | ~250 MB | ~14k | **16384** (Small RAM spill) |
| **DeepSeek R1** | 32B | Q4_K_M | ~22.0 GB | ~250 MB | ~20k | **24576** |
| **Qwen 2.5 Coder** | 32B | Q4_K_M | ~22.0 GB | ~250 MB | ~20k | **24576** |
| **Qwen 3 Thinking** | 30B | Q4_K_M | ~20.3 GB | ~250 MB | ~27k | **32768** |
| **GLM-4** | 30B | Q4_K_M | ~20.3 GB | ~120 MB | ~65k | **32768** |
| **Gemma 2** | 27B | Q4_K_M | ~18.5 GB | ~360 MB | ~24k | **32768** |
| **GPT-OSS** | 20B | Q4_K_M | ~13.8 GB | ~172 MB | ~78k | **65536** |

## Key Findings

1.  **Command R (35B)** is the heaviest model. With 23.6GB base usage, it leaves only ~4GB for context. A 128k context is impossible on this hardware without massive RAM offloading (extremely slow). **Limit to 16k.**
2.  **32B Models (DeepSeek, Qwen)** fit comfortably with **24k context**. Pushing to 32k might cause slight spilling to system RAM, affecting speed but remaining stable.
3.  **Gemma 2 (27B)** has a very large KV cache footprint (360MB/1k tokens). While the model is smaller, the context grows faster in memory than Qwen. **32k is the sweet spot.**

## Recommendation

Update `models.yaml` to enforce these limits. Setting context to `262144` (262k) for 32B models on 28GB VRAM will almost certainly cause OOM crashes or system instability.

**Safe Limits:**
*   35B Models: **16k**
*   32B Models: **24k**
*   27B Models: **32k**
*   <20B Models: **32k - 128k** (depending on specific size)
