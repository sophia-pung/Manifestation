import { useState } from 'react'

export function EvalPanel({ evalReport }) {
  const [open, setOpen] = useState(false)

  if (!evalReport) {
    return (
      <div className="border-t border-gray-800 px-4 py-2 text-xs text-gray-700">
        Eval: no report loaded — run <code>python -m evals.eval_runner</code>
      </div>
    )
  }

  const passRate = Math.round(evalReport.pass_rate * 100)
  const color = passRate >= 85 ? 'text-green-400' : passRate >= 70 ? 'text-yellow-400' : 'text-red-400'

  return (
    <div className="border-t border-gray-800">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-4 py-2 text-xs text-gray-500 hover:text-gray-300 transition-colors"
      >
        <span className="flex items-center gap-3">
          <span>Eval</span>
          <span className={`font-bold ${color}`}>{evalReport.passed}/{evalReport.total} passed ({passRate}%)</span>
          <span>· avg depth {evalReport.avg_depth}</span>
          <span>· hallucinations {evalReport.hallucination_count}</span>
        </span>
        <span>{open ? '▲' : '▼'}</span>
      </button>

      {open && (
        <div className="px-4 pb-3 max-h-48 overflow-y-auto">
          <div className="grid grid-cols-2 gap-1">
            {evalReport.results?.map(r => (
              <div key={r.id} className={`text-xs flex items-center gap-2 py-0.5 ${r.passed ? 'text-gray-500' : 'text-red-400'}`}>
                <span>{r.passed ? '✓' : '✗'}</span>
                <span>{r.id}</span>
                <span className="text-gray-700">d={r.depth}</span>
                {!r.passed && <span className="truncate">{JSON.stringify(r.failure_reason)}</span>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
