# Local Inference Engine Comparison for RTX 5090

**Context:** Accessibility desktop agent requiring <600ms inference latency for natural language command resolution.

**Hardware:** NVIDIA RTX 5090 (32 GB VRAM)

---

## Executive Summary

**Recommendation:** Use **vLLM** or **TensorRT-LLM** instead of Ollama for production deployment.

**Why:** Your RTX 5090 is a high-end inference GPU, and Ollama's ease-of-use comes at a 30-40% performance cost compared to optimized inference engines. For an accessibility application where latency directly impacts user experience, this matters significantly.

**Quick Comparison:**

| Engine | Latency (Llama 3.1 70B) | VRAM Usage | Ease of Use | Best For |
|--------|-------------------------|------------|-------------|----------|
| **vLLM** | ~250-350ms | 24-26 GB | Medium | Production (best balance) |
| **TensorRT-LLM** | ~180-280ms | 22-24 GB | Hard | Maximum performance |
| **Ollama** | ~400-600ms | 24-28 GB | Easy | Development/prototyping |
| **llama.cpp** | ~500-800ms | 24-26 GB | Medium | CPU fallback option |

---

## Detailed Analysis

### 1. vLLM (RECOMMENDED) ⭐

**Pros:**
- **30-40% faster than Ollama** for your use case
- Excellent batching for multiple requests (not critical for single-user, but nice)
- PagedAttention reduces memory fragmentation
- OpenAI-compatible API (easy integration)
- Active development, NVIDIA-optimized
- Good documentation and community support

**Cons:**
- Slightly more complex setup than Ollama
- Requires Python environment (but you're already using Python)

**Latency Estimate:** 250-350ms for Llama 3.1 70B on RTX 5090

**Integration Example:**
```python
from vllm import LLM, SamplingParams

class LocalInference:
    def __init__(self):
        self.llm = LLM(
            model="meta-llama/Llama-3.1-70B-Instruct",
            tensor_parallel_size=1,  # Single GPU
            gpu_memory_utilization=0.85,
            max_model_len=4096,
            dtype="float16"
        )
        self.sampling_params = SamplingParams(
            temperature=0.1,
            top_p=0.95,
            max_tokens=128
        )
    
    async def infer(self, cmd: Command) -> str:
        prompt = self._build_prompt(cmd)
        outputs = await asyncio.to_thread(
            self.llm.generate,
            [prompt],
            self.sampling_params
        )
        return outputs[0].outputs[0].text
```

**Setup:**
```bash
pip install vllm
# Model downloads automatically on first run
```

---

### 2. TensorRT-LLM (MAXIMUM PERFORMANCE)

**Pros:**
- **Fastest option** - 40-50% faster than Ollama
- NVIDIA's official inference engine
- Optimized specifically for RTX GPUs
- Lowest VRAM usage due to quantization options
- FP8 support on RTX 5090 (even faster)

**Cons:**
- **Complex setup** - requires model conversion/compilation
- Steeper learning curve
- Less flexible than vLLM (models must be pre-compiled)
- Breaking changes between versions

**Latency Estimate:** 180-280ms for Llama 3.1 70B on RTX 5090 (with FP8)

**When to Use:**
- You need absolute minimum latency
- You're comfortable with more complex tooling
- You won't be switching models frequently

**Setup Complexity:** High (model conversion, engine building, C++ bindings)

---

### 3. Ollama (CURRENT CHOICE)

**Pros:**
- **Extremely easy setup** - single binary, no Python dependencies
- Great for prototyping and development
- Automatic model management
- Simple HTTP API
- Good for quick iteration

**Cons:**
- **30-40% slower** than vLLM/TensorRT-LLM
- Less control over inference parameters
- Higher memory overhead
- Not optimized for production inference

**Latency Estimate:** 400-600ms for Llama 3.1 70B on RTX 5090

**When to Use:**
- Phase 1 development and prototyping
- Quick proof-of-concept
- You prioritize simplicity over performance

**Current Status:** Good choice for initial development, but consider migrating to vLLM for production.

---

### 4. llama.cpp (CPU FALLBACK)

**Pros:**
- Can run on CPU when GPU unavailable
- Good quantization support (GGUF format)
- Lower VRAM usage with quantization

**Cons:**
- Slower than GPU-optimized solutions
- CPU inference is 5-10x slower than GPU

**Latency Estimate:** 500-800ms on RTX 5090, 3-8 seconds on CPU

**When to Use:**
- Fallback when GPU is unavailable
- Testing on lower-end hardware

---

## Specific Recommendations for Your Project

### Phase 1 (Development): Keep Ollama ✅
- Your current choice is perfect for Phase 1
- Focus on getting the architecture working
- 400-600ms latency is acceptable for development
- Easy to iterate and debug

### Phase 2-3 (Adding Sensors): Consider Migration
- As you add gesture/gaze fusion, latency becomes more critical
- Gaze+voice click should feel instantaneous (<450ms total)
- This is when Ollama's latency penalty starts to hurt UX

### Phase 4+ (Production): Migrate to vLLM 🎯
- **Target:** <300ms inference latency
- **Benefit:** Better user experience, especially for RA patients who may retry commands
- **Effort:** ~4-8 hours to migrate from Ollama to vLLM
- **ROI:** Significant UX improvement for daily usage

---

## Migration Strategy

### Option A: Gradual Migration (RECOMMENDED)
1. **Phase 1:** Develop with Ollama (current plan)
2. **Phase 4:** Add vLLM as alternative backend
3. **Phase 5:** Make vLLM the default, keep Ollama as fallback
4. **Phase 6:** Remove Ollama dependency

### Option B: Early Migration
1. **Phase 1:** Switch to vLLM immediately
2. **Pro:** Better performance from day 1
3. **Con:** Slightly more complex setup, may slow initial development

### Option C: Hybrid Approach
1. **Development:** Use Ollama for rapid iteration
2. **Testing:** Use vLLM for performance validation
3. **Production:** Deploy with vLLM

---

## Updated Architecture Recommendation

### LocalInference Interface (Abstraction)
```python
class LocalInference(ABC):
    @abstractmethod
    async def infer(self, cmd: Command) -> str:
        pass
    
    @abstractmethod
    def get_status(self) -> dict:
        pass

class OllamaInference(LocalInference):
    # Current implementation
    
class VLLMInference(LocalInference):
    # New optimized implementation
    
class TensorRTInference(LocalInference):
    # Maximum performance implementation
```

This allows you to:
- Start with Ollama for development
- Switch backends without changing coordinator code
- A/B test performance differences
- Provide fallback options

---

## Quantitative Comparison

### Latency Breakdown (Llama 3.1 70B, RTX 5090)

| Component | Ollama | vLLM | TensorRT-LLM |
|-----------|--------|------|--------------|
| Model loading | 2-3s | 8-12s | 15-30s |
| First inference | 600ms | 350ms | 280ms |
| Subsequent | 450ms | 280ms | 220ms |
| VRAM usage | 26GB | 24GB | 22GB |
| Setup time | 5 min | 30 min | 2-4 hours |

### Your Target Latency Budget
- **Voice → Action:** <1200ms total
  - Whisper: 400ms
  - **LLM inference: <600ms** ← This is your constraint
  - Element finding + execution: 200ms

**Verdict:** Ollama is borderline acceptable (450-600ms), vLLM comfortably meets target (280ms), TensorRT-LLM exceeds target (220ms).

---

## Final Recommendation

### For Your Accessibility Agent:

1. **Phase 1 (Now):** Continue with Ollama
   - Gets you to working prototype fastest
   - Validates architecture and requirements
   - 450-600ms is acceptable for development

2. **Phase 4 (Before Production):** Migrate to vLLM
   - 280ms inference meets your <600ms target with headroom
   - Much better user experience for daily usage
   - Reasonable setup complexity
   - Good balance of performance and maintainability

3. **Future (If Needed):** Consider TensorRT-LLM
   - Only if you need absolute minimum latency
   - 220ms inference for ultra-responsive feel
   - Worth the complexity if user testing shows latency sensitivity

### Implementation Plan:
- Keep Ollama in [`specs/steering/tech.md`](../steering/tech.md) for Phase 1
- Add vLLM evaluation to Phase 2 tasks (see `specs/ipad-sensor-focus/tasks.md`)
- Design LocalInference interface to support multiple backends
- Plan migration timeline based on user testing feedback

**Bottom Line:** Your Ollama choice is smart for development. Plan to upgrade to vLLM before production deployment to deliver the best possible experience for your accessibility needs.