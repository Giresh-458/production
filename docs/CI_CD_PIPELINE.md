# CI/CD & Deployment Pipeline

The Research Intelligence Framework (RIF) uses a fully automated CI/CD pipeline. Every time code is merged into the `main` branch, the system validates the code logic and then automatically deploys and runs the full live pipeline on the GPU server.

## Stage 1: Continuous Integration (CI Logic Validation)

Because GitHub Actions runners are free CPU servers, they cannot execute the heavy LLM models required for proposal generation. Therefore, Stage 1 tests the logic without hitting the live models.

1. **Environment Setup**: GitHub provisions a fresh Ubuntu runner.
2. **Automated Testing**: The pipeline executes over **390 deterministic Pytest regression tests**. These tests validate crawler limits, evidence quality gates, SQLite interactions, and context propagation securely and instantly.
3. **Safety Gate**: If *any* test fails, the pipeline aborts. Broken code is never deployed.

---

## Stage 2: Automated Live Execution (CD)

If the logic tests pass, the pipeline connects to your RTX 5070 server, updates the code, and autonomously triggers a full end-to-end run (from funding collection to proposal generation).

1. **Secure Connection**: Connects to the GPU server via SSH.
2. **Docker Orchestration**: 
   - Builds the updated Python environment.
   - Spins up the `rif-ollama` container (bound to the RTX 5070 GPU).
   - Ensures the `gemma` LLM is fully downloaded.
3. **Live Execution Trigger**: 
   - Spins up the `rif-pipeline` container in the background.
   - The container immediately executes `python run_full_pipeline.py`.
   - The pipeline will autonomously crawl the web, process the intelligence, and synthesize the grant proposals in the background on your server.
