import React, { useState, useEffect, useMemo } from 'react';
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, BarChart, Bar } from 'recharts';
import { Activity, Clock, Zap, Server, ChevronDown, ChevronUp, Filter } from 'lucide-react';

const StatCard = ({ title, value, unit, icon: Icon, trend }) => (
  <div className="bg-slate-900 border border-slate-800 p-6 rounded-xl shadow-lg">
    <div className="flex items-center justify-between mb-4">
      <h3 className="text-slate-400 text-sm font-medium">{title}</h3>
      <Icon className="w-5 h-5 text-indigo-500" />
    </div>
    <div className="flex items-end space-x-2">
      <span className="text-3xl font-bold text-white">{value}</span>
      <span className="text-slate-500 mb-1 text-sm">{unit}</span>
    </div>
    {trend && (
      <div className={`text-xs mt-2 ${trend > 0 ? 'text-emerald-500' : 'text-rose-500'}`}>
        {trend > 0 ? '+' : ''}{trend}% from average
      </div>
    )}
  </div>
);

const App = () => {
  const [rawData, setRawData] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedModels, setSelectedModels] = useState(new Set());
  const [excludeFailures, setExcludeFailures] = useState(true);

  // Colors for charts
  const colors = [
    '#6366f1', '#ec4899', '#10b981', '#f59e0b', '#8b5cf6', 
    '#ef4444', '#06b6d4', '#84cc16', '#d946ef', '#f97316',
    '#14b8a6', '#64748b', '#f43f5e', '#a855f7', '#3b82f6'
  ];

  useEffect(() => {
    fetch('/data.json')
      .then(res => res.json())
      .then(data => {
        setRawData(data);
        // Default to selecting all models initially
        const models = new Set(data.map(d => d.model_name));
        setSelectedModels(models);
        setLoading(false);
      })
      .catch(err => {
        console.error("Failed to load data", err);
        setLoading(false);
      });
  }, []);

  const processedData = useMemo(() => {
    if (!rawData.length) return { models: [], chartData: [] };

    let filtered = rawData;
    if (excludeFailures) {
      filtered = filtered.filter(d => d.status === 'success');
    }

    // Get unique context sizes for X-axis
    const contextSizes = [...new Set(filtered.map(d => d.context_size))].sort((a, b) => a - b);
    
    // Group by model
    const startData = {}; // Map of model -> array of runs
    filtered.forEach(item => {
      if (!startData[item.model_name]) startData[item.model_name] = [];
      startData[item.model_name].push(item);
    });

    const models = Object.keys(startData);

    // Prepare chart data: array of objects { context_size, model1: tps, model2: tps, ... }
    const chartData = contextSizes.map(size => {
      const entry = { name: String(size) };
      models.forEach(model => {
        // Find run for this model and context size
        const runs = startData[model]
          .filter(r => r.context_size === size)
          .sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp));
        
        if (runs.length > 0) {
          entry[model] = runs[0].tokens_per_second;
          entry[`${model}_load`] = runs[0].load_time_seconds;
          entry[`${model}_len`] = runs[0].output_length_chars;
        }
      });
      return entry;
    });

    return { models, chartData };
  }, [rawData, excludeFailures]);

  const toggleModel = (model) => {
    const next = new Set(selectedModels);
    if (next.has(model)) next.delete(model);
    else next.add(model);
    setSelectedModels(next);
  };

  const visibleModels = processedData.models.filter(m => selectedModels.has(m));

  if (loading) return <div className="flex items-center justify-center h-screen bg-slate-950 text-white">Loading Benchmark Suite...</div>;

  return (
    <div className="min-h-screen bg-slate-950 p-8 font-sans">
      <header className="mb-8 flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold bg-gradient-to-r from-indigo-400 to-cyan-400 bg-clip-text text-transparent">
            LLM Benchmark Dashboard
          </h1>
          <p className="text-slate-400 mt-2">Performance analysis across context windows</p>
        </div>
        <div className="flex gap-4">
           {/* Controls could go here */}
        </div>
      </header>

      {/* Stats Row */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8">
        <StatCard 
          title="Total Benchmarks" 
          value={rawData.length} 
          unit="runs" 
          icon={Activity}
        />
        <StatCard 
          title="Models Tested" 
          value={processedData.models.length} 
          unit="models" 
          icon={Server}
        />
        <StatCard 
          title="Avg Load Time" 
          value={(rawData.reduce((acc, curr) => acc + (curr.load_time_seconds || 0), 0) / (rawData.length || 1)).toFixed(2)} 
          unit="sec" 
          icon={Clock}
        />
        <StatCard 
          title="Avg Resp. Length" 
          value={(rawData.reduce((acc, curr) => acc + (curr.output_length_chars || 0), 0) / (rawData.length || 1)).toFixed(0)} 
          unit="chars" 
          icon={Server} // Reusing Server icon or finding a better one but Lucide needs import
        />
        <StatCard 
          title="Peak TPS" 
          value={Math.max(...rawData.map(d => d.tokens_per_second || 0)).toFixed(1)} 
          unit="t/s" 
          icon={Zap}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-8">
        
        {/* Sidebar / Filters */}
        <div className="lg:col-span-1 space-y-6">
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-6">
            <h3 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
              <Filter className="w-4 h-4" /> Filters
            </h3>
            
            <div className="space-y-2 mb-6">
              <label className="flex items-center space-x-2 text-slate-300 cursor-pointer">
                <input 
                  type="checkbox" 
                  checked={excludeFailures} 
                  onChange={e => setExcludeFailures(e.target.checked)}
                  className="rounded border-slate-700 bg-slate-800 text-indigo-500 focus:ring-indigo-500"
                />
                <span>Exclude Failures</span>
              </label>
            </div>

            <h4 className="text-xs font-semibold text-slate-500 uppercase tracking-wider mb-3">Models</h4>
            <div className="space-y-2 max-h-[60vh] overflow-y-auto pr-2 custom-scrollbar">
              {processedData.models.map((model, idx) => (
                <div 
                  key={model} 
                  className={`flex items-center space-x-2 px-3 py-2 rounded-lg cursor-pointer transition-colors ${selectedModels.has(model) ? 'bg-slate-800 text-white' : 'text-slate-400 hover:bg-slate-800/50'}`}
                  onClick={() => toggleModel(model)}
                >
                  <div className="w-3 h-3 rounded-full" style={{ backgroundColor: colors[idx % colors.length] }}></div>
                  <span className="text-sm truncate" title={model}>{model.replace('.gguf', '')}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Main Content */}
        <div className="lg:col-span-3 space-y-8">
          
          {/* TPS Chart */}
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-xl">
            <h3 className="text-xl font-semibold text-white mb-6">Inference Speed vs Context Size</h3>
            <div className="h-[400px]">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={processedData.chartData} margin={{ top: 5, right: 30, left: 20, bottom: 5 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis 
                    dataKey="name" 
                    stroke="#94a3b8"
                    tick={{ fill: '#94a3b8' }}
                    label={{ value: 'Context Size (tokens)', position: 'insideBottom', offset: -5, fill: '#94a3b8' }} 
                  />
                  <YAxis 
                    stroke="#94a3b8"
                    tick={{ fill: '#94a3b8' }}
                    label={{ value: 'Tokens / Second', angle: -90, position: 'insideLeft', fill: '#94a3b8' }}
                  />
                  <Tooltip 
                    contentStyle={{ backgroundColor: '#0f172a', borderColor: '#1e293b', color: '#f8fafc' }}
                    itemStyle={{ color: '#e2e8f0' }}
                  />
                  <Legend />
                  {visibleModels.map((model, idx) => (
                    <Line
                      key={model}
                      type="monotone"
                      dataKey={model}
                      stroke={colors[processedData.models.indexOf(model) % colors.length]}
                      activeDot={{ r: 8 }}
                      strokeWidth={2}
                      connectNulls
                    />
                  ))}
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Load Time Chart (Optional secondary view) */}
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-xl">
            <h3 className="text-xl font-semibold text-white mb-6">Model Load Times</h3>
            <div className="h-[300px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={processedData.chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="name" stroke="#94a3b8" />
                  <YAxis stroke="#94a3b8" label={{ value: 'Seconds', angle: -90, position: 'insideLeft', fill: '#94a3b8' }}/>
                  <Tooltip 
                    contentStyle={{ backgroundColor: '#0f172a', borderColor: '#1e293b', color: '#f8fafc' }}
                  />
                  {visibleModels.map((model, idx) => (
                    <Bar 
                      key={model} 
                      dataKey={`${model}_load`} 
                      name={`${model} Load Time`}
                      fill={colors[processedData.models.indexOf(model) % colors.length]}
                      opacity={0.7}

          {/* Response Length Chart */}
          <div className="bg-slate-900 border border-slate-800 rounded-xl p-6 shadow-xl">
            <h3 className="text-xl font-semibold text-white mb-6">Generated Response Length (Characters)</h3>
            <div className="h-[300px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={processedData.chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="name" stroke="#94a3b8" />
                  <YAxis stroke="#94a3b8" label={{ value: 'Characters', angle: -90, position: 'insideLeft', fill: '#94a3b8' }}/>
                  <Tooltip 
                    contentStyle={{ backgroundColor: '#0f172a', borderColor: '#1e293b', color: '#f8fafc' }}
                  />
                  {visibleModels.map((model, idx) => (
                    <Bar 
                      key={model} 
                      dataKey={`${model}_len`} 
                      name={`${model} Length`}
                      fill={colors[processedData.models.indexOf(model) % colors.length]}
                      opacity={0.6}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>

        </div>
      </div>
    </div>
  );
}

export default App;
