import asyncio
import logging
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.tweaker.benchmark import BenchmarkSuite

# Configure logging
logging.basicConfig(level=logging.INFO)

async def run_quick_test():
    print("🚀 Starting Quick Benchmark Test...")
    
    # Initialize Suite
    suite = BenchmarkSuite()
    
    # Override settings for a quick test
    suite.models_to_test = ["qwen2.5:0.5b"] # Small model
    suite.ctx_options = [2048, 4096]        # Two context sizes
    suite.batch_options = [128, 256]        # Two batch sizes
    
    # Force a fresh queue for this test
    suite.state_file = "data/test_benchmark_state.json"
    if os.path.exists(suite.state_file):
        os.remove(suite.state_file)
        
    print(f"📋 Test Plan: Model={suite.models_to_test[0]}, Ctx={suite.ctx_options[0]}, Batch={suite.batch_options[0]}")
    
    # Run
    await suite.run_suite()
    
    print("✅ Test Completed!")
    
    # Show results
    import json
    if os.path.exists(suite.state_file):
        with open(suite.state_file, 'r') as f:
            data = json.load(f)
            print("\n📊 Results:")
            print(json.dumps(data["completed"], indent=2))

if __name__ == "__main__":
    asyncio.run(run_quick_test())
