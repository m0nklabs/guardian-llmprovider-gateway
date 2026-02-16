# Benchmark Dashboard

A modern React/Vite dashboard for visualizing LLM benchmark results from `docs/benchmark_results.json`.

## Features
- Interactive TPS vs Context Window charts
- Load Time comparisons
- Model filtering and selection
- Statistical summaries

## Usage

1. **Install Dependencies** (first time only)
   ```bash
   npm install
   ```

2. **Start Development Server**
   ```bash
   npm run dev
   ```

3. **Access Dashboard**
   - Local: `http://localhost:5173`
   - Network: `http://<your-ip>:5173`

The dashboard automatically syncs with new benchmark data by linking `docs/benchmark_results.json` to `public/data.json`.
