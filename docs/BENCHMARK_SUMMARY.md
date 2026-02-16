# 📊 Benchmark Analysis Summary
**Generated:** Sun Feb 15 08:00:33 PM UTC 2026

This document summarizes the raw JSON benchmark data into actionable insights.

## 🏆 Global Rankings
### 🧠 Top 5 Models by Max Context
| Rank | Model | Max Context | Best KV Type |
|------|-------|-------------|--------------|
| #1 | DeepSeek-R1-Distill-Qwen-32B-Uncensored.Q4_K_M | **1M** | q4_0 |
| #2 | Ministral-3-14B-Reasoning-2512-Q8_0 | **1M** | q4_0 |
| #3 | Phi-4-reasoning-plus-Q8_0 | **1M** | q4_0 |
| #4 | Qwen3-30B-A3B-Thinking-2507-Q4_K_M-latest | **1M** | q8_0 |
| #5 | glm-4-9b-chat-Q8_0 | **1M** | q4_0 |
| #6 | Qwen3-VL-30B-A3B-Thinking-Q4_K_M | 512k | f16 |
| #7 | gpt-oss-20b-uncensored.Q4_K_M | 512k | q4_0 |
| #8 | Step3-VL-10B-F16 | 256k | q4_0 |
| #9 | DeepSeek-R1-Distill-Qwen-14B-Q8_0 | 128k | q4_0 |
| #10 | GLM-4.7-Flash-Q4_K_M-latest | 64k | f16 |

### ⚡ Top 5 Fastest Models (Avg TPS)
| Rank | Model | Avg TPS |
|------|-------|---------|
| #1 | gpt-oss-20b-uncensored.Q4_K_M | 94.17 |
| #2 | gpt-oss-20b-uncensored.Q8_0 | 77.86 |
| #3 | Qwen3-VL-30B-A3B-Thinking-Q4_K_M | 72.66 |
| #4 | Qwen3-30B-A3B-Thinking-2507-Q4_K_M-latest | 67.41 |
| #5 | glm-4-9b-chat-Q8_0 | 35.68 |
| #6 | GLM-4.7-Flash-Q4_K_M-latest | 25.57 |
| #7 | DeepSeek-R1-Distill-Qwen-14B-Q8_0 | 23.51 |
| #8 | Step3-VL-10B-F16 | 22.69 |
| #9 | Ministral-3-14B-Reasoning-2512-Q8_0 | 19.00 |
| #10 | DeepSeek-R1-Distill-Qwen-32B-Uncensored.Q4_K_M | 13.28 |

## 🔬 Detailed Breakdown
| Rank | Model | Quant | F16 Context | Q8 Context | Q4 Context | Avg TPS (F16) | Avg TPS (Q4) | Impact (Q4 vs F16) |
|------|-------|-------|-------------|------------|------------|---------------|--------------|--------------------|
| 1 | DeepSeek-R1-Distill-Qwen-14B-Q8_0 | Q8_0 | 32k | 96k | 128k | 23.87 | 23.36 | -2.1% |
| 2 | DeepSeek-R1-Distill-Qwen-32B-Uncensored.Q4_K_M | Q4_K_M | 256k | 256k | **1M** | 13.84 | 12.37 | -10.6% |
| 3 | GLM-4.7-Flash-Q4_K_M-latest | Q4_K_M | 64k | 64k | 64k | 25.44 | 25.88 | +1.7% |
| 4 | Ministral-3-14B-Reasoning-2512-Q8_0 | Q8_0 | 256k | 512k | **1M** | 11.51 | 22.44 | +95.0% |
| 5 | Phi-4-reasoning-plus-Q8_0 | Q8_0 | 256k | 512k | **1M** | - | - | - |
| 6 | Qwen3-30B-A3B-Thinking-2507-Q4_K_M-latest | Q4_K_M | 512k | **1M** | **1M** | 64.80 | 69.24 | +6.9% |
| 7 | Qwen3-VL-30B-A3B-Thinking-Q4_K_M | Q4_K_M | 512k | 512k | 256k | 63.33 | 103.70 | +63.8% |
| 8 | Step3-VL-10B-F16 | F16 | 64k | 128k | 256k | 22.91 | 22.56 | -1.5% |
| 9 | glm-4-9b-chat-Q8_0 | Q8_0 | 256k | 512k | **1M** | 36.37 | 35.06 | -3.6% |
| 10 | gpt-oss-20b-uncensored.Q4_K_M | Q4_K_M | 128k | 256k | 512k | 96.80 | 91.86 | -5.1% |
| 11 | gpt-oss-20b-uncensored.Q8_0 | Q8_0 | 64k | 32k | - | 77.91 | - | - |